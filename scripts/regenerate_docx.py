# -*- coding: utf-8 -*-
"""
regenerate_docx.py — 原创题评审交互平台 · 筛选 / 定向重生成（需求 2.9.0 能力一、二）

消费：selection_feedback.json（HTML 导出）+ 原创题_*.json（build 产出 bundle）

两种模式：
  --mode filter   仅导出「可用」题 → 新 docx（题号重排、配图/解析/溯源/自检裁剪同步）
  --mode apply    按建议「定向重生成」：用 patch JSON（id→新 problem 字段）替换被建议题，
                  未建议题保持不动；校验被建议题 content_fp 变化（杜绝换汤不换药），
                  未建议题 content_fp 不变。产出新 docx。

patch JSON 结构（--patch）：
  { "p3": {"stem":"...","answer":"...","analysis":[...],"adaptation":"...","innovation":"...",
           "subquestions":[...],"difficulty_anchor":[...],"figure":"fig_st3.png"},
    "p5": {...} }

退出码：命中「重命制守卫违规」（被建议题指纹未变）时返回 2，便于 CI/人工拦截。
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_data as RD


def load_json(p):
    return json.load(open(p, "r", encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="原创题评审·筛选/定向重生成")
    ap.add_argument("--mode", required=True, choices=["filter", "apply"])
    ap.add_argument("--data", required=True, help="原创题_*.json（bundle）")
    ap.add_argument("--feedback", help="selection_feedback.json（filter 模式必填）")
    ap.add_argument("--patch", help="regenerate_patch.json（apply 模式必填，id→新字段）")
    ap.add_argument("--out", required=True, help="输出 docx 路径")
    ap.add_argument("--figures-dir", default=None,
                    help="配图目录（默认取 bundle.figures_dir 或 data 同目录）")
    args = ap.parse_args()

    bundle = load_json(args.data)
    problems = bundle.get("problems", [])
    fidir = args.figures_dir or bundle.get("figures_dir", ".")
    if not os.path.isabs(fidir):
        fidir = os.path.join(os.path.dirname(os.path.abspath(args.data)), fidir)

    by_id = {p["id"]: p for p in problems}

    if args.mode == "filter":
        if not args.feedback:
            print("ERROR filter 模式需要 --feedback", file=sys.stderr)
            return 1
        fb = load_json(args.feedback)
        sel = fb.get("selection", {})
        keep = [p for p in problems if sel.get(p["id"], "usable") == "usable"]
        if not keep:
            print("ERROR 没有任何可用题，禁止产出空文档。", file=sys.stderr)
            return 1
        new_fp = RD.content_fp_of(keep)
        out = RD.problems_to_docx(
            keep, args.out,
            title="原创题（精选 %d 道·可用题）" % len(keep),
            sources=bundle.get("sources"), content_fp=new_fp, figures_dir=fidir)
        print("OK 精选 docx ->", out)
        print("可用题数:", len(keep), "content_fp:", new_fp)
        return 0

    if args.mode == "apply":
        if not args.patch:
            print("ERROR apply 模式需要 --patch", file=sys.stderr)
            return 1
        patch = load_json(args.patch)
        targets = set(patch.keys())
        # 旧指纹（基于原始 problems）
        old_fp_map = {p["id"]: RD.content_fp_of([p]) for p in problems}

        new_problems = []
        for p in problems:
            pid = p["id"]
            if pid in targets:
                merged = dict(p)
                merged.update(patch[pid])
                # 必须带 id，避免错位
                merged["id"] = pid
                new_problems.append(merged)
            else:
                new_problems.append(p)

        # 守卫：被建议题指纹必须变化；未建议题指纹必须不变
        violations = []
        for p in new_problems:
            pid = p["id"]
            if pid in targets:
                if RD.content_fp_of([p]) == old_fp_map[pid]:
                    violations.append(pid)
            else:
                if RD.content_fp_of([p]) != old_fp_map[pid]:
                    violations.append("UNCHANGED-BUT-DIFFERED:" + pid)
        if violations:
            print("GUARD FAIL 重命制守卫违规：", violations, file=sys.stderr)
            print("被建议题必须与旧版有可辨识差异（content_fp 变化）；未建议题必须保持不变。",
                  file=sys.stderr)
            return 2

        new_fp = RD.content_fp_of(new_problems)
        out = RD.problems_to_docx(
            new_problems, args.out,
            title="原创题（定向重生成 %d 题）" % len(targets),
            sources=bundle.get("sources"), content_fp=new_fp, figures_dir=fidir)
        print("OK 定向重生成 docx ->", out)
        print("重生成题:", sorted(targets), "content_fp:", new_fp)
        print("未变题指纹:", {k: old_fp_map[k] for k in by_id if k not in targets})
        return 0


if __name__ == "__main__":
    sys.exit(main())
