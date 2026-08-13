#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import runpy
import tempfile
import threading
import unittest
import urllib.request

import qb_manager as qb


def question(qid: str, answer: str = "B") -> dict:
    return {
        "id": qid,
        "nodeId": "node-1",
        "subject": "物理",
        "difficulty": "low",
        "pool": "display",
        "prompt": "一物体做匀速直线运动，2 s 内通过 6 m，速度为（　）",
        "options": [
            {"id": "A", "text": "2 m/s"},
            {"id": "B", "text": "3 m/s"},
            {"id": "C", "text": "6 m/s"},
            {"id": "D", "text": "12 m/s"},
        ],
        "answer": answer,
        "explanation": "v=s/t=3 m/s。",
    }


def solution_skill_candidate(name: str = "比例关系双向复核") -> dict:
    return {
        "name": name,
        "description": "适用于由定义式建立比例关系并需要用逆运算检查结果的题目。",
        "applicability": ["已知两个物理量并由定义式求第三个量"],
        "ordered_steps": ["列出定义式及适用条件", "统一单位后代入", "用逆运算代回原条件"],
        "verification_checks": ["检查量纲", "检查代回后是否恢复已知量"],
        "pitfalls": ["不要在单位未统一时直接代入"],
        "tags": ["定义式", "代回检查"],
        "related_skill_id": "",
        "novelty_rationale": "把正向计算和逆向代回固定为一个可复用闭环。",
        "useful": True,
        "novel": True,
        "generalized": True,
        "action": "create",
    }


class FakeRunner:
    model = "fake-model"
    cli_version = "fake-cli 1.0"

    def __init__(self, disagree: bool = False) -> None:
        self.disagree = disagree

    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        invocation_dir.mkdir(parents=True, exist_ok=True)
        qb.atomic_write_text(invocation_dir / "prompt.md", prompt)
        qb.atomic_write_json(invocation_dir / "request.json", request)
        if role.startswith("solver"):
            answer = "C" if self.disagree and role == "solver3" else "B"
            payload = {
                "solutions": [
                    {
                        "id": item["id"],
                        "answer": answer,
                        "solution": "v=s/t=6/2=3 m/s。" if answer == "B" else "错误试解。",
                        "independent_check": "3×2=6。",
                        "question_valid": True,
                        "confidence": "high",
                        "diagnostic_issue_codes_checked": [
                            str(code)
                            for code in item.get("verification_feedback", {}).get("issue_codes", [])
                        ],
                        "diagnostic_focus_codes_checked": [
                            str(code)
                            for code in item.get("verification_feedback", {}).get("focus_codes", [])
                        ],
                        "solution_skill_ids_considered": [],
                    }
                    for item in request["questions"]
                ]
            }
        elif role == "teacher":
            payload = {
                "reviews": [
                    {
                        "id": item["id"],
                        "verdict": "disagreement" if self.disagree else "pass",
                        "answer_consistent": not self.disagree,
                        "teacher_answer": "B",
                        "teacher_solution": "由 v=s/t，得 v=6/2=3 m/s，故选 B。",
                        "process_review": "检查定义、计算和代回。",
                        "agent_feedback": [
                            {
                                "agent_id": agent,
                                "fully_correct": not (self.disagree and agent == "solver3"),
                                "issues": ["最终答案错误"] if self.disagree and agent == "solver3" else [],
                            }
                            for agent in ("solver1", "solver2", "solver3")
                        ],
                        "retry_feedback": {
                            "disposition": "human_review" if self.disagree else "none",
                            "issue_codes": ["wrong_model"] if self.disagree else [],
                            "focus_codes": ["derive_independently"] if self.disagree else [],
                        },
                        "question_annotation": {
                            "validity": "valid",
                            "question_type": "multiple_choice",
                            "difficulty": "low",
                            "annotation_codes": ["WELL_POSED", "UNIQUE_ANSWER"],
                            "revision_required": False,
                            "summary": "题面完整且答案唯一。",
                            "proposed_revision": None,
                        },
                        "skill_candidate": None,
                        "auto_promote": not self.disagree,
                    }
                    for item in request["questions"]
                ]
            }
        else:
            raise AssertionError(role)
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        meta = {
            "prompt_sha256": qb.sha256_text(prompt),
            "response_sha256": qb.sha256_text(raw),
        }
        return payload, meta


class FakeExpandRunner(FakeRunner):
    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        if role != "generator-solver":
            return super().run(
                role=role,
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
                images=images,
                progress=progress,
            )
        invocation_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "classifications": [],
            "questions": [
                {
                    "difficulty": "low",
                    "pool": "display",
                    "prompt": "一物体 4 s 匀速通过 12 m，速度为（　）",
                    "options": [
                        {"id": "A", "text": "2 m/s"},
                        {"id": "B", "text": "3 m/s"},
                        {"id": "C", "text": "4 m/s"},
                        {"id": "D", "text": "12 m/s"},
                    ],
                    "answer": "B",
                    "solution": "v=s/t=12/4=3 m/s。",
                    "independent_check": "3×4=12。",
                    "skillTarget": "速度定义式",
                    "hint": "用路程除以时间",
                }
            ],
        }
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "prompt.md", prompt)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        return payload, {
            "prompt_sha256": qb.sha256_text(prompt),
            "response_sha256": qb.sha256_text(raw),
        }


class FakeBlindMismatchRunner(FakeRunner):
    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        payload, meta = super().run(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            progress=progress,
        )
        if role == "solver-blind-recheck":
            for item in payload["solutions"]:
                item["answer"] = "C"
                item["solution"] = "独立重算得到另一选项。"
            raw = qb.compact_json(payload)
            qb.atomic_write_text(invocation_dir / "response.json", raw)
            meta["response_sha256"] = qb.sha256_text(raw)
        return payload, meta


class FakeTwoExpandRunner(FakeExpandRunner):
    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        if role != "generator-solver":
            return super().run(
                role=role,
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
                images=images,
                progress=progress,
            )
        payload, meta = super().run(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            progress=progress,
        )
        display = payload["questions"][0]
        display["difficulty"] = "high"
        display["pool"] = "display"
        exam = json.loads(json.dumps(display, ensure_ascii=False))
        exam["pool"] = "exam"
        exam["prompt"] = "另一道会被题库 validator 拒绝的高难题（　）"
        payload["questions"] = [display, exam]
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        return payload, {
            "prompt_sha256": qb.sha256_text(prompt),
            "response_sha256": qb.sha256_text(raw),
        }


class FakeInvalidTeacherFormatRunner(FakeRunner):
    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        payload, meta = super().run(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            progress=progress,
        )
        if role == "teacher":
            for review in payload["reviews"]:
                review["teacher_solution"] = r"由 \(v=s/t\) 得答案 B。"
            raw = qb.compact_json(payload)
            qb.atomic_write_text(invocation_dir / "response.json", raw)
            meta["response_sha256"] = qb.sha256_text(raw)
        return payload, meta


class FakePartialBatchRunner(FakeRunner):
    """Omit one solver answer and one Teacher review to test per-item isolation."""

    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        payload, meta = super().run(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            progress=progress,
        )
        if role == "solver3" and payload["solutions"]:
            payload["solutions"] = payload["solutions"][1:]
        elif role == "teacher" and payload["reviews"]:
            payload["reviews"] = payload["reviews"][1:]
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        meta["response_sha256"] = qb.sha256_text(raw)
        return payload, meta


class FakeLegacyRetryFeedbackRunner(FakeRunner):
    """Emit the retired retry disposition to prove it cannot trigger another 3+1."""

    def __init__(self):
        super().__init__(disagree=True)
        self.roles: list[str] = []

    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        self.roles.append(role)
        if role.startswith("solver"):
            return super().run(
                role=role, prompt=prompt, schema_path=schema_path,
                invocation_dir=invocation_dir, request=request, images=images,
                progress=progress,
            )
        if role == "teacher":
            payload, meta = super().run(
                role=role, prompt=prompt, schema_path=schema_path,
                invocation_dir=invocation_dir, request=request, images=images,
                progress=progress,
            )
            for review in payload["reviews"]:
                review["retry_feedback"] = {
                    "disposition": "retry",
                    "issue_codes": ["wrong_model", "answer_process_mismatch"],
                    "focus_codes": ["derive_independently", "cross_check_second_method"],
                }
            raw = qb.compact_json(payload)
            qb.atomic_write_text(invocation_dir / "response.json", raw)
            meta["response_sha256"] = qb.sha256_text(raw)
            return payload, meta
        raise AssertionError(role)


class FakeRegenerationRunner(FakeExpandRunner):
    """Reject one generated candidate, then create a distinct replacement next run."""

    def __init__(self):
        super().__init__()
        self.generation_count = 0
        self.roles: list[str] = []
        self.generation_requests: list[dict] = []

    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        self.roles.append(role)
        if role == "generator-solver":
            self.generation_count += 1
            self.generation_requests.append(json.loads(json.dumps(request, ensure_ascii=False)))
            payload, meta = super().run(
                role=role, prompt=prompt, schema_path=schema_path,
                invocation_dir=invocation_dir, request=request, images=images,
                progress=progress,
            )
            if self.generation_count > 1:
                replacement = payload["questions"][0]
                replacement["prompt"] = "一辆小车 5 s 匀速通过 15 m，速度为（　）"
                replacement["solution"] = "v=s/t=15/5=3 m/s。"
                replacement["independent_check"] = "3×5=15。"
                raw = qb.compact_json(payload)
                qb.atomic_write_text(invocation_dir / "response.json", raw)
                meta["response_sha256"] = qb.sha256_text(raw)
            return payload, meta
        return FakeRunner(disagree=self.generation_count == 1).run(
            role=role, prompt=prompt, schema_path=schema_path,
            invocation_dir=invocation_dir, request=request, images=images,
            progress=progress,
        )


class FakeSkillRunner(FakeRunner):
    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        if role != "skill-editor":
            return super().run(
                role=role,
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
                images=images,
                progress=progress,
            )
        invocation_dir.mkdir(parents=True, exist_ok=True)
        qb.atomic_write_text(invocation_dir / "prompt.md", prompt)
        qb.atomic_write_json(invocation_dir / "request.json", request)
        candidate = solution_skill_candidate("比例关系的单位与边界双重复核")
        candidate.update(
            {
                "action": "update",
                "related_skill_id": request["skill_id"],
                "description": "适用于定义式比例计算，并用单位、代回与边界趋势三重检查结果。",
                "verification_checks": ["检查量纲", "逆运算代回", "检查边界趋势"],
            }
        )
        payload = {"skill_candidate": candidate}
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        return payload, {
            "prompt_sha256": qb.sha256_text(prompt),
            "response_sha256": qb.sha256_text(raw),
        }


class FakeHistorySkillRunner(FakeRunner):
    max_processes = 2

    def run(self, *, role, prompt, schema_path, invocation_dir, request, images=(), progress=None):
        invocation_dir.mkdir(parents=True, exist_ok=True)
        qb.atomic_write_text(invocation_dir / "prompt.md", prompt)
        qb.atomic_write_json(invocation_dir / "request.json", request)
        if role == "skill-history-curator":
            source_keys = [
                str(item["question_key"]) for item in request["verified_questions"]
            ]
            candidate = solution_skill_candidate("定义式比例计算的单位与逆向复核")
            candidate["novel"] = False
            candidate["novelty_rationale"] = (
                "方法并不新颖，但在多道定义式计算中都能稳定减少单位和代回错误。"
            )
            payload = {
                "review_summary": "已逐题回顾当前批次。",
                "skill_candidates": [
                    {
                        "source_question_keys": source_keys,
                        "source_candidate_ids": [],
                        "reuse_rationale": "两道题都需要先统一单位，再用逆运算恢复已知量。",
                        "skill_candidate": candidate,
                    }
                ],
            }
        elif role == "skill-history-consolidator":
            item = dict(request["batch_candidates"][0])
            candidate = dict(item["skill_candidate"])
            candidate.pop("skill_id", None)
            payload = {
                "review_summary": "已完成跨批次合并与证据核验。",
                "skill_candidates": [
                    {
                        "source_question_keys": item["source_question_keys"],
                        "source_candidate_ids": [item["candidate_id"]],
                        "reuse_rationale": item["reuse_rationale"],
                        "skill_candidate": candidate,
                    }
                ],
            }
        else:
            return super().run(
                role=role,
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
                images=images,
                progress=progress,
            )
        raw = qb.compact_json(payload)
        qb.atomic_write_text(invocation_dir / "response.json", raw)
        return payload, {
            "prompt_sha256": qb.sha256_text(prompt),
            "response_sha256": qb.sha256_text(raw),
        }


class CapturingExecutor:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        return None


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = Path(self.tmp.name) / "bank"
        self.node = self.bank / "school" / "物理" / "node-1"
        self.node.mkdir(parents=True)
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [question("q-1"), question("q-2")])
        qb.atomic_write_text(self.node / "reference.md", "# 匀速直线运动\n")
        self.state = qb.State(self.bank)
        self.state.ensure()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def rows(self):
        with self.state.connect() as conn:
            return list(conn.execute("SELECT * FROM questions ORDER BY qid"))

    def test_prompt_payload_latex_unit_is_not_treated_as_placeholder(self) -> None:
        rendered = qb.render_prompt(
            "题目数据：{QUESTION_BATCH_JSON}",
            {"QUESTION_BATCH_JSON": r'{"unit":"$1\\,\\mathrm{J}$"}'},
        )
        self.assertIn(r"\mathrm{J}", rendered)

    def test_prompts_include_delivery_quality_gates(self) -> None:
        generator = (qb.REFERENCE_ROOT / "generator-solver-prompt.md").read_text(encoding="utf-8")
        solver = (qb.REFERENCE_ROOT / "solver-prompt.md").read_text(encoding="utf-8")
        teacher = (qb.REFERENCE_ROOT / "teacher-prompt.md").read_text(encoding="utf-8")
        manager = (qb.REFERENCE_ROOT / "manager-prompt.md").read_text(encoding="utf-8")
        for prompt in (generator, solver, teacher):
            self.assertIn("控制字符", prompt)
            self.assertIn(r"\mathrm", prompt)
        self.assertIn("Markdown 表格", generator)
        self.assertIn(r"\%", generator)
        self.assertIn("空公式", teacher)
        self.assertIn("UNIT_DIMENSION_MISMATCH", teacher)
        self.assertIn("--prepare-delivery", manager)
        self.assertIn("question_snapshot_sha256", manager)
        for prompt in (generator, solver, teacher):
            self.assertIn("zh-Hant-HK", prompt)
        self.assertIn("delivery-issues-v1.md", manager)

    def test_safe_format_repair_adds_sentence_periods_idempotently(self) -> None:
        row = question("sentence-options")
        row["options"] = [
            {"id": "A", "text": "物体速度始终保持不变"},
            {"id": "B", "text": "物体所受合外力始终为零"},
            {"id": "C", "text": "物体在相等时间通过相等路程"},
            {"id": "D", "text": "物体运动方向始终保持不变"},
        ]
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [row])
        self.assertEqual(qb.count_safe_format_repairs([row]), 1)
        self.assertEqual(qb.apply_safe_format_repairs(self.state, self.node / "questions.jsonl"), 1)
        repaired, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertTrue(all(option["text"].endswith("。") for option in repaired[0]["options"]))
        self.assertEqual(qb.apply_safe_format_repairs(self.state, self.node / "questions.jsonl"), 0)

    def test_generated_formula_direction_options_are_normalized_before_validation(self) -> None:
        row = question("formula-direction")
        row["options"] = [
            {"id": "A", "text": "$0.25g$，竖直向下"},
            {"id": "B", "text": "$0.75g$，竖直向上"},
            {"id": "C", "text": "$g$，竖直向上"},
            {"id": "D", "text": "$1.25g$，竖直向下"},
        ]
        self.assertTrue(qb.question_options_need_periods(row))
        self.assertTrue(qb.repair_question_option_periods(row))
        self.assertTrue(all(option["text"].endswith("。") for option in row["options"]))
        self.assertFalse(qb.repair_question_option_periods(row))

    def test_physics_Mg_is_not_misclassified_as_chemical_magnesium(self) -> None:
        project_root = Path(__file__).resolve().parents[4]
        validator_path = next(
            (
                candidate
                for candidate in (
                    project_root / "practice-bank-expansion-pack" / "validate.py",
                    project_root
                    / "question generation"
                    / "practice-bank-expansion-pack"
                    / "validate.py",
                )
                if candidate.is_file()
            ),
            project_root / "practice-bank-expansion-pack" / "validate.py",
        )
        if not validator_path.is_file():
            self.skipTest("题库 validate.py 未包含在独立 Skill 发布包中")
        validator = runpy.run_path(str(validator_path), run_name="__validator_test__")
        physics = question("physics-Mg")
        physics["explanation"] = "$Mg$ 表示质量与重力加速度的乘积。"
        self.assertFalse(
            any("化学式/元素" in error for error in validator["check_question"](physics, 1))
        )
        chemistry = dict(physics)
        chemistry["subject"] = "化学"
        self.assertTrue(
            any("化学式/元素" in error for error in validator["check_question"](chemistry, 1))
        )

    def test_seed_and_generated_variants_do_not_inherit_node_image(self) -> None:
        seed = question("pb_物理_node-1_seed_001")
        original = question("pb_物理_node-1_original_001")
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [seed, original])
        qb.atomic_write_text(self.node / "question.png", "fixture")
        qb.scan_bank(self.state)
        rows = {row["qid"]: row for row in self.rows()}

        self.assertIsNone(
            qb.question_node_image(self.state, rows["pb_物理_node-1_seed_001"])
        )
        self.assertEqual(
            qb.question_node_image(self.state, rows["pb_物理_node-1_original_001"]),
            self.node / "question.png",
        )

        generated = question("pb_物理_node-1_custom_001")
        qfile_rel = qb.safe_rel(self.node / "questions.jsonl", self.bank)
        key = qb.question_key(qfile_rel, generated["id"])
        qb.upsert_question_row(
            self.state,
            key=key,
            qid=generated["id"],
            node_dir=qb.safe_rel(self.node, self.bank),
            question_file=qfile_rel,
            subject="物理",
            question=generated,
            source_kind="generated",
        )
        self.assertIsNone(
            qb.question_node_image(self.state, qb.get_question_row(self.state, key))
        )

    def test_scan_and_human_accept(self) -> None:
        report = qb.scan_bank(self.state)
        self.assertEqual(report["questions"], 2)
        row = self.rows()[0]
        qb.accept_final(
            self.state,
            row["question_key"],
            run_id="human-test",
            source="human_accept:custom",
            answer="B",
            solution="6÷2=3，所以选 B。",
        )
        final_rows, errors = qb.read_jsonl(self.node / "answer_final.jsonl")
        self.assertFalse(errors)
        self.assertEqual(final_rows[0]["answer"], "B")
        self.assertEqual(qb.get_question_row(self.state, row["question_key"])["status"], "final")

    def test_audit_qid_like_limits_retry_selection(self) -> None:
        qb.atomic_write_jsonl(
            self.node / "questions.jsonl",
            [question("pb_物理_node-1_seed_001"), question("pb_物理_node-1_gen_abc")],
        )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=1)
        preview = pipeline.audit(
            scope=None,
            subject="物理",
            qid_like="%_seed_%",
            node_limit=None,
            question_limit=None,
            batch_size=15,
            include_disagreements=False,
            force=False,
            auto_promote=True,
            dry_run=True,
        )
        self.assertEqual(preview["selected_questions"], 1)

    def test_cli_parser_accepts_qid_like(self) -> None:
        parsed = qb.build_parser().parse_args(
            ["audit", "--bank", str(self.bank), "--qid-like", "%_seed_%", "--dry-run"]
        )
        self.assertEqual(parsed.qid_like, "%_seed_%")

    def test_cli_parser_accepts_blind_recheck(self) -> None:
        parsed = qb.build_parser().parse_args(
            [
                "blind-recheck",
                "--bank",
                str(self.bank),
                "--target",
                "school/物理",
                "--batch-size",
                "12",
                "--dry-run",
            ]
        )
        self.assertEqual(parsed.target, ["school/物理"])
        self.assertEqual(parsed.batch_size, 12)

    def test_cli_parser_accepts_serve_scope(self) -> None:
        parsed = qb.build_parser().parse_args(
            ["serve", "--bank", str(self.bank), "--scope", "school/物理/*"]
        )
        self.assertEqual(parsed.scope, ["school/物理/*"])

    def test_cli_parser_accepts_auto_provider_and_batch_mode(self) -> None:
        parsed = qb.build_parser().parse_args(
            [
                "audit",
                "--bank",
                str(self.bank),
                "--provider",
                "auto",
                "--api-mode",
                "batch",
                "--dry-run",
            ]
        )
        self.assertEqual(parsed.provider, "auto")
        self.assertEqual(parsed.api_mode, "batch")
        self.assertIsNone(parsed.max_agent_processes)

    def test_cli_parser_accepts_historical_skill_curation(self) -> None:
        parsed = qb.build_parser().parse_args(
            [
                "curate-skills",
                "--bank",
                str(self.bank),
                "--target",
                "school/物理",
                "--subject",
                "物理",
                "--dry-run",
            ]
        )
        self.assertEqual(parsed.target, ["school/物理"])
        self.assertEqual(parsed.min_source_questions, 2)
        self.assertEqual(parsed.min_source_nodes, 1)

    def test_responses_api_artifacts_are_stateless_and_do_not_persist_key(self) -> None:
        class Response:
            status = 200
            headers = {"x-request-id": "req-fixture"}

            def read(self):
                envelope = {
                    "id": "resp-fixture",
                    "status": "completed",
                    "model": "gpt-5.6-sol",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": '{"ok":true}'}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
                return json.dumps(envelope).encode()

            def close(self):
                return None

        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            return Response()

        schema = self.bank / "api-fixture.schema.json"
        qb.atomic_write_json(
            schema,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
        )
        runner = qb.AgentRunner(
            self.state,
            provider="auto",
            api_mode="responses",
            environ={"OPENAI_API_KEY": "super-secret-fixture"},
            urlopen=fake_urlopen,
            retries=0,
        )
        self.assertEqual(runner.provider, "api")
        self.assertEqual(runner.max_processes, 9)
        invocation = self.state.runs_dir / "api-fixture" / "solver1"
        payload, meta = runner.run(
            role="solver1",
            prompt="只返回结构化 fixture。",
            schema_path=schema,
            invocation_dir=invocation,
            request={"questions": [], "agent_id": "solver1"},
        )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(meta["provider_response_id"], "resp-fixture")
        provider_body = json.loads((invocation / "provider-request.json").read_text())
        self.assertIs(provider_body["store"], False)
        self.assertNotIn("tools", provider_body)
        self.assertNotIn("conversation", provider_body)
        self.assertNotIn("previous_response_id", provider_body)
        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in invocation.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("super-secret-fixture", persisted)
        self.assertEqual(len(seen), 1)

    def test_consensus_pipeline_promotes_and_verifies(self) -> None:
        qb.scan_bank(self.state)
        rows = self.rows()
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        run_id = qb.new_run_id("test")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows(rows, run_id=run_id, run_dir=run_dir)
        pipeline.finish_manifest(run_dir, counts)
        self.assertEqual(counts["final"], 2)
        self.assertTrue(all(row["status"] == "final" for row in self.rows()))
        database_mtime = self.state.db_path.stat().st_mtime_ns
        verification = qb.verify_state(self.state)
        self.assertTrue(verification["ok"], verification["errors"])
        self.assertEqual(self.state.db_path.stat().st_mtime_ns, database_mtime)
        self.assertEqual(verification["ledger_entries"], 1)
        artifact = next(path for path in run_dir.rglob("prompt.md"))
        qb.atomic_write_text(artifact, artifact.read_text(encoding="utf-8") + "\n篡改")
        tampered = qb.verify_state(self.state)
        self.assertFalse(tampered["ok"])
        self.assertTrue(any("artifact inventory" in item for item in tampered["errors"]))

    def test_blind_recheck_certifies_without_exposing_final_answer(self) -> None:
        qb.scan_bank(self.state)
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        initial_run = qb.new_run_id("initial")
        initial_dir = pipeline.create_manifest(initial_run, "test", {"fixture": True})
        counts = pipeline.audit_rows(self.rows(), run_id=initial_run, run_dir=initial_dir)
        pipeline.finish_manifest(initial_dir, counts)

        pipeline.runner.max_processes = 2
        result = pipeline.blind_recheck(
            targets=["school/物理"],
            subject="物理",
            batch_size=1,
            force=False,
            dry_run=False,
        )
        self.assertEqual(result["result"]["passed"], 2)
        self.assertEqual(result["result"]["error"], 0)
        self.assertEqual(result["max_parallel_batches"], 2)
        with self.state.connect() as conn:
            certificates = list(conn.execute("SELECT * FROM blind_rechecks"))
        self.assertEqual(len(certificates), 2)
        self.assertTrue(all(item["matched"] for item in certificates))
        request_path = next(
            (self.state.runs_dir / result["run_id"]).rglob(
                "solver-blind-recheck/request.json"
            )
        )
        serialized = request_path.read_text(encoding="utf-8")
        self.assertNotIn('"answer"', serialized)
        self.assertNotIn('"explanation"', serialized)
        reviews, errors = qb.read_jsonl(self.node / "answer_review.jsonl")
        self.assertFalse(errors)
        self.assertTrue(all(item["blind_recheck"]["matched"] for item in reviews))

    def test_blind_mismatch_removes_generated_candidate_from_authoritative_bank(self) -> None:
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [question("generated-only")])
        qb.scan_bank(self.state)
        row = self.rows()[0]
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE questions SET source_kind='generated' WHERE question_key=?",
                (row["question_key"],),
            )
        row = qb.get_question_row(self.state, row["question_key"])
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        initial_run = qb.new_run_id("generated-initial")
        initial_dir = pipeline.create_manifest(initial_run, "test", {"fixture": True})
        counts = pipeline.audit_rows([row], run_id=initial_run, run_dir=initial_dir)
        pipeline.finish_manifest(initial_dir, counts)
        self.assertEqual(counts["final"], 1)

        pipeline.runner = FakeBlindMismatchRunner()
        result = pipeline.blind_recheck(
            targets=["school/物理"],
            subject="物理",
            batch_size=15,
            force=False,
            dry_run=False,
        )
        self.assertEqual(result["result"]["generated_rejected"], 1)
        source_rows, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertEqual(source_rows, [])
        current = qb.get_question_row(self.state, row["question_key"])
        self.assertEqual(current["status"], "invalid")
        reviews, errors = qb.read_jsonl(self.node / "answer_review.jsonl")
        self.assertFalse(errors)
        self.assertEqual(reviews, [])

    def test_partial_batch_omissions_only_fail_affected_questions(self) -> None:
        qb.atomic_write_jsonl(
            self.node / "questions.jsonl",
            [question("q-1"), question("q-2"), question("q-3")],
        )
        qb.scan_bank(self.state)
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakePartialBatchRunner()
        run_id = qb.new_run_id("partial")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows(self.rows(), run_id=run_id, run_dir=run_dir)
        self.assertEqual(counts, {"final": 1, "disagreement": 0, "invalid": 0, "error": 2})
        statuses = {row["qid"]: row["status"] for row in self.rows()}
        self.assertEqual(statuses, {"q-1": "error", "q-2": "error", "q-3": "final"})

    def test_disagreement_is_exported(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner(disagree=True)
        run_id = qb.new_run_id("test-disagreement")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows([row], run_id=run_id, run_dir=run_dir)
        self.assertEqual(counts["disagreement"], 1)
        exported = qb.export_unresolved(self.state)
        self.assertEqual(exported["unresolved"], 1)
        wrong, errors = qb.read_jsonl(self.bank / "错题集.jsonl")
        self.assertFalse(errors)
        self.assertEqual(wrong[0]["id"], "q-1")
        self.assertEqual(len(wrong[0]["attempts"]), 3)
        with self.state.connect() as conn:
            reviews_before = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        qb.scan_bank(self.state)
        with self.state.connect() as conn:
            reviews_after = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        self.assertEqual(reviews_after, reviews_before)

    def test_teacher_retry_diagnostic_does_not_trigger_automatic_second_round(self) -> None:
        teacher_schema = json.loads(
            (Path(qb.__file__).parent / "teacher_batch.schema.json").read_text(encoding="utf-8")
        )
        dispositions = teacher_schema["properties"]["reviews"]["items"]["properties"][
            "retry_feedback"
        ]["properties"]["disposition"]["enum"]
        self.assertNotIn("retry", dispositions)
        qb.scan_bank(self.state)
        row = self.rows()[0]
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        runner = FakeLegacyRetryFeedbackRunner()
        pipeline.runner = runner
        run_id = qb.new_run_id("single-round")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows([row], run_id=run_id, run_dir=run_dir)
        pipeline.finish_manifest(run_dir, counts)
        self.assertEqual(counts, {"final": 0, "disagreement": 1, "invalid": 0, "error": 0})
        current = qb.get_question_row(self.state, row["question_key"])
        self.assertEqual(current["status"], "disagreement")
        self.assertEqual(current["current_run_id"], run_id)
        with self.state.connect() as conn:
            reviews = list(
                conn.execute(
                    "SELECT * FROM reviews WHERE question_key=? ORDER BY created_at,run_id",
                    (row["question_key"],),
                )
            )
            attempts = list(
                conn.execute(
                    "SELECT * FROM attempts WHERE question_key=? ORDER BY run_id,agent_id",
                    (row["question_key"],),
                )
            )
        self.assertEqual(len(reviews), 1)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sorted(runner.roles), ["solver1", "solver2", "solver3", "teacher"])
        self.assertFalse(any(path.name.startswith("postverify") for path in self.state.runs_dir.iterdir()))
        stored_review = json.loads(reviews[0]["raw_json"])
        self.assertEqual(stored_review["retry_feedback"]["disposition"], "retry")
        verified = qb.verify_state(self.state)
        self.assertTrue(verified["ok"], verified["errors"])

    def test_generated_question_is_committed_only_on_accept(self) -> None:
        qb.scan_bank(self.state)
        qfile_rel = qb.safe_rel(self.node / "questions.jsonl", self.bank)
        generated = question("q-generated")
        key = qb.question_key(qfile_rel, generated["id"])
        qb.upsert_question_row(
            self.state,
            key=key,
            qid=generated["id"],
            node_dir=qb.safe_rel(self.node, self.bank),
            question_file=qfile_rel,
            subject="物理",
            question=generated,
            source_kind="generated",
        )
        before, _ = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertEqual(len(before), 2)
        qb.accept_final(
            self.state,
            key,
            run_id="generated-test",
            source="human_accept:teacher",
            answer="B",
            solution="核验后的过程。",
        )
        after, _ = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertEqual(len(after), 3)
        self.assertEqual(after[-1]["explanation"], "核验后的过程。")

    def test_existing_accept_updates_authoritative_questions_jsonl(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        qb.accept_final(
            self.state,
            row["question_key"],
            run_id="existing-final-test",
            source="human_accept:teacher",
            answer="D",
            solution="核验后的权威解析。",
        )
        source_rows, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        source = next(item for item in source_rows if item["id"] == row["qid"])
        self.assertEqual(source["answer"], "D")
        self.assertEqual(source["explanation"], "核验后的权威解析。")
        stored = qb.get_question_row(self.state, row["question_key"])
        self.assertEqual(json.loads(stored["question_json"]), source)
        verified = qb.verify_state(self.state)
        self.assertTrue(verified["ok"], verified["errors"])

    def test_existing_accept_refuses_to_overwrite_post_scan_source_edit(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        source_rows, _ = qb.read_jsonl(self.node / "questions.jsonl")
        edited = next(item for item in source_rows if item["id"] == row["qid"])
        edited["prompt"] = "用户在扫描后修改了题干（　）"
        qb.atomic_write_jsonl(self.node / "questions.jsonl", source_rows)
        with self.assertRaisesRegex(qb.ManagerError, "已在扫描/解题后变化"):
            qb.accept_final(
                self.state,
                row["question_key"],
                run_id="stale-source-test",
                source="human_accept:teacher",
                answer="D",
                solution="不应写入。",
            )
        current_rows, _ = qb.read_jsonl(self.node / "questions.jsonl")
        current = next(item for item in current_rows if item["id"] == row["qid"])
        self.assertEqual(current["prompt"], "用户在扫描后修改了题干（　）")
        finals, _ = qb.read_jsonl(self.node / "answer_final.jsonl")
        self.assertFalse(any(item["id"] == row["qid"] for item in finals))

    def test_export_review_contains_question_and_solution_hashes(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        run_id = qb.new_run_id("review-hash")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows([row], run_id=run_id, run_dir=run_dir)
        self.assertEqual(counts["final"], 1)
        qb.export_unresolved(self.state)
        reviews, errors = qb.read_jsonl(self.node / "answer_review.jsonl")
        self.assertFalse(errors)
        review = next(item for item in reviews if item["id"] == row["qid"])
        current = qb.get_question_row(self.state, row["question_key"])
        self.assertEqual(review["manager_status"], "final")
        self.assertTrue(review["auto_promote"])
        self.assertEqual(
            review["question_snapshot_sha256"],
            qb.public_question_snapshot_sha256(current),
        )
        validator = runpy.run_path(
            str(Path(__file__).resolve().parents[4] / "practice-bank-expansion-pack" / "validate.py"),
            run_name="__delivery_validator_test__",
        )
        self.assertEqual(
            review["question_snapshot_sha256"],
            validator["question_snapshot_sha256"](
                json.loads(current["question_json"]), current["question_key"]
            ),
        )
        self.assertEqual(
            review["teacher_solution_sha256"],
            qb.sha256_text(json.loads(current["question_json"])["explanation"]),
        )

    def test_human_accept_cannot_bypass_bank_validator_for_generated_question(self) -> None:
        qb.scan_bank(self.state)
        qfile_rel = qb.safe_rel(self.node / "questions.jsonl", self.bank)
        generated = question("q-generated-invalid")
        key = qb.question_key(qfile_rel, generated["id"])
        qb.upsert_question_row(
            self.state,
            key=key,
            qid=generated["id"],
            node_dir=qb.safe_rel(self.node, self.bank),
            question_file=qfile_rel,
            subject="物理",
            question=generated,
            source_kind="generated",
        )
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no):\n    return ['still-invalid']\n",
        )
        with self.assertRaisesRegex(qb.ManagerError, "still-invalid"):
            qb.accept_final(
                self.state,
                key,
                run_id="human-test",
                source="human_accept:teacher",
                answer="B",
                solution="人工选择的解法。",
            )
        after, _ = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(any(row["id"] == generated["id"] for row in after))

    def test_existing_question_cannot_enter_final_when_bank_validator_rejects(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no):\n    return ['existing-invalid']\n",
        )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        run_id = qb.new_run_id("existing-validator")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows([row], run_id=run_id, run_dir=run_dir)
        self.assertEqual(counts["final"], 0)
        self.assertEqual(counts["disagreement"], 1)
        self.assertEqual(qb.get_question_row(self.state, row["question_key"])["status"], "disagreement")
        finals, _ = qb.read_jsonl(self.node / "answer_final.jsonl")
        self.assertFalse(any(item["id"] == row["qid"] for item in finals))

    def test_teacher_format_rejection_downgrades_only_generated_item(self) -> None:
        qb.scan_bank(self.state)
        qfile_rel = qb.safe_rel(self.node / "questions.jsonl", self.bank)
        generated = question("q-generated-teacher-format")
        key = qb.question_key(qfile_rel, generated["id"])
        qb.upsert_question_row(
            self.state,
            key=key,
            qid=generated["id"],
            node_dir=qb.safe_rel(self.node, self.bank),
            question_file=qfile_rel,
            subject="物理",
            question=generated,
            source_kind="generated",
        )
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no):\n"
            "    return ['bad-delimiter'] if r'\\(' in question.get('explanation','') else []\n",
        )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeInvalidTeacherFormatRunner()
        run_id = qb.new_run_id("teacher-format")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows(
            [qb.get_question_row(self.state, key)], run_id=run_id, run_dir=run_dir
        )
        self.assertEqual(counts["disagreement"], 1)
        self.assertEqual(qb.get_question_row(self.state, key)["status"], "disagreement")
        source_rows, _ = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(any(row["id"] == generated["id"] for row in source_rows))

    def test_expand_generates_missing_slot_and_audits(self) -> None:
        rows = []
        allocation = {
            "low": {"display": 2, "exam": 2},
            "mid": {"display": 3, "exam": 2},
            "high": {"display": 3, "exam": 2},
        }
        index = 0
        for difficulty, pools in allocation.items():
            for pool, count in pools.items():
                for _ in range(count):
                    index += 1
                    item = question(f"quota-{index:02d}")
                    item["difficulty"] = difficulty
                    item["pool"] = pool
                    rows.append(item)
        qb.atomic_write_jsonl(self.node / "questions.jsonl", rows)
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeExpandRunner()
        result = pipeline.expand(
            scope=None,
            subject=None,
            node_limit=1,
            auto_promote=True,
            dry_run=False,
        )
        self.assertEqual(result["result"]["generated"], 1)
        self.assertEqual(result["result"]["final"], 1)
        after, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertEqual(len(after), 15)
        self.assertEqual(qb.quota_deficits(after)["low"]["display"], 0)

    def test_failed_generated_question_is_replaced_on_next_expand(self) -> None:
        rows = []
        allocation = {
            "low": {"display": 2, "exam": 2},
            "mid": {"display": 3, "exam": 2},
            "high": {"display": 3, "exam": 2},
        }
        index = 0
        for difficulty, pools in allocation.items():
            for pool, count in pools.items():
                for _ in range(count):
                    index += 1
                    item = question(f"replace-{index:02d}")
                    item["difficulty"] = difficulty
                    item["pool"] = pool
                    rows.append(item)
        qb.atomic_write_jsonl(self.node / "questions.jsonl", rows)
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        runner = FakeRegenerationRunner()
        pipeline.runner = runner

        first = pipeline.expand(
            scope=None,
            subject=None,
            node_limit=1,
            auto_promote=True,
            dry_run=False,
        )
        self.assertEqual(first["result"]["generated"], 1)
        self.assertEqual(first["result"]["disagreement"], 1)
        self.assertEqual(first["result"]["regeneration_needed"], 1)
        after_first, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertEqual(len(after_first), 14)
        self.assertEqual(qb.quota_deficits(after_first)["low"]["display"], 1)
        self.assertEqual(len(runner.roles), 4)

        second = pipeline.expand(
            scope=None,
            subject=None,
            node_limit=1,
            auto_promote=True,
            dry_run=False,
        )
        self.assertEqual(second["result"]["generated"], 1)
        self.assertEqual(second["result"]["final"], 1)
        self.assertEqual(second["result"]["regeneration_needed"], 0)
        rejected = runner.generation_requests[1]["rejected_generated_questions"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("4 s 匀速通过 12 m", rejected[0]["prompt"])
        after_second, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertEqual(len(after_second), 15)
        self.assertEqual(qb.quota_deficits(after_second)["low"]["display"], 0)
        self.assertIn("5 s 匀速通过 15 m", after_second[-1]["prompt"])
        self.assertEqual(len(runner.roles), 8)
        self.assertFalse(any(path.name.startswith("postverify") for path in self.state.runs_dir.iterdir()))

    def test_run_targets_accepts_absolute_and_relative_dirs_as_one_batch(self) -> None:
        node_two = self.bank / "school" / "物理" / "node-2"
        node_two.mkdir(parents=True)
        qb.atomic_write_jsonl(
            node_two / "questions.jsonl",
            [question("q-3"), question("q-4")],
        )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        result = pipeline.run_targets(
            targets=[qb.safe_rel(self.node, self.bank), str(node_two.resolve())],
            mode="audit",
            subject=None,
            batch_size=15,
            auto_promote=True,
            dry_run=False,
        )
        self.assertEqual(result["target_count"], 2)
        self.assertEqual(result["audit"]["selected_nodes"], 2)
        self.assertEqual(result["audit"]["selected_questions"], 4)
        self.assertEqual(result["audit"]["result"]["final"], 4)
        self.assertEqual(len(list(self.state.runs_dir.iterdir())), 1)

    def test_run_targets_rejects_missing_directory(self) -> None:
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner()
        with self.assertRaisesRegex(qb.ManagerError, "--target 不存在"):
            pipeline.run_targets(
                targets=["school/物理/not-there"],
                mode="audit",
                subject=None,
                batch_size=15,
                auto_promote=True,
                dry_run=True,
            )

    def test_generation_deficit_is_computed_after_existing_classification(self) -> None:
        rows = [question(f"seed-{index}") for index in range(4)]
        for row in rows:
            row["difficulty"] = ""
            row["pool"] = ""
        classifications = [
            {"id": "seed-0", "difficulty": "low", "pool": "display"},
            {"id": "seed-1", "difficulty": "low", "pool": "exam"},
            {"id": "seed-2", "difficulty": "mid", "pool": "display"},
            {"id": "seed-3", "difficulty": "high", "pool": "exam"},
        ]
        classified = qb.classified_rows_for_generation(rows, classifications)
        deficits = qb.quota_deficits(classified)
        self.assertEqual(sum(sum(value.values()) for value in deficits.values()), 11)
        self.assertEqual(deficits["low"], {"display": 2, "exam": 1})

    def test_generation_requires_every_blank_existing_classification(self) -> None:
        rows = [question("seed-0")]
        rows[0]["difficulty"] = ""
        rows[0]["pool"] = ""
        with self.assertRaisesRegex(qb.ManagerError, "缺少现有题分类"):
            qb.classified_rows_for_generation(rows, [])

    def test_multiselect_seed_does_not_consume_single_choice_quota(self) -> None:
        seed = question("multiselect-seed", "A、C")
        seed["difficulty"] = ""
        seed["pool"] = ""
        classified = qb.classified_rows_for_generation(
            [seed],
            [{"id": seed["id"], "difficulty": "low", "pool": "display"}],
        )
        deficits = qb.quota_deficits(classified)
        self.assertFalse(qb.question_counts_toward_quota(seed))
        self.assertEqual(sum(sum(value.values()) for value in deficits.values()), 15)

    def test_unresolved_seed_no_longer_consumes_delivery_quota(self) -> None:
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [question("bad-seed")])
        qb.scan_bank(self.state)
        row = self.rows()[0]
        qb.update_question_status(
            self.state,
            [row["question_key"]],
            "disagreement",
            "failed-seed-run",
            verdict="solver disagreement",
        )
        eligible = qb.expansion_quota_eligible_ids(
            self.state,
            self.node / "questions.jsonl",
            [question("bad-seed")],
        )
        self.assertEqual(eligible, set())
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=1)
        preview = pipeline.expand(
            scope="school/物理",
            subject="物理",
            node_limit=None,
            auto_promote=True,
            dry_run=True,
        )
        self.assertEqual(preview["new_questions_after_classification"]["maximum"], 15)

    def test_generated_overflow_is_capped_without_discarding_needed_items(self) -> None:
        items = [
            {"difficulty": "high", "pool": "exam", "prompt": "needed"},
            {"difficulty": "high", "pool": "exam", "prompt": "overflow"},
            {"difficulty": "low", "pool": "display", "prompt": "also-needed"},
        ]
        expected = {
            "low": {"display": 1, "exam": 0},
            "mid": {"display": 0, "exam": 0},
            "high": {"display": 0, "exam": 1},
        }
        selected, overflow = qb.cap_generated_to_deficits(items, expected)
        self.assertEqual([item["prompt"] for item in selected], ["needed", "also-needed"])
        self.assertEqual([item["prompt"] for item in overflow], ["overflow"])

    def test_validator_rejection_keeps_valid_siblings_and_blocks_bad_item(self) -> None:
        rows = []
        allocation = {
            "low": {"display": 3, "exam": 2},
            "mid": {"display": 3, "exam": 2},
            "high": {"display": 2, "exam": 1},
        }
        index = 0
        for difficulty, pools in allocation.items():
            for pool, count in pools.items():
                for _ in range(count):
                    index += 1
                    item = question(f"atomic-{index:02d}")
                    item["difficulty"] = difficulty
                    item["pool"] = pool
                    item["prompt"] = f"已有题 {index}（　）"
                    rows.append(item)
        qb.atomic_write_jsonl(self.node / "questions.jsonl", rows)
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no):\n"
            "    return ['reject-exam'] if question.get('pool') == 'exam' else []\n",
        )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeTwoExpandRunner()
        result = pipeline.expand(
            scope=None,
            subject=None,
            node_limit=1,
            auto_promote=True,
            dry_run=False,
        )
        self.assertEqual(result["result"]["error"], 0)
        self.assertEqual(result["result"]["generated"], 2)
        self.assertEqual(result["result"]["final"], 1)
        self.assertEqual(result["result"]["invalid"], 1)
        with self.state.connect() as conn:
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status,COUNT(*) AS n FROM questions "
                    "WHERE source_kind='generated' GROUP BY status"
                )
            }
        self.assertEqual(by_status, {"final": 1, "invalid": 1})
        source_rows, errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(errors)
        self.assertEqual(len(source_rows), 14)

    def test_review_server_serves_ui_and_api(self) -> None:
        qb.scan_bank(self.state)
        app = qb.ReviewApplication(self.state, model=None, max_agent_processes=3)
        handler = type("TestReviewHandler", (qb.ReviewHandler,), {"app": app})
        try:
            server = qb.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except PermissionError:
            self.skipTest("当前 sandbox 不允许绑定 localhost socket")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/summary", timeout=5) as response:
                summary = json.loads(response.read().decode("utf-8"))
            self.assertEqual(summary["total"], 2)
            with urllib.request.urlopen(base + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("题库共识审校台", html)
            with urllib.request.urlopen(base + "/app.js", timeout=5) as response:
                script = response.read().decode("utf-8")
            self.assertIn("resolveAndMaybeNext", script)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_solver_snapshot_strips_nested_option_answer_fields(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        question_value = json.loads(row["question_json"])
        question_value["options"][0]["correct"] = True
        question_value["options"][0]["answer"] = "A"
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE questions SET question_json=? WHERE question_key=?",
                (qb.compact_json(question_value), row["question_key"]),
            )
        snapshot = qb.sanitized_question(qb.get_question_row(self.state, row["question_key"]))
        self.assertEqual(set(snapshot["options"][0]), {"id", "text"})
        self.assertNotIn("answer", snapshot)
        self.assertNotIn("explanation", snapshot)

    def test_hong_kong_node_adds_traditional_language_contract(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE questions SET node_dir=? WHERE question_key=?",
                ("hk-hongkong-1-2026/数学/中五數學卷一_1", row["question_key"]),
            )
        snapshot = qb.sanitized_question(
            qb.get_question_row(self.state, row["question_key"])
        )
        self.assertEqual(snapshot["language_variant"], "zh-Hant-HK")
        self.assertTrue(set(snapshot).issubset(qb.SOLVER_QUESTION_FIELDS))
        self.assertEqual(
            qb.language_variant_for_node("cn-shanghai-2-2026/物理/node-1"),
            "zh-Hans-CN",
        )

    def test_bank_validator_receives_derived_hong_kong_locale(self) -> None:
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no, *, locale=None):\n"
            "    return [] if locale == 'zh-Hant-HK' else ['wrong-locale']\n",
        )
        qb.validate_with_bank_contract(
            self.state,
            question("hk-locale"),
            node_dir="hk-hongkong-1-2026/数学/中五數學卷一_1",
        )
        with self.assertRaises(qb.ManagerError):
            qb.validate_with_bank_contract(
                self.state,
                question("sh-locale"),
                node_dir="cn-shanghai-2-2026/物理/node-1",
            )

    def test_pagination_reaches_question_301(self) -> None:
        rows = [question(f"q-{index:03d}") for index in range(1, 302)]
        qb.atomic_write_jsonl(self.node / "questions.jsonl", rows)
        qb.scan_bank(self.state)
        page = qb.list_questions(
            self.state,
            statuses=[],
            subject="",
            query="",
            limit=100,
            offset=300,
        )
        self.assertEqual(page["total"], 301)
        self.assertEqual(len(page["items"]), 1)

    def test_review_buckets_split_seed_from_other_disagreements(self) -> None:
        qb.atomic_write_jsonl(
            self.node / "questions.jsonl",
            [question("pb_物理_node-1_seed_001"), question("pb_物理_node-1_gen_001")],
        )
        qb.scan_bank(self.state)
        with self.state.connect() as conn:
            conn.execute("UPDATE questions SET status='disagreement'")
        seed = qb.list_questions(
            self.state,
            statuses=["disagreement"],
            subject="",
            query="",
            limit=100,
            offset=0,
            review_bucket="seed",
        )
        candidate = qb.list_questions(
            self.state,
            statuses=["disagreement"],
            subject="",
            query="",
            limit=100,
            offset=0,
            review_bucket="candidate",
        )
        self.assertEqual([item["id"] for item in seed["items"]], ["pb_物理_node-1_seed_001"])
        self.assertEqual([item["id"] for item in candidate["items"]], ["pb_物理_node-1_gen_001"])

    def test_portable_review_queue_restores_and_resolves_excluded_seed(self) -> None:
        seed_id = "pb_物理_node-1_seed_001"
        generated_id = "pb_物理_node-1_gen_001"
        qb.atomic_write_jsonl(
            self.node / "questions.jsonl",
            [question(seed_id), question(generated_id)],
        )
        qb.scan_bank(self.state)
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner(disagree=True)
        run_id = qb.new_run_id("portable-queue")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        counts = pipeline.audit_rows(self.rows(), run_id=run_id, run_dir=run_dir)
        self.assertEqual(counts["disagreement"], 2)
        exported = qb.export_unresolved(self.state)
        self.assertEqual(exported["portable_review_queue"], 2)

        queue_path = qb.portable_review_queue_path(self.state)
        queue_rows, queue_errors = qb.read_jsonl(queue_path)
        self.assertFalse(queue_errors)
        self.assertEqual({item["id"] for item in queue_rows}, {seed_id, generated_id})
        self.assertTrue(all(item["review_queue_schema_version"] == 1 for item in queue_rows))

        # Simulate a clean Git checkout: disputed rows are absent from the
        # authoritative source and the local SQLite state is new.
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [])
        fresh = qb.State(self.bank, self.bank / ".fresh-review")
        fresh.ensure()
        report = qb.scan_bank(fresh)
        self.assertFalse(report["errors"], report["errors"])
        self.assertEqual(report["portable_review_queue"]["records_seen"], 2)
        with fresh.connect() as conn:
            restored = list(conn.execute("SELECT * FROM questions ORDER BY qid"))
            attempt_count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            review_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        self.assertEqual(len(restored), 2)
        self.assertEqual(attempt_count, 6)
        self.assertEqual(review_count, 2)
        by_id = {row["qid"]: row for row in restored}
        self.assertEqual(by_id[seed_id]["source_kind"], "seed_review")
        self.assertEqual(by_id[generated_id]["source_kind"], "generated")

        # Repeated scans are idempotent and do not duplicate evidence.
        qb.scan_bank(fresh)
        with fresh.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 2)

        seed_page = qb.list_questions(
            fresh,
            statuses=["disagreement", "invalid", "error"],
            subject="",
            query="",
            limit=100,
            offset=0,
            review_bucket="seed",
        )
        self.assertEqual([item["id"] for item in seed_page["items"]], [seed_id])
        seed_key = seed_page["items"][0]["question_key"]
        detail = qb.question_detail(fresh, seed_key)
        self.assertEqual(len(detail["attempts"]), 3)
        self.assertIsNotNone(detail["review"])
        qb.accept_review_choice(
            fresh,
            seed_key,
            source="solver1",
            requested_run_id=detail["current_run_id"],
        )
        qb.export_unresolved(fresh)
        source_rows, source_errors = qb.read_jsonl(self.node / "questions.jsonl")
        self.assertFalse(source_errors)
        self.assertEqual([item["id"] for item in source_rows], [seed_id])
        remaining, remaining_errors = qb.read_jsonl(queue_path)
        self.assertFalse(remaining_errors)
        self.assertEqual([item["id"] for item in remaining], [generated_id])

    def test_stale_portable_queue_never_demotes_a_final_seed(self) -> None:
        seed_id = "pb_物理_node-1_seed_001"
        qb.atomic_write_jsonl(self.node / "questions.jsonl", [question(seed_id)])
        qb.scan_bank(self.state)
        row = self.rows()[0]
        qb.accept_final(
            self.state,
            row["question_key"],
            run_id="accepted-run",
            source="human_accept:custom",
            answer="B",
            solution="由速度定义计算并代回检查，故选B。",
        )
        stale_record = {
            "review_queue_schema_version": 1,
            "question_key": row["question_key"],
            "id": seed_id,
            "node_dir": row["node_dir"],
            "question_file": row["question_file"],
            "subject": row["subject"],
            "source_kind": "seed_review",
            "status": "invalid",
            "question": question(seed_id),
            "attempts": [],
            "teacher_review": None,
            "updated_at": qb.utc_now(),
        }
        qb.atomic_write_jsonl(qb.portable_review_queue_path(self.state), [stale_record])
        fresh = qb.State(self.bank, self.bank / ".fresh-final-state")
        fresh.ensure()
        report = qb.scan_bank(fresh)
        self.assertFalse(report["errors"], report["errors"])
        restored = qb.get_question_row(fresh, row["question_key"])
        self.assertEqual(restored["status"], "final")
        self.assertEqual(report["portable_review_queue"]["resolved_records_skipped"], 1)

    def test_scoped_export_preserves_unscanned_portable_records(self) -> None:
        outside_node = self.bank / "other-school" / "物理" / "node-2"
        outside_node.mkdir(parents=True)
        outside_question = question("pb_物理_node-2_seed_001")
        outside_question["nodeId"] = "node-2"
        qb.atomic_write_jsonl(outside_node / "questions.jsonl", [])
        outside_file = qb.safe_rel(outside_node / "questions.jsonl", self.bank)
        outside_key = qb.question_key(outside_file, outside_question["id"])
        portable = {
            "review_queue_schema_version": 1,
            "question_key": outside_key,
            "id": outside_question["id"],
            "node_dir": qb.safe_rel(outside_node, self.bank),
            "question_file": outside_file,
            "subject": "物理",
            "source_kind": "seed_review",
            "status": "invalid",
            "question": outside_question,
            "attempts": [],
            "teacher_review": None,
            "updated_at": qb.utc_now(),
        }
        qb.atomic_write_jsonl(qb.portable_review_queue_path(self.state), [portable])
        qb.scan_bank(self.state, "school/物理/node-1")
        result = qb.export_unresolved(self.state)
        self.assertEqual(result["portable_review_queue"], 1)
        remaining, errors = qb.read_jsonl(qb.portable_review_queue_path(self.state))
        self.assertFalse(errors)
        self.assertEqual([item["question_key"] for item in remaining], [outside_key])

    def test_review_ui_defaults_seed_queue_to_all_human_review_statuses(self) -> None:
        app = (qb.ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        html = (qb.ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('seed: "disagreement,invalid,error"', app)
        self.assertIn('candidate: "disagreement"', app)
        self.assertIn('value="disagreement,invalid,error">待人工审查（默认）', html)

    def test_solution_skill_versioning_dedup_and_user_guided_revision(self) -> None:
        event = qb.record_solution_skill(
            self.state,
            candidate=solution_skill_candidate(),
            source={"kind": "fixture", "question_key": "q"},
            verification_run_id="fixture-run",
            activate=True,
        )
        self.assertIsNotNone(event)
        skill_id = event["skill_id"]
        detail = qb.solution_skill_detail(self.state, skill_id)
        self.assertEqual(detail["current_version"], 1)
        self.assertEqual(qb.sha256_text(detail["content"]), detail["current_sha256"])
        duplicate = qb.record_solution_skill(
            self.state,
            candidate=solution_skill_candidate(),
            source={"kind": "duplicate-fixture"},
            verification_run_id="duplicate-run",
            activate=True,
        )
        self.assertIsNone(duplicate)
        self.assertEqual(qb.list_solution_skills(self.state, query="", limit=50, offset=0)["total"], 1)

        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=1)
        pipeline.runner = FakeSkillRunner()
        coordinator = qb.JobCoordinator(self.state, pipeline)
        coordinator.executor.shutdown(wait=True)
        capture = CapturingExecutor()
        coordinator.executor = capture
        queued = coordinator.enqueue_skill_revision(
            skill_id,
            base_sha256=detail["current_sha256"],
            guidance="增加边界趋势检查，并保留原来的单位与代回复核。",
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(len(capture.calls), 1)
        function, args = capture.calls[0]
        function(*args)
        revised = qb.solution_skill_detail(self.state, skill_id)
        self.assertEqual(revised["current_version"], 2)
        self.assertIn("边界趋势", revised["content"])
        self.assertEqual(revised["versions"][0]["status"], "active")
        with self.state.connect() as conn:
            job = conn.execute(
                "SELECT * FROM skill_jobs WHERE job_id=?", (queued["job_id"],)
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        with self.assertRaisesRegex(qb.ManagerError, "最新 SHA"):
            coordinator.enqueue_skill_revision(
                skill_id,
                base_sha256=detail["current_sha256"],
                guidance="这是基于旧版本的请求",
            )
        stale_candidate = solution_skill_candidate("旧基线更新")
        stale_candidate.update(
            {"action": "update", "related_skill_id": skill_id}
        )
        with self.assertRaisesRegex(qb.ManagerError, "基线 SHA"):
            qb.record_solution_skill(
                self.state,
                candidate=stale_candidate,
                source={"kind": "stale-race-fixture"},
                verification_run_id="stale-run",
                activate=True,
                expected_base_sha256=detail["current_sha256"],
            )
        verified = qb.verify_state(self.state)
        self.assertTrue(verified["ok"], verified["errors"])

    def test_solution_skill_novelty_is_metadata_not_activation_gate(self) -> None:
        candidate = solution_skill_candidate("非新颖但可复用的定义式复核")
        candidate["novel"] = False
        candidate["novelty_rationale"] = "不是新方法，但适用于多种定义式比例计算。"
        event = qb.record_solution_skill(
            self.state,
            candidate=candidate,
            source={"kind": "lowered-threshold-fixture"},
            verification_run_id="lowered-threshold-run",
            activate=True,
        )
        self.assertIsNotNone(event)
        detail = qb.solution_skill_detail(self.state, str(event["skill_id"]))
        self.assertFalse(detail["metadata"]["novel"])

    def test_historical_skill_rejects_solution_copy_and_unlinked_lineage(self) -> None:
        copied_fragment = "先建立统一坐标并逐项列出所有外力及其方向再开始计算"
        evidence = {
            "key-1": {
                "display_id": "source-q-1",
                "node_dir": "school/物理/node-a",
                "source_kind": "generated",
                "question": {"prompt": "第一道用于证据检查的完整物理题干文本。", "options": []},
                "verified_answer": "B",
                "verified_solution": copied_fragment + "。随后用另一种方法复核结果。",
            },
            "key-2": {
                "display_id": "source-q-2",
                "node_dir": "school/物理/node-b",
                "source_kind": "existing",
                "question": {"prompt": "第二道用于证据检查的完整物理题干文本。", "options": []},
                "verified_answer": "C",
                "verified_solution": "独立列式求解，并检查单位、方向与边界条件是否一致。",
            },
        }
        copied_candidate = solution_skill_candidate("包含原解片段的候选")
        copied_candidate["ordered_steps"][0] = copied_fragment
        copied_payload = {
            "review_summary": "fixture",
            "skill_candidates": [
                {
                    "source_question_keys": ["key-1", "key-2"],
                    "source_candidate_ids": [],
                    "reuse_rationale": "fixture",
                    "skill_candidate": copied_candidate,
                }
            ],
        }
        accepted, rejected = qb.validate_historical_skill_candidates(
            copied_payload,
            evidence_by_key=evidence,
            existing_skill_ids=set(),
            min_source_questions=2,
            min_source_nodes=2,
        )
        self.assertFalse(accepted)
        self.assertIn("过长原解片段", rejected[0]["error"])

        clean_candidate = solution_skill_candidate("有精确候选血缘的通用方法")
        lineage = {
            "batch-1-candidate-01": {"source_question_keys": ["key-1"]},
            "batch-2-candidate-01": {"source_question_keys": ["key-2"]},
        }
        lineage_payload = {
            "review_summary": "fixture",
            "skill_candidates": [
                {
                    "source_question_keys": ["key-1", "key-2"],
                    "source_candidate_ids": ["batch-1-candidate-01"],
                    "reuse_rationale": "fixture",
                    "skill_candidate": clean_candidate,
                }
            ],
        }
        accepted, rejected = qb.validate_historical_skill_candidates(
            lineage_payload,
            evidence_by_key=evidence,
            existing_skill_ids=set(),
            lineage_candidates=lineage,
        )
        self.assertFalse(accepted)
        self.assertIn("未由所声明的候选血缘支持", rejected[0]["error"])
        lineage_payload["skill_candidates"][0]["source_candidate_ids"].append(
            "batch-2-candidate-01"
        )
        accepted, rejected = qb.validate_historical_skill_candidates(
            lineage_payload,
            evidence_by_key=evidence,
            existing_skill_ids=set(),
            lineage_candidates=lineage,
        )
        self.assertEqual(len(accepted), 1)
        self.assertFalse(rejected)

    def test_historical_skill_curation_covers_all_final_rows_and_consolidates(self) -> None:
        qb.scan_bank(self.state)
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE questions SET status='final',teacher_answer='B',"
                "teacher_solution='先统一单位，由定义式求值，再用逆运算代回。',"
                "teacher_verdict='pass',current_run_id='fixture-final'"
            )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=1)
        pipeline.runner = FakeHistorySkillRunner()
        result = pipeline.curate_solution_skills(
            targets=["school/物理"],
            subject="物理",
            character_budget=20_000,
            min_source_questions=2,
            min_source_nodes=1,
            dry_run=False,
        )
        self.assertEqual(result["scope_questions"], 2)
        self.assertEqual(result["final_evidence_questions"], 2)
        self.assertEqual(result["excluded_nonfinal_questions"], 0)
        self.assertEqual(result["activated_or_updated"], 1)
        with self.state.connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM solution_skills WHERE status='active'"
            ).fetchone()
        metadata = json.loads(row["metadata_json"])
        self.assertEqual(metadata["source"]["kind"], "historical_verified_solution_curation")
        self.assertEqual(len(metadata["source"]["source_question_keys"]), 2)
        run_dir = self.state.runs_dir / result["run_id"]
        coverage = json.loads((run_dir / "final-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(len(coverage), 2)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest.get("finished_at"))

    def test_review_queries_are_limited_to_server_scope(self) -> None:
        other = self.bank / "other-school" / "物理" / "node-2"
        other.mkdir(parents=True)
        qb.atomic_write_jsonl(other / "questions.jsonl", [question("q-outside")])
        qb.scan_bank(self.state)
        allowed = {qb.safe_rel(self.node, self.bank)}

        page = qb.list_questions(
            self.state,
            statuses=[],
            subject="",
            query="",
            limit=100,
            offset=0,
            node_dirs=allowed,
        )
        scoped_summary = qb.summary(self.state, allowed)
        self.assertEqual(page["total"], 2)
        self.assertEqual(scoped_summary["total"], 2)
        self.assertEqual(scoped_summary["subjects"], {"物理": 2})

    def test_fixed_review_view_limits_list_summary_and_detail(self) -> None:
        qb.scan_bank(self.state)
        rows = self.rows()
        allowed_keys = {str(rows[0]["question_key"])}
        page = qb.list_questions(
            self.state,
            statuses=[],
            subject="",
            query="",
            limit=100,
            offset=0,
            question_keys=allowed_keys,
        )
        scoped_summary = qb.summary(
            self.state,
            question_keys=allowed_keys,
            review_title="正式 Seed 分歧题",
        )
        app = qb.ReviewApplication(
            self.state,
            model=None,
            max_agent_processes=3,
            allowed_question_keys=allowed_keys,
            review_title="正式 Seed 分歧题",
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(scoped_summary["total"], 1)
        self.assertEqual(scoped_summary["review_view"]["question_count"], 1)
        self.assertEqual(app.checked_row(rows[0]["question_key"])["qid"], rows[0]["qid"])
        with self.assertRaisesRegex(qb.ManagerError, "固定审阅清单"):
            app.checked_row(rows[1]["question_key"])

    def test_orphaned_job_is_failed_and_retry_is_unblocked(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        created = qb.utc_now()
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE questions SET status='running',current_run_id='old-run' "
                "WHERE question_key=?",
                (row["question_key"],),
            )
            conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "old-job",
                    row["question_key"],
                    "resolve",
                    "running",
                    40,
                    "旧进程",
                    "",
                    "old-run",
                    None,
                    created,
                    created,
                ),
            )
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        coordinator = qb.JobCoordinator(self.state, pipeline)
        coordinator.executor.shutdown(wait=True)
        capture = CapturingExecutor()
        coordinator.executor = capture
        with self.state.connect() as conn:
            old = conn.execute("SELECT * FROM jobs WHERE job_id='old-job'").fetchone()
        self.assertEqual(old["status"], "failed")
        self.assertEqual(qb.get_question_row(self.state, row["question_key"])["status"], "error")
        queued = coordinator.enqueue_resolve(row["question_key"], "重新检查")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["question_id"], row["qid"])
        self.assertEqual(len(capture.calls), 1)
        self.assertTrue(any(job["job_id"] == "old-job" for job in qb.summary(self.state)["recent_jobs"]))

    def test_stale_run_cannot_accept_current_candidate(self) -> None:
        qb.scan_bank(self.state)
        row = self.rows()[0]
        pipeline = qb.Pipeline(self.state, model=None, max_agent_processes=3)
        pipeline.runner = FakeRunner(disagree=True)
        run_id = qb.new_run_id("current")
        run_dir = pipeline.create_manifest(run_id, "test", {"fixture": True})
        pipeline.audit_rows([row], run_id=run_id, run_dir=run_dir)
        with self.assertRaisesRegex(qb.ManagerError, "run 已过期"):
            qb.accept_review_choice(
                self.state,
                row["question_key"],
                source="solver3",
                requested_run_id="stale-run",
            )
        finals, _ = qb.read_jsonl(self.node / "answer_final.jsonl")
        self.assertFalse(any(item["id"] == row["qid"] for item in finals))
        accepted = qb.accept_review_choice(
            self.state,
            row["question_key"],
            source="solver3",
            requested_run_id=run_id,
        )
        self.assertEqual(accepted["answer"], "C")
        with self.state.connect() as conn:
            decision = conn.execute(
                "SELECT * FROM decisions WHERE question_key=? ORDER BY created_at DESC LIMIT 1",
                (row["question_key"],),
            ).fetchone()
        self.assertEqual(decision["run_id"], run_id)

    def test_strict_profile_denies_bank_except_own_invocation(self) -> None:
        runner = qb.AgentRunner(self.state, isolation_mode="strict", retries=0)
        invocation = self.state.runs_dir / "run-x" / "batch" / "solver1"
        invocation.mkdir(parents=True)
        schema = invocation / "output.schema.json"
        qb.atomic_write_json(schema, {"type": "object"})
        profile = runner._strict_profile(invocation, [])
        self.assertIn(f'(deny file-read-data (subpath "{self.bank}"))', profile)
        self.assertIn(f'(allow file-read-data (literal "{schema}"))', profile)
        self.assertNotIn("file-write", profile)

    def test_bank_validator_can_reject_generated_question_before_writeback(self) -> None:
        qb.atomic_write_text(
            self.bank / "validate.py",
            "def check_question(question, line_no):\n"
            "    return [f'line {line_no}: rejected-by-bank']\n",
        )
        with self.assertRaisesRegex(qb.ManagerError, "rejected-by-bank"):
            qb.validate_with_bank_contract(self.state, question("candidate"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
