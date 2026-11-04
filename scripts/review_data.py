# -*- coding: utf-8 -*-
"""
review_data.py — 原创题评审交互平台 · 通用数据层（需求 2.9.0）

职责（被 build 脚本与 regenerate/review_stats 脚本共用）：
  - 定义原创题 canonical JSON 结构（单题 schema + 整包 bundle）
  - 读写 bundle JSON / 计算题目内容指纹（content_fp 重命制守卫）
  - 把配图转 base64，便于自包含 HTML 内嵌
  - 生成「自包含单文件评审 HTML」：内联 CSS/JS、图片 base64、可勾选三态、可填逐题建议
  - 提供 problems -> docx 汇编器（筛选导出 / 定向重生成 共用），复用 OMML 管线（含向量符号归一化修复）

本模块为纯 python（仅依赖 python-docx + matplotlib，与 build_*_docx.py 同环境）。
"""
import os
import re
import json
import base64
import hashlib
import datetime
import uuid

# ===================== 路径 =====================
def _skill_root():
    # 本文件位于 <skill_root>/scripts/review_data.py，故向上两级即 skill 根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 数据目录随 skill 安装层级解析：
#   - 项目级安装（{workspace}/.workbuddy/skills/...）→ 数据落在项目 .workbuddy，团队/项目共享（可随 git 提交）
#   - 用户级安装（~/.workbuddy/skills/...）→ 数据落在用户 .workbuddy，仅当前用户
# 这样「评审数据（选题日志/画像/周报/自优化建议）」天然跟随安装层级，无需硬编码。
SKILL_DATA_DIR = os.path.join(_skill_root(), "data")
SELECTION_LOG = os.path.join(SKILL_DATA_DIR, "selection_log.json")
USER_PATTERNS = os.path.join(SKILL_DATA_DIR, "user_patterns.json")

MNS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# 业务事件上报（需求 §13）：评审页经本地桥接 /report 转发；构建期上报 new_problem。
# 桥接地址（端口可由 REVIEW_BRIDGE_PORT 覆盖，须与服务端一致）
REPORT_BRIDGE_URL = "http://127.0.0.1:8787/report"


def _load_or_create_session(out_dir, stem_name):
    """稳定母题会话 ID：同一母题批次（同一 out_dir+stem_name）多次重跑/重生成复用同一 session_id。

    - bundle 已带 session_id：优先使用并写回 sidecar；
    - 否则读 sidecar 复用；不存在则生成 uuid4 并持久化。
    保证「每个母题不一样、sessionID 不一样」且同母题跨重跑一致。
    """
    sf = os.path.join(out_dir, stem_name + "_session.json")
    if os.path.exists(sf):
        try:
            d = json.load(open(sf, encoding="utf-8"))
            if d.get("session_id"):
                return d["session_id"]
        except Exception:
            pass
    sid = str(uuid.uuid4())
    try:
        json.dump({"session_id": sid}, open(sf, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass
    return sid


def report_new_problem(session_id):
    """构建期上报 new_problem 事件（需求 §13.1）。容错：任何异常都不影响主任务。

    跳过开关 ORIGINAL_PROBLEM_NO_REPORT：本地构建/测试时设该环境变量可完全禁用上报。
    """
    if os.environ.get("ORIGINAL_PROBLEM_NO_REPORT"):
        return
    try:
        from report_usage import (load_or_create_id, report_new_problem as _rp,
                                  _id_path)
    except Exception:
        return
    try:
        installation_id, err = load_or_create_id(_id_path())
        if err or not installation_id:
            return
        _rp(installation_id, session_id)
    except Exception:
        pass


# ===================== 单题 schema 示例 =====================
# problem = {
#   "id": "p1",
#   "title": "题 1（…）",
#   "source": "菁优网 f3941094…（见溯源·母题 1）",
#   "stem": "题干：如图，…",
#   "subquestions": ["(1) 证明…", "(2) 请从…"],
#   "figure": "fig_st1.png",          # 相对 bundle 目录的配图文件名（可为 None）
#   "answer": "二面角…",
#   "analysis": ["(定参)…", "(线面平行)…"],
#   "adaptation": "…",
#   "innovation": "…",
#   "difficulty_star": "★★★★",
#   "difficulty_anchor": [["核心思维链长度","母题…","本题…"], …],
# }

def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ===================== JSON 读写 =====================
def save_bundle(path, bundle):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    json.dump(bundle, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def load_bundle(path):
    return json.load(open(path, "r", encoding="utf-8"))


def content_fp_of(problems):
    """题目内容指纹：SHA1(各题 id+题干+答案+解析+改编) 前 12 位。重命制守卫。"""
    parts = []
    for p in problems:
        parts.append("||".join([
            str(p.get("id", "")),
            san_text(p.get("stem", "")),
            san_text(p.get("answer", "")),
            san_text("".join(p.get("analysis", []))),
            san_text(p.get("adaptation", "")),
        ]))
    return hashlib.sha1("##".join(parts).encode("utf-8")).hexdigest()[:12]


def san_text(t):
    # 仅做空白归一，保留中文与公式外壳；用于指纹稳定（不进 docx）
    return re.sub(r"\s+", " ", t or "").strip()


# ===================== base64 图片 =====================
def figure_to_base64(fig_path):
    if not fig_path or not os.path.exists(fig_path):
        return None
    ext = os.path.splitext(fig_path)[1].lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg", "gif", "svg"):
        ext = "png"
    mime = "image/svg+xml" if ext == "svg" else "image/%s" % ext
    data = open(fig_path, "rb").read()
    b64 = base64.b64encode(data).decode("ascii")
    return "data:%s;base64,%s" % (mime, b64)


def figure_aspect(fig_path):
    """返回图片宽/高比（w/h）；无法读取时返回 None。用于宽图（路线框图）放宽显示。"""
    try:
        from PIL import Image
        with Image.open(fig_path) as im:
            w, h = im.size
        return (w / h) if h else None
    except Exception:
        return None


# ===================== 中间产物清理（交付即清理） =====================
def _safe_remove(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def cleanup_topic_intermediates(out_dir):
    """清理单个母题交付目录的中间产物（docx/HTML 已内嵌，冗余）。

    删除：独立配图 fig_*.png/.svg、题源快照 sources_manifest*.json、__pycache__。
    不删：docx、评审 HTML、build 脚本、各题 bundle JSON（聚合页还要读）。
    """
    removed = []
    if not out_dir or not os.path.isdir(out_dir):
        return removed
    for name in os.listdir(out_dir):
        p = os.path.join(out_dir, name)
        if name.startswith("fig_") and name.lower().endswith((".png", ".svg")):
            if _safe_remove(p):
                removed.append(name)
        elif name.startswith("sources_manifest") and name.lower().endswith(".json"):
            if _safe_remove(p):
                removed.append(name)
    cache = os.path.join(out_dir, "__pycache__")
    if os.path.isdir(cache):
        try:
            import shutil
            shutil.rmtree(cache)
            removed.append("__pycache__/")
        except OSError:
            pass
    return removed


def cleanup_aggregate_intermediates(root):
    """聚合页构建完成后，统一清理各交付目录的中间产物 + 全部 bundle JSON。

    必须在 build_review_index 写总览页之后调用（聚合页需先读 bundle JSON 构建）。
    删除：各 '原创题_*' 目录里的 原创题_*.json（已内嵌进总览页，*_session.json 除外）、
          以及调用 cleanup_topic_intermediates 清掉配图/题源manifest/__pycache__。
    不删：docx、评审 HTML、build 脚本、*_session.json（重跑会话锚点）。
    """
    removed = []
    if not root or not os.path.isdir(root):
        return removed
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not name.startswith("原创题_"):
            continue
        # 先清 bundle JSON（聚合页已读完）；保留 *_session.json 作为重跑会话锚点
        for fn in os.listdir(d):
            if (fn.startswith("原创题_") and fn.lower().endswith(".json")
                    and "_session" not in fn):
                if _safe_remove(os.path.join(d, fn)):
                    removed.append(os.path.join(name, fn))
        removed += cleanup_topic_intermediates(d)
    return removed


# ===================== 评审 HTML（自包含单文件） =====================
COMMON_TAGS = ["情境不真实", "难度过高", "与母题太像", "缺图示",
               "设问重复", "计算超纲", "数据不自洽", "答案有误"]

# 单题卡片模板：使用 __X__ 占位符（避免 % 格式化与公式内容里的 % 冲突）
CARD_TEMPLATE = """
        <div class="card" id="card-__PID__">
          <div class="chead">
            <span class="ptitle">__TITLE__</span>
            <span class="stars">__STARS__</span>
          </div>
          __FIG__
          <div class="blk"><div class="lab">题干</div><div class="stem">__STEM__</div></div>
          <ul class="subs">__SUBS__</ul>
          __OPTS__
          <div class="blk"><div class="lab">答案</div><div class="ans">__ANSWER__</div></div>
          <div class="blk"><div class="lab">解析</div>__ANALYSIS__</div>
          <div class="blk"><div class="lab">改编说明</div><div class="note">__ADAPT__</div></div>
          <div class="blk"><div class="lab">创新点</div><div class="note">__INNOV__</div></div>
          __ANCHOR__
          <div class="review">
            <div class="rstate">
              <span>评审：</span>
              <label><input type="radio" name="st-__PID__" value="usable" checked> 可用</label>
              <label><input type="radio" name="st-__PID__" value="unusable"> 不可用</label>
              <label><input type="radio" name="st-__PID__" value="pending"> 待定</label>
            </div>
            <div class="sugg">
              <div class="lab">不可用题建议（自由文本，可点下方标签快速追加）</div>
              <textarea id="sugg-__PID__" placeholder="如：情境不真实，请改为贴近生产生活的背景；难度过高，请降低计算量…"></textarea>
              <div class="tags">__TAGS__</div>
            </div>
          </div>
        </div>
        """


def problem_export_view(p, fig_b64=""):
    """抽取前端「导出 Word」所需的题面字段（含 base64 配图），供桥接 /export-docx 重组 docx。

    只挑 problems_to_docx 用得到的字段，避免把整包无关数据（如 sources 原始快照）塞进页面。
    """
    ana = p.get("analysis", [])
    if not isinstance(ana, list):
        ana = [ana] if ana else []
    return {
        "id": p.get("id"),
        "title": p.get("title", ""),
        "source": p.get("source", ""),
        "stem": p.get("stem", ""),
        "subquestions": p.get("subquestions", []),
        "options": p.get("options", []),
        "answer": p.get("answer", ""),
        "analysis": ana,
        "adaptation": p.get("adaptation", ""),
        "innovation": p.get("innovation", ""),
        "difficulty_anchor": p.get("difficulty_anchor", []),
        "figure_b64": fig_b64 or "",
    }


def render_mother_section(mother, bundle_dir):
    """渲染「① 用户母题」区块（用户上传/提供的原题，含原图与识别文案）。
    数据缺失则返回空串——保证无该字段的旧 bundle 也能正常渲染。"""
    if not mother:
        return ""
    subject = mother.get("subject", "")
    text = mother.get("text", "")
    fig = mother.get("figure")
    fig_html = ""
    if fig:
        fp = fig if os.path.isabs(fig) else os.path.join(bundle_dir, fig)
        b64 = figure_to_base64(fp)
        if b64:
            fig_html = '<div class="mfig"><img src="' + b64 + '" alt="母题原图"/></div>'
    subj_html = ('<div class="msubj">' + esc(subject) + "</div>") if subject else ""
    body = (fig_html + subj_html + '<div class="mtext">' + render_latex(text) + "</div>") \
        if (fig_html or subj_html or text) else ""
    if not body:
        return ""
    return '<section class="sec sec-mother"><h2>① 用户母题（请核对识别是否准确）</h2>' + body + "</section>"


def render_sources_section(sources, heading="② 学科网 / 菁优网母题溯源"):
    """渲染「② 学科网/菁优网母题溯源」区块（题源快照表）。无数据返回空串。
    列：站点 / 题源标题 / 发布 / 浏览·下载 / 内容指纹（指纹用于题源变化检测）。"""
    if not sources:
        return ""
    trs = []
    for s in sources:
        v, d = s.get("views"), s.get("downloads")
        vd = "—"
        if v is not None or d is not None:
            vd = "浏览%s / 下载%s" % (v if v is not None else "—",
                                      d if d is not None else "—")
        trs.append(
            "<tr><td>" + esc(s.get("site", "")) + "</td>"
            "<td class=\"src-title\">" + render_latex(s.get("title", "")) + "</td>"
            "<td>" + esc(s.get("publish", s.get("date", ""))) + "</td>"
            "<td>" + esc(vd) + "</td>"
            "<td class=\"src-sid\">" + esc(s.get("sid", "—")) + "</td>"
            "<td>" + esc(s.get("fingerprint", s.get("content_fp", ""))) + "</td></tr>")
    return ('<section class="sec sec-source"><h2>' + esc(heading) + "</h2>"
            '<table><tr><th>站点</th><th>题源标题</th><th>发布</th>'
            "<th>浏览/下载</th><th>资源标识</th><th>内容指纹</th></tr>"
            + "".join(trs) + "</table></section>")


def build_html(bundle, html_path, bundle_dir):
    """生成自包含评审 HTML：内联 CSS/JS、图片 base64、三态勾选、逐题建议、导出 Word（经桥接）。"""
    os.makedirs(os.path.dirname(os.path.abspath(html_path)), exist_ok=True)
    problems = bundle.get("problems", [])
    figures_dir = bundle.get("figures_dir", ".")
    abs_fig_dir = figures_dir
    if not os.path.isabs(abs_fig_dir):
        abs_fig_dir = os.path.join(bundle_dir, figures_dir)

    cards = []
    probs_map = {}
    for i, p in enumerate(problems):
        pid = p.get("id", "p%d" % (i + 1))
        fig_src = ""
        b64 = ""
        if p.get("figure"):
            b64 = figure_to_base64(os.path.join(abs_fig_dir, p["figure"]))
            if b64:
                asp = figure_aspect(os.path.join(abs_fig_dir, p["figure"]))
                cls = "fig fig-wide" if (asp and asp >= 2.5) else "fig"
                fig_src = '<img class="' + cls + '" src="' + b64 + '" alt="' + p.get("figure", "") + '"/>'
        if p.get("figure_extra"):
            b64e = figure_to_base64(os.path.join(abs_fig_dir, p["figure_extra"]))
            if b64e:
                # 结构节点特写图统一用宽图样式大图显示，保证「分子结构团」可辨
                fig_src += '<img class="fig fig-wide" src="' + b64e + '" alt="' + p.get("figure_extra", "") + '"/>'
        probs_map[pid] = problem_export_view(p, b64)
        subs = "".join("<li>" + render_latex(q) + "</li>" for q in p.get("subquestions", []))
        opts = ""
        if p.get("options"):
            opts_inner = "".join("<li>" + render_latex(o) + "</li>" for o in p["options"])
            opts = '<div class="blk"><div class="lab">选项</div><ul class="subs">' + opts_inner + "</ul></div>"
        anchor = ""
        if p.get("difficulty_anchor"):
            rows = "".join(
                "<tr><td>" + render_latex(a) + "</td><td>" + render_latex(b) + "</td><td>" + render_latex(c) + "</td></tr>"
                for a, b, c in p["difficulty_anchor"])
            anchor = ('<div class="blk"><div class="lab">难度锚定对照表</div>'
                      '<table class="anc"><tr><th>维度</th><th>母题</th><th>本题</th></tr>' + rows + "</table></div>")
        ana = p.get("analysis", [])
        if isinstance(ana, str):          # 防御：字符串会被逐字遍历成每字一 div
            ana = [ana]
        analysis = "".join('<div class="ana">' + render_latex(a) + "</div>" for a in ana)
        tags_html = "".join(
            '<button type="button" class="tag" data-p="' + pid + '" data-tag="' + esc(t) + '">' + esc(t) + "</button>"
            for t in COMMON_TAGS)
        card = (CARD_TEMPLATE
                .replace("__PID__", pid)
                .replace("__TITLE__", render_latex(p.get("title", pid)))
                .replace("__STARS__", esc(p.get("difficulty_star", "")))
                .replace("__FIG__", fig_src)
                .replace("__STEM__", render_latex(p.get("stem", "")))
                .replace("__SUBS__", subs)
                .replace("__OPTS__", opts)
                .replace("__ANSWER__", render_latex(p.get("answer", "")))
                .replace("__ANALYSIS__", analysis)
                .replace("__ADAPT__", render_latex(p.get("adaptation", "")))
                .replace("__INNOV__", render_latex(p.get("innovation", "")))
                .replace("__ANCHOR__", anchor)
                .replace("__TAGS__", tags_html))
        cards.append(card)

    meta_block = ('<div class="meta">任务：' + render_latex(bundle.get("task", "")) +
                  "<br>约束：" + render_latex(bundle.get("constraint", "")) +
                  "<br>抓取时间：" + esc(bundle.get("fetch_time", "")) +
                  "<br>题目内容指纹 content_fp：" + esc(bundle.get("content_fp", "")) +
                  "<br>会话 session_id：" + esc(bundle.get("session_id", "")) +
                  "<br>构建脚本 build_script：" + esc(bundle.get("build_script", "")) + "</div>")

    # 三栏结构：① 用户母题 / ② 母题溯源 / ③ 原创题（cards）
    mother_html = render_mother_section(bundle.get("mother"), abs_fig_dir)
    sources_html = render_sources_section(bundle.get("sources"))

    bundle_meta = {"task": bundle.get("task", ""), "content_fp": bundle.get("content_fp", ""),
                   "fetch_time": bundle.get("fetch_time", ""),
                   "build_script": bundle.get("build_script", ""),
                   "session_id": bundle.get("session_id", "")}

    problems_js_json = json.dumps(probs_map, ensure_ascii=False)

    html = (HTML_TEMPLATE
            .replace("__TITLE__", esc(bundle.get("task", "原创题评审")))
            .replace("__META__", meta_block)
            .replace("__MOTHER__", mother_html)
            .replace("__SOURCES__", sources_html)
            .replace("__NPROB__", str(len(problems)))
            .replace("__CARDS__", "\n".join(cards))
            .replace("__COMMON_TAGS__", json.dumps(COMMON_TAGS, ensure_ascii=False))
            .replace("__BUNDLE_META__", json.dumps(bundle_meta, ensure_ascii=False))
            .replace("__PROBLEMS_JS__", problems_js_json))
    open(html_path, "w", encoding="utf-8").write(html)
    return html_path


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _mathml(tex):
    """LaTeX -> 原生 MathML 字符串（浏览器无需任何 JS 即可渲染，保持单文件自包含）。"""
    import latex2mathml.converter as lc
    try:
        mm = lc.convert((tex or "").strip())
        if "xmlns=" not in mm[:260]:
            mm = mm.replace("<math>", '<math xmlns="http://www.w3.org/1998/Math/MathML">', 1)
        return mm
    except Exception:
        return None


def render_latex(text):
    """把文本里的 $...$（行内）与 $$...$$（独立）转成原生 MathML；其余 HTML 转义。
    非公式文本与解析失败的公式均以转义文本兜底，避免 XSS 与乱码。
    """
    if text is None:
        return ""
    text = str(text)
    pat = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.DOTALL)
    out = []
    last = 0
    for m in pat.finditer(text):
        out.append(esc(text[last:m.start()]))
        if m.group(1) is not None:                      # 独立公式
            mm = _mathml(m.group(1))
            out.append('<span class="math-block">' + mm + "</span>" if mm else esc(m.group(0)))
        else:                                           # 行内公式
            mm = _mathml(m.group(2))
            out.append('<span class="math">' + mm + "</span>" if mm else esc(m.group(0)))
        last = m.end()
    out.append(esc(text[last:]))
    return "".join(out)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · 评审</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ea;--ink:#1f2329;--sub:#6b7280;
        --ok:#1a7f37;--bad:#cf222e;--pen:#bf8700;--ac:#2563eb;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       font-size:14px;line-height:1.6}
  header{padding:16px 20px;background:#fff;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:18px}
  .meta{margin-top:8px;color:var(--sub);font-size:12.5px;background:#fafbfc;
        border:1px solid var(--line);border-radius:8px;padding:8px 12px}
  .toolbar{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);
           padding:10px 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .toolbar button{background:var(--ac);color:#fff;border:0;border-radius:8px;
                  padding:8px 14px;font-size:14px;cursor:pointer}
  .toolbar button:disabled{background:#c8ccd4;cursor:not-allowed}
  .toolbar button.sec{background:#fff;color:var(--ac);border:1px solid var(--ac)}
  .toolbar .hint{color:var(--sub);font-size:12.5px}
  .wrap{padding:16px 20px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
  @media(max-width:1100px){.wrap{grid-template-columns:repeat(2,minmax(0,1fr))}}
  @media(max-width:720px){.wrap{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;
        box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .chead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}
  .ptitle{font-weight:600;font-size:15px}
  .stars{color:var(--pen);font-size:14px;white-space:nowrap}
  .fig{max-width:280px;max-height:220px;width:auto;height:auto;display:block;object-fit:contain;border:1px solid var(--line);border-radius:8px;margin:6px 0;background:#fff;cursor:zoom-in}
  .fig:hover{box-shadow:0 0 0 2px var(--ac)}
  /* 宽图（如合成路线框图，宽高比≥2.5）：填满内容区而非压成细条，保证结构可辨认 */
  .fig-wide{display:block;width:100%;height:auto;max-width:760px;margin:6px 0;border:1px solid var(--line);border-radius:8px;background:#fff;cursor:zoom-in}
  .fig-wide:hover{box-shadow:0 0 0 2px var(--ac)}
  .mother img{cursor:zoom-in}
  .mother img:hover{box-shadow:0 0 0 2px var(--ac)}
  /* 题目配图点击放大 lightbox 遮罩 */
  .zoom-mask{display:none;position:fixed;z-index:50;left:0;top:0;width:100%;height:100%;
            background:rgba(20,22,28,.86);align-items:center;justify-content:center;cursor:zoom-out}
  .zoom-mask.show{display:flex}
  .zoom-mask img{max-width:94vw;max-height:94vh;object-fit:contain;border-radius:8px;background:#fff;
                box-shadow:0 8px 40px rgba(0,0,0,.5)}
  .zoom-mask .hint{position:absolute;top:14px;left:50%;transform:translateX(-50%);color:#fff;
                   font-size:13px;opacity:.85;pointer-events:none}
  .blk{margin:8px 0}
  .lab{font-size:12px;color:var(--sub);font-weight:600;margin-bottom:2px}
  .stem,.ans,.note,.ana{white-space:pre-wrap;word-break:break-word}
  .ans{color:var(--ok);font-weight:600}
  /* 原生 MathML 公式（无外链、无需 JS） */
  .math{display:inline-block;vertical-align:middle;font-size:1.02em;line-height:1.1}
  .math-block{display:block;text-align:center;margin:10px 0;overflow-x:auto}
  math{font-style:normal}
  .subs{margin:6px 0;padding-left:20px}
  .anc{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:4px}
  .anc th,.anc td{border:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
  .anc th{background:#f0f3f7}
  .review{border-top:1px dashed var(--line);margin-top:10px;padding-top:10px}
  .rstate{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  .rstate label{cursor:pointer}
  textarea{width:100%;min-height:64px;margin-top:6px;border:1px solid var(--line);
           border-radius:8px;padding:8px;font-family:inherit;font-size:13px;resize:vertical}
  .tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .tag{background:#eef2ff;color:var(--ac);border:1px solid #c7d7ff;border-radius:999px;
       padding:3px 10px;font-size:12px;cursor:pointer}
  .tag.on{background:var(--ac);color:#fff}
  .card.unusable{border-color:var(--bad)}
  .card.usable{border-color:var(--ok)}
  /* 三栏结构：① 用户母题 / ② 学科网·菁优网母题溯源 / ③ 原创题 */
  .sech{margin:18px 20px 6px;font-size:16px;font-weight:600;color:var(--ink);
        border-left:4px solid var(--ac);padding-left:10px}
  .sec{margin:16px 20px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .sec > h2{margin:0 0 10px;font-size:16px;font-weight:600;color:var(--ink);
            border-left:4px solid var(--ac);padding-left:10px}
  .sec-mother .mfig{display:flex;gap:14px;align-items:flex-start;margin-bottom:8px}
  .sec-mother .mfig img{max-width:260px;max-height:220px;border:1px solid var(--line);
            border-radius:8px;background:#fff;cursor:zoom-in}
  .sec-mother .mfig img:hover{box-shadow:0 0 0 2px var(--ac)}
  .sec-mother .msubj{color:var(--sub);font-size:13px;margin-bottom:6px}
  .sec-mother .mtext{white-space:pre-wrap;word-break:break-word}
  .sec-source{overflow-x:auto}
  .sec-source table{width:100%;border-collapse:collapse;font-size:12.5px}
  .sec-source th,.sec-source td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
  .sec-source th{background:#f0f3f7}
  .sec-source .src-title{font-weight:600}
</style>
</head>
<body>
<header><h1>__TITLE__ · 原创题评审交互平台</h1>__META__</header>
<div class="toolbar">
  <button id="btnExport">导出可用题（Word 文档 .docx）</button>
  <button id="btnRegen" title="需先勾选至少一道「不可用」题后点击；未勾选时点击会提示">一键重新生成</button>
  <button id="btnPreview" class="sec">预览可用题（仅看题）</button>
  <span class="hint" id="hint"></span>
</div>
__MOTHER__
__SOURCES__
<h2 class="sech">③ 原创题（__NPROB__ 道）</h2>
<div class="wrap" id="wrap">__CARDS__</div>
<script>
const COMMON_TAGS = __COMMON_TAGS__;
const META = __BUNDLE_META__;
const BRIDGE_URL = 'http://127.0.0.1:8787/rerun';  // 本地桥接服务（端口可由 REVIEW_BRIDGE_PORT 覆盖，须与服务端一致）
const REPORT_URL = 'http://127.0.0.1:8787/report';  // 业务事件上报（available/optimize）经本地桥接转发后端；generate/export 由桥接服务端补报（更可靠）
const SID = (META.session_id || 'unknown');          // 母题会话 ID（同一母题批次固定）
const LIMIT = 16384;                                  // 整请求体 16KiB 上限（接口硬约束）
const BRIDGE_BASE = BRIDGE_URL.split('/rerun')[0];  // 桥接根地址（端口随 REVIEW_BRIDGE_PORT）
// 导出用题面数据（含 base64 配图），由 build 注入；键为卡片 pid
const PROBLEMS = __PROBLEMS_JS__;
// localStorage 去重：同一 session 仅首次上报 available（导出 / 重生成各自记一次）
function _rk(k){return k+'_'+SID;}
function _isReported(k){try{return localStorage.getItem(_rk(k))==='1';}catch(e){return false;}}
function _markReported(k){try{localStorage.setItem(_rk(k),'1');}catch(e){}}
function reportEvent(event, info){
  // 向本地桥接 POST 业务事件；桥接补全 installation_id 后转发后端。失败静默（不影响评审）。
  const payload=JSON.stringify({event:event, session_id:SID, info:info});
  if(payload.length>LIMIT) return; // 超 16KiB 直接丢弃（调用方已做裁剪守卫）
  fetch(REPORT_URL,{method:'POST',headers:{'Content-Type':'application/json'},body:payload})
    .then(r=>r.json()).then(()=>{}).catch(()=>{});
}
function reportAvailable(){
  // available 事件：每题一条事件，扁平 info={questionID, available}（usable=是，其余=否）
  const data=collect();
  for(const pid of Object.keys(data.selection)){
    const st=data.selection[pid];
    reportEvent('available', {questionID:pid, available:(st==='usable'?'是':'否')});
  }
}
function reportOptimize(){
  // optimize 事件：仅含被标不可用且有建议的题；每题一条事件，扁平 info={questionID, text}（text 裁剪至 2000 字）
  const data=collect();
  for(const pid of Object.keys(data.suggestions)){
    const txt=(data.suggestions[pid]||'').trim();
    if(!txt) continue;
    reportEvent('optimize', {questionID:pid, text:txt.slice(0,2000)});
  }
}
function getState(pid){
  const els=document.querySelectorAll('input[name="st-'+pid+'"]');
  for(const e of els){if(e.checked)return e.value;}
  return 'usable';
}
function collect(){
  const cards=[...document.querySelectorAll('.card')];
  const out={exported_at:new Date().toISOString(),selection:{},suggestions:{},regenerate:{}};
  for(const c of cards){
    const pid=c.id.replace('card-','');
    const st=getState(pid);
    out.selection[pid]=st;
    const sugg=document.getElementById('sugg-'+pid).value.trim();
    if(st==='unusable'){
      out.suggestions[pid]=sugg;
      if(sugg) out.regenerate[pid]=sugg;   // 有建议即触发定向重生成
    }
  }
  return out;
}
function download(name,text){
  const blob=new Blob([text],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=name;a.click();URL.revokeObjectURL(a.href);
}
function updateRegenBtn(){
  // 「一键重新生成」始终可点击：未勾选「不可用」时点击会弹窗提示；勾选后才提交重跑队列（禁用态会静默吞掉点击，故不禁用）。
  const has=[...document.querySelectorAll('.card')].some(c=>{
    const pid=c.id.replace('card-','');return getState(pid)==='unusable';});
  const b=document.getElementById('btnRegen');
  b.title=has?'点击后自动生成重跑消息并发送给使用 skill 的 agent（不可用题母题先更新再重跑）'
             :'需先勾选至少一道「不可用」题';
}
function buildRerunMsg(data){
  const bad=Object.keys(data.selection).filter(k=>data.selection[k]==='unusable');
  const L=[];
  L.push('【原创题重跑请求】（来自评审页「一键重新生成」）');
  L.push('- 任务：'+(META.task||''));
  L.push('- 原批次 content_fp：'+(META.content_fp||'')+'（重跑后该指纹须变化）');
  L.push('- 不可用题：'+bad.join('、'));
  L.push('');
  L.push('逐题建议：');
  bad.forEach(pid=>{L.push('- '+pid+'：'+(data.suggestions[pid]||'（未填建议，请按通用质量约束针对性重命制）'));});
  L.push('');
  L.push('重跑硬要求：');
  L.push('1. 对每道不可用题，先重新检索并更新其母题源：题源仅限菁优网（jyeoo.com）与学科网（zxxk.com），并按题源快照机制更新 sources manifest 与实时元数据/指纹。');
  L.push('2. 母题更新后，再按该题建议做定向重命制：建议命中既有约束则强化该约束，自由文本作为该题专属重命制指令注入生成提示。');
  L.push('3. 未标记不可用的题保持不变（content_fp 不变，重命制守卫继续生效）。');
  L.push('4. 重跑完成后交付更新后的完整评审包（docx + 原创题_*.json + 评审 HTML），供下一轮评审。');
  return L.join('\\n');
}
function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(t);}
  const ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);return Promise.resolve();
}
document.getElementById('btnRegen').onclick=()=>{
  const data=collect();
  const bad=Object.keys(data.selection).filter(k=>data.selection[k]==='unusable');
  if(bad.length===0){alert('请先勾选至少一道「不可用」题。');return;}
  // 业务事件上报（向本地桥接 /report，桥接转发后端；失败静默）
  if(!_isReported('avail_regen')){ reportAvailable(); _markReported('avail_regen'); }  // 首次点重生成 → available
  reportOptimize();                                                                      // 每次点重生成 → optimize
  data.rerun=true;        // 标记本反馈用于「母题更新+重跑」
  data.meta=META;         // 附带批次元数据（task/content_fp 等），便于 agent 定位重跑目标
  const payload=JSON.stringify(data);
  fetch(BRIDGE_URL,{method:'POST',headers:{'Content-Type':'application/json'},body:payload})
    .then(r=>r.json())
    .then(j=>{
      if(j&&j.accepted){
        document.getElementById('hint').textContent=
          '✅ 已提交重跑队列（'+bad.length+' 道不可用题）：请在对话中发送「处理队列」，我将立即用 agent 更新母题并重命制。';
      }else{
        throw new Error((j&&j.error)? j.error : 'bridge rejected');
      }
    })
    .catch(err=>{
      // 降级：桥接服务未启动 → 下载 JSON + 复制重跑消息（人工兜底）
      download('selection_feedback.json',payload);
      const msg=buildRerunMsg(data);
      copyText(msg);
      download('重跑消息.md',msg);
      document.getElementById('hint').textContent=
        '⚠️ 桥接服务未启动（'+err.message+'）：已下载 selection_feedback.json 并复制重跑消息到剪贴板，请在对话中发送「处理队列」，我将立即处理。';
    });
};
function downloadBlob(name, blob){
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1500);
}
function doExportDocx(){
  const data=collect();
  const picks=[];
  for(const pid of Object.keys(data.selection)){
    if(data.selection[pid]==='usable' && PROBLEMS[pid]) picks.push(PROBLEMS[pid]);
  }
  if(picks.length===0){alert('未勾选任何可用题，禁止导出空文档。');return;}
  if(!_isReported('avail_export')){ reportAvailable(); _markReported('avail_export'); }  // 首次点导出可用题 → available
  const hint=document.getElementById('hint');
  hint.textContent='正在生成 Word 文档…';
  fetch(BRIDGE_BASE + '/export-docx',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:'原创题（精选可用题）',problems:picks,session_id:SID})
  })
    .then(r=>{ if(!r.ok) throw new Error('服务返回 '+r.status); return r.blob(); })
    .then(blob=>{
      downloadBlob(String(META.task||'原创题').split('/').join('_')+'_原创题生成.docx', blob);
      hint.textContent='✅ 已导出 '+picks.length+' 道可用题为 Word 文档（.docx）。';
    })
    .catch(err=>{
      // 桥接未启动 / 导出失败 → 退化导出 JSON（保留原能力，绝不静默失败）
      download(String(META.task||'原创题').split('/').join('_')+'_原创题生成.json', JSON.stringify({exported_at:new Date().toISOString(),problems:picks},null,2));
      hint.textContent='⚠️ 导出 Word 失败（'+(err&&err.message||err)+'）：已退化为导出 JSON，请确认桥接服务已启动。';
    });
}
document.getElementById('btnExport').onclick=doExportDocx;
document.getElementById('btnPreview').onclick=()=>{
  // 预览可用题：去掉评审模块（三态/建议框/标签），仅看试题本身。
  // 未做选择时默认全部为 usable，故「预览可用题 = 全部试题」。
  const data=collect();
  const ok=[...document.querySelectorAll('.card')].filter(c=>{
    const pid=c.id.replace('card-','');return data.selection[pid]==='usable';});
  if(ok.length===0){alert('没有可用题可预览。');return;}
  const w=window.open('','_blank');
  w.document.write('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">');
  w.document.write('<title>可用题预览（'+ok.length+' 题）</title>');
  w.document.write('<style>body{margin:16px;background:#f5f6f8;color:#1f2329;');
  w.document.write('font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}');
  w.document.write('.card{border:1px solid #e3e6ea;border-radius:12px;padding:14px;margin:12px 0;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.04)}');
  w.document.write('.chead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}');
  w.document.write('.ptitle{font-weight:600;font-size:15px}.stars{color:#bf8700;font-size:14px;white-space:nowrap}');
  w.document.write('.fig{max-width:280px;max-height:220px;width:auto;height:auto;display:block;object-fit:contain;border:1px solid #e3e6ea;border-radius:8px;margin:6px 0;background:#fff}');
  w.document.write('.fig-wide{display:block;width:100%;height:auto;max-width:760px;margin:6px 0;border:1px solid #e3e6ea;border-radius:8px;background:#fff}');
  w.document.write('.blk{margin:8px 0}.lab{font-size:12px;color:#6b7280;font-weight:600;margin-bottom:2px}');
  w.document.write('.stem,.ans,.note,.ana{white-space:pre-wrap;word-break:break-word}.ans{color:#1a7f37;font-weight:600}');
  w.document.write('math{font-style:normal}.math{display:inline-block;vertical-align:middle}.math-block{display:block;text-align:center;margin:10px 0;overflow-x:auto}');
  w.document.write('</style></head><body>');
  w.document.write('<h2>可用题预览（'+ok.length+' 题）</h2>');
  ok.forEach(c=>{
    const clone=c.cloneNode(true);
    const rev=clone.querySelector('.review'); if(rev) rev.remove();  // 去掉评审模块
    w.document.write('<div class="card">'+clone.innerHTML+'</div>');
  });
  w.document.write('</body></html>');
  w.document.close();
};
// 标签快速追加到对应建议框
document.querySelectorAll('.tag').forEach(b=>{
  b.onclick=()=>{
    const pid=b.getAttribute('data-p');const tag=b.getAttribute('data-tag');
    const ta=document.getElementById('sugg-'+pid);
    const cur=ta.value.trim();
    ta.value=(cur?cur+'；':'')+tag;
    b.classList.toggle('on');
  };
});
// 三态视觉反馈 + 「一键重新生成」按钮状态联动
document.querySelectorAll('input[type=radio]').forEach(r=>{
  r.onchange=()=>{
    const pid=r.name.replace('st-','');const card=document.getElementById('card-'+pid);
    card.classList.remove('usable','unusable');
    const st=getState(pid);
    if(st==='usable')card.classList.add('usable');
    if(st==='unusable')card.classList.add('unusable');
    updateRegenBtn();
  };
});
updateRegenBtn();
// 题目配图点击放大（lightbox）：卡片条件图 .fig、母题原图 .mother img 与三栏① .sec-mother img 均可点开原图
(function(){
  const mask=document.createElement('div');
  mask.className='zoom-mask';
  mask.innerHTML='<span class="hint">点击任意处关闭</span><img alt="放大图">';
  document.body.appendChild(mask);
  const zimg=mask.querySelector('img');
  function open(src){ if(!src) return; zimg.src=src; mask.classList.add('show'); }
  function close(){ mask.classList.remove('show'); zimg.src=''; }
  mask.addEventListener('click', close);
  document.addEventListener('click', function(e){
    const t=e.target;
    if(t && (t.classList.contains('fig') || (t.tagName==='IMG' && (t.closest('.mother') || t.closest('.sec-mother'))))){
      e.preventDefault(); open(t.getAttribute('src') || t.src);
    }
  });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') close(); });
})();
</script>
</body></html>
"""


# ===================== OMML 管线（复用 build 脚本修复版） =====================
def omml_element(latex):
    import latex2mathml.converter as lc
    import mathml2omml
    from docx.oxml import parse_xml
    latex = latex.replace('\\\\', '\\')
    # 向量符号归一化为加粗，规避 mathml2omml 对 \\overrightarrow/\\vec 的非法 XML
    latex = re.sub(r'\\overrightarrow\{([^}]*)\}', r'\\mathbf{\1}', latex)
    latex = re.sub(r'\\vec\s+(\w)', r'\\mathbf{\1}', latex)
    latex = re.sub(r'\\vec\{([^}]*)\}', r'\\mathbf{\1}', latex)
    mm = lc.convert(latex)
    om = mathml2omml.convert(mm)
    if "xmlns:m=" not in om:
        om = om.replace("<m:oMath>", '<m:oMath xmlns:m="%s">' % MNS, 1)
    return parse_xml(om)


def set_run_font(run, eastasia="宋体", latin="Times New Roman"):
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts')
    if rfonts is None:
        from docx.oxml.ns import qn
        rfonts = rpr.makeelement(qn('w:rFonts'), {})
        rpr.append(rfonts)
    rfonts.set('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ascii', latin)
    rfonts.set('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hAnsi', latin)
    rfonts.set('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia', eastasia)


def add_rich(par, text):
    """按 $...$ 拆分，公式走 OMML，其余走普通 run。"""
    TOK = re.compile(r'\$(.+?)\$')
    parts = TOK.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            try:
                par._p.append(omml_element(part))
            except Exception:
                r = par.add_run(part)
                set_run_font(r)
        else:
            if part:
                r = par.add_run(part)
                set_run_font(r)


def H(doc, text, level=1):
    h = doc.add_heading(level=level)
    r = h.add_run(text)
    set_run_font(r)
    return h


def P(doc, text=""):
    p = doc.add_paragraph()
    if text:
        add_rich(p, text)
    return p


def labeled(doc, label, text):
    p = doc.add_paragraph()
    r = p.add_run(label + "：")
    set_run_font(r)
    r.bold = True
    add_rich(p, text)
    return p


def add_fig(doc, path, caption, width_cm=9):
    if path and os.path.exists(path):
        from docx.shared import Cm
        doc.add_picture(path, width=Cm(width_cm))
        last = doc.paragraphs[-1]
        last.alignment = 1
        cp = doc.add_paragraph()
        cp.alignment = 1
        r = cp.add_run(caption)
        set_run_font(r)
        r.font.size = doc.styles["Normal"].font.size


def anchor_table(doc, rows):
    from docx.shared import Pt
    t = doc.add_table(rows=1, cols=3)
    t.style = "Light Grid Accent 1"
    hdr = t.rows[0].cells
    for i, h in enumerate(["维度", "母题", "本题"]):
        hdr[i].text = h
        for para in hdr[i].paragraphs:
            for rr in para.runs:
                rr.bold = True
                set_run_font(rr)
    for a, b, c in rows:
        cells = t.add_row().cells
        cells[0].text = a
        cells[1].text = b
        cells[2].text = c
        for cc in cells:
            for para in cc.paragraphs:
                for rr in para.runs:
                    set_run_font(rr)
    return t


# ===================== problems -> docx 汇编器 =====================
def problems_to_docx(problems, out_path, title="原创题（精选/重生成）",
                     sources=None, content_fp=None, figures_dir="."):
    """把 problem 列表汇编成 docx（筛选导出 / 定向重生成 共用）。
    含：题标题 + 母题来源 + 题干 + 子问 + 配图 + 答案 + 解析 + 改编说明 + 创新点 + 难度锚定表 + 自检/指纹。
    """
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    H(doc, title, 1)
    if sources:
        H(doc, "母题溯源（全局）", 2)
        for i, s in enumerate(sources):
            labeled(doc, "母题 %d" % (i + 1),
                    "%s｜%s｜发布 %s｜组卷 %s｜引用 %s｜难度 %s｜指纹 %s"
                    % (s.get("site", ""), s.get("sid", ""), s.get("date", ""),
                       s.get("zujuan", ""), s.get("yinyong", ""),
                       s.get("difficulty", ""), s.get("content_fp", "")))
    for idx, p in enumerate(problems, 1):
        H(doc, "题 %d（%s）" % (idx, p.get("title", p.get("id", ""))), 2)
        if p.get("source"):
            labeled(doc, "母题来源", p["source"])
        add_rich(doc.add_paragraph(), p.get("stem", ""))
        for q in p.get("subquestions", []):
            P(doc, q)
        for o in p.get("options", []):
            P(doc, o)
        if p.get("figure"):
            fpath = p["figure"] if os.path.isabs(p["figure"]) else os.path.join(figures_dir, p["figure"])
            add_fig(doc, fpath, "图 %d  %s 条件图" % (idx, p.get("id", "")), 8)
        labeled(doc, "答案", p.get("answer", ""))
        for j, a in enumerate(p.get("analysis", [])):
            labeled(doc, "解析" if j == 0 else "解析（续）", a)
        labeled(doc, "改编说明", p.get("adaptation", ""))
        labeled(doc, "创新点说明", p.get("innovation", ""))
        if p.get("difficulty_anchor"):
            anchor_table(doc, p["difficulty_anchor"])
    # 自检 + 指纹
    H(doc, "一致性自检清单", 1)
    P(doc, "① 知识点与方法与原题一致；② 主方法未偷换；③ 非纯数字替换（≥2 类结构性改动）；")
    P(doc, "④ 命题创新点明确且未以降难为代价；⑤ 难度锚定母题（6 维无维度减少）；")
    P(doc, "⑥ 公式 LaTeX→OMML，无 CJK/命令残留；⑦ 配图仅含题设元素、无答案；")
    P(doc, "⑧ 题源快照已附（见溯源）；⑨ 答案逐问完整、数值均来自题面。")
    if content_fp:
        labeled(doc, "题目内容指纹（content_fp）", content_fp + "（重命制守卫）")
    doc.save(out_path)
    return out_path


# ===================== emit_bundle（被 build 脚本调用） =====================
def emit_bundle(bundle, out_dir, stem_name):
    """随 docx 额外写出 JSON 数据 + 自包含评审 HTML。
    bundle 须含 problems / sources / content_fp / task / constraint / fetch_time / figures_dir。
    自动注入稳定的 session_id（同一母题批次复用），并构建期上报 new_problem 事件。
    返回 (json_path, html_path, session_id)。
    """
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, stem_name + ".json")
    html_path = os.path.join(out_dir, stem_name + "_评审.html")
    # session_id：稳定复用（同一母题批次多次重跑共用），不在 bundle 内则读/写 sidecar
    session_id = bundle.get("session_id") or _load_or_create_session(out_dir, stem_name)
    bundle["session_id"] = session_id
    save_bundle(json_path, bundle)
    build_html(bundle, html_path, out_dir)
    # 构建期上报 new_problem（容错，不影响主任务）
    try:
        report_new_problem(session_id)
    except Exception:
        pass
    return json_path, html_path


if __name__ == "__main__":
    import sys
    print("review_data.py — 原创题评审数据层（2.9.0）。由 build/regenerate/review_stats 调用。")
