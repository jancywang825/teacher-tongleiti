# -*- coding: utf-8 -*-
"""
review_stats.py — 原创题评审交互平台 · 选题规律 + 周度可用率（需求 2.9.0 能力三、四）

消费：selection_feedback.json（HTML 导出）
产出（持久化于 skill data 目录，跨批次累积、不丢历史）：
  - selection_log.json      逐批选题与建议（原始流水）
  - user_patterns.json      跨批聚合画像（高频弃用原因 / 建议标签聚类）
  - weekly_report_*.html    周度可用率周报（含 matplotlib 趋势折线图，base64 嵌入）
  - self_opt_proposals.json 自优化「建议」（可解释、可回退；不静默改 SKILL.md）

北极星指标：周度可用率 = 当周标记「可用」题数 ÷ 当周全部进入评审题数，逼近 90%→100%。

说明：自优化注入约束是敏感动作，本脚本只「生成建议 + 变更记录草案」，由 agent 在用户确认后
显式修改 SKILL.md 约束并写入 user_patterns 的 changelog（不静默覆盖、用户可否决）。
"""
import os
import sys
import json
import argparse
import datetime
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_data as RD

# 常用建议标签（与 HTML 一致）
COMMON_TAGS = ["情境不真实", "难度过高", "与母题太像", "缺图示",
               "设问重复", "计算超浪", "计算超纲", "数据不自洽", "答案有误"]
# 修正：上面 "计算超浪" 为笔误占位，下面统一用规范集
COMMON_TAGS = ["情境不真实", "难度过高", "与母题太像", "缺图示",
               "设问重复", "计算超纲", "数据不自洽", "答案有误"]


def ensure_dir():
    os.makedirs(RD.SKILL_DATA_DIR, exist_ok=True)


def iso_week(ts):
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except Exception:
        dt = datetime.datetime.now()
    y, w, _ = dt.isocalendar()
    return "%d-W%02d" % (y, w)


def extract_reasons(sugg_text):
    """从自由建议文本中命中常用标签词，返回命中的标签集合。"""
    s = sugg_text or ""
    hit = set()
    for tag in COMMON_TAGS:
        if tag in s:
            hit.add(tag)
    return hit


def append_selection_log(fb):
    ensure_dir()
    log = []
    if os.path.exists(RD.SELECTION_LOG):
        try:
            log = json.load(open(RD.SELECTION_LOG, "r", encoding="utf-8"))
        except Exception:
            log = []
    batch = fb.get("exported_at") or RD.now_iso()
    sel = fb.get("selection", {})
    sugg = fb.get("suggestions", {})
    total = len(sel)
    usable = [k for k, v in sel.items() if v == "usable"]
    unusable = [k for k, v in sel.items() if v == "unusable"]
    pending = [k for k, v in sel.items() if v == "pending"]
    reasons = {}
    for pid, text in sugg.items():
        for r in extract_reasons(text):
            reasons.setdefault(r, []).append(pid)
    entry = dict(
        batch=batch,
        week=iso_week(batch),
        total=total,
        usable=len(usable),
        unusable=len(unusable),
        pending=len(pending),
        selection=sel,
        suggestions={k: v for k, v in sugg.items()},
        reason_tag_map=reasons,
    )
    log.append(entry)
    json.dump(log, open(RD.SELECTION_LOG, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return entry, log


def aggregate_patterns(log):
    tag_counter = collections.Counter()
    pid_discard = collections.Counter()   # 被反复弃用的题（按 id）
    week_batches = collections.defaultdict(list)
    total_gen = 0
    total_ret = 0
    for e in log:
        week_batches[e["week"]].append(e)
        total_gen += e["total"]
        total_ret += e["usable"]
        for pid, st in e["selection"].items():
            if st == "unusable":
                pid_discard[pid] += 1
        for r, pids in e.get("reason_tag_map", {}).items():
            tag_counter[r] += len(pids)
    # 周度聚合
    weeks = []
    for wk in sorted(week_batches.keys()):
        batches = week_batches[wk]
        g = sum(b["total"] for b in batches)
        r = sum(b["usable"] for b in batches)
        weeks.append(dict(week=wk, generated=g, retained=r,
                          rate=round(r / g, 4) if g else 0.0,
                          batches=len(batches)))
    patterns = dict(
        updated_at=RD.now_iso(),
        total_generated=total_gen,
        total_retained=total_ret,
        overall_rate=round(total_ret / total_gen, 4) if total_gen else 0.0,
        top_reasons=tag_counter.most_common(),
        most_discarded_problems=pid_discard.most_common(10),
        weekly=weeks,
    )
    json.dump(patterns, open(RD.USER_PATTERNS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return patterns


def make_self_opt_proposals(patterns, threshold_ratio=0.34):
    """把高频弃用原因翻译成「可注入 SKILL.md 的约束建议」。仅建议，不执行。"""
    proposals = []
    total_unusable = sum(c for _, c in patterns["top_reasons"]) or 1
    for reason, cnt in patterns["top_reasons"]:
        if cnt / total_unusable >= threshold_ratio or cnt >= 3:
            if reason == "情境不真实":
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="在生成约束中加入：「情境须贴近真实生产生活，避免虚构/理想化背景；改编说明须说明情境来源合理性」。",
                    target="SKILL.md · 第 6 节一致性约束 / 第 8 节范围"))
            elif reason == "与母题太像":
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="强化「非纯数字替换 + ≥2 类结构性改动」的硬性校验（已在 12.6/12.7），并在自检清单显式断言「改编说明列出 ≥2 类结构性改动」。",
                    target="SKILL.md · 第 12 节重命制语义硬化 / 第 9 节验收"))
            elif reason == "难度过高":
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="在难度锚定环节增加「计算复杂度」上限提示；若连续被标难度过高，下次生成主动下调 1 个子维度但保持星级≤母题+0。",
                    target="SKILL.md · 第 6 节难度一致性"))
            elif reason == "缺图示":
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="重申配图强制（第 8 节第 7 条）：凡含坐标/几何/装置/统计条件的题必须出图，自检清单加「离图无法表述的题均已配图」。",
                    target="SKILL.md · 第 8 节配图强制"))
            elif reason == "计算超纲":
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="强化超纲黑名单（第 6 节）：禁止出现积分/微分方程解析解/双曲函数/大学定义式，自检加「答案无超纲符号」。",
                    target="SKILL.md · 第 6 节严禁超纲"))
            else:
                proposals.append(dict(
                    trigger=reason, count=cnt,
                    proposal="针对「%s」补充对应生成约束与自检项（具体由教研确认）。" % reason,
                    target="SKILL.md · 第 6 节一致性约束"))
    # 写建议文件（变更记录草案，可回退：保留历史提案）
    ensure_dir()
    history = []
    if os.path.exists(os.path.join(RD.SKILL_DATA_DIR, "self_opt_proposals.json")):
        try:
            history = json.load(open(os.path.join(RD.SKILL_DATA_DIR, "self_opt_proposals.json"),
                                     "r", encoding="utf-8"))
        except Exception:
            history = []
    record = dict(generated_at=RD.now_iso(),
                  overall_rate=patterns["overall_rate"],
                  proposals=proposals)
    history.append(record)
    json.dump(history, open(os.path.join(RD.SKILL_DATA_DIR, "self_opt_proposals.json"),
                            "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return proposals


def build_weekly_report(patterns, out_html):
    ensure_dir()
    weeks = patterns["weekly"]
    rates = [w["rate"] * 100 for w in weeks]
    labels = [w["week"] for w in weeks]
    chart_b64 = None
    if len(weeks) >= 1:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(7, 3.2))
            ax.plot(range(len(labels)), rates, marker="o", color="#2563eb", linewidth=2)
            ax.axhline(90, color="#1a7f37", linestyle="--", linewidth=1, label="90% 目标")
            ax.axhline(100, color="#16a34a", linestyle=":", linewidth=1, label="100% 目标")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
            ax.set_ylabel("可用率 %")
            ax.set_ylim(0, 105)
            ax.set_title("周度可用率趋势", fontsize=11)
            ax.legend(fontsize=8)
            ax.grid(alpha=0.3)
            import io, base64
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
            plt.close(fig)
            chart_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            chart_b64 = None
            sys.stderr.write("趋势图生成失败: %s\n" % e)

    reasons_rows = "".join(
        "<tr><td>%s</td><td>%d</td></tr>" % (RD.esc(r), c) for r, c in patterns["top_reasons"]) or "<tr><td colspan=2>暂无</td></tr>"
    weekly_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%d</td><td>%.1f%%</td></tr>" % (w["week"], w["generated"], w["retained"], w["rate"] * 100)
        for w in weeks) or "<tr><td colspan=4>暂无</td></tr>"

    chart_html = '<img src="%s" style="max-width:100%%">' % chart_b64 if chart_b64 else '<p style="color:#888">趋势图生成失败</p>'

    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>周度可用率周报</title><style>body{font-family:-apple-system,"PingFang SC",sans-serif;margin:24px;color:#1f2329}
h1{font-size:20px}h2{font-size:16px;margin-top:24px}table{border-collapse:collapse;width:100%}
th,td{border:1px solid #e3e6ea;padding:6px 8px;text-align:left;font-size:13px}
th{background:#f0f3f7}.big{font-size:28px;font-weight:700;color:#2563eb}</style></head><body>
<h1>原创题评审 · 周度可用率周报</h1>
<p>生成时间：__ISO__</p>
<div class="big">总体可用率 __RATE__%</div>
<p>累计生成 __GEN__ 题，留用 __RET__ 题。</p>
<h2>周度趋势</h2>__CHART__
<h2>各周明细</h2>
<table><tr><th>周</th><th>生成</th><th>留用</th><th>可用率</th></tr>__WEEKS__</table>
<h2>Top 弃用原因 / 建议标签</h2>
<table><tr><th>原因/标签</th><th>命中次数</th></tr>__REASONS__</table>
<p style="color:#6b7280;font-size:12px">北极星：可用率逐步逼近 90% → 100%。未达预期周，请优先注入对应 skill 约束（见 self_opt_proposals.json）。</p>
</body></html>"""
    html = (html
            .replace("__ISO__", RD.now_iso())
            .replace("__RATE__", "%.1f" % (patterns["overall_rate"] * 100))
            .replace("__GEN__", str(patterns["total_generated"]))
            .replace("__RET__", str(patterns["total_retained"]))
            .replace("__CHART__", chart_html)
            .replace("__WEEKS__", weekly_rows)
            .replace("__REASONS__", reasons_rows))
    open(out_html, "w", encoding="utf-8").write(html)
    return out_html


def main():
    ap = argparse.ArgumentParser(description="原创题评审·统计与画像")
    ap.add_argument("--feedback", required=True, help="selection_feedback.json")
    ap.add_argument("--report-out", default=None, help="周报 HTML 输出路径（默认 data 目录）")
    args = ap.parse_args()
    fb = json.load(open(args.feedback, "r", encoding="utf-8"))
    entry, log = append_selection_log(fb)
    patterns = aggregate_patterns(log)
    proposals = make_self_opt_proposals(patterns)
    out_html = args.report_out or os.path.join(
        RD.SKILL_DATA_DIR, "weekly_report_%s.html" % datetime.datetime.now().strftime("%Y%m%d"))
    report = build_weekly_report(patterns, out_html)
    print("OK 选题日志 ->", RD.SELECTION_LOG)
    print("OK 用户画像 ->", RD.USER_PATTERNS)
    print("OK 自优化建议 ->", os.path.join(RD.SKILL_DATA_DIR, "self_opt_proposals.json"))
    print("OK 周报 ->", report)
    print("本批：生成 %d，可用 %d，不可用 %d，待定 %d" % (
        entry["total"], entry["usable"], entry["unusable"], entry["pending"]))
    print("总体可用率 %.1f%%" % (patterns["overall_rate"] * 100))
    print("自优化建议数：", len(proposals))
    for p in proposals:
        print("  - [%s] %s" % (p["trigger"], p["proposal"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
