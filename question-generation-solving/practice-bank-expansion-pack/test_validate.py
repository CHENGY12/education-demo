#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


VALIDATOR_PATH = Path(__file__).with_name("validate.py")
SPEC = importlib.util.spec_from_file_location("question_bank_validate", VALIDATOR_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def valid_question(qid: str = "q-1", answer: str = "B") -> dict:
    return {
        "id": qid,
        "nodeId": "node-1",
        "subject": "物理",
        "difficulty": "low",
        "pool": "display",
        "prompt": "某物体在 $2\\,\\mathrm{s}$ 内通过 $6\\,\\mathrm{m}$，其速度为（　）",
        "options": [
            {"id": "A", "text": "$2\\,\\mathrm{m/s}$"},
            {"id": "B", "text": "$3\\,\\mathrm{m/s}$"},
            {"id": "C", "text": "$6\\,\\mathrm{m/s}$"},
            {"id": "D", "text": "$12\\,\\mathrm{m/s}$"},
        ],
        "answer": answer,
        "explanation": "由 $v=s/t$ 得 $v=3\\,\\mathrm{m/s}$，代回可得路程为 $6\\,\\mathrm{m}$。",
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def review_for(question: dict, qfile: Path, bank: Path) -> dict:
    key = validator.question_key(qfile, bank, question["id"])
    answer = question["answer"]
    return {
        "id": question["id"],
        "question_key": key,
        "student_answers": {"solver1": answer, "solver2": answer, "solver3": answer},
        "answer_consistent": True,
        "teacher_answer": answer,
        "question_snapshot_sha256": validator.question_snapshot_sha256(question, key),
        "teacher_solution_sha256": validator.sha256_text(question["explanation"]),
        "manager_status": "final",
        "auto_promote": True,
        "correct": True,
        "teacher_verdict": "pass",
        "process_review": "三路过程与教师复算均通过。",
        "run_id": "run-1",
        "reviewed_on": "2026-08-10T00:00:00+00:00",
    }


class PerQuestionValidationTests(unittest.TestCase):
    def errors(self, question: dict) -> str:
        return "\n".join(validator.check_question(question, 1))

    def test_valid_question_passes(self) -> None:
        self.assertEqual(validator.check_question(valid_question(), 1), [])

    def test_control_character_from_bad_latex_escape_is_rejected(self) -> None:
        question = valid_question()
        question["explanation"] = "$2t=m\times x$"
        self.assertIn("控制字符", self.errors(question))

    def test_formula_outside_math_is_rejected(self) -> None:
        question = valid_question()
        question["explanation"] = "由 v^2=2as 可得结论。"
        errors = self.errors(question)
        self.assertIn("数学定界符外", errors)
        self.assertIn("表达式未置于", errors)

    def test_unescaped_percent_and_malformed_exponent_are_rejected(self) -> None:
        question = valid_question()
        question["explanation"] = "$50%+30%=80%$，且 $10^-8$。"
        errors = self.errors(question)
        self.assertIn("% 未写成", errors)
        self.assertIn("指数须写成", errors)

    def test_naked_units_are_rejected_inside_and_outside_math(self) -> None:
        question = valid_question()
        question["options"][0]["text"] = "0.5\\,\\mathrm{Hz}"
        question["options"][1]["text"] = "$1\\,Hz$"
        errors = self.errors(question)
        self.assertIn("数学定界符外存在 LaTeX", errors)
        self.assertIn("单位须用", errors)

    def test_coefficient_times_mass_variable_is_not_misread_as_unit(self) -> None:
        question = valid_question()
        question["explanation"] = "由 $F=2ma$ 可得结论。"
        self.assertNotIn("单位须用", self.errors(question))

    def test_stem_option_dump_and_letter_only_options_are_rejected(self) -> None:
        question = valid_question()
        question["prompt"] += "\nA. 甲\nB. 乙\nC. 丙\nD. 丁"
        question["options"] = [
            {"id": option, "text": option} for option in validator.OPTION_IDS
        ]
        errors = self.errors(question)
        self.assertIn("题干含 A/B/C/D", errors)
        self.assertIn("不得只是 A/B/C/D", errors)

    def test_empty_formula_repair_chatter_and_internal_terms_are_rejected(self) -> None:
        question = valid_question()
        question["explanation"] = "独立解：更正如下，$$a_1=$$"
        errors = self.errors(question)
        self.assertIn("内部残留用语", errors)
        self.assertIn("空公式", errors)


class DeliveryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.bank = Path(self.temporary.name) / "bank"
        self.node = self.bank / "cohort" / "物理" / "node-1"
        self.qfile = self.node / "questions.jsonl"
        questions: list[dict] = []
        for difficulty in validator.DIFFS:
            for index in range(5):
                question = valid_question(f"{difficulty}-{index}", validator.OPTION_IDS[index % 4])
                question["difficulty"] = difficulty
                question["pool"] = "display" if index < 3 else "exam"
                questions.append(question)
        self.questions = questions
        write_jsonl(self.qfile, questions)
        write_jsonl(
            self.node / "answer_review.jsonl",
            [review_for(question, self.qfile, self.bank) for question in questions],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_delivery_contract_passes_with_exact_snapshots(self) -> None:
        errors, questions, reviews = validator.check_delivery_node(
            self.qfile, bank_root=self.bank
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(questions), 15)
        self.assertEqual(len(reviews), 15)

    def test_delivery_rejects_stale_answer_and_forbidden_artifacts(self) -> None:
        reviews, _ = validator._read_jsonl(self.node / "answer_review.jsonl")
        reviews[0]["teacher_answer"] = "D"
        reviews[0]["auto_promote"] = False
        write_jsonl(self.node / "answer_review.jsonl", reviews)
        write_jsonl(self.node / "answer_final.jsonl", [])
        errors, _, _ = validator.check_delivery_node(self.qfile, bank_root=self.bank)
        joined = "\n".join(errors)
        self.assertIn("不一致", joined)
        self.assertIn("auto_promote", joined)
        self.assertIn("不得包含 answer_final.jsonl", joined)

    def test_prepare_delivery_filters_nonpass_and_writes_only_contract_files(self) -> None:
        reviews, _ = validator._read_jsonl(self.node / "answer_review.jsonl")
        # Keep quotas valid after excluding one extra sixth low/display item.
        extra = valid_question("low-extra", "A")
        self.questions.append(extra)
        reviews.append(review_for(extra, self.qfile, self.bank))
        # The key must be computed after the row is in the same qfile path; path itself is stable.
        reviews[-1]["teacher_verdict"] = "disagreement"
        reviews[-1]["correct"] = False
        reviews[-1]["answer_consistent"] = False
        write_jsonl(self.qfile, self.questions)
        write_jsonl(self.node / "answer_review.jsonl", reviews)
        write_jsonl(self.node / "answer_final.jsonl", [{"id": "legacy"}])

        output = Path(self.temporary.name) / "delivery"
        result = validator.prepare_delivery(
            self.bank / "cohort" / "物理",
            output,
            bank_root=self.bank,
            include_source_assets=False,
            answer_share_min=0.15,
            answer_share_max=0.35,
            distribution_min_count=40,
        )
        target_node = output / "cohort" / "物理" / "node-1"
        self.assertTrue(result["ok"])
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(
            sorted(path.name for path in target_node.iterdir()),
            ["answer_review.jsonl", "questions.jsonl"],
        )
        packaged, _ = validator._read_jsonl(target_node / "questions.jsonl")
        self.assertNotIn("low-extra", {question["id"] for question in packaged})
        validated = validator.validate_scope(
            output,
            bank_root=output,
            delivery=True,
            distribution_policy="error",
            answer_share_min=0.15,
            answer_share_max=0.35,
            distribution_min_count=40,
        )
        self.assertTrue(validated["ok"])

    def test_distribution_gate_reports_extreme_skew(self) -> None:
        questions = [valid_question(f"q-{index}", "A") for index in range(40)]
        findings = validator._distribution_findings(
            questions, minimum=0.15, maximum=0.35, minimum_count=40
        )
        self.assertTrue(any("超出" in finding for finding in findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
