# -*- coding: utf-8 -*-
r"""Skill 使用统计上报（original-problem-generator）。

每次执行本 skill 时由 SKILL.md 的 Step 0 调用一次，向分析服务上报一条使用记录。
设计要点（严格遵循需求）：
- installation_id：首次运行生成标准 UUID v4（36 字符，含连字符）并持久化；后续直接读取复用，
  不重新生成、不覆盖已有 ID。
- 持久化：统一存于 `~/.workbuddy/skill-usage/installation_id`（WorkBuddy 用户级持久配置目录，
  沙箱必然持久，且与 skill 包目录解耦——更新 skill 包不会丢失，同一台电脑多次运行 ID 保持不变）；
  采用「临时文件 + 原子 rename」创建，并对已存在文件加文件锁（fcntl，Windows 下退化为无锁但仍是
  原子 rename），保证并发一致；首次运行若主位置缺失但旧规范位置（macOS ~/Library、Linux $XDG、
  Windows %LOCALAPPDATA% 下的 skill-usage/installation_id）已有有效 UUID，则自动迁移复用，
  保证该电脑 ID 不变。
- 必须成功保存后再读取并上报，绝不使用未持久化的临时 ID。
- 上报：HTTPS POST，Content-Type: application/json，body 为
  {"skill","version","installation_id","event"}；version 运行时自动读取 SKILL.md frontmatter，
  不写死；每次执行最多发送一次；总超时 3 秒，不重试、不跟随重定向、正常验证 HTTPS 证书。
- 失败/异常不影响主任务：本脚本始终以退出码 0 结束，不向外发送 body 之外的任何数据。

存储位置：
- 主位置（WorkBuddy 内统一）：`~/.workbuddy/skill-usage/installation_id`
- 兼容迁移源（旧规范）：macOS `~/Library/Application Support/skill-usage/installation_id`、
  Linux `${XDG_DATA_HOME:-$HOME/.local/share}/skill-usage/installation_id`、
  Windows `%LOCALAPPDATA%\skill-usage\installation_id`

测试钩子（仅用于自测，不影响生产）：
- 环境变量 SKILL_USAGE_REPORT_URL 可覆盖上报地址（例如指向本地 mock 服务）。
- 环境变量 SKILL_USAGE_ID_PATH 可覆盖 installation_id 文件路径（便于隔离测试）。
"""
import os
import sys
import re
import json
import uuid
import http.client
import urllib.parse

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

SKILL_NAME = "original-problem-generator"
# 上报 body 的 version 不写死：运行时自动读取 SKILL.md frontmatter 的版本号；
# 读取失败 / 文件缺失时回退到 FALLBACK_SKILL_VERSION，保证脚本永不崩。
FALLBACK_SKILL_VERSION = "1.0.0"
DEFAULT_REPORT_URL = "https://stadminv2.xqmsx.cn/api/ai/reportSkillUsage"
REPORT_TIMEOUT = 3.0


def _id_path():
    override = os.environ.get("SKILL_USAGE_ID_PATH")
    if override:
        return override
    # 主位置：WorkBuddy 用户级持久目录（沙箱必然持久，且与 skill 包解耦）
    return os.path.join(os.path.expanduser("~/.workbuddy"), "skill-usage", "installation_id")


def _legacy_paths():
    """旧规范位置的候选列表（仅用于首次运行的兼容迁移，不依赖 skill 包）。"""
    paths = []
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        paths.append(os.path.join(base, "skill-usage", "installation_id"))
    else:
        paths.append(os.path.expanduser(
            "~/Library/Application Support/skill-usage/installation_id"))
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        paths.append(os.path.join(base, "skill-usage", "installation_id"))
    return paths


def _valid_uuid(s):
    if not s or len(s) != 36:
        return False
    try:
        u = uuid.UUID(s)
        return u.version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def _write_atomic(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (content + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)  # 原子替换


def _read_locked(path):
    with open(path, "r+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f.read().strip()


def _migrate_legacy_id(path):
    """若主位置尚无 ID，但旧位置有有效 UUID，则迁移复用，保证该电脑 ID 不变。"""
    for lp in _legacy_paths():
        if lp == path or not os.path.exists(lp):
            continue
        try:
            with open(lp, "r", encoding="utf-8") as f:
                c = f.read().strip()
            if _valid_uuid(c):
                _write_atomic(path, c)
                return c
        except Exception:
            pass
    return None


def load_or_create_id(path):
    """返回 (installation_id, error)。error 非空表示应跳过上报并说明原因。"""
    # 已存在且有效 → 直接复用（不重新生成、不覆盖）
    if os.path.exists(path):
        try:
            content = _read_locked(path)
            if _valid_uuid(content):
                return content, None
        except Exception as e:
            return None, "读取 installation_id 失败：%s" % e
        # 内容无效：走下方重建

    # 主位置缺失/无效 → 先尝试从旧规范位置迁移复用，保证该电脑 ID 不变
    migrated = _migrate_legacy_id(path)
    if migrated:
        return migrated, None

    # 创建 / 重建
    new_id = str(uuid.uuid4())
    try:
        _write_atomic(path, new_id)
    except FileExistsError:
        # 并发：另一个进程已建好，读回
        try:
            content = _read_locked(path)
            if _valid_uuid(content):
                return content, None
        except Exception:
            pass
        return None, "并发创建 installation_id 时冲突且读回失败"
    except Exception as e:
        return None, "写入 installation_id 失败：%s" % e

    # 创建成功后，加锁读回确认（必须成功保存后再读取并上报）
    try:
        content = _read_locked(path)
        if not _valid_uuid(content):
            return None, "installation_id 内容校验失败"
        return content, None
    except Exception as e:
        return None, "读取/锁定 installation_id 失败：%s" % e


def load_skill_version():
    """读取同仓库 SKILL.md frontmatter 的 version 字段。

    脚本位于 <skill>/scripts/report_usage.py，SKILL.md 在 <skill>/SKILL.md。
    解析文件开头两个 '---' 之间的 frontmatter，匹配 'version: x.y.z'。
    任何异常（文件缺失、格式异常）均返回回退版本，不影响主任务。
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        skill_md = os.path.join(os.path.dirname(here), "SKILL.md")
        if not os.path.exists(skill_md):
            return FALLBACK_SKILL_VERSION
        with open(skill_md, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.startswith("---"):
            return FALLBACK_SKILL_VERSION
        end = text.find("\n---", 3)
        if end == -1:
            return FALLBACK_SKILL_VERSION
        fm = text[3:end]
        m = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", fm, re.MULTILINE)
        return m.group(1) if m else FALLBACK_SKILL_VERSION
    except Exception:
        return FALLBACK_SKILL_VERSION


def _build_connection(parsed):
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.netloc, timeout=REPORT_TIMEOUT)
    # http（含本地 mock 自测）或非标准 scheme 都走 HTTP，避免无谓的 TLS
    return http.client.HTTPConnection(parsed.netloc, timeout=REPORT_TIMEOUT)


def report(installation_id, url=None, version=None):
    """发送一次上报，返回 (ok, status_or_None)。异常由调用方捕获。"""
    endpoint = url or os.environ.get("SKILL_USAGE_REPORT_URL") or DEFAULT_REPORT_URL
    if version is None:
        version = load_skill_version()
    payload = json.dumps({
        "skill": SKILL_NAME,
        "version": version,
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
