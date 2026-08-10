#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题库静态校验与干净交付打包。

常用命令：

    python3 validate.py
    python3 validate.py cn-nanjing-g11-2026/物理
    python3 validate.py cn-nanjing-g11-2026/物理 --delivery
    python3 validate.py cn-nanjing-g11-2026/物理 \
        --prepare-delivery /absolute/path/to/delivery

普通模式检查题目结构、配额、公式和解析质量。``--delivery`` 还检查
``answer_review.jsonl`` 与最终题面/答案/解析的一致性，并拒绝交付目录中的
``answer_final.jsonl`` 和三路原始答案文件。``--prepare-delivery`` 从工作题库
生成一个全新的干净目录；它只收录 Teacher 严格通过、manager 已 final 且所有哈希一致的题目，
不会删除或改写源题库。

本模块的 ``check_question(question, line_no)`` 是 manager 写回前使用的稳定
逐题契约。请保持该函数无外部依赖、无文件写入。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Iterator, Sequence


ROOT = Path(__file__).resolve().parent
DIFFS = ("low", "mid", "high")
POOLS = ("display", "exam")
OPTION_IDS = ("A", "B", "C", "D")
REQ = (
    "id",
    "nodeId",
    "subject",
    "difficulty",
    "pool",
    "prompt",
    "options",
    "answer",
    "explanation",
)
FORBIDDEN_DELIVERY_FILES = (
    "answer_final.jsonl",
    "answers1.jsonl",
    "answers2.jsonl",
    "answers3.jsonl",
)
OPTIONAL_SOURCE_ASSETS = ("reference.md", "question.png", "question.jpg", "question.jpeg")

# Newlines are allowed in stems/solutions; every other C0 control is rejected.
# This deliberately catches the tab/carriage-return damage produced by JSON
# strings such as ``\times`` or ``\rm`` when a backslash is mishandled.
CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
LATEX_OUTSIDE_MATH = re.compile(r"\\(?:[A-Za-z]+|,)")
NAKED_EQUATION = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9]*|\d+(?:\.\d+)?)\s*"
    r"(?:=|<=|>=|<|>|≤|≥)"
)
NAKED_VALUE_WITH_UNIT = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*"
    r"(?:km/h|m/s|cm/s|mm/s|rad/s|kg/m\^?3|kg|mg|km|cm|mm|nm|m|s|ms|"
    r"Hz|kHz|MHz|N|J|W|Pa|kPa|MPa|T|C|V|A|mol|L|dB)\b",
    re.IGNORECASE,
)
OPTION_LINE = re.compile(r"(?m)^\s*(?:[-*]\s*)?[A-DＡ-Ｄ]\s*[.．、:：)]\s*\S")
MARKDOWN_OPTION_ROW = re.compile(r"(?m)^\s*\|\s*[A-DＡ-Ｄ]\s*\|")
SINGLE_LETTER_OPTION = re.compile(r"^[A-DＡ-Ｄ]$", re.IGNORECASE)
CJK = re.compile(r"[\u4e00-\u9fff]")
CLAUSE = re.compile(r"[，、；,;]")
END_PERIOD = re.compile(r"[。！？.!?]\s*$")

REPAIR_CHATTER = (
    "更正如下",
    "规范写为",
    "实际应为",
    "进一步写成标准形式",
    "重新生成",
    "生成失败",
)
INTERNAL_PROCESS_TERMS = (
    "独立解：",
    "独立解:",
    "独立核验：",
    "独立核验:",
    "候选解答",
    "候选答案",
    "解题 Agent",
    "Teacher 核验",
)

CHEM = re.compile(
    r"\b(NO_?[23]?|N_?2|O_?2|CO_?2?|CH_?4|SO_?[23]|NH_?3|H_?2O|HCl|NaOH|"
    r"Cl_?2|KMnO_?4|NaHCO_?3|CaCO_?3|Fe|Zn|Mg|Cu|Al|Ca|Na|Ag|Ba)\b"
)
UNIT_TOKEN = (
    r"(?:rad/s|km/h|m/s|cm/s|mm/s|kg/m\^?3|Hz|kHz|MHz|kg|mg|km|cm|mm|"
    r"nm|mol|Pa|kPa|MPa|dB|N|J|W|T|C|V|A|L|m|s)"
)
COMPOUND_UNIT_TOKEN = (
    r"(?:rad/s|km/h|m/s|cm/s|mm/s|kg/m\^?3|Hz|kHz|MHz|kg|mg|km|cm|mm|"
    r"nm|mol|Pa|kPa|MPa|dB|N|J|W|T|C|V|A|L)"
)
MATH_NAKED_UNIT = re.compile(
    rf"(?:\\,\s*({UNIT_TOKEN})|(?<=[0-9}})])\s+({UNIT_TOKEN})|"
    rf"(?<=[0-9}})])({COMPOUND_UNIT_TOKEN}))(?![A-Za-z])"
)
MALFORMED_SUPERSCRIPT = re.compile(r"\^(?:\(|[-+]\s*[A-Za-z0-9]|[A-Za-z0-9]{2,})")
MALFORMED_SUBSCRIPT = re.compile(r"_(?:\(|[-+]\s*[A-Za-z0-9]|[A-Za-z0-9]{2,})")
EMPTY_EQUATION = re.compile(r"^\s*[^=\n]{1,80}=\s*$")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _walk_strings(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")


def _split_math(value: str) -> tuple[list[str], str, list[str]]:
    """Return math bodies, concatenated non-math text, and delimiter errors."""
    spans: list[str] = []
    outside: list[str] = []
    errors: list[str] = []
    cursor = 0
    plain_start = 0
    length = len(value)
    while cursor < length:
        delimiter = ""
        if value.startswith("$$", cursor):
            delimiter = "$$"
        elif value[cursor] == "$":
            delimiter = "$"
        if not delimiter:
            cursor += 1
            continue
        outside.append(value[plain_start:cursor])
        end = value.find(delimiter, cursor + len(delimiter))
        if end < 0:
            errors.append(f"{delimiter} 数学定界符未闭合")
            outside.append(value[cursor:])
            plain_start = length
            cursor = length
            break
        spans.append(value[cursor + len(delimiter):end])
        cursor = end + len(delimiter)
        plain_start = cursor
    outside.append(value[plain_start:])
    return spans, " ".join(outside), errors


def _without_text_commands(math_body: str) -> str:
    previous = None
    current = math_body
    pattern = re.compile(r"\\(?:mathrm|text)\s*\{[^{}]*\}")
    while previous != current:
        previous, current = current, pattern.sub("", current)
    return current


def formula_errors(value: str, *, check_chem: bool = True) -> list[str]:
    errors: list[str] = []
    if "\\(" in value or "\\)" in value or "\\[" in value or "\\]" in value:
        errors.append(r"用了 \(、\) 或 \[、\]，须改用 $…$ / $$…$$")
    spans, outside, delimiter_errors = _split_math(value)
    errors.extend(delimiter_errors)
    if LATEX_OUTSIDE_MATH.search(outside):
        errors.append(r"数学定界符外存在 LaTeX 命令或 \,")
    if "^" in outside or "_" in outside:
        errors.append("数学定界符外存在上标 ^ 或下标 _")
    if NAKED_EQUATION.search(outside):
        errors.append("变量或表达式未置于 $...$ 数学定界符内")
    if NAKED_VALUE_WITH_UNIT.search(outside):
        errors.append("带单位数值未置于 $...$ 数学定界符内")
    for span in spans:
        if re.search(r"(?<!\\)%", span):
            errors.append(r"数学环境中的 % 未写成 \%")
        if MALFORMED_SUPERSCRIPT.search(span):
            errors.append("多字符、带符号或括号指数须写成 ^{...}")
        if MALFORMED_SUBSCRIPT.search(span):
            errors.append("多字符、带符号或括号下标须写成 _{...}")
        stripped = _without_text_commands(span)
        if MATH_NAKED_UNIT.search(stripped):
            errors.append(r"数学环境中的单位须用 \mathrm{...}")
        if check_chem and CHEM.search(stripped):
            errors.append(r"化学式/元素须包 \mathrm{...}（否则显示成斜体）")
    if "\\ce" in value:
        errors.append(r"含 \ce（平台不支持 mhchem；改用 \mathrm）")
    return sorted(set(errors))


def _looks_sentence(text: str) -> bool:
    stripped = text.strip()
    if not CJK.search(stripped):
        return False
    if CLAUSE.search(stripped):
        return True
    return len(CJK.findall(stripped)) >= 10


def option_period_error(options: Sequence[dict[str, Any]]) -> str | None:
    texts = [str(option.get("text", "")) for option in options if str(option.get("text", "")).strip()]
    if len(texts) < 2:
        return None
    sentences = [text for text in texts if _looks_sentence(text)]
    if sentences and len(sentences) == len(texts) and any(
        not END_PERIOD.search(text.strip()) for text in texts
    ):
        return "选项均为句子，须统一以句号结尾（部分缺句号）"
    return None


def _content_fields(question: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [
        ("prompt", str(question.get("prompt", ""))),
        ("explanation", str(question.get("explanation", ""))),
    ]
    for extra in ("hint", "skillTarget"):
        if question.get(extra):
            fields.append((extra, str(question.get(extra, ""))))
    options = question.get("options")
    if isinstance(options, list):
        fields.extend(
            (f"options[{index}].text", str(option.get("text", "")))
            for index, option in enumerate(options)
            if isinstance(option, dict)
        )
    return fields


def check_question(question: dict[str, Any], line_no: int) -> list[str]:
    """Return deterministic per-question violations, prefixed with line number."""
    errors: list[str] = []
    if not isinstance(question, dict):
        return [f"第 {line_no} 行：题目必须是 JSON object"]
    for key in REQ:
        if key not in question or (
            key in ("prompt", "explanation", "answer")
            and not str(question.get(key, "")).strip()
        ):
            errors.append(f"缺/空字段 {key}")
    for path, value in _walk_strings(question):
        if CONTROL_CHARS.search(value):
            errors.append(f"{path} 含控制字符（疑似 JSON/LaTeX 反斜杠转义错误）")

    difficulty = question.get("difficulty")
    if not difficulty:
        errors.append("difficulty 未填（判定 low/mid/high）")
    elif difficulty not in DIFFS:
        errors.append(f"difficulty 非法：{difficulty!r}")
    pool = question.get("pool")
    if not pool:
        errors.append("pool 未填（判定 display/exam）")
    elif pool not in POOLS:
        errors.append(f"pool 非法：{pool!r}")

    options = question.get("options")
    valid_options: list[dict[str, Any]] = []
    if not isinstance(options, list) or len(options) != 4:
        errors.append("options 须为 4 项")
    else:
        if any(not isinstance(option, dict) for option in options):
            errors.append("每个 option 必须是 object")
        valid_options = [option for option in options if isinstance(option, dict)]
        ids = [option.get("id") for option in valid_options]
        if ids != list(OPTION_IDS):
            errors.append(f"选项 id 须为 A/B/C/D，现为 {ids}")
        texts = [str(option.get("text", "")).strip() for option in valid_options]
        if len(texts) != 4 or any(not text for text in texts):
            errors.append("存在空选项")
        if any(SINGLE_LETTER_OPTION.fullmatch(text) for text in texts if text):
            errors.append("options[].text 不得只是 A/B/C/D 单个字母")
        if len(texts) == 4 and len(set(texts)) != 4:
            errors.append("四个选项文本必须互不相同")
        period_error = option_period_error(valid_options)
        if period_error:
            errors.append(period_error)

    answer = question.get("answer")
    if answer not in OPTION_IDS:
        errors.append(f"answer 须为 A/B/C/D，现为 {answer!r}")

    prompt = str(question.get("prompt", ""))
    if OPTION_LINE.search(prompt):
        errors.append("题干含 A/B/C/D 选项行；选项只能放在 options[].text")
    if MARKDOWN_OPTION_ROW.search(prompt) or ("|---" in prompt and "|" in prompt):
        errors.append("题干不得用 Markdown 表格承载选项")
    repeated = [
        text for text in (str(option.get("text", "")).strip() for option in valid_options)
        if len(text) >= 2 and text in prompt
    ]
    if len(repeated) >= 2:
        errors.append("题干重复包含多个 options[].text 的选项内容")

    explanation = str(question.get("explanation", ""))
    for phrase in (*REPAIR_CHATTER, *INTERNAL_PROCESS_TERMS):
        if phrase in explanation:
            errors.append(f"explanation 含生成/审校内部残留用语：{phrase}")
    explanation_math, _, _ = _split_math(explanation)
    if any(not span.strip() or EMPTY_EQUATION.fullmatch(span) for span in explanation_math):
        errors.append("explanation 含空公式或等号右侧为空的失败残留")

    check_chem = question.get("subject") != "物理"
    for field_name, value in _content_fields(question):
        for violation in formula_errors(value, check_chem=check_chem):
            errors.append(f"{field_name}：{violation}")
    return [f"第 {line_no} 行：{error}" for error in sorted(set(errors))]


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.is_file():
        return rows, [f"缺少文件 {path.name}"]
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"第 {line_no} 行：JSON 解析失败（{str(exc)[:120]}）")
                continue
            if not isinstance(value, dict):
                errors.append(f"第 {line_no} 行：顶层必须是 object")
                continue
            rows.append(value)
    return rows, errors


def _quota_errors(questions: Sequence[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for difficulty in DIFFS:
        subset = [q for q in questions if q.get("difficulty") == difficulty]
        display = sum(1 for q in subset if q.get("pool") == "display")
        exam = sum(1 for q in subset if q.get("pool") == "exam")
        if len(subset) < 5:
            errors.append(f"{difficulty}：{len(subset)} 题，须 ≥5")
        if display < 3:
            errors.append(f"{difficulty}：display {display} 题，须 ≥3")
        if exam < 2:
            errors.append(f"{difficulty}：exam {exam} 题，须 ≥2")
    return errors


def check_node_detailed(path: Path) -> tuple[str, list[str], list[dict[str, Any]]]:
    questions, errors = _read_jsonl(path)
    seen: set[str] = set()
    for line_no, question in enumerate(questions, 1):
        errors.extend(check_question(question, line_no))
        qid = str(question.get("id", ""))
        if qid in seen:
            errors.append(f"第 {line_no} 行：重复题目 id {qid!r}")
        seen.add(qid)
    if not questions:
        return ("FAIL" if errors else "EMPTY"), errors, questions
    errors.extend(_quota_errors(questions))
    return ("PASS" if not errors else "FAIL"), errors, questions


def check_node(path: os.PathLike[str] | str) -> tuple[str, list[str]]:
    """Backward-compatible node validator used by existing scripts."""
    status, errors, _ = check_node_detailed(Path(path))
    return status, errors


def question_key(question_file: Path, bank_root: Path, qid: str) -> str:
    relative = question_file.resolve().relative_to(bank_root.resolve()).as_posix()
    return sha256_text(f"{relative}\0{qid}")


def question_snapshot_sha256(question: dict[str, Any], key: str) -> str:
    options = question.get("options", [])
    snapshot = {
        "id": key,
        "display_id": str(question.get("id", "")),
        "subject": str(question.get("subject", "")),
        "prompt": str(question.get("prompt", "")),
        "options": [
            {"id": option.get("id", ""), "text": option.get("text", "")}
            for option in options
            if isinstance(option, dict)
        ],
        "difficulty": str(question.get("difficulty", "")),
        "question_type": "multiple_choice" if options else "open_response",
    }
    return sha256_text(compact_json(snapshot))


def _review_errors(
    question: dict[str, Any],
    review: dict[str, Any],
    *,
    expected_key: str,
) -> list[str]:
    errors: list[str] = []
    qid = str(question.get("id", ""))
    if str(review.get("id", "")) != qid:
        errors.append("review id 与题目 id 不一致")
    if str(review.get("question_key", "")) != expected_key:
        errors.append("answer_review.question_key 与题目路径/id 不一致")
    if review.get("teacher_verdict") != "pass":
        errors.append(f"teacher_verdict 不是 pass（{review.get('teacher_verdict')!r}）")
    if review.get("correct") is not True:
        errors.append("answer_review.correct 不是 true")
    if review.get("answer_consistent") is not True:
        errors.append("answer_review.answer_consistent 不是 true")
    if review.get("manager_status") != "final":
        errors.append("answer_review.manager_status 不是 final")
    if review.get("auto_promote") is not True:
        errors.append("answer_review.auto_promote 不是 true；缺少 manager 严格门槛证书")
    answer = str(question.get("answer", ""))
    teacher_answer = str(review.get("teacher_answer", ""))
    if teacher_answer != answer:
        errors.append(
            f"questions.answer={answer!r} 与 answer_review.teacher_answer={teacher_answer!r} 不一致"
        )
    student_answers = review.get("student_answers")
    if not isinstance(student_answers, dict) or set(student_answers) != {
        "solver1",
        "solver2",
        "solver3",
    }:
        errors.append("student_answers 必须恰含 solver1/solver2/solver3")
    elif any(str(student_answers[agent]) != teacher_answer for agent in student_answers):
        errors.append("pass 记录中三名 solver 的答案未全部等于 teacher_answer")
    expected_snapshot = question_snapshot_sha256(question, expected_key)
    actual_snapshot = str(review.get("question_snapshot_sha256", ""))
    if not actual_snapshot:
        errors.append("answer_review 缺 question_snapshot_sha256；请用新版 manager 重新 export")
    elif actual_snapshot != expected_snapshot:
        errors.append("复核所用题面快照与当前 questions.jsonl 不一致")
    expected_solution = sha256_text(str(question.get("explanation", "")))
    actual_solution = str(review.get("teacher_solution_sha256", ""))
    if not actual_solution:
        errors.append("answer_review 缺 teacher_solution_sha256；请用新版 manager 重新 export")
    elif actual_solution != expected_solution:
        errors.append("questions.explanation 与 Teacher 最终解法哈希不一致")
    return errors


def check_delivery_node(
    question_file: Path,
    *,
    bank_root: Path,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[str] = []
    questions, question_errors = _read_jsonl(question_file)
    errors.extend(question_errors)
    review_file = question_file.parent / "answer_review.jsonl"
    reviews, review_errors = _read_jsonl(review_file)
    errors.extend(f"answer_review.jsonl：{error}" for error in review_errors)

    question_by_id: dict[str, dict[str, Any]] = {}
    for question in questions:
        qid = str(question.get("id", ""))
        if qid in question_by_id:
            errors.append(f"questions.jsonl 重复 id {qid!r}")
        question_by_id[qid] = question
    reviews_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        reviews_by_id[str(review.get("id", ""))].append(review)
    for qid, question in question_by_id.items():
        matching = reviews_by_id.get(qid, [])
        if len(matching) != 1:
            errors.append(f"{qid}：answer_review 记录应恰为 1 条，现为 {len(matching)}")
            continue
        key = question_key(question_file, bank_root, qid)
        errors.extend(f"{qid}：{error}" for error in _review_errors(question, matching[0], expected_key=key))
    for qid in sorted(set(reviews_by_id) - set(question_by_id)):
        errors.append(f"answer_review 含 questions.jsonl 中不存在的 id {qid!r}")
    for forbidden in FORBIDDEN_DELIVERY_FILES:
        if (question_file.parent / forbidden).exists():
            errors.append(f"交付目录不得包含 {forbidden}")
    return errors, questions, reviews


def _distribution_findings(
    questions: Sequence[dict[str, Any]],
    *,
    minimum: float,
    maximum: float,
    minimum_count: int,
) -> list[str]:
    findings: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question in questions:
        groups[str(question.get("subject") or "未标科目")].append(question)
    for subject, rows in sorted(groups.items()):
        if len(rows) < minimum_count:
            findings.append(
                f"答案分布[{subject}]：样本 {len(rows)} < {minimum_count}，仅记录不作比例门槛"
            )
            continue
        counts = Counter(str(row.get("answer", "")) for row in rows)
        shares = {option: counts[option] / len(rows) for option in OPTION_IDS}
        bad = [option for option, share in shares.items() if share < minimum or share > maximum]
        detail = "、".join(
            f"{option}={counts[option]}({shares[option]:.1%})" for option in OPTION_IDS
        )
        if bad:
            findings.append(
                f"答案分布[{subject}] 超出 [{minimum:.0%}, {maximum:.0%}]：{detail}；"
                "应在出题阶段调整选项顺序后重新做 3+1 核验，禁止只改 answer 字段"
            )
    return findings


def _find_question_files(base: Path) -> list[Path]:
    if base.is_file():
        return [base] if base.name == "questions.jsonl" else []
    return sorted(
        path for path in base.rglob("questions.jsonl")
        if ".qb-review" not in path.parts
    )


def _print_results(
    results: Sequence[tuple[Path, str, Sequence[str]]],
    *,
    display_root: Path,
    warnings: Sequence[str],
) -> tuple[int, int, int]:
    passed = failed = empty = 0
    for path, status, errors in results:
        try:
            node = path.parent.resolve().relative_to(display_root.resolve()).as_posix() or "."
        except ValueError:
            node = str(path.parent)
        if status == "EMPTY":
            empty += 1
            print(f"[EMPTY] {node}")
        elif status == "PASS":
            passed += 1
            print(f"[PASS] {node}")
        else:
            failed += 1
            print(f"[FAIL] {node}")
            for error in list(errors)[:20]:
                print(f"       - {error}")
            if len(errors) > 20:
                print(f"       …… 另有 {len(errors) - 20} 条")
    for warning in warnings:
        print(f"[WARN] {warning}")
    print("-" * 64)
    print(f"PASS {passed} · FAIL {failed} · 空 {empty} · 共 {len(results)} 个节点")
    return passed, failed, empty


def validate_scope(
    base: Path,
    *,
    bank_root: Path,
    delivery: bool,
    distribution_policy: str,
    answer_share_min: float,
    answer_share_max: float,
    distribution_min_count: int,
) -> dict[str, Any]:
    files = _find_question_files(base)
    if not files:
        raise ValueError("未找到 questions.jsonl。请在包根目录运行，或指定正确路径。")
    results: list[tuple[Path, str, list[str]]] = []
    all_questions: list[dict[str, Any]] = []
    for question_file in files:
        status, errors, questions = check_node_detailed(question_file)
        if delivery:
            delivery_errors, _, _ = check_delivery_node(question_file, bank_root=bank_root)
            errors.extend(delivery_errors)
            status = "PASS" if questions and not errors else ("EMPTY" if not questions else "FAIL")
        results.append((question_file, status, errors))
        all_questions.extend(questions)
    findings = _distribution_findings(
        all_questions,
        minimum=answer_share_min,
        maximum=answer_share_max,
        minimum_count=distribution_min_count,
    )
    warnings: list[str] = []
    if distribution_policy == "error" and findings:
        # Small-sample notes remain warnings; actual out-of-range findings are errors.
        hard = [item for item in findings if "超出" in item]
        warnings.extend(item for item in findings if item not in hard)
        if hard:
            path, status, errors = results[0]
            results[0] = (path, "FAIL", [*errors, *hard])
    elif distribution_policy == "warn":
        warnings.extend(findings)
    passed, failed, empty = _print_results(results, display_root=bank_root, warnings=warnings)
    return {
        "ok": failed == 0,
        "pass": passed,
        "fail": failed,
        "empty": empty,
        "nodes": len(files),
        "questions": len(all_questions),
        "warnings": warnings,
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(compact_json(row) + "\n" for row in rows)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def prepare_delivery(
    base: Path,
    output: Path,
    *,
    bank_root: Path,
    include_source_assets: bool,
    answer_share_min: float,
    answer_share_max: float,
    distribution_min_count: int,
) -> dict[str, Any]:
    files = _find_question_files(base)
    if not files:
        raise ValueError("未找到 questions.jsonl，无法生成交付目录")
    output_resolved = output.resolve()
    if output_resolved == bank_root.resolve() or bank_root.resolve() in output_resolved.parents:
        raise ValueError("交付目录不得位于源题库内部，避免递归扫描或覆盖源文件")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"交付目录必须不存在或为空：{output}")

    selected_by_file: dict[Path, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    exclusions: list[str] = []
    integrity_errors: list[str] = []
    all_selected: list[dict[str, Any]] = []
    for question_file in files:
        questions, question_read_errors = _read_jsonl(question_file)
        reviews, review_read_errors = _read_jsonl(question_file.parent / "answer_review.jsonl")
        if question_read_errors:
            integrity_errors.extend(f"{question_file}: {error}" for error in question_read_errors)
            continue
        if review_read_errors:
            integrity_errors.extend(
                f"{question_file.parent / 'answer_review.jsonl'}: {error}" for error in review_read_errors
            )
            continue
        reviews_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for review in reviews:
            reviews_by_id[str(review.get("id", ""))].append(review)
        question_ids = {str(question.get("id", "")) for question in questions}
        for extra_id in sorted(set(reviews_by_id) - question_ids):
            integrity_errors.append(
                f"{question_file.parent}: answer_review 含不存在的题目 {extra_id!r}"
            )
        selected_questions: list[dict[str, Any]] = []
        selected_reviews: list[dict[str, Any]] = []
        for line_no, question in enumerate(questions, 1):
            qid = str(question.get("id", ""))
            matching = reviews_by_id.get(qid, [])
            if len(matching) != 1:
                exclusions.append(
                    f"{question_file.parent.name}/{qid}: review 数量为 {len(matching)}，未交付"
                )
                continue
            review = matching[0]
            if (
                review.get("teacher_verdict") != "pass"
                or review.get("correct") is not True
                or review.get("answer_consistent") is not True
                or review.get("manager_status") != "final"
                or review.get("auto_promote") is not True
            ):
                exclusions.append(
                    f"{question_file.parent.name}/{qid}: 未获 Teacher pass + manager final，未交付"
                )
                continue
            per_question_errors = check_question(question, line_no)
            key = question_key(question_file, bank_root, qid)
            review_errors = _review_errors(question, review, expected_key=key)
            if per_question_errors or review_errors:
                integrity_errors.extend(
                    f"{question_file.parent}/{qid}: {error}"
                    for error in [*per_question_errors, *review_errors]
                )
                continue
            selected_questions.append(question)
            selected_reviews.append(review)
        if selected_questions:
            quota_errors = _quota_errors(selected_questions)
            integrity_errors.extend(
                f"{question_file.parent}: 过滤未通过题后 {error}" for error in quota_errors
            )
            selected_by_file[question_file] = (selected_questions, selected_reviews)
            all_selected.extend(selected_questions)

    if not all_selected:
        integrity_errors.append("没有任何题同时满足 Teacher pass、快照一致和静态校验")
    distribution_errors = [
        item for item in _distribution_findings(
            all_selected,
            minimum=answer_share_min,
            maximum=answer_share_max,
            minimum_count=distribution_min_count,
        )
        if "超出" in item
    ]
    integrity_errors.extend(distribution_errors)
    if integrity_errors:
        preview = "\n".join(f"- {item}" for item in integrity_errors[:30])
        if len(integrity_errors) > 30:
            preview += f"\n- ……另有 {len(integrity_errors) - 30} 条"
        raise ValueError("交付门槛未通过，未写入任何交付文件：\n" + preview)

    for question_file, (questions, reviews) in selected_by_file.items():
        relative_node = question_file.parent.resolve().relative_to(bank_root.resolve())
        target_node = output / relative_node
        _write_jsonl(target_node / "questions.jsonl", questions)
        _write_jsonl(target_node / "answer_review.jsonl", reviews)
        if include_source_assets:
            for name in OPTIONAL_SOURCE_ASSETS:
                source = question_file.parent / name
                if source.is_file():
                    target_node.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target_node / name)
    return {
        "ok": True,
        "output": str(output.resolve()),
        "nodes": len(selected_by_file),
        "questions": len(all_selected),
        "excluded": len(exclusions),
        "exclusion_examples": exclusions[:20],
        "included_files": ["questions.jsonl", "answer_review.jsonl"],
        "source_assets_included": include_source_assets,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="题库静态校验与干净交付打包")
    parser.add_argument("target", nargs="?", default=".", help="题库内范围；默认全部")
    parser.add_argument(
        "--delivery",
        action="store_true",
        help="启用 answer_review、快照、答案、解析哈希及交付文件构成检查",
    )
    parser.add_argument(
        "--prepare-delivery",
        metavar="OUTPUT_DIR",
        help="生成全新干净交付目录；只收 Teacher pass、manager final 且哈希一致的题",
    )
    parser.add_argument(
        "--include-source-assets",
        action="store_true",
        help="打包时额外复制 reference.md 和 question 图片",
    )
    parser.add_argument(
        "--distribution-policy",
        choices=("off", "warn", "error"),
        help="答案分布处理；普通校验默认 warn，delivery/打包默认 error",
    )
    parser.add_argument("--answer-share-min", type=float, default=0.15)
    parser.add_argument("--answer-share-max", type=float, default=0.35)
    parser.add_argument("--distribution-min-count", type=int, default=40)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.answer_share_min < args.answer_share_max <= 1:
        print("answer share 范围必须满足 0 <= min < max <= 1", file=sys.stderr)
        return 2
    target = Path(args.target)
    base = target.resolve() if target.is_absolute() else (ROOT / target).resolve()
    policy = args.distribution_policy or (
        "error" if args.delivery or args.prepare_delivery else "warn"
    )
    try:
        if args.prepare_delivery:
            output = Path(args.prepare_delivery).expanduser().resolve()
            result = prepare_delivery(
                base,
                output,
                bank_root=ROOT,
                include_source_assets=args.include_source_assets,
                answer_share_min=args.answer_share_min,
                answer_share_max=args.answer_share_max,
                distribution_min_count=max(1, args.distribution_min_count),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        # An external prepared package keeps paths relative to its own root.
        bank_root = ROOT if base == ROOT or ROOT in base.parents else (
            base if base.is_dir() else base.parent
        )
        result = validate_scope(
            base,
            bank_root=bank_root,
            delivery=args.delivery,
            distribution_policy=policy,
            answer_share_min=args.answer_share_min,
            answer_share_max=args.answer_share_max,
            distribution_min_count=max(1, args.distribution_min_count),
        )
        return 0 if result["ok"] else 1
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
