#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_pipeline.py — 原创题「唯一交付入口」（skill v2.10.1，需求 R0–R3 流程确定性改造 + R1 配图质检）

把"机械"交付步骤收口为单一脚本，使流程跨机器/跨会话确定、不依赖模型是否幻觉：
  ① Step 0 使用统计上报（执行前必跑，失败不影响主任务）
  ② R2 溯源闸门（通道=jyeoo/zxxk 时强制站内母源证据；websearch 时强制"未联网核验"标注）
  ③ 构建 docx（母题原图嵌入 + 各题 LaTeX→OMML + 卡片结构）
  ④ emit_bundle 写出评审 HTML + JSON + 构建期 new_problem 上报 + 桥接自举
  ⑤ R1 交付硬门（断言 docx + 评审HTML + JSON(content_fp) 三件套齐全且一致 + OMML 无残留 + 母图内嵌
                + 配图质检：题干含"如图"等引用图表述则必须配图，几何/物理/化学尤甚）
  ⑥ 清理中间产物

模型职责（缩减到最小）：识别母题 / 数学命制 / 选溯源通道 / 准备结构化 JSON，然后**唯一合法交付动作 = 调用本脚本**。
模型不得手写 docx 构建代码、不得把 md 当主交付。

输入 JSON 契约（最小字段）：
  stem_name   (str, 必填)  交付物前缀，如 "原创题_指数函数奇偶单调_5道_2026-11-04"
  problems    (list, 必填) 每题 {id,title,stem,subquestions?,options?,figure?,answer,analysis[],adaptation,innovation,difficulty_anchor[[dim,mother,this]...],source?}
  mother      (obj, 可选)  {subject,text,figure?}            —— 母题识别节 + 母题原图
  sources     (list, 可选)  通道=jyeoo/zxxk 时必填；每题母源 {site,sid/url,publish/date,zujuan,yinyong,difficulty,fingerprint/content_fp}
  channel     (str)         jyeoo | zxxk | websearch（缺失默认 websearch 并告警）
  fetch_time  (str, 可选)
  task        (str, 可选)
  constraint  (str, 可选)
  figures_dir (str, 可选)   相对/绝对 figure 路径的解析基目录（默认=输出目录）
  session_id  (str, 可选)   沿用既有会话（重跑时）

退出码：0=交付成功；2=网关/校验失败（缺溯源证据、缺交付文件、OMML 残留、指纹不一致等）。
"""

import argparse
import json
import os
import re
import sys

# 让脚本无论从何处被调用都能 import 同目录的 review_data / report_usage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report_usage as ru
import review_data as rd

# OMML 校验时的 LaTeX 命令残留黑名单（lg 是合法对数运算符，不在此列）
RESIDUE_KW = ("frac", "times", "sqrt", "dfrac", "infty")
PICTURE_TAG = "pic:pic"

# 配图强约束（R1 扩展，2.10.1）：题干出现"如图"等引用图的表述则必须配图；
# 几何/物理/化学类试题尤甚。抽象含"图"词（图象/图像/图表）不触发强制。
FIG_CUES = ("如图", "见下图", "见上图", "下图", "上图", "图中",
            "如图所示", "图示", "图所示", "图见", "配图",
            "示意", "参考图", "给定图", "所给图", "下图所示")
FIG_ABSTRACT = ("图象", "图像", "图表", "统计图", "图像法", "图象法")


def needs_figure(stem):
    """题干是否引用了图（需要配图）。"""
    if not stem:
        return False
    s = stem
    for ab in FIG_ABSTRACT:  # 剔除抽象含"图"词，避免误触发
        s = s.replace(ab, "")
    return any(cue in s for cue in FIG_CUES)


def figure_gate(problems, out_dir):
    """R1 扩展（2.10.1）配图质检：
    题干出现如图类引用词但缺 figure → 拒绝交付；
    已声明 figure 但文件未就绪（未生成/未暂存）→ 拒绝交付。
    几何/物理/化学类试题应在命制时主动配图。"""
    missing, broken = [], []
    for p in problems or []:
        stem = p.get("stem") or ""
        fig = p.get("figure")
        if needs_figure(stem) and not fig:
            snippet = stem[:20] + "…" if len(stem) > 20 else stem
            missing.append((p.get("id"), snippet))
        if fig and not os.path.exists(os.path.join(out_dir, fig)):
            broken.append((p.get("id"), fig))
    if missing:
        detail = "；".join("%s(题干含'%s')" % (i, s) for i, s in missing)
        fail("⚠️ R1 交付硬门失败：以下条件引用了图却缺少 figure 配图 — %s。" % detail)
    if broken:
        detail = "；".join("%s(figure=%s 文件缺失)" % (i, f) for i, f in broken)
        fail("⚠️ R1 交付硬门失败：已声明 figure 但图片未就绪 — %s。" % detail)
    print("✅ 配图质检通过：引用图的题目均已配图，且图文件就绪。")


def fail(msg, code=2):
    print("❌ " + msg, file=sys.stderr)
    sys.exit(code)


# ===================== 图文件暂存 =====================
def _first_existing(candidates):
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def stage_figures(data, out_dir, input_dir):
    """把母题/各题引用的图文件复制到 out_dir，并把路径归一为 basename，
    使 docx(add_fig) 与评审 HTML(base64) 都能在 out_dir 内本地解析（与既有构建脚本惯例一致）。"""
    src_base = data.get("figures_dir")
    refs = []
    mother = data.get("mother") or {}
    if mother.get("figure"):
        refs.append(("mother", mother))
    for p in data.get("problems", []) or []:
        if p.get("figure"):
            refs.append(("problem", p))
    for kind, obj in refs:
        fig = obj["figure"]
        found = _first_existing([
            fig if os.path.isabs(fig) else None,
            os.path.join(src_base, fig) if src_base else None,
            os.path.join(input_dir, fig) if input_dir else None,
            os.path.join(out_dir, fig) if out_dir else None,
        ])
        base = os.path.basename(fig)
        if found:
            import shutil
            try:
                shutil.copy(found, os.path.join(out_dir, base))
            except OSError as e:  # noqa: BLE001
                print("⚠️ 图文件暂存失败 %s：%s" % (fig, e), file=sys.stderr)
        obj["figure"] = base  # 归一为 basename，供 docx/HTML 在 out_dir 内解析
    # 归一 figures_dir 为 out_dir（所有图已就位）
    data["figures_dir"] = out_dir


# ===================== ① Step 0 上报 =====================
def step0_report():
    """执行前必跑的使用统计上报（run）。失败不影响主流程。"""
    try:
        iid = ru.load_or_create_id(ru._id_path())
        ru.report(iid, version=ru.load_skill_version(), event="run")
    except Exception as e:  # noqa: BLE001 — 上报异常绝不阻断交付
        print("⚠️ Step0 上报异常(不影响主流程): %s" % e, file=sys.stderr)


# ===================== ② R2 溯源闸门 =====================
def trace_gate(channel, sources, problems):
    """通道=jyeoo/zxxk 强制站内母源证据；websearch 强制"未联网核验"标注。"""
    if channel in ("jyeoo", "zxxk"):
        if not sources:
            fail("⚠️ R2 溯源闸门失败：通道=%s 要求站内检索，但缺少母源溯源证据"
                 "（sources 为空）。必须在 Step 3 完成站内检索并写入 sources 后才能交付。" % channel)
        for i, s in enumerate(sources):
            sid = s.get("sid") or s.get("url")
            fp = s.get("content_fp") or s.get("fingerprint")
            if not sid:
                fail("⚠️ R2 溯源闸门失败：通道=%s 的母源 #%d 缺少唯一标识(sid/url)，无法溯源。" % (channel, i))
            if not fp:
                fail("⚠️ R2 溯源闸门失败：通道=%s 的母源 #%d 缺少内容指纹(fingerprint/content_fp)，"
                     "无法做变化检测。" % (channel, i))
        print("✅ R2 溯源闸门：通道=%s，%d 条站内母源证据齐全（sid+指纹）。" % (channel, len(sources)))
    elif channel == "websearch":
        blob = " ".join((p.get("adaptation", "") + " " + p.get("source", "")) for p in problems)
        missing = [p.get("id", "?") for p in problems if "未联网核验" not in blob]
        if missing:
            print("⚠️ R2 溯源闸门(WebSearch)：以下题未标注『未联网核验，建议人工核对』：%s。"
                  "已继续交付，但请补全溯源说明。" % ",".join(missing), file=sys.stderr)
        else:
            print("✅ R2 溯源闸门：WebSearch 通道，全部题已标注未联网核验。")
    else:
        fail("⚠️ R2 溯源闸门失败：未知溯源通道 channel=%r（应为 jyeoo/zxxk/websearch）。" % channel)


# ===================== ③ 构建 docx =====================
def build_docx(data, out_docx, figures_dir):
    """复用 review_data 的 H/P/labeled/add_fig/add_rich/anchor_table/content_fp_of 汇编 docx，
    并额外嵌入母题识别节（含母题原图）。返回 content_fp。"""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)

    rd.H(doc, data["stem_name"], 1)

    mother = data.get("mother") or {}
    if mother:
        rd.H(doc, "母题识别", 2)
        if mother.get("subject"):
            rd.labeled(doc, "学科/学段", mother["subject"])
        if mother.get("text"):
            rd.add_rich(doc.add_paragraph(), mother["text"])
        mf = mother.get("figure")
        if mf:
            mfpath = mf if os.path.isabs(mf) else os.path.join(figures_dir, mf)
            rd.add_fig(doc, mfpath, "母题原图", 9)

    sources = data.get("sources") or []
    if sources:
        rd.H(doc, "母题溯源（全局）", 2)
        for i, s in enumerate(sources):
            fp = s.get("content_fp") or s.get("fingerprint") or ""
            rd.labeled(doc, "母题 %d" % (i + 1),
                       "%s｜%s｜发布 %s｜组卷 %s｜引用 %s｜难度 %s｜指纹 %s"
                       % (s.get("site", ""), s.get("sid") or s.get("url", ""),
                          s.get("publish", s.get("date", "")), s.get("zujuan", ""),
                          s.get("yinyong", ""), s.get("difficulty", ""), fp))

    problems = data["problems"]
    for idx, p in enumerate(problems, 1):
        rd.H(doc, "题 %d（%s）" % (idx, p.get("title", p.get("id", ""))), 2)
        if p.get("source"):
            rd.labeled(doc, "母题来源", p["source"])
        rd.add_rich(doc.add_paragraph(), p.get("stem", ""))
        for q in p.get("subquestions", []) or []:
            rd.P(doc, q)
        for o in p.get("options", []) or []:
            rd.P(doc, o)
        fig = p.get("figure")
        if fig:
            fpath = fig if os.path.isabs(fig) else os.path.join(figures_dir, fig)
            rd.add_fig(doc, fpath, "图 %d %s 条件图" % (idx, p.get("id", "")), 8)
        rd.labeled(doc, "答案", p.get("answer", ""))
        for j, a in enumerate(p.get("analysis", []) or []):
            rd.labeled(doc, "解析" if j == 0 else "解析（续）", a)
        rd.labeled(doc, "改编说明", p.get("adaptation", ""))
        rd.labeled(doc, "创新点说明", p.get("innovation", ""))
        if p.get("difficulty_anchor"):
            rd.anchor_table(doc, p["difficulty_anchor"])

    rd.H(doc, "一致性自检清单", 1)
    rd.P(doc, "① 知识点与方法与原题一致；② 主方法未偷换；③ 非纯数字替换（≥2 类结构性改动）；")
    rd.P(doc, "④ 命题创新点明确且未以降难为代价；⑤ 难度锚定母题（6 维无维度减少）；")
    rd.P(doc, "⑥ 公式 LaTeX→OMML，无 CJK/命令残留；⑦ 配图仅含题设元素、无答案；")
    rd.P(doc, "⑧ 题源快照已附（见溯源）；⑨ 答案逐问完整、数值均来自题面。")

    cfp = rd.content_fp_of(problems)
    rd.labeled(doc, "题目内容指纹（content_fp）", cfp + "（重命制守卫）")
    doc.save(out_docx)
    return cfp


# ===================== ⑤ R1 交付硬门 / OMML 校验 =====================
def validate_docx(out_docx, mother_has_fig):
    """R1 子项：遍历 docx 全部 m:oMath，断言无 CJK、无 LaTeX 命令残留；母题含图则必须内嵌图片。"""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(out_docx)
    n_omml = 0
    n_pic = 0
    for omath in doc.element.findall(".//" + qn("m:oMath")):
        txt = "".join(omath.itertext())
        n_omml += 1
        for ch in txt:
            if ord(ch) > 0x2E80:
                fail("⚠️ R1 交付硬门失败：docx 公式含中日韩字符 %r（OMML 乱码风险）。" % ch)
        for kw in RESIDUE_KW:
            if kw in txt:
                fail("⚠️ R1 交付硬门失败：docx 公式含 LaTeX 命令残留字面 %r。" % kw)
    for _ in doc.element.findall(".//" + qn(PICTURE_TAG)):
        n_pic += 1
    if mother_has_fig and n_pic == 0:
        fail("⚠️ R1 交付硬门失败：母题含原图，但 docx 未内嵌任何图片。")
    print("✅ OMML 校验通过（oMath 节点 %d，图片 %d，无 CJK/命令残留）。" % (n_omml, n_pic))


def delivery_gate(out_docx, json_path, html_path, cfp, mother_has_fig):
    """R1 主项：三件套齐全且 content_fp 一致、HTML 含评审卡片与母图 base64。"""
    for f in (out_docx, json_path, html_path):
        if not os.path.exists(f):
            fail("⚠️ R1 交付硬门失败：缺少交付文件 %s。" % f)
    j = json.load(open(json_path, encoding="utf-8"))
    if j.get("content_fp") != cfp:
        fail("⚠️ R1 交付硬门失败：JSON content_fp(%s) 与 docx 指纹(%s) 不一致。"
             % (j.get("content_fp"), cfp))
    html = open(html_path, encoding="utf-8").read()
    if 'class="card"' not in html:
        fail("⚠️ R1 交付硬门失败：评审 HTML 缺少 .card 评审卡片。")
    if mother_has_fig and "data:image/png;base64" not in html:
        fail("⚠️ R1 交付硬门失败：母题含原图，但评审 HTML 未内嵌母图 base64。")
    print("✅ R1 交付硬门：docx + 评审HTML + JSON(content_fp=%s) 三件套齐全且一致。" % cfp)


# ===================== 主流程 =====================
def main():
    ap = argparse.ArgumentParser(description="原创题唯一交付入口（v2.10.0）")
    ap.add_argument("--input", required=True, help="结构化交付输入 JSON 路径")
    ap.add_argument("--out-dir", default=None, help="交付目录（默认=输入 JSON 所在目录）")
    args = ap.parse_args()

    data = json.load(open(args.input, encoding="utf-8"))
    stem_name = data.get("stem_name")
    if not stem_name:
        fail("⚠️ 输入缺少 stem_name。")
    if not data.get("problems"):
        fail("⚠️ 输入缺少 problems。")

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.input)) or "."
    os.makedirs(out_dir, exist_ok=True)
    input_dir = os.path.dirname(os.path.abspath(args.input))
    channel = data.get("channel") or "websearch"
    if not data.get("channel"):
        print("⚠️ 输入未指定 channel，按 websearch 处理（若实际为站点通道请补全）。", file=sys.stderr)

    # 图文件暂存（母题/各题引用图复制进 out_dir 并归一 basename）
    stage_figures(data, out_dir, input_dir)
    figures_dir = data["figures_dir"]  # 已被 stage_figures 设为 out_dir

    # ① Step 0
    step0_report()
    # ② R2 溯源闸门
    trace_gate(channel, data.get("sources") or [], data["problems"])

    # ②-b R1 扩展 配图质检（题意引用图则必须配图；几何/物理/化学尤甚）
    figure_gate(data["problems"], out_dir)

    mother = data.get("mother") or {}
    mother_has_fig = bool(mother.get("figure"))

    # ③ 构建 docx
    out_docx = os.path.join(out_dir, stem_name + "_原创题生成.docx")
    cfp = build_docx(data, out_docx, figures_dir)

    # ⑤ OMML 校验 + 母图内嵌校验
    validate_docx(out_docx, mother_has_fig)

    # ④ emit_bundle（评审 HTML + JSON + new_problem 上报 + 桥接自举）
    bundle = {
        "task": data.get("task", ""),
        "constraint": data.get("constraint", ""),
        "fetch_time": data.get("fetch_time", ""),
        "content_fp": cfp,
        "mother": mother,
        "sources": data.get("sources") or [],
        "problems": data["problems"],
        "figures_dir": figures_dir,
        "channel": channel,
    }
    if data.get("session_id"):
        bundle["session_id"] = data["session_id"]
    json_path, html_path = rd.emit_bundle(bundle, out_dir, stem_name)

    # ⑤ R1 交付硬门
    delivery_gate(out_docx, json_path, html_path, cfp, mother_has_fig)

    # ⑥ 清理中间产物
    rd.cleanup_topic_intermediates(out_dir)

    print("✅ 交付完成：")
    print("   DOCX : " + out_docx)
    print("   JSON : " + json_path)
    print("   HTML : " + html_path)


if __name__ == "__main__":
    main()
