#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原创题评审 · 本地桥接服务（重跑闭环：评审页 → 项目级队列的桥梁）。

评审页「一键重新生成」点击后，向本服务 POST 重跑请求；本服务把请求落盘到
项目级队列目录 `.workbuddy/rerun_queue/`，由 agent 在对话中收到「处理队列」
指令时消费，完成「不可用题母题更新 + 定向重命制 + 重新 build」。

启动： python review_bridge.py
端口： 默认 127.0.0.1:8787，可用环境变量 REVIEW_BRIDGE_PORT 覆盖
队列： 默认 <cwd>/.workbuddy/rerun_queue，可用 REVIEW_QUEUE_DIR 覆盖

依赖：仅 Python 标准库（http.server / json / os ...），无第三方包。
"""
import os
import sys
import json
import time
import base64
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("REVIEW_BRIDGE_PORT", "8787"))
QUEUE_DIR = os.environ.get("REVIEW_QUEUE_DIR") or os.path.join(
    os.getcwd(), ".workbuddy", "rerun_queue")


def ensure_queue():
    os.makedirs(QUEUE_DIR, exist_ok=True)


def pending_requests():
    if not os.path.isdir(QUEUE_DIR):
        return []
    return sorted(
        f for f in os.listdir(QUEUE_DIR)
        if f.startswith("request_") and f.endswith(".json")
    )


# 本地审计日志路径（默认放在 .workbuddy/ 下，与重跑队列同级；可用环境变量覆盖）。
REPORT_AUDIT_LOG = os.environ.get("REVIEW_REPORT_AUDIT") or os.path.join(
    os.path.dirname(QUEUE_DIR.rstrip(os.sep)) or ".", "review_report_audit.log")


def audit_report(event, session_id, info, result):
    """本地审计日志：记录每次 /report 收到的事件与上游返回，便于事后核查。

    纯旁路：任何异常都不影响 /report 的正常流转与返回值（与 log_message 不同，
    这里默认落盘；如需关闭，设环境变量 REVIEW_REPORT_AUDIT=/dev/null 即可）。
    """
    try:
        d = os.path.dirname(REPORT_AUDIT_LOG)
        if d:
            os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            info_s = json.dumps(info, ensure_ascii=False) if info is not None else ""
        except Exception:
            info_s = str(info)
        try:
            result_s = json.dumps(result, ensure_ascii=False)
        except Exception:
            result_s = str(result)
        line = "ts=%s | event=%s | session=%s | info=%s | result=%s\n" % (
            ts, event, session_id, info_s[:600], result_s[:800])
        with open(REPORT_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    # 安静日志（避免每次请求刷屏）
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # 预检（评审页为 file://，POST JSON 触发浏览器 CORS 预检）
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        files = pending_requests()
        if p == "/status":
            return self._send_json({
                "ok": True, "running": True,
                "queue_dir": QUEUE_DIR, "pending": len(files),
            })
        if p in ("/", "/index.html"):
            html = (
                "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>原创题评审桥接服务</title></head><body style='font-family:sans-serif;margin:24px'>"
                "<h2>原创题评审 · 桥接服务</h2>"
                "<p><b>状态：</b>运行中</p>"
                "<p><b>队列目录：</b>%s</p>"
                "<p><b>待处理重跑请求：</b><span style='font-size:20px;color:#2563eb'>%d</span> 个</p>"
                "<hr>"
                "<p>· <code>GET /status</code> 返回 JSON 状态</p>"
                "<p>· <code>POST /rerun</code> 接收评审页「一键重新生成」的重跑请求</p>"
                "<p>· <code>POST /report</code> 接收评审页业务事件（available/optimize），"
                "补全会话身份后转发后端上报接口</p>"
                "<p style='color:#6b7280'>提示：点击后会把请求写入项目级队列；在对话中发送「处理队列」，"
                "agent 会立即消费队列并完成母题更新+重命制+重新 build。</p>"
                "</body></html>"
            ) % (QUEUE_DIR, len(files))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return
        self._send_json({"error": "not found"}, 404)

    def _handle_report(self):
        """POST /report：接收评审页业务事件，补 installation_id/version 后转发后端。

        请求体（来自 file:// 评审页，HTML 不持有机器 ID，规避隐私泄露）：
          {"event": "available"|"optimize", "session_id": "<母题UUID>", "info": {...}}
        本服务读取持久化 installation_id，补全 skill/version，转发至上报接口，
        并以接口返回的 code 字段判定成败（0 成功 / 7 失败）。任何异常都不影响评审页。
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa
            audit_report("?", None, None, {"stage": "parse_error", "http": 400, "error": str(e)})
            return self._send_json({"accepted": False, "error": "bad json: %s" % e}, 400)
        event = (data.get("event") or "").strip()
        if not event or len(event) > 32:
            audit_report(event or "?", data.get("session_id"), data.get("info"),
                         {"stage": "invalid_event", "http": 400, "error": "event 非法或超长"})
            return self._send_json({"accepted": False, "error": "invalid event"}, 400)
        session_id = data.get("session_id") or None
        info = data.get("info")
        # 读取并补全 installation_id（复用 report_usage 持久化机制）
        try:
            from report_usage import (load_or_create_id, load_skill_version,
                                      report as _report, _id_path)
        except Exception as e:  # noqa
            audit_report(event, session_id, info, {"stage": "report_usage_unavailable", "http": 500, "error": str(e)})
            return self._send_json({"accepted": False, "error": "report_usage 不可用: %s" % e}, 500)
        installation_id, err = load_or_create_id(_id_path())
        if err or not installation_id:
            audit_report(event, session_id, info, {"stage": "no_installation_id", "http": 500, "error": err or "缺失"})
            return self._send_json({"accepted": False, "error": "installation_id: %s" % (err or "缺失")}, 500)
        version = load_skill_version()
        url = os.environ.get("SKILL_USAGE_REPORT_URL")  # 测试可指向本地 mock
        ok, status, detail = _report(installation_id, url=url, version=version,
                                     event=event, session_id=session_id, info=info)
        audit_report(event, session_id, info, {"stage": "forwarded", "http": 200,
                                               "accepted": ok, "upstream_status": status, "detail": detail})
        return self._send_json({
            "accepted": ok, "event": event,
            "status": status, "detail": detail,
        })

    def _handle_export_docx(self):
        """POST /export-docx：接收前端选中的可用题（含 base64 配图），用 problems_to_docx
        组装真实 Word 文档并返回文件下载流（Content-Type 为 docx）。

        这是「导出」按钮的落地：此前前端只下载 selection_feedback.json，名实不符；
        现由服务端用既有 OMML 管线生成 .docx。任何失败返回 JSON 错误，由前端退化为
        JSON 导出，绝不静默失败。
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa
            return self._send_json({"accepted": False, "error": "bad json: %s" % e}, 400)
        problems = data.get("problems")
        if not isinstance(problems, list) or not problems:
            return self._send_json({"accepted": False, "error": "empty problems"}, 400)
        title = data.get("title") or "原创题（精选/重生成）"
        work = tempfile.mkdtemp(prefix="review_export_")
        # 把 base64 配图落为临时文件，problems_to_docx 按路径嵌入。
        # 仅接受光栅格式（docx 不支持 SVG）：SVG 优先用 cairosvg 转 PNG，否则跳过该图以免组装 500。
        for p in problems:
            fb = (p.get("figure_b64") or "")
            p["figure"] = None
            if isinstance(fb, str) and fb.startswith("data:image"):
                try:
                    _, bdata = fb.split(",", 1)
                    raw = base64.b64decode(bdata)
                    if raw[:4] == b"\x89PNG" or raw[:3] == b"\xff\xd8\xff" or raw[:3] == b"GIF":
                        p["figure"] = os.path.join(work, "fig_%s.png" % (p.get("id") or "x"))
                        open(p["figure"], "wb").write(raw)
                    elif raw.lstrip()[:5] in (b"<?xml", b"<svg "):
                        try:
                            import cairosvg
                            p["figure"] = os.path.join(work, "fig_%s.png" % (p.get("id") or "x"))
                            cairosvg.svg2png(bytestring=raw, write_to=p["figure"])
                        except Exception:
                            p["figure"] = None
                except Exception:
                    p["figure"] = None
            p.pop("figure_b64", None)
        out = os.path.join(work, "export.docx")
        try:
            from review_data import problems_to_docx
            problems_to_docx(problems, out, title=title, figures_dir=work)
        except Exception as e:  # noqa
            return self._send_json({"accepted": False, "error": "docx build failed: %s" % e}, 500)
        try:
            with open(out, "rb") as f:
                blob = f.read()
        except Exception as e:  # noqa
            return self._send_json({"accepted": False, "error": "read failed: %s" % e}, 500)
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        # 文件名用 ASCII（兼容 Content-Disposition）；真正保存名由前端 a.download 指定
        self.send_header("Content-Disposition", 'attachment; filename="export_usable.docx"')
        self._cors()
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/report":
            return self._handle_report()
        if p == "/export-docx":
            return self._handle_export_docx()
        if p != "/rerun":
            return self._send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa
            return self._send_json({"accepted": False, "error": "bad json: %s" % e}, 400)
        # 基本校验：必须是一次评审反馈（含 selection 字典）
        if not isinstance(data.get("selection"), dict):
            return self._send_json({"accepted": False, "error": "missing selection"}, 400)
        data.setdefault("rerun", True)
        data.setdefault("received_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        # 落盘：request_<UTC>.json（防并发重名）
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = os.path.join(QUEUE_DIR, "request_%s.json" % ts)
        n = 0
        while os.path.exists(path):
            n += 1
            path = os.path.join(QUEUE_DIR, "request_%s_%d.json" % (ts, n))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._send_json({
            "accepted": True,
            "request_id": os.path.basename(path),
            "queue_dir": QUEUE_DIR,
        })


def main():
    ensure_queue()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("原创题评审桥接服务已启动 → http://127.0.0.1:%d/rerun" % PORT)
    print("队列目录： %s" % QUEUE_DIR)
    print("提示：点击后会把请求写入队列；在对话中发送「处理队列」即可让 agent 立即消费。")
    print("按 Ctrl+C 停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n桥接服务已停止。")


if __name__ == "__main__":
    main()
