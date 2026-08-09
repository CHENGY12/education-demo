#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扩题自检。检查每个 questions.jsonl 的结构、难度、池、选项、答案与公式。

    python3 validate.py                          检查全部
    python3 validate.py cn-nanjing-g11-2026/物理   检查指定范围

以下均为要求，不满足即 FAIL：
  结构：字段齐全、difficulty∈{low,mid,high}、pool∈{display,exam}、4 选项 A-D、answer 有效；
  配比：每档 ≥5 题，其中 display ≥3、exam ≥2；
  公式：定界符成对闭合；只用 $…$/$$…$$（不用 \\( \\[）；化学式/单位包 \\mathrm；不用 \\ce；同题选项句号统一。
本脚本为静态检查；入库时另有 KaTeX 真渲染校验。
"""
import sys, os, json, re, glob

DIFFS = ("low", "mid", "high")
POOLS = ("display", "exam")
REQ = ("id", "nodeId", "subject", "difficulty", "pool", "prompt", "options", "answer", "explanation")
DELIMS = [("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")]
CHEM = re.compile(r"\b(NO_?[23]?|N_?2|O_?2|CO_?2?|CH_?4|SO_?[23]|NH_?3|H_?2O|HCl|NaOH|Cl_?2|"
                  r"KMnO_?4|NaHCO_?3|CaCO_?3|Fe|Zn|Mg|Cu|Al|Ca|Na|Ag|Ba)\b")
CJK = re.compile(r"[一-鿿]")
CLAUSE = re.compile(r"[，、；,;]")
END_PERIOD = re.compile(r"[。！？.!?]\s*$")

def _math_spans(s):
    spans, i, n = [], 0, len(s)
    while i < n:
        hit = False
        for l, r in DELIMS:
            if s.startswith(l, i):
                e = s.find(r, i + len(l))
                if e >= 0:
                    spans.append(s[i + len(l):e]); i = e + len(r); hit = True; break
        if not hit: i += 1
    return spans

def _delim_unclosed(s):
    """模拟渲染闸门：左到右匹配定界符，非公式段里残留 $ / \\( / \\[ = 未闭合。"""
    i, n, buf = 0, len(s), ""
    while i < n:
        hit = False
        for l, r in DELIMS:
            if s.startswith(l, i):
                e = s.find(r, i + len(l))
                if e >= 0:
                    if re.search(r"\$|\\\(|\\\[", buf): return True
                    buf = ""; i = e + len(r); hit = True; break
        if not hit: buf += s[i]; i += 1
    return bool(re.search(r"\$|\\\(|\\\[", buf))

def formula_errors(s, *, check_chem=True):
    e = []
    if _delim_unclosed(s): e.append("定界符未闭合（$ 或 \\( \\[ 缺配对）")
    if "\\(" in s or "\\[" in s: e.append("用了 \\( 或 \\[，须改用 $…$ / $$…$$")
    if "\\ce" in s: e.append("含 \\ce（平台不支持 mhchem，会渲染失败；改用 \\mathrm）")
    if check_chem:
        for sp in _math_spans(s):
            if CHEM.search(re.sub(r"\\(?:mathrm|text|ce)\s*\{[^{}]*\}", "", sp)):
                e.append("化学式/元素须包 \\mathrm（否则显示成斜体）"); break
    return e

def _looks_sentence(t):
    s = t.strip()
    if not CJK.search(s): return False
    if CLAUSE.search(s): return True
    return len(CJK.findall(s)) >= 10

def option_period_error(opts):
    texts = [str(o.get("text", "")) for o in opts if isinstance(o, dict) and str(o.get("text", "")).strip()]
    if len(texts) < 2: return None
    sents = [t for t in texts if _looks_sentence(t)]
    if sents and len(sents) == len(texts) and any(not END_PERIOD.search(t.strip()) for t in texts):
        return "选项均为句子，须统一以句号结尾（部分缺句号）"
    return None

def check_question(q, ln):
    e = []
    for k in REQ:
        if k not in q or (k in ("prompt", "explanation", "answer") and not str(q.get(k, "")).strip()):
            e.append(f"缺/空字段 {k}")
    d = q.get("difficulty")
    if not d: e.append("difficulty 未填（判定 low/mid/high）")
    elif d not in DIFFS: e.append(f"difficulty 非法：{d!r}")
    p = q.get("pool")
    if not p: e.append("pool 未填（判定 display/exam）")
    elif p not in POOLS: e.append(f"pool 非法：{p!r}")
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) != 4:
        e.append("options 须为 4 项")
    else:
        ids = [o.get("id") for o in opts if isinstance(o, dict)]
        if ids != ["A", "B", "C", "D"]: e.append(f"选项 id 须为 A/B/C/D，现为 {ids}")
        if any(not str(o.get("text", "")).strip() for o in opts if isinstance(o, dict)):
            e.append("存在空选项")
        pe = option_period_error(opts)
        if pe: e.append(pe)
    if q.get("answer") not in ("A", "B", "C", "D"): e.append(f"answer 须为 A/B/C/D，现为 {q.get('answer')!r}")
    fields = [str(q.get("prompt", "")), str(q.get("explanation", ""))] + \
             [str(o.get("text", "")) for o in (opts if isinstance(opts, list) else []) if isinstance(o, dict)]
    # In physics, concatenations such as Mg and 2Mg conventionally mean M*g;
    # treating them as the chemical element magnesium is a false positive.
    check_chem = q.get("subject") != "物理"
    for fld in fields:
        e += formula_errors(fld, check_chem=check_chem)
    return [f"第 {ln} 行：{x}" for x in sorted(set(e))]

def check_node(path):
    errs, qs = [], []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try: q = json.loads(line)
            except Exception as ex:
                errs.append(f"第 {i} 行：JSON 解析失败（{str(ex)[:40]}）"); continue
            qs.append(q); errs += check_question(q, i)
    if not qs:
        return "EMPTY", []
    for d in DIFFS:
        dq = [q for q in qs if q.get("difficulty") == d]
        disp = sum(1 for q in dq if q.get("pool") == "display")
        exam = sum(1 for q in dq if q.get("pool") == "exam")
        if len(dq) < 5: errs.append(f"{d}：{len(dq)} 题，须 ≥5")
        if disp < 3: errs.append(f"{d}：display {disp} 题，须 ≥3")
        if exam < 2: errs.append(f"{d}：exam {exam} 题，须 ≥2")
    return ("PASS" if not errs else "FAIL"), errs

def main():
    root = os.path.dirname(os.path.abspath(__file__))
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    base = target if os.path.isabs(target) else os.path.join(root, target)
    files = sorted(glob.glob(os.path.join(base, "**", "questions.jsonl"), recursive=True))
    if not files:
        print("未找到 questions.jsonl。请在包根目录运行，或指定正确路径。"); sys.exit(2)
    npass = nfail = nempty = 0
    for p in files:
        node = os.path.relpath(os.path.dirname(p), root)
        st, errs = check_node(p)
        if st == "EMPTY":
            nempty += 1; continue
        if st == "PASS":
            npass += 1; print(f"[PASS] {node}")
        else:
            nfail += 1; print(f"[FAIL] {node}")
            for x in errs[:12]: print(f"       - {x}")
            if len(errs) > 12: print(f"       …… 另有 {len(errs) - 12} 条")
    print("-" * 48)
    print(f"PASS {npass} · FAIL {nfail} · 空 {nempty} · 共 {len(files)} 个节点")
    sys.exit(1 if nfail else 0)

if __name__ == "__main__":
    main()
