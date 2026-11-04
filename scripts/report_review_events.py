#!/usr/bin/env python3
"""服务端上报评审事件（available/optimize）—— 替代浏览器在「一键重新生成」点击时的即时上报。

背景与动机：
  原流程中，评审页「一键重新生成」按钮在 *浏览器侧* 通过 fetch 本地桥接 /report 上报 available/
  optimize 事件，要求「点击按钮那一刻」本地桥接服务存活。Windows 上桥接因 Job Object 回收
  （_ensure_bridge 的 start_new_session 为 POSIX-only 被忽略）且无常驻服务而必死，导致上报静默
  失败；即便桥接偶然存活，/report 返回非 accepted 时前端 also 静默丢弃。

  改为：浏览器只负责把重跑请求送达（/rerun 或兜底下载 JSON+复制消息）；available/optimize 改由
  *agent 侧* 在用户于 WorkBuddy 发送「发送队列」/「处理队列」消费队列时统一上报。agent 运行环境
  可调用 _ensure_bridge() 拉起桥接，是可靠上报点。

本脚本：读取待处理队列项（仅顶层 request_*.json），逐题上报 available/optimize，并打 reported
标记保证幂等（重复发送命令不会重复上报）。不改任何重生成 / 母题更新逻辑——那是 处理队列 流程的职责。

用法：
  python report_review_events.py            # 上报当前 cwd 下 .workbuddy/rerun_queue 的全部待处理项
  REVIEW_QUEUE_DIR=/path python ...         # 显式指定队列目录
"""
import os
import sys
import json
import time
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from report_usage import (  # noqa: E402
    _ensure_bridge, report, load_or_create_id, load_skill_version, _id_path,
)

QUEUE_DIR = os.environ.get("REVIEW_QUEUE_DIR") or os.path.join(
    os.getcwd(), ".workbuddy", "rerun_queue")


def _report_one(installation_id, version, event, session_id, info):
    ok, status, detail = report(
        installation_id, version=version,
        event=event, session_id=session_id, info=info,
    )
    print("  [%s] ok=%s status=%s detail=%s info=%s"
          % (event, ok, status, detail, info))
    return ok


def report_item(item_path):
    with open(item_path, encoding="utf-8") as f:
        item = json.load(f)
    if item.get("reported"):
        print("  跳过已上报：%s" % os.path.basename(item_path))
        return 0
    session_id = ((item.get("meta") or {}).get("session_id") or item.get("session_id"))
    selection = item.get("selection") or {}
    suggestions = item.get("suggestions") or {}
    installation_id, err = load_or_create_id(_id_path())
    if err or not installation_id:
        print("  installation_id 缺失，跳过：%s" % err)
        return 0
    version = load_skill_version()
    n = 0
    # available：每题一条，扁平 info={questionID, available}（usable=是，其余=否）
    for pid, st in selection.items():
        avail = "是" if st == "usable" else "否"
        _report_one(installation_id, version, "available", session_id,
                    {"questionID": pid, "available": avail})
        n += 1
    # optimize：仅不可用且有建议文本的题，text 裁剪至 2000 字
    for pid, txt in suggestions.items():
        if txt and txt.strip():
            _report_one(installation_id, version, "optimize", session_id,
                        {"questionID": pid, "text": txt.strip()[:2000]})
            n += 1
    item["reported"] = True
    item["reported_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(item_path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)
    print("  已上报 %d 条事件并标记 reported：%s" % (n, os.path.basename(item_path)))
    return n


def main():
    # 先确保桥接存活（best-effort）：Windows 上浏览器上报失败的根因就是桥接未起。
    alive = _ensure_bridge()
    print("[report-events] 桥接存活=%s" % alive)
    files = sorted(glob.glob(os.path.join(QUEUE_DIR, "request_*.json")))
    if not files:
        print("[report-events] 队列为空（无待处理 request_*.json），无需上报。")
        return
    total = 0
    for fp in files:
        print("[report-events] 处理：%s" % os.path.basename(fp))
        total += report_item(fp)
    print("[report-events] 共上报 %d 条事件。" % total)


if __name__ == "__main__":
    main()
