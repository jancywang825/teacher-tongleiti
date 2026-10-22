# -*- coding: utf-8 -*-
r"""Skill 使用统计上报（original-problem-generator）。

每次执行本 skill 时由 SKILL.md 的 Step 0 调用一次，向分析服务上报一条使用记录。
设计要点（严格遵循需求）：
- installation_id：首次运行生成标准 UUID v4（36 字符，含连字符）并持久化；后续直接读取复用，
  不重新生成、不覆盖已有 ID。
- 持久化：按平台选择目录；目录不存在则创建；采用「临时文件 + 原子 rename」创建，
  并对已存在文件加文件锁（fcntl，Windows 下退化为无锁但仍是原子 rename），保证并发一致。
- 必须成功保存后再读取并上报，绝不使用未持久化的临时 ID。
- 上报：HTTPS POST，Content-Type: application/json，body 仅含
  {"skill","version","installation_id","event"}；每次执行最多发送一次；
  总超时 3 秒，不重试、不跟随重定向、正常验证 HTTPS 证书。
- 失败/异常不影响主任务：本脚本始终以退出码 0 结束，不向外发送 body 之外的任何数据。

跨平台：
- macOS   : ~/Library/Application Support/skill-usage/installation_id
- Linux   : ${XDG_DATA_HOME:-$HOME/.local/share}/skill-usage/installation_id
- Windows : %LOCALAPPDATA%\skill-usage\installation_id

测试钩子（仅用于自测，不影响生产）：
- 环境变量 SKILL_USAGE_REPORT_URL 可覆盖上报地址（例如指向本地 mock 服务）。
- 环境变量 SKILL_USAGE_ID_PATH 可覆盖 installation_id 文件路径（便于隔离测试）。
"""
import os
import sys
import json
import uuid
import http.client
import urllib.parse

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

SKILL_NAME = "original-problem-generator"
SKILL_VERSION = "1.0.0"  # 上报 body 固定版本，按需求保持不变（仅 installation_id 替换）
DEFAULT_REPORT_URL = "https://stadminv2.xqmsx.cn/api/ai/reportSkillUsage"
REPORT_TIMEOUT = 3.0


def _id_path():
    override = os.environ.get("SKILL_USAGE_ID_PATH")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/skill-usage")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        base = os.path.join(base, "skill-usage")
    else:  # linux / other
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        base = os.path.join(base, "skill-usage")
    return os.path.join(base, "installation_id")


def _valid_uuid(s):
    if not s or len(s) != 36:
        return False
    try:
        u = uuid.UUID(s)
        return u.version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def load_or_create_id(path):
    """返回 (installation_id, error)。error 非空表示应跳过上报并说明原因。"""
    # 已存在且有效 → 直接复用（不重新生成、不覆盖）
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if _valid_uuid(content):
                return content, None
            # 内容无效：重建
        except Exception as e:
            return None, "读取 installation_id 失败：%s" % e

    # 创建 / 重建
    new_id = str(uuid.uuid4())
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (new_id + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)  # 原子替换
    except FileExistsError:
        # 并发：另一个进程已建好，读回
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if _valid_uuid(content):
                return content, None
        except Exception:
            pass
        return None, "并发创建 installation_id 时冲突且读回失败"
    except Exception as e:
        return None, "写入 installation_id 失败：%s" % e

    # 创建成功后，加锁读回确认（必须成功保存后再读取并上报）
    try:
        with open(path, "r+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            content = f.read().strip()
        if not _valid_uuid(content):
            return None, "installation_id 内容校验失败"
        return content, None
    except Exception as e:
        return None, "读取/锁定 installation_id 失败：%s" % e


def _build_connection(parsed):
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.netloc, timeout=REPORT_TIMEOUT)
    # http（含本地 mock 自测）或非标准 scheme 都走 HTTP，避免无谓的 TLS
    return http.client.HTTPConnection(parsed.netloc, timeout=REPORT_TIMEOUT)


def report(installation_id, url=None):
    """发送一次上报，返回 (ok, status_or_None)。异常由调用方捕获。"""
    endpoint = url or os.environ.get("SKILL_USAGE_REPORT_URL") or DEFAULT_REPORT_URL
    payload = json.dumps({
        "skill": SKILL_NAME,
        "version": SKILL_VERSION,
        "installation_id": installation_id,
        "event": "run",
    }).encode("utf-8")
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    conn = _build_connection(parsed)
    try:
        conn.request(
            "POST", path,
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        _ = resp.read()  # 不跟随重定向：遇到 3xx 直接读响应即结束
        return True, resp.status
    finally:
        conn.close()


def main():
    path = _id_path()
    installation_id, err = load_or_create_id(path)
    if err or not installation_id:
        sys.stderr.write("[usage-report] 跳过上报：%s\n" % (err or "未知错误"))
        return 0
    try:
        ok, status = report(installation_id)
        if ok:
            sys.stderr.write("[usage-report] 上报成功 (HTTP %s)\n" % status)
        else:
            sys.stderr.write("[usage-report] 上报失败 (HTTP %s)，不影响主任务\n" % status)
    except Exception as e:  # 任何异常都不影响主任务
        sys.stderr.write("[usage-report] 上报异常：%s，不影响主任务\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
