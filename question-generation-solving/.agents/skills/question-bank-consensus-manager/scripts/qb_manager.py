#!/usr/bin/env python3
"""Reproducible multi-agent question-bank expansion, audit, and review server.

The runtime intentionally uses only the Python standard library. It selects the
OpenAI Responses API when ``OPENAI_API_KEY`` is present and otherwise keeps the
authenticated Codex CLI path. Credentials are read from the process environment
only and are never written to question-bank artifacts.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import fnmatch
import hashlib
import http.server
import inspect
import json
import mimetypes
import os
from pathlib import Path
import re
import runpy
import selectors
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterable, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback remains thread-safe.
    fcntl = None  # type: ignore[assignment]


SKILL_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = SKILL_ROOT / "assets" / "review-ui"
REFERENCE_ROOT = SKILL_ROOT / "references"
SCRIPT_ROOT = SKILL_ROOT / "scripts"
STATE_DIRNAME = ".qb-review"
SOLUTION_SKILLS_DIRNAME = "解题技能库"
SCHEMA_VERSION = "3"
STATUSES = ("pending", "running", "final", "disagreement", "invalid", "error")
UNRESOLVED_STATUSES = ("disagreement", "invalid", "error")
FILE_LOCK = threading.RLock()
VALIDATOR_CACHE: dict[str, Callable[..., list[str]]] = {}

SOLVER_LENSES = {
    "solver1": "从第一性原理建立模型，完整推导后用代回或守恒关系复核。",
    "solver2": "尽量采用与常规首解不同的路线，重点检查条件、符号、单位与选项唯一性。",
    "solver3": "以反例审查者视角做题，主动寻找陷阱，并用边界、量纲或极端情形复核。",
}

# One shared transport contract keeps request construction and forensic
# verification aligned.  ``language_variant`` is routing metadata (for example
# Hong Kong Traditional Chinese), so it is allowed in solver requests even
# though it is deliberately excluded from the content snapshot hash.
SOLVER_QUESTION_FIELDS = frozenset(
    {
        "id",
        "display_id",
        "subject",
        "prompt",
        "options",
        "difficulty",
        "question_type",
        "language_variant",
        "user_guidance",
        "image_attachment",
        "question_snapshot_sha256",
        "solution_skills",
        "verification_feedback",
    }
)

DEFAULT_QUOTAS = {
    "low": {"display": 3, "exam": 2},
    "mid": {"display": 3, "exam": 2},
    "high": {"display": 3, "exam": 2},
}

DISABLED_AGENT_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "enable_mcp_apps",
    "hooks",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
)

class ManagerError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_inventory(root: Path, *, exclude: Sequence[str] = ()) -> dict[str, str]:
    excluded = set(exclude)
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, pretty_json(value))


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(compact_json(row) + "\n" for row in rows)
    atomic_write_text(path, text)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with FILE_LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(compact_json(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


@contextlib.contextmanager
def advisory_file_lock(path: Path) -> Iterator[None]:
    """Serialize a small filesystem transaction across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with FILE_LOCK:
        with path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return rows, errors
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{line_no}: JSON 解析失败: {exc}")
                continue
            if not isinstance(item, dict):
                errors.append(f"{path}:{line_no}: 每行必须是 JSON object")
                continue
            rows.append(item)
    return rows, errors


def safe_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ManagerError(f"路径不在题库根目录内: {path}") from exc


def question_key(question_file_rel: str, qid: str) -> str:
    return sha256_text(f"{question_file_rel}\0{qid}")


def safe_component(value: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    if not slug:
        slug = "item"
    return slug[:max_len]


def new_run_id(prefix: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def load_prompt(name: str) -> str:
    path = REFERENCE_ROOT / name
    if not path.exists():
        raise ManagerError(f"缺少 prompt 文件: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    # Manager placeholders are deliberately multi-word SCREAMING_SNAKE_CASE.
    # Single-token braces such as LaTeX \mathrm{N} are literal prompt content.
    template_variables = set(
        re.findall(r"\{([A-Z][A-Z0-9]*_[A-Z0-9_]+)\}", template)
    )
    unknown = sorted(template_variables - set(replacements))
    if unknown:
        raise ManagerError(f"prompt 缺少替换值: {', '.join(unknown)}")
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    # Only re-check placeholders that originated in the template. Inserted
    # physics/chemistry LaTeX such as \mathrm{J} is payload, not a variable.
    unresolved = sorted(key for key in template_variables if "{" + key + "}" in rendered)
    if unresolved:
        raise ManagerError(f"prompt 尚有未替换变量: {', '.join(unresolved)}")
    return rendered


def normalize_answer(answer: Any) -> str:
    text = re.sub(r"\s+", "", str(answer or "")).strip()
    if len(text) == 1 and text.upper() in "ABCD":
        return text.upper()
    return text.replace("（", "(").replace("）", ")")


class State:
    def __init__(self, bank: Path, state_dir: Path | None = None) -> None:
        self.bank = bank.expanduser().resolve()
        if not self.bank.is_dir():
            raise ManagerError(f"题库根目录不存在: {self.bank}")
        self.root = (state_dir or (self.bank / STATE_DIRNAME)).expanduser().resolve()
        try:
            self.root.relative_to(self.bank)
        except ValueError as exc:
            raise ManagerError("状态目录必须位于题库根目录内，以避免跨目录写入") from exc
        self.db_path = self.root / "review.sqlite3"
        self.config_path = self.root / "config.json"
        self.runs_dir = self.root / "runs"
        self.decisions_path = self.root / "decisions.jsonl"
        self.ledger_path = self.root / "run-ledger.jsonl"
        self.solution_skills_root = self.bank / SOLUTION_SKILLS_DIRNAME
        self.skill_events_path = self.root / "solution-skill-events.jsonl"
        self.skill_versions_root = self.root / "solution-skill-versions"
        self.blind_rechecks_path = self.root / "blind-rechecks.jsonl"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            atomic_write_json(
                self.config_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "bank_root": str(self.bank),
                    "codex_bin": shutil.which("codex") or "codex",
                    "model": None,
                    "batch_size": 15,
                    "max_agent_processes": None,
                    "retries": 2,
                    "timeout_seconds": 1800,
                    "isolation_mode": "strict",
                    "provider": "auto",
                    "api_mode": "responses",
                    "api_model": "gpt-5.6-sol",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_max_concurrency": 9,
                    "api_batch_poll_seconds": 15,
                    "reasoning_effort": {
                        "solver": "xhigh",
                        "teacher_consensus": "xhigh",
                        "teacher_disagreement": "max",
                        "skill": "xhigh"
                    },
                    "solution_skills": {
                        "enabled": True,
                        "max_context_skills": 5,
                        "similarity_threshold": 0.68
                    },
                    "quotas": DEFAULT_QUOTAS,
                    "created_at": utc_now(),
                },
            )
        self._init_db()

    def config(self) -> dict[str, Any]:
        self.ensure()
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError(f"状态配置损坏: {self.config_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ManagerError(f"状态配置必须是 object: {self.config_path}")
        return value

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.ensure_dirs_only()
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def ensure_dirs_only(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _init_db(self) -> None:
        self.ensure_dirs_only()
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS questions (
                    question_key TEXT PRIMARY KEY,
                    qid TEXT NOT NULL,
                    node_dir TEXT NOT NULL,
                    question_file TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    question_json TEXT NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT 'existing',
                    status TEXT NOT NULL DEFAULT 'pending',
                    current_run_id TEXT,
                    teacher_answer TEXT,
                    teacher_solution TEXT,
                    teacher_verdict TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_questions_status ON questions(status);
                CREATE INDEX IF NOT EXISTS idx_questions_node ON questions(node_dir);
                CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);
                CREATE TABLE IF NOT EXISTS attempts (
                    question_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    independent_check TEXT NOT NULL,
                    question_valid INTEGER NOT NULL,
                    confidence TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    invocation_dir TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(question_key, run_id, agent_id),
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_question ON attempts(question_key, created_at);
                CREATE TABLE IF NOT EXISTS reviews (
                    question_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    answer_consistent INTEGER NOT NULL,
                    teacher_answer TEXT NOT NULL,
                    teacher_solution TEXT NOT NULL,
                    process_review TEXT NOT NULL,
                    agent_feedback_json TEXT NOT NULL,
                    auto_promote INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    invocation_dir TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(question_key, run_id),
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_question ON reviews(question_key, created_at);
                CREATE TABLE IF NOT EXISTS blind_rechecks (
                    question_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    independent_check TEXT NOT NULL,
                    question_valid INTEGER NOT NULL,
                    matched INTEGER NOT NULL,
                    question_snapshot_sha256 TEXT NOT NULL,
                    final_content_sha256 TEXT NOT NULL,
                    invocation_dir TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(question_key, run_id),
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE INDEX IF NOT EXISTS idx_blind_rechecks_question
                    ON blind_rechecks(question_key, created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    question_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    guidance TEXT NOT NULL,
                    run_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    question_key TEXT NOT NULL,
                    run_id TEXT,
                    source TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE TABLE IF NOT EXISTS question_annotations (
                    annotation_id TEXT PRIMARY KEY,
                    question_key TEXT NOT NULL,
                    run_id TEXT,
                    status TEXT NOT NULL,
                    issue_codes_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    proposed_revision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(question_key) REFERENCES questions(question_key)
                );
                CREATE INDEX IF NOT EXISTS idx_annotations_question
                    ON question_annotations(question_key, created_at);
                CREATE TABLE IF NOT EXISTS skill_jobs (
                    job_id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    guidance TEXT NOT NULL,
                    base_sha256 TEXT,
                    run_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_jobs_status
                    ON skill_jobs(status, updated_at);
                CREATE TABLE IF NOT EXISTS solution_skills (
                    skill_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    current_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_solution_skills_updated
                    ON solution_skills(updated_at);
                CREATE TABLE IF NOT EXISTS solution_skill_versions (
                    skill_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    verification_run_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(skill_id, version),
                    FOREIGN KEY(skill_id) REFERENCES solution_skills(skill_id)
                );
                CREATE INDEX IF NOT EXISTS idx_skill_versions_created
                    ON solution_skill_versions(created_at);
                """
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()


ScopeValue = str | Sequence[str] | None


def resolve_scope(bank: Path, scope: ScopeValue) -> list[Path]:
    if scope is not None and not isinstance(scope, str):
        resolved: dict[str, Path] = {}
        for item in scope:
            for path in resolve_scope(bank, str(item)):
                resolved[safe_rel(path, bank)] = path
        return [resolved[key] for key in sorted(resolved)]
    if not scope:
        base = bank
        return sorted(p for p in base.rglob("questions.jsonl") if STATE_DIRNAME not in p.parts)
    candidate = (bank / scope).resolve()
    try:
        candidate.relative_to(bank.resolve())
    except ValueError as exc:
        raise ManagerError("--scope 不得跳出题库根目录") from exc
    if candidate.is_file():
        return [candidate] if candidate.name == "questions.jsonl" else []
    if candidate.is_dir():
        return sorted(p for p in candidate.rglob("questions.jsonl") if STATE_DIRNAME not in p.parts)
    pattern = scope.replace(os.sep, "/")
    return sorted(
        p
        for p in bank.rglob("questions.jsonl")
        if STATE_DIRNAME not in p.parts and fnmatch.fnmatch(safe_rel(p.parent, bank), pattern)
    )


def normalize_target_scopes(bank: Path, targets: Sequence[str]) -> list[str]:
    """Resolve exact user-supplied directories/files without accepting silent typos."""
    bank = bank.resolve()
    normalized: dict[str, None] = {}
    for raw_target in targets:
        raw = str(raw_target).strip()
        if not raw:
            raise ManagerError("--target 不得为空")
        supplied = Path(raw)
        candidate = supplied.resolve() if supplied.is_absolute() else (bank / supplied).resolve()
        try:
            candidate.relative_to(bank)
        except ValueError as exc:
            raise ManagerError(f"--target 不得跳出题库根目录: {raw}") from exc
        if candidate.is_file():
            if candidate.name != "questions.jsonl":
                raise ManagerError(f"--target 文件必须是 questions.jsonl: {raw}")
        elif not candidate.is_dir():
            raise ManagerError(f"--target 不存在: {raw}")
        relative = safe_rel(candidate, bank)
        if not resolve_scope(bank, relative):
            raise ManagerError(f"--target 下没有 questions.jsonl: {raw}")
        normalized[relative] = None
    if not normalized:
        raise ManagerError("至少需要一个 --target")
    return sorted(normalized)


def upsert_question_row(
    state: State,
    *,
    key: str,
    qid: str,
    node_dir: str,
    question_file: str,
    subject: str,
    question: dict[str, Any],
    source_kind: str = "existing",
    status_if_new: str = "pending",
) -> None:
    with state.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO questions(
                question_key, qid, node_dir, question_file, subject,
                question_json, source_kind, status, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(question_key) DO UPDATE SET
                qid=excluded.qid,
                node_dir=excluded.node_dir,
                question_file=excluded.question_file,
                subject=excluded.subject,
                question_json=excluded.question_json,
                updated_at=excluded.updated_at
            """,
            (
                key,
                qid,
                node_dir,
                question_file,
                subject,
                compact_json(question),
                source_kind,
                status_if_new,
                utc_now(),
            ),
        )
        conn.execute("COMMIT")


def import_legacy_node(state: State, qfile: Path, keys_by_id: dict[str, str]) -> int:
    node = qfile.parent
    candidates = {
        "solver1": node / "answers1.jsonl",
        "solver2": node / "answers2.jsonl",
        "solver3": node / "answers3.jsonl",
    }
    evidence_files = [p for p in candidates.values() if p.exists()]
    review_path = node / "answer_review.jsonl"
    final_path = node / "answer_final.jsonl"
    if review_path.exists():
        evidence_files.append(review_path)
    if final_path.exists():
        evidence_files.append(final_path)
    if not evidence_files:
        return 0
    digest = hashlib.sha256()
    for path in sorted(evidence_files):
        digest.update(safe_rel(path, state.bank).encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    run_id = f"legacy-{digest.hexdigest()[:16]}"
    imported = 0
    for agent_id, path in candidates.items():
        rows, _ = read_jsonl(path)
        for item in rows:
            qid = str(item.get("id", ""))
            key = keys_by_id.get(qid)
            if not key:
                continue
            solution = str(item.get("solution") or item.get("explanation") or "")
            raw = compact_json(item)
            with state.connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO attempts(
                        question_key,run_id,agent_id,answer,solution,independent_check,
                        question_valid,confidence,raw_json,invocation_dir,prompt_sha256,
                        response_sha256,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        run_id,
                        agent_id,
                        str(item.get("answer", "")),
                        solution,
                        "legacy import",
                        1,
                        "medium",
                        raw,
                        safe_rel(node, state.bank),
                        "legacy",
                        sha256_text(raw),
                        utc_now(),
                    ),
                )
            imported += 1
    review_rows, _ = read_jsonl(review_path)
    for item in review_rows:
        # answer_review.jsonl emitted by this manager is a derived view of rows
        # already in SQLite. Re-importing it as legacy would manufacture a
        # second review without its three attempts on every later scan.
        if item.get("question_key") or item.get("run_id"):
            continue
        qid = str(item.get("id", ""))
        key = keys_by_id.get(qid)
        if not key:
            continue
        correct = bool(item.get("correct"))
        consistent = bool(item.get("answer_consistent"))
        verdict = "pass" if correct and consistent else "disagreement"
        raw = compact_json(item)
        with state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO reviews(
                    question_key,run_id,verdict,answer_consistent,teacher_answer,
                    teacher_solution,process_review,agent_feedback_json,auto_promote,
                    raw_json,invocation_dir,prompt_sha256,response_sha256,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    run_id,
                    verdict,
                    int(consistent),
                    str(item.get("teacher_answer", "")),
                    str(item.get("teacher_solution") or item.get("process_review") or ""),
                    str(item.get("process_review", "")),
                    "[]",
                    int(correct and consistent),
                    raw,
                    safe_rel(node, state.bank),
                    "legacy",
                    sha256_text(raw),
                    str(item.get("reviewed_on") or utc_now()),
                ),
            )
            if not correct or not consistent:
                conn.execute(
                    "UPDATE questions SET status='disagreement',current_run_id=?,updated_at=? "
                    "WHERE question_key=? AND status!='final'",
                    (run_id, utc_now(), key),
                )
            conn.execute("COMMIT")
    final_rows, _ = read_jsonl(final_path)
    for item in final_rows:
        qid = str(item.get("id", ""))
        key = keys_by_id.get(qid)
        if not key:
            continue
        with state.connect() as conn:
            conn.execute(
                """
                UPDATE questions SET status='final',
                    current_run_id=CASE
                        WHEN current_run_id IS NULL OR current_run_id LIKE 'legacy-%' THEN ?
                        ELSE current_run_id
                    END,
                    teacher_answer=CASE
                        WHEN current_run_id IS NULL OR current_run_id LIKE 'legacy-%' THEN ?
                        ELSE teacher_answer
                    END,
                    teacher_solution=CASE
                        WHEN current_run_id IS NULL OR current_run_id LIKE 'legacy-%' THEN ?
                        ELSE teacher_solution
                    END,
                    teacher_verdict=CASE
                        WHEN current_run_id IS NULL OR current_run_id LIKE 'legacy-%' THEN 'legacy_final'
                        ELSE teacher_verdict
                    END,
                    updated_at=?
                WHERE question_key=?
                """,
                (
                    run_id,
                    str(item.get("answer", "")),
                    str(item.get("solution") or item.get("explanation") or ""),
                    utc_now(),
                    key,
                ),
            )
    return imported


def scan_bank(state: State, scope: ScopeValue = None, subject: str | None = None) -> dict[str, Any]:
    state.ensure()
    files = resolve_scope(state.bank, scope)
    errors: list[str] = []
    questions = 0
    legacy = 0
    nodes = 0
    for qfile in files:
        rows, row_errors = read_jsonl(qfile)
        errors.extend(row_errors)
        node_rel = safe_rel(qfile.parent, state.bank)
        qfile_rel = safe_rel(qfile, state.bank)
        keys_by_id: dict[str, str] = {}
        accepted_rows = 0
        for item in rows:
            qid = str(item.get("id", "")).strip()
            if not qid:
                errors.append(f"{qfile}: 缺少 id，已跳过")
                continue
            item_subject = str(item.get("subject") or qfile.parent.parent.name)
            if subject and item_subject != subject:
                continue
            key = question_key(qfile_rel, qid)
            keys_by_id[qid] = key
            upsert_question_row(
                state,
                key=key,
                qid=qid,
                node_dir=node_rel,
                question_file=qfile_rel,
                subject=item_subject,
                question=item,
            )
            accepted_rows += 1
        if accepted_rows:
            nodes += 1
            questions += accepted_rows
            legacy += import_legacy_node(state, qfile, keys_by_id)
    with state.connect() as conn:
        by_status = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM questions GROUP BY status"
        )}
    return {
        "files": len(files),
        "nodes": nodes,
        "questions": questions,
        "legacy_attempts_seen": legacy,
        "errors": errors,
        "status": by_status,
    }


def get_question_row(state: State, key: str) -> sqlite3.Row:
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM questions WHERE question_key=?", (key,)).fetchone()
    if row is None:
        raise ManagerError(f"未知 question_key: {key}")
    return row


def language_variant_for_node(node_dir: str) -> str:
    """Derive the required natural-language variant from the cohort path."""
    normalized = str(node_dir).replace("\\", "/").lower()
    parts = [part for part in normalized.split("/") if part]
    if any(part.startswith("hk-") or "hongkong" in part for part in parts):
        return "zh-Hant-HK"
    return "zh-Hans-CN"


def sanitized_question(
    row: sqlite3.Row,
    guidance: str = "",
    *,
    solution_skills: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    question = json.loads(row["question_json"])
    raw_options = question.get("options", [])
    options = [
        {"id": option.get("id", ""), "text": option.get("text", "")}
        for option in raw_options
        if isinstance(option, dict)
    ]
    result: dict[str, Any] = {
        "id": row["question_key"],
        "display_id": row["qid"],
        "subject": row["subject"],
        "prompt": question.get("prompt", ""),
        "options": options,
        "difficulty": question.get("difficulty", ""),
        "question_type": "multiple_choice" if options else "open_response",
        "language_variant": language_variant_for_node(str(row["node_dir"])),
        "user_guidance": guidance,
        "solution_skills": list(solution_skills),
    }
    snapshot = dict(result)
    snapshot.pop("user_guidance", None)
    snapshot.pop("solution_skills", None)
    # The locale is deterministic transport metadata, not part of the public
    # question JSONL snapshot used by delivery-hash compatibility.
    snapshot.pop("language_variant", None)
    result["question_snapshot_sha256"] = sha256_text(compact_json(snapshot))
    return result


def node_question_image(state: State, node_dir: str) -> Path | None:
    node = state.bank / node_dir
    return next(
        (
            path
            for path in sorted(node.glob("question.*"))
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ),
        None,
    )


GENERATED_VARIANT_QID = re.compile(r"_(?:seed|gen)_[^/]+$")


def question_node_image(state: State, row: sqlite3.Row) -> Path | None:
    """Return the node image only when it belongs to this question.

    ``question.png`` is the source/reference image for a node. Seed and
    manager-generated variants are self-contained and must not inherit it;
    attaching that source image creates a false image/text mismatch.
    """
    if str(row["source_kind"]) == "generated":
        return None
    if GENERATED_VARIANT_QID.search(str(row["qid"])):
        return None
    return node_question_image(state, str(row["node_dir"]))


def update_question_status(
    state: State,
    keys: Sequence[str],
    status: str,
    run_id: str | None,
    *,
    verdict: str | None = None,
    answer: str | None = None,
    solution: str | None = None,
) -> None:
    if status not in STATUSES:
        raise ManagerError(f"非法状态: {status}")
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    with state.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"""
            UPDATE questions SET status=?,current_run_id=?,teacher_verdict=COALESCE(?,teacher_verdict),
                teacher_answer=COALESCE(?,teacher_answer),teacher_solution=COALESCE(?,teacher_solution),
                updated_at=? WHERE question_key IN ({placeholders})
            """,
            (status, run_id, verdict, answer, solution, utc_now(), *keys),
        )
        conn.execute("COMMIT")


def claim_question_rows(
    state: State, rows: Sequence[sqlite3.Row], run_id: str
) -> list[sqlite3.Row]:
    """Atomically claim rows unless another run already has them in progress."""
    claimed: list[str] = []
    with state.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE questions SET status='running',current_run_id=?,updated_at=?
                WHERE question_key=?
                  AND (status!='running' OR current_run_id=? OR current_run_id IS NULL)
                """,
                (run_id, utc_now(), row["question_key"], run_id),
            )
            if cursor.rowcount == 1:
                claimed.append(row["question_key"])
        conn.execute("COMMIT")
    return [get_question_row(state, key) for key in claimed]


def upsert_jsonl_by_id(path: Path, record: dict[str, Any]) -> None:
    with FILE_LOCK:
        rows, errors = read_jsonl(path)
        if errors:
            raise ManagerError("拒绝覆盖包含无效 JSONL 的文件: " + errors[0])
        target_id = str(record.get("id", ""))
        output: list[dict[str, Any]] = []
        replaced = False
        for row in rows:
            if str(row.get("id", "")) == target_id:
                if not replaced:
                    output.append(record)
                    replaced = True
            else:
                output.append(row)
        if not replaced:
            output.append(record)
        atomic_write_jsonl(path, output)


def write_final_question_to_source(
    state: State, row: sqlite3.Row, final_question: dict[str, Any]
) -> None:
    """Make questions.jsonl authoritative for every accepted final answer.

    ``answer_final.jsonl`` remains a compatibility/audit artifact, but delivery
    consumers use ``questions.jsonl``.  Existing seed questions therefore need
    the same atomic answer/explanation replacement as generated questions.
    """
    qfile = state.bank / row["question_file"]
    existing, errors = read_jsonl(qfile)
    if errors:
        raise ManagerError("无法写入最终题目，源 JSONL 已损坏: " + errors[0])
    qid = str(row["qid"])
    matching = [index for index, item in enumerate(existing) if str(item.get("id", "")) == qid]
    if len(matching) > 1:
        raise ManagerError(f"源 questions.jsonl 中 id={qid!r} 出现多次，拒绝写回")
    if not matching and row["source_kind"] != "generated":
        raise ManagerError(f"源 questions.jsonl 中找不到现有题 id={qid!r}，拒绝写回")
    if matching:
        indexed_question = json.loads(row["question_json"])
        if compact_json(existing[matching[0]]) != compact_json(indexed_question):
            raise ManagerError(
                f"源 questions.jsonl 中 id={qid!r} 已在扫描/解题后变化；"
                "拒绝覆盖，请重新 scan 并重做该题"
            )
    validate_with_bank_contract(state, final_question, node_dir=str(row["node_dir"]))
    if matching:
        existing[matching[0]] = final_question
    else:
        existing.append(final_question)
    atomic_write_jsonl(qfile, existing)


def accept_final(
    state: State,
    key: str,
    *,
    run_id: str | None,
    source: str,
    answer: str,
    solution: str,
) -> dict[str, Any]:
    answer = str(answer).strip()
    solution = str(solution).strip()
    if not answer or not solution:
        raise ManagerError("写入 final 前必须同时有 answer 和 solution")
    lock_path = state.root / "locks" / "writeback.lock"
    with advisory_file_lock(lock_path):
        row = get_question_row(state, key)
        current_run_id = str(row["current_run_id"] or "") or None
        if run_id and current_run_id and run_id != current_run_id:
            raise ManagerError("写回前 run 已变化；拒绝覆盖较新的结果")
        final_question = json.loads(row["question_json"])
        final_question["answer"] = answer
        final_question["explanation"] = solution
        validate_with_bank_contract(state, final_question, node_dir=str(row["node_dir"]))
        final_path = state.bank / row["node_dir"] / "answer_final.jsonl"
        final_rows, final_errors = read_jsonl(final_path)
        if final_errors:
            raise ManagerError("拒绝覆盖包含无效 JSONL 的 answer_final: " + final_errors[0])
        if sum(1 for item in final_rows if str(item.get("id", "")) == row["qid"]) > 1:
            raise ManagerError(f"answer_final.jsonl 中 id={row['qid']!r} 出现多次，拒绝写回")
        write_final_question_to_source(state, row, final_question)
        canonical = {"id": row["qid"], "answer": answer, "solution": solution}
        upsert_jsonl_by_id(final_path, canonical)
        decision_id = uuid.uuid4().hex
        created = utc_now()
        with state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE questions SET status='final',current_run_id=?,question_json=?,
                    teacher_answer=?,teacher_solution=?,teacher_verdict=?,updated_at=?
                WHERE question_key=?
                """,
                (run_id, compact_json(final_question), answer, solution, source, created, key),
            )
            conn.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                (decision_id, key, run_id, source, answer, solution, created),
            )
            conn.execute("COMMIT")
        audit = {
            "decision_id": decision_id,
            "question_key": key,
            "id": row["qid"],
            "node_dir": row["node_dir"],
            "run_id": run_id,
            "source": source,
            "answer": answer,
            "solution_sha256": sha256_text(solution),
            "written_to": safe_rel(final_path, state.bank),
            "authoritative_question_file": str(row["question_file"]),
            "created_at": created,
        }
        append_jsonl(state.decisions_path, audit)
    return canonical


class AgentRunner:
    def __init__(
        self,
        state: State,
        *,
        model: str | None = None,
        max_processes: int | None = None,
        retries: int | None = None,
        timeout_seconds: int | None = None,
        isolation_mode: str | None = None,
        provider: str | None = None,
        api_mode: str | None = None,
        environ: dict[str, str] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        config = state.config()
        env = dict(os.environ if environ is None else environ)
        self.state = state
        self.codex_bin = str(config.get("codex_bin") or shutil.which("codex") or "codex")
        self.provider_requested = str(provider or config.get("provider") or "auto")
        if self.provider_requested not in {"auto", "api", "codex-cli"}:
            raise ManagerError("provider 只允许 auto、api 或 codex-cli")
        self.api_key = str(env.get("OPENAI_API_KEY") or "").strip()
        self.provider = (
            "api" if self.provider_requested == "auto" and self.api_key
            else "codex-cli" if self.provider_requested == "auto"
            else self.provider_requested
        )
        if self.provider == "api" and not self.api_key:
            raise ManagerError("选择 API provider 但环境变量 OPENAI_API_KEY 未设置")
        self.api_mode = str(api_mode or config.get("api_mode") or "responses")
        if self.api_mode == "sync":
            self.api_mode = "responses"
        if self.api_mode not in {"responses", "batch"}:
            raise ManagerError("api_mode 只允许 responses 或 batch")
        self.api_base_url = str(
            config.get("api_base_url") or "https://api.openai.com/v1"
        ).rstrip("/")
        self._urlopen = urlopen or urllib.request.urlopen
        configured_model = model if model is not None else config.get("model")
        self.model = (
            str(configured_model or config.get("api_model") or "gpt-5.6-sol")
            if self.provider == "api"
            else configured_model
        )
        self.retries = int(retries if retries is not None else config.get("retries", 2))
        self.timeout_seconds = int(
            timeout_seconds if timeout_seconds is not None else config.get("timeout_seconds", 1800)
        )
        self.isolation_mode = str(
            isolation_mode if isolation_mode is not None else config.get("isolation_mode", "strict")
        )
        if self.isolation_mode not in {"strict", "soft"}:
            raise ManagerError("isolation_mode 只允许 strict 或 soft")
        if max_processes is None:
            if self.provider == "api":
                max_processes = int(config.get("api_max_concurrency") or 9)
            else:
                max_processes = int(config.get("max_agent_processes") or 3)
        self.max_processes = max(1, int(max_processes))
        self.semaphore = threading.BoundedSemaphore(self.max_processes)
        efforts = config.get("reasoning_effort")
        self.reasoning_efforts = {
            "solver": "xhigh",
            "teacher_consensus": "xhigh",
            "teacher_disagreement": "max",
            "skill": "xhigh",
            **(efforts if isinstance(efforts, dict) else {}),
        }
        self.cli_version = self._cli_version() if self.provider == "codex-cli" else "not-used"
        self.execution_mode = self.api_mode if self.provider == "api" else "ephemeral-cli"
        self.batch_poll_seconds = max(1, int(config.get("api_batch_poll_seconds") or 15))

    @staticmethod
    def strict_isolation_backend() -> str | None:
        seatbelt = Path("/usr/bin/sandbox-exec")
        if sys.platform == "darwin" and seatbelt.is_file():
            return str(seatbelt)
        return None

    @staticmethod
    def _seatbelt_path(path: Path) -> str:
        return json.dumps(str(path.resolve()), ensure_ascii=False)

    def _strict_profile(self, invocation_dir: Path, images: Sequence[Path]) -> str:
        bank = self._seatbelt_path(self.state.bank)
        rules = [
            "(version 1)",
            "(allow default)",
            f"(deny file-read-data (subpath {bank}))",
        ]
        # Seatbelt's literal rule is more specific than the parent subpath deny.
        # Enumerating current invocation artifacts avoids granting access to any
        # sibling solver directory while still letting Codex read its schema.
        allowed_files = [path for path in invocation_dir.rglob("*") if path.is_file()]
        allowed_files.extend(image for image in images if image.exists())
        for path in sorted({path.resolve() for path in allowed_files}):
            rules.append(f"(allow file-read-data (literal {self._seatbelt_path(path)}))")
        return "\n".join(rules) + "\n"

    def _launch_command(
        self,
        command: list[str],
        *,
        invocation_dir: Path,
        attempt_dir: Path,
        images: Sequence[Path],
    ) -> tuple[list[str], dict[str, Any]]:
        if self.isolation_mode == "soft":
            return command, {
                "isolation_mode": "soft",
                "isolation_backend": "codex-read-only-plus-ephemeral-context",
            }
        backend = self.strict_isolation_backend()
        if not backend:
            raise ManagerError(
                "严格隔离不可用：当前平台没有 macOS sandbox-exec；"
                "请在受控容器/VM 中运行，或明确传 --isolation soft 接受软隔离"
            )
        profile_path = attempt_dir / "seatbelt.sb"
        atomic_write_text(profile_path, self._strict_profile(invocation_dir, images))
        return [backend, "-f", str(profile_path), *command], {
            "isolation_mode": "strict",
            "isolation_backend": "macOS-seatbelt",
            "isolation_profile": safe_rel(profile_path, self.state.root),
            "isolation_profile_sha256": sha256_file(profile_path),
            "isolation_policy": (
                "deny bank file contents except enumerated own invocation artifacts and images; "
                "Codex read-only sandbox blocks writes"
            ),
        }

    def _cli_version(self) -> str:
        try:
            result = subprocess.run(
                [self.codex_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return (result.stdout or result.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable: {exc}"

    def run(
        self,
        *,
        role: str,
        prompt: str,
        schema_path: Path,
        invocation_dir: Path,
        request: dict[str, Any],
        images: Sequence[Path] = (),
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.provider == "api":
            if self.api_mode == "batch":
                return self._run_api_batch_single(
                    role=role,
                    prompt=prompt,
                    schema_path=schema_path,
                    invocation_dir=invocation_dir,
                    request=request,
                    images=images,
                    progress=progress,
                )
            return self._run_api(
                role=role,
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
                images=images,
                progress=progress,
            )
        return self._run_cli(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            progress=progress,
        )

    def _run_cli(
        self,
        *,
        role: str,
        prompt: str,
        schema_path: Path,
        invocation_dir: Path,
        request: dict[str, Any],
        images: Sequence[Path] = (),
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        invocation_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(invocation_dir / "prompt.md", prompt)
        atomic_write_json(invocation_dir / "request.json", request)
        local_schema = invocation_dir / "output.schema.json"
        shutil.copyfile(schema_path, local_schema)
        base_meta = {
            "role": role,
            "contract_version": SCHEMA_VERSION,
            "provider_requested": self.provider_requested,
            "provider_resolved": self.provider,
            "execution_mode": self.execution_mode,
            "model_requested": self.model,
            "codex_cli_version": self.cli_version,
            "prompt_sha256": sha256_text(prompt),
            "request_sha256": sha256_text(compact_json(request)),
            "schema_sha256": sha256_file(local_schema),
            "images": [str(p) for p in images],
            "disabled_agent_features": list(DISABLED_AGENT_FEATURES),
            "isolation_mode": self.isolation_mode,
            "created_at": utc_now(),
        }
        atomic_write_json(invocation_dir / "invocation.json", base_meta)
        last_error = ""
        for attempt_no in range(1, self.retries + 2):
            attempt_dir = invocation_dir / f"try-{attempt_no:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            response_path = attempt_dir / "response.json"
            events_path = attempt_dir / "events.jsonl"
            stderr_path = attempt_dir / "stderr.log"
            codex_command = [
                self.codex_bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
            ]
            for feature in DISABLED_AGENT_FEATURES:
                codex_command.extend(["--disable", feature])
            codex_command.extend([
                "--sandbox",
                "read-only",
                "--cd",
                str(invocation_dir),
                "--output-schema",
                str(local_schema),
                "--output-last-message",
                str(response_path),
            ])
            if self.model:
                codex_command.extend(["--model", str(self.model)])
            for image in images:
                if image.exists():
                    codex_command.extend(["--image", str(image.resolve())])
            codex_command.append("-")
            command, isolation_meta = self._launch_command(
                codex_command,
                invocation_dir=invocation_dir,
                attempt_dir=attempt_dir,
                images=images,
            )
            started = utc_now()
            if progress:
                progress(f"{role} 第 {attempt_no} 次调用已启动")
            with self.semaphore:
                exit_code, timed_out = self._stream_process(
                    command,
                    prompt,
                    events_path,
                    stderr_path,
                    progress=progress,
                )
            finished = utc_now()
            try:
                raw_response = response_path.read_text(encoding="utf-8").strip()
                payload = json.loads(raw_response) if raw_response else None
            except (OSError, json.JSONDecodeError) as exc:
                payload = None
                raw_response = ""
                last_error = f"无法读取结构化响应: {exc}"
            success = exit_code == 0 and isinstance(payload, dict)
            response_sha256 = sha256_file(response_path) if response_path.exists() else sha256_text(raw_response)
            attempt_meta = {
                **base_meta,
                **isolation_meta,
                "attempt": attempt_no,
                "command": command[:-1] + ["<prompt-via-stdin>"],
                "started_at": started,
                "finished_at": finished,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "response_sha256": response_sha256,
                "events_sha256": sha256_file(events_path) if events_path.exists() else None,
                "stderr_sha256": sha256_file(stderr_path) if stderr_path.exists() else None,
                "thread_id": self._thread_id(events_path),
                "success": success,
            }
            atomic_write_json(attempt_dir / "meta.json", attempt_meta)
            if success:
                if progress:
                    progress(f"{role} 已完成")
                return payload, attempt_meta
            if timed_out:
                last_error = f"{role} 超过 {self.timeout_seconds}s"
            elif exit_code != 0:
                last_error = f"{role} 退出码 {exit_code}"
            if attempt_no <= self.retries:
                delay = min(30, 2 ** (attempt_no - 1))
                if progress:
                    progress(f"{role} 调用失败，{delay}s 后重试")
                time.sleep(delay)
        raise ManagerError(f"{role} 多次调用失败: {last_error}")

    @staticmethod
    def _api_schema_name(role: str) -> str:
        """Return a Responses-compatible, stable schema name."""
        component = re.sub(r"[^A-Za-z0-9_-]+", "_", role).strip("_") or "agent"
        return f"qb_{component}"[:64]

    def _api_reasoning_effort(self, role: str, request: dict[str, Any]) -> str | None:
        if "solver" in role:
            key = "solver"
        elif role == "teacher":
            answer_groups: list[list[str]] = []
            candidates = request.get("candidates")
            if isinstance(candidates, list):
                for group in candidates:
                    if not isinstance(group, dict):
                        continue
                    solutions = group.get("solutions")
                    if not isinstance(solutions, list):
                        continue
                    answers = [
                        normalize_answer(item.get("answer"))
                        for item in solutions
                        if isinstance(item, dict)
                    ]
                    answer_groups.append([answer for answer in answers if answer])
            has_disagreement = any(len(set(answers)) > 1 for answers in answer_groups)
            key = "teacher_disagreement" if has_disagreement else "teacher_consensus"
        else:
            key = "skill"
        effort = self.reasoning_efforts.get(key)
        return str(effort) if effort else None

    def _redact_api_secret(self, text: str) -> str:
        """Keep credentials out of diagnostic artifacts even on unusual failures."""
        if self.api_key:
            return text.replace(self.api_key, "<redacted>")
        return text

    def _redact_api_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_api_secret(value)
        if isinstance(value, list):
            return [self._redact_api_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): self._redact_api_value(item)
                for key, item in value.items()
            }
        return value

    def _api_image_items(
        self, images: Sequence[Path]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        metadata: list[dict[str, Any]] = []
        supported = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        for image in images:
            if not image.is_file():
                raise ManagerError(f"图片不存在或不是文件: {image}")
            resolved = image.resolve()
            mime_type = (mimetypes.guess_type(resolved.name)[0] or "").lower()
            if mime_type not in supported:
                raise ManagerError(f"不支持的图片类型 {mime_type or '未知'}: {image}")
            raw = resolved.read_bytes()
            items.append(
                {
                    "type": "input_image",
                    "image_url": (
                        f"data:{mime_type};base64,"
                        + base64.b64encode(raw).decode("ascii")
                    ),
                    "detail": "auto",
                }
            )
            metadata.append(
                {
                    "path": str(resolved),
                    "mime_type": mime_type,
                    "bytes": len(raw),
                    "sha256": sha256_bytes(raw),
                    "transport": "data-url",
                }
            )
        return items, metadata

    def _prepare_api_invocation(
        self,
        *,
        role: str,
        prompt: str,
        schema_path: Path,
        invocation_dir: Path,
        request: dict[str, Any],
        images: Sequence[Path],
        batch_scope: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        invocation_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(invocation_dir / "prompt.md", prompt)
        atomic_write_json(invocation_dir / "request.json", request)
        local_schema = invocation_dir / "output.schema.json"
        shutil.copyfile(schema_path, local_schema)
        try:
            loaded_schema = json.loads(local_schema.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError(f"无法读取 API 输出 schema: {exc}") from exc
        if not isinstance(loaded_schema, dict):
            raise ManagerError("API 输出 schema 顶层必须是 JSON object")
        # `$schema` is useful in the local audit copy but is not part of the
        # supported Structured Outputs subset sent to the provider.
        api_schema = dict(loaded_schema)
        api_schema.pop("$schema", None)
        image_items, image_metadata = self._api_image_items(images)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend(image_items)
        body: dict[str, Any] = {
            "model": str(self.model),
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": self._api_schema_name(role),
                    "strict": True,
                    "schema": api_schema,
                }
            },
            "store": False,
        }
        effort = self._api_reasoning_effort(role, request)
        if effort:
            body["reasoning"] = {"effort": effort}
        provider_request_path = invocation_dir / "provider-request.json"
        atomic_write_json(provider_request_path, body)
        base_meta: dict[str, Any] = {
            "role": role,
            "contract_version": SCHEMA_VERSION,
            "provider_requested": self.provider_requested,
            "provider_resolved": self.provider,
            "execution_mode": self.execution_mode,
            "provider_endpoint": "/v1/responses",
            "model_requested": self.model,
            "reasoning_effort": effort,
            "prompt_sha256": sha256_text(prompt),
            "request_sha256": sha256_text(compact_json(request)),
            "schema_sha256": sha256_file(local_schema),
            "api_schema_sha256": sha256_text(compact_json(api_schema)),
            "provider_request_sha256": sha256_file(provider_request_path),
            "images": image_metadata,
            "store": False,
            "tools_enabled": False,
            "conversation_enabled": False,
            "previous_response_id": None,
            "credential_source": "environment:OPENAI_API_KEY",
            "credential_persisted": False,
            "disabled_agent_features": list(DISABLED_AGENT_FEATURES),
            "isolation_mode": "api-payload-only",
            "isolation_backend": "openai-responses-api",
            "isolation_policy": (
                "one self-contained request; no tools, conversation, or previous response; "
                "images embedded as data URLs"
            ),
            "created_at": utc_now(),
        }
        if batch_scope:
            base_meta["batch_scope"] = batch_scope
        atomic_write_json(invocation_dir / "invocation.json", base_meta)
        return body, base_meta

    @staticmethod
    def _api_response_headers(headers: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        normalized: dict[str, str] = {}
        try:
            normalized = {
                str(name).lower(): str(value) for name, value in headers.items()
            }
        except (AttributeError, TypeError):
            pass
        for name in ("x-request-id", "retry-after", "content-type"):
            value = normalized.get(name)
            if value is None:
                try:
                    value = headers.get(name) if headers is not None else None
                except (AttributeError, TypeError):
                    value = None
            if value is not None:
                result[name] = str(value)
        return result

    def _api_open(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, str], bytes, str | None]:
        if body is not None and raw_body is not None:
            raise ManagerError("API 请求不能同时传 body 和 raw_body")
        data = raw_body
        if body is not None:
            data = compact_json(body).encode("utf-8")
        url = path if path.startswith(("http://", "https://")) else self.api_base_url + path
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = self._urlopen(request, timeout=self.timeout_seconds)
            try:
                raw = response.read()
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else 200
                response_headers = self._api_response_headers(
                    getattr(response, "headers", None)
                )
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            return int(status), response_headers, bytes(raw), None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            finally:
                close = getattr(exc, "close", None)
                if callable(close):
                    close()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            return (
                int(exc.code),
                self._api_response_headers(exc.headers),
                bytes(raw),
                None,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return 0, {}, b"", self._redact_api_secret(str(reason))

    @staticmethod
    def _api_retryable_status(status: int) -> bool:
        return status in {408, 409, 429, 500, 502, 503, 504}

    @staticmethod
    def _api_retry_delay(attempt_no: int, headers: dict[str, str]) -> float:
        retry_after = headers.get("retry-after", "").strip()
        try:
            return max(0.0, min(60.0, float(retry_after)))
        except ValueError:
            return float(min(30, 2 ** (attempt_no - 1)))

    def _api_error_message(self, status: int, raw: bytes, transport_error: str | None) -> str:
        if transport_error:
            return self._redact_api_secret(transport_error)
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    error = value.get("error")
                    if isinstance(error, dict) and error.get("message"):
                        text = str(error["message"])
            except json.JSONDecodeError:
                pass
        text = self._redact_api_secret(text[:4000])
        return f"HTTP {status}: {text}" if status else text or "API transport error"

    @staticmethod
    def _extract_api_payload(envelope: dict[str, Any]) -> dict[str, Any]:
        status = envelope.get("status")
        if status == "incomplete":
            details = envelope.get("incomplete_details")
            raise ManagerError(f"Responses API 输出不完整: {details}")
        if status not in {None, "completed"}:
            raise ManagerError(f"Responses API 状态不可用: {status}")
        parsed_payload: dict[str, Any] | None = None
        text_parts: list[str] = []
        output = envelope.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "refusal":
                        raise ManagerError(
                            f"Responses API 拒绝请求: {part.get('refusal', '')}"
                        )
                    if isinstance(part.get("parsed"), dict):
                        parsed_payload = part["parsed"]
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
        if parsed_payload is not None:
            return parsed_payload
        if not text_parts and isinstance(envelope.get("output_text"), str):
            text_parts.append(envelope["output_text"])
        if not text_parts:
            raise ManagerError("Responses API 响应中没有 output_text")
        try:
            payload = json.loads("".join(text_parts))
        except json.JSONDecodeError as exc:
            raise ManagerError(f"Responses API 结构化输出不是有效 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ManagerError("Responses API 结构化输出顶层不是 object")
        return payload

    def _run_api(
        self,
        *,
        role: str,
        prompt: str,
        schema_path: Path,
        invocation_dir: Path,
        request: dict[str, Any],
        images: Sequence[Path] = (),
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body, base_meta = self._prepare_api_invocation(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
        )
        last_error = ""
        for attempt_no in range(1, self.retries + 2):
            attempt_dir = invocation_dir / f"try-{attempt_no:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            response_path = attempt_dir / "response.json"
            provider_response_path = attempt_dir / "provider-response.json"
            events_path = attempt_dir / "events.jsonl"
            stderr_path = attempt_dir / "stderr.log"
            started = utc_now()
            events: list[dict[str, Any]] = [
                {
                    "type": "manager.api_request_started",
                    "at": started,
                    "role": role,
                    "attempt": attempt_no,
                    "endpoint": "/v1/responses",
                }
            ]
            if progress:
                progress(f"{role} API 第 {attempt_no} 次调用已启动")
            with self.semaphore:
                status, headers, raw, transport_error = self._api_open(
                    method="POST", path="/responses", body=body
                )
            finished = utc_now()
            raw_text = raw.decode("utf-8", errors="replace")
            atomic_write_text(
                provider_response_path, self._redact_api_secret(raw_text)
            )
            envelope: dict[str, Any] | None = None
            payload: dict[str, Any] | None = None
            extraction_error: str | None = None
            if 200 <= status < 300 and not transport_error:
                try:
                    decoded = json.loads(raw_text)
                    if not isinstance(decoded, dict):
                        raise ManagerError("Responses API 响应顶层不是 object")
                    envelope = decoded
                    payload = self._redact_api_value(
                        self._extract_api_payload(envelope)
                    )
                    atomic_write_json(response_path, payload)
                except (json.JSONDecodeError, ManagerError) as exc:
                    extraction_error = self._redact_api_secret(str(exc))
            else:
                extraction_error = self._api_error_message(status, raw, transport_error)
            success = payload is not None
            if success:
                events.append(
                    {
                        "type": "manager.api_request_completed",
                        "at": finished,
                        "status": status,
                        "response_id": envelope.get("id") if envelope else None,
                    }
                )
                atomic_write_text(stderr_path, "")
            else:
                last_error = extraction_error or "未知 API 错误"
                events.append(
                    {
                        "type": "manager.api_request_failed",
                        "at": finished,
                        "status": status,
                        "error": last_error,
                    }
                )
                atomic_write_text(stderr_path, last_error + "\n")
            atomic_write_jsonl(
                events_path,
                [self._redact_api_value(event) for event in events],
            )
            usage = envelope.get("usage") if envelope else None
            retryable = bool(transport_error) or self._api_retryable_status(status)
            retry_delay = (
                self._api_retry_delay(attempt_no, headers)
                if attempt_no <= self.retries and retryable
                else None
            )
            attempt_meta = {
                **base_meta,
                "attempt": attempt_no,
                "command": [],
                "started_at": started,
                "finished_at": finished,
                "http_status": status,
                "http_response_headers": headers,
                "timed_out": bool(transport_error and "timed out" in transport_error.lower()),
                "provider_request_id": headers.get("x-request-id"),
                "provider_response_id": envelope.get("id") if envelope else None,
                "response_status": envelope.get("status") if envelope else None,
                "model_resolved": envelope.get("model") if envelope else None,
                "usage": usage if isinstance(usage, dict) else None,
                "response_sha256": sha256_file(response_path) if response_path.exists() else None,
                "provider_response_sha256": sha256_file(provider_response_path),
                "provider_response_wire_sha256": sha256_bytes(raw),
                "events_sha256": sha256_file(events_path),
                "stderr_sha256": sha256_file(stderr_path),
                "thread_id": None,
                "error": None if success else last_error,
                "retryable": retryable,
                "retry_delay_seconds": retry_delay,
                "success": success,
            }
            attempt_meta = self._redact_api_value(attempt_meta)
            atomic_write_json(attempt_dir / "meta.json", attempt_meta)
            if success:
                if progress:
                    progress(f"{role} 已完成")
                return payload, attempt_meta
            if attempt_no <= self.retries and retryable:
                assert retry_delay is not None
                delay = retry_delay
                if progress:
                    progress(f"{role} API 调用失败，{delay:g}s 后重试")
                time.sleep(delay)
                continue
            break
        raise ManagerError(f"{role} API 调用失败: {last_error}")

    def _run_api_batch_single(
        self,
        *,
        role: str,
        prompt: str,
        schema_path: Path,
        invocation_dir: Path,
        request: dict[str, Any],
        images: Sequence[Path] = (),
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Submit and wait for one Responses request via Batch API.

        This path is intentionally and explicitly single-request. Pipeline-wide
        JSONL aggregation can reuse the same wire format without pretending this
        per-invocation fallback already provides bulk scheduling.
        """
        body, base_meta = self._prepare_api_invocation(
            role=role,
            prompt=prompt,
            schema_path=schema_path,
            invocation_dir=invocation_dir,
            request=request,
            images=images,
            batch_scope="single",
        )
        base_meta["api_batch_scope"] = "single"
        base_meta["batch_endpoint"] = "/v1/batches"
        atomic_write_json(invocation_dir / "invocation.json", base_meta)

        custom_id = (
            f"qb-{self._api_schema_name(role)[3:]}-"
            f"{sha256_text(compact_json(body))[:24]}"
        )[:64]
        batch_line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/responses",
            "body": body,
        }
        batch_input_path = invocation_dir / "batch-input.jsonl"
        atomic_write_jsonl(batch_input_path, [batch_line])

        attempt_dir = invocation_dir / "try-01"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        response_path = attempt_dir / "response.json"
        events_path = attempt_dir / "events.jsonl"
        stderr_path = attempt_dir / "stderr.log"
        started = utc_now()
        events: list[dict[str, Any]] = [
            {
                "type": "manager.batch_started",
                "at": started,
                "role": role,
                "batch_scope": "single",
                "custom_id": custom_id,
            }
        ]
        deadline = time.monotonic() + self.timeout_seconds
        http_sequence = 0
        input_file_id: str | None = None
        batch_id: str | None = None
        output_file_id: str | None = None
        error_file_id: str | None = None
        batch_status: str | None = None
        batch_request_counts: dict[str, Any] | None = None
        provider_request_id: str | None = None
        provider_response_id: str | None = None
        response_envelope: dict[str, Any] | None = None
        poll_count = 0
        last_error = ""

        def api_call(
            label: str,
            method: str,
            path: str,
            *,
            json_body: dict[str, Any] | None = None,
            raw_body: bytes | None = None,
            content_type: str = "application/json",
        ) -> tuple[dict[str, str], bytes]:
            nonlocal http_sequence
            operation_error = ""
            for http_attempt in range(1, self.retries + 2):
                if time.monotonic() >= deadline:
                    raise ManagerError(
                        f"Batch API 超过总超时 {self.timeout_seconds}s"
                    )
                http_sequence += 1
                with self.semaphore:
                    status, headers, raw, transport_error = self._api_open(
                        method=method,
                        path=path,
                        body=json_body,
                        raw_body=raw_body,
                        content_type=content_type,
                    )
                artifact_name = (
                    f"http-{http_sequence:03d}-{safe_component(label)}-response.txt"
                )
                artifact_path = attempt_dir / artifact_name
                atomic_write_text(
                    artifact_path,
                    self._redact_api_secret(raw.decode("utf-8", errors="replace")),
                )
                operation_error = self._api_error_message(status, raw, transport_error)
                successful = 200 <= status < 300 and not transport_error
                events.append(
                    {
                        "type": "manager.batch_http",
                        "at": utc_now(),
                        "operation": label,
                        "http_attempt": http_attempt,
                        "status": status,
                        "provider_request_id": headers.get("x-request-id"),
                        "response_artifact": artifact_name,
                        "response_sha256": sha256_file(artifact_path),
                        "wire_response_sha256": sha256_bytes(raw),
                        "success": successful,
                        "error": None if successful else operation_error,
                    }
                )
                if successful:
                    return headers, raw
                retryable = bool(transport_error) or self._api_retryable_status(status)
                if http_attempt > self.retries or not retryable:
                    break
                delay = self._api_retry_delay(http_attempt, headers)
                events.append(
                    {
                        "type": "manager.batch_http_retry",
                        "at": utc_now(),
                        "operation": label,
                        "delay_seconds": delay,
                    }
                )
                if progress:
                    progress(f"Batch {label} 失败，{delay:g}s 后重试")
                time.sleep(delay)
            raise ManagerError(f"Batch {label} 失败: {operation_error}")

        def decode_object(raw: bytes, label: str) -> dict[str, Any]:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManagerError(f"Batch {label} 响应不是有效 JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ManagerError(f"Batch {label} 响应顶层不是 object")
            return value

        try:
            if progress:
                progress(f"{role} 单请求 Batch 已启动")
            boundary = f"qb-manager-{uuid.uuid4().hex}"
            batch_input = batch_input_path.read_bytes()
            multipart = b"".join(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    b'Content-Disposition: form-data; name="purpose"\r\n\r\n',
                    b"batch\r\n",
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        b'Content-Disposition: form-data; name="file"; '
                        b'filename="batch-input.jsonl"\r\n'
                    ),
                    b"Content-Type: application/jsonl\r\n\r\n",
                    batch_input,
                    b"\r\n",
                    f"--{boundary}--\r\n".encode("ascii"),
                ]
            )
            multipart_path = invocation_dir / "file-upload-request.multipart"
            atomic_write_text(multipart_path, multipart.decode("utf-8"))
            upload_headers, upload_raw = api_call(
                "file-upload",
                "POST",
                "/files",
                raw_body=multipart,
                content_type=f"multipart/form-data; boundary={boundary}",
            )
            upload_response = decode_object(upload_raw, "file-upload")
            input_file_id = str(upload_response.get("id") or "") or None
            if not input_file_id:
                raise ManagerError("Batch file-upload 响应缺少 file id")

            batch_create_request = {
                "input_file_id": input_file_id,
                "endpoint": "/v1/responses",
                "completion_window": "24h",
                "metadata": {"scope": "single", "role": role[:64]},
            }
            batch_create_path = invocation_dir / "batch-create-request.json"
            atomic_write_json(batch_create_path, batch_create_request)
            create_headers, create_raw = api_call(
                "batch-create",
                "POST",
                "/batches",
                json_body=batch_create_request,
            )
            batch_object = decode_object(create_raw, "batch-create")
            batch_id = str(batch_object.get("id") or "") or None
            provider_request_id = create_headers.get("x-request-id")
            if not batch_id:
                raise ManagerError("Batch create 响应缺少 batch id")

            terminal = {"completed", "failed", "expired", "cancelled"}
            while True:
                batch_status = str(batch_object.get("status") or "") or None
                output_file_id = str(batch_object.get("output_file_id") or "") or None
                error_file_id = str(batch_object.get("error_file_id") or "") or None
                request_counts = batch_object.get("request_counts")
                batch_request_counts = request_counts if isinstance(request_counts, dict) else None
                poll_count += 1
                events.append(
                    {
                        "type": "manager.batch_status",
                        "at": utc_now(),
                        "batch_id": batch_id,
                        "status": batch_status,
                        "poll_count": poll_count,
                        "request_counts": batch_request_counts,
                    }
                )
                if progress:
                    completed_count = (
                        batch_request_counts.get("completed", 0)
                        if batch_request_counts
                        else 0
                    )
                    progress(
                        f"Batch {batch_id} 状态 {batch_status or '未知'} "
                        f"({completed_count}/1)"
                    )
                if batch_status in terminal:
                    break
                if time.monotonic() >= deadline:
                    raise ManagerError(
                        f"Batch API 超过总超时 {self.timeout_seconds}s"
                    )
                time.sleep(
                    min(
                        float(self.batch_poll_seconds),
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                _, poll_raw = api_call(
                    "batch-poll",
                    "GET",
                    f"/batches/{urllib.parse.quote(batch_id, safe='')}",
                )
                batch_object = decode_object(poll_raw, "batch-poll")

            atomic_write_json(
                attempt_dir / "batch-final.json", self._redact_api_value(batch_object)
            )
            if batch_status != "completed":
                if error_file_id:
                    _, error_raw = api_call(
                        "batch-error-content",
                        "GET",
                        f"/files/{urllib.parse.quote(error_file_id, safe='')}/content",
                    )
                    atomic_write_text(
                        attempt_dir / "batch-errors.jsonl",
                        self._redact_api_secret(
                            error_raw.decode("utf-8", errors="replace")
                        ),
                    )
                raise ManagerError(f"Batch {batch_id} 终止于状态 {batch_status}")
            if not output_file_id:
                raise ManagerError(f"已完成 Batch {batch_id} 缺少 output_file_id")

            output_headers, output_raw = api_call(
                "batch-output-content",
                "GET",
                f"/files/{urllib.parse.quote(output_file_id, safe='')}/content",
            )
            batch_output_path = attempt_dir / "batch-output.jsonl"
            atomic_write_text(
                batch_output_path,
                self._redact_api_secret(output_raw.decode("utf-8", errors="replace")),
            )
            selected: dict[str, Any] | None = None
            for line_no, raw_line in enumerate(output_raw.splitlines(), 1):
                if not raw_line.strip():
                    continue
                try:
                    candidate = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ManagerError(
                        f"Batch output 第 {line_no} 行不是有效 JSON: {exc}"
                    ) from exc
                if isinstance(candidate, dict) and candidate.get("custom_id") == custom_id:
                    selected = candidate
                    break
            if selected is None:
                raise ManagerError(f"Batch output 中缺少 custom_id={custom_id}")
            if selected.get("error"):
                raise ManagerError(f"Batch Responses 请求失败: {selected.get('error')}")
            response_wrapper = selected.get("response")
            if not isinstance(response_wrapper, dict):
                raise ManagerError("Batch output 缺少 response object")
            response_status_code = int(response_wrapper.get("status_code") or 0)
            if not 200 <= response_status_code < 300:
                raise ManagerError(
                    f"Batch Responses 请求返回 HTTP {response_status_code}: "
                    f"{response_wrapper.get('body')}"
                )
            candidate_envelope = response_wrapper.get("body")
            if not isinstance(candidate_envelope, dict):
                raise ManagerError("Batch Responses body 顶层不是 object")
            response_envelope = candidate_envelope
            provider_request_id = str(
                response_wrapper.get("request_id")
                or output_headers.get("x-request-id")
                or provider_request_id
                or ""
            ) or None
            provider_response_id = str(response_envelope.get("id") or "") or None
            payload = self._redact_api_value(
                self._extract_api_payload(response_envelope)
            )
            atomic_write_json(response_path, payload)
            atomic_write_text(stderr_path, "")
            finished = utc_now()
            events.append(
                {
                    "type": "manager.batch_completed",
                    "at": finished,
                    "batch_id": batch_id,
                    "custom_id": custom_id,
                    "provider_response_id": provider_response_id,
                }
            )
            atomic_write_jsonl(
                events_path,
                [self._redact_api_value(event) for event in events],
            )
            usage = response_envelope.get("usage")
            attempt_meta = {
                **base_meta,
                "attempt": 1,
                "command": [],
                "started_at": started,
                "finished_at": finished,
                "http_status": 200,
                "timed_out": False,
                "custom_id": custom_id,
                "input_file_id": input_file_id,
                "batch_id": batch_id,
                "batch_status": batch_status,
                "batch_poll_count": poll_count,
                "batch_request_counts": batch_request_counts,
                "output_file_id": output_file_id,
                "error_file_id": error_file_id,
                "provider_request_id": provider_request_id,
                "provider_response_id": provider_response_id,
                "response_status": response_envelope.get("status"),
                "model_resolved": response_envelope.get("model"),
                "usage": usage if isinstance(usage, dict) else None,
                "batch_input_sha256": sha256_file(batch_input_path),
                "batch_upload_sha256": sha256_file(multipart_path),
                "batch_create_request_sha256": sha256_file(batch_create_path),
                "batch_output_sha256": sha256_file(batch_output_path),
                "response_sha256": sha256_file(response_path),
                "events_sha256": sha256_file(events_path),
                "stderr_sha256": sha256_file(stderr_path),
                "thread_id": None,
                "error": None,
                "retryable": False,
                "success": True,
            }
            attempt_meta = self._redact_api_value(attempt_meta)
            atomic_write_json(attempt_dir / "meta.json", attempt_meta)
            if progress:
                progress(f"{role} 单请求 Batch 已完成")
            return payload, attempt_meta
        except Exception as exc:
            last_error = self._redact_api_secret(str(exc))
            finished = utc_now()
            events.append(
                {
                    "type": "manager.batch_failed",
                    "at": finished,
                    "batch_id": batch_id,
                    "status": batch_status,
                    "error": last_error,
                }
            )
            atomic_write_jsonl(
                events_path,
                [self._redact_api_value(event) for event in events],
            )
            atomic_write_text(stderr_path, last_error + "\n")
            failed_meta = {
                **base_meta,
                "attempt": 1,
                "command": [],
                "started_at": started,
                "finished_at": finished,
                "http_status": None,
                "timed_out": "超时" in last_error or "timed out" in last_error.lower(),
                "custom_id": custom_id,
                "input_file_id": input_file_id,
                "batch_id": batch_id,
                "batch_status": batch_status,
                "batch_poll_count": poll_count,
                "batch_request_counts": batch_request_counts,
                "output_file_id": output_file_id,
                "error_file_id": error_file_id,
                "provider_request_id": provider_request_id,
                "provider_response_id": provider_response_id,
                "response_status": (
                    response_envelope.get("status") if response_envelope else None
                ),
                "batch_input_sha256": sha256_file(batch_input_path),
                "response_sha256": (
                    sha256_file(response_path) if response_path.exists() else None
                ),
                "events_sha256": sha256_file(events_path),
                "stderr_sha256": sha256_file(stderr_path),
                "thread_id": None,
                "error": last_error,
                "retryable": False,
                "success": False,
            }
            failed_meta = self._redact_api_value(failed_meta)
            atomic_write_json(attempt_dir / "meta.json", failed_meta)
            if isinstance(exc, ManagerError):
                raise
            raise ManagerError(f"{role} Batch API 调用失败: {last_error}") from exc

    def _stream_process(
        self,
        command: list[str],
        prompt: str,
        events_path: Path,
        stderr_path: Path,
        *,
        progress: Callable[[str], None] | None,
    ) -> tuple[int, bool]:
        with stderr_path.open("w", encoding="utf-8", newline="\n") as stderr:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
            except OSError as exc:
                atomic_write_text(
                    events_path,
                    compact_json(
                        {"type": "manager.launch_failed", "at": utc_now(), "error": str(exc)}
                    )
                    + "\n",
                )
                stderr.write(str(exc) + "\n")
                return 127, False
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + self.timeout_seconds
            timed_out = False
            with events_path.open("w", encoding="utf-8", newline="\n") as events:
                while True:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        proc.kill()
                        events.write(compact_json({"type": "manager.timeout", "at": utc_now()}) + "\n")
                        break
                    ready = selector.select(timeout=1.0)
                    for key, _ in ready:
                        line = key.fileobj.readline()
                        if line:
                            events.write(line)
                            events.flush()
                            if progress:
                                try:
                                    event = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                event_type = str(event.get("type", ""))
                                if event_type in {"turn.started", "item.completed", "turn.completed"}:
                                    progress(event_type)
                    if proc.poll() is not None:
                        remainder = proc.stdout.read()
                        if remainder:
                            events.write(remainder)
                        break
            selector.close()
            return proc.wait(), timed_out

    @staticmethod
    def _thread_id(events_path: Path) -> str | None:
        if not events_path.exists():
            return None
        with events_path.open(encoding="utf-8") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                thread_id = event.get("thread_id") or event.get("threadId")
                if thread_id:
                    return str(thread_id)
        return None


def store_attempt(
    state: State,
    *,
    key: str,
    run_id: str,
    agent_id: str,
    solution: dict[str, Any],
    invocation_dir: Path,
    meta: dict[str, Any],
) -> None:
    raw = compact_json(solution)
    with state.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO attempts(
                question_key,run_id,agent_id,answer,solution,independent_check,
                question_valid,confidence,raw_json,invocation_dir,prompt_sha256,
                response_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                run_id,
                agent_id,
                str(solution.get("answer", "")),
                str(solution.get("solution", "")),
                str(solution.get("independent_check", "")),
                int(bool(solution.get("question_valid", True))),
                str(solution.get("confidence", "medium")),
                raw,
                safe_rel(invocation_dir, state.root),
                str(meta.get("prompt_sha256", "")),
                str(meta.get("response_sha256", "")),
                utc_now(),
            ),
        )


def store_review(
    state: State,
    *,
    key: str,
    run_id: str,
    review: dict[str, Any],
    invocation_dir: Path,
    meta: dict[str, Any],
    effective_auto_promote: bool,
) -> None:
    with state.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reviews(
                question_key,run_id,verdict,answer_consistent,teacher_answer,
                teacher_solution,process_review,agent_feedback_json,auto_promote,
                raw_json,invocation_dir,prompt_sha256,response_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                run_id,
                str(review.get("verdict", "disagreement")),
                int(bool(review.get("answer_consistent"))),
                str(review.get("teacher_answer", "")),
                str(review.get("teacher_solution", "")),
                str(review.get("process_review", "")),
                compact_json(review.get("agent_feedback", [])),
                int(effective_auto_promote),
                compact_json(review),
                safe_rel(invocation_dir, state.root),
                str(meta.get("prompt_sha256", "")),
                str(meta.get("response_sha256", "")),
                utc_now(),
            ),
        )


def store_blind_recheck(
    state: State,
    *,
    row: sqlite3.Row,
    run_id: str,
    solution: dict[str, Any],
    matched: bool,
    invocation_dir: Path,
    meta: dict[str, Any],
    expected_final_sha256: str,
) -> None:
    attempt_number = meta.get("attempt")
    artifact_dir = invocation_dir
    if isinstance(attempt_number, int):
        candidate = invocation_dir / f"try-{attempt_number:02d}"
        if candidate.is_dir():
            artifact_dir = candidate
    record = {
        "question_key": str(row["question_key"]),
        "id": str(row["qid"]),
        "node_dir": str(row["node_dir"]),
        "run_id": run_id,
        "answer": str(solution.get("answer", "")),
        "question_valid": bool(solution.get("question_valid")),
        "matched": matched,
        "question_snapshot_sha256": public_question_snapshot_sha256(row),
        "final_content_sha256": expected_final_sha256,
        "invocation_dir": safe_rel(artifact_dir, state.root),
        "prompt_sha256": str(meta.get("prompt_sha256", "")),
        "response_sha256": str(meta.get("response_sha256", "")),
        "created_at": utc_now(),
    }
    with state.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO blind_rechecks(
                question_key,run_id,answer,solution,independent_check,
                question_valid,matched,question_snapshot_sha256,final_content_sha256,
                invocation_dir,prompt_sha256,response_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["question_key"],
                run_id,
                record["answer"],
                str(solution.get("solution", "")),
                str(solution.get("independent_check", "")),
                int(record["question_valid"]),
                int(matched),
                record["question_snapshot_sha256"],
                expected_final_sha256,
                record["invocation_dir"],
                record["prompt_sha256"],
                record["response_sha256"],
                record["created_at"],
            ),
        )
    append_jsonl(state.blind_rechecks_path, record)


def latest_valid_blind_recheck(
    state: State, row: sqlite3.Row
) -> sqlite3.Row | None:
    """Return the newest passing certificate for the exact current final bytes."""
    expected_snapshot = public_question_snapshot_sha256(row)
    expected_final = final_content_sha256(row)
    with state.connect() as conn:
        return conn.execute(
            """
            SELECT * FROM blind_rechecks
            WHERE question_key=? AND matched=1 AND question_valid=1
              AND question_snapshot_sha256=? AND final_content_sha256=?
            ORDER BY created_at DESC,run_id DESC LIMIT 1
            """,
            (row["question_key"], expected_snapshot, expected_final),
        ).fetchone()


def remove_rejected_generated_final_from_source(state: State, row: sqlite3.Row) -> None:
    """Remove a generated final that failed the separate blind delivery gate."""
    if str(row["source_kind"]) != "generated":
        return
    qfile = state.bank / str(row["question_file"])
    questions, errors = read_jsonl(qfile)
    if errors:
        raise ManagerError("盲解淘汰写回前源 JSONL 已损坏: " + errors[0])
    qid = str(row["qid"])
    matching = [item for item in questions if str(item.get("id", "")) == qid]
    if len(matching) != 1:
        raise ManagerError(f"盲解淘汰时源题 {qid!r} 数量不是 1")
    if compact_json(matching[0]) != compact_json(json.loads(row["question_json"])):
        raise ManagerError(f"盲解期间源题 {qid!r} 已变化，拒绝自动移除")
    atomic_write_jsonl(qfile, [item for item in questions if str(item.get("id", "")) != qid])
    final_path = state.bank / str(row["node_dir"]) / "answer_final.jsonl"
    if final_path.is_file():
        finals, final_errors = read_jsonl(final_path)
        if final_errors:
            raise ManagerError("盲解淘汰前 answer_final 已损坏: " + final_errors[0])
        atomic_write_jsonl(
            final_path,
            [item for item in finals if str(item.get("id", "")) != qid],
        )


def public_question_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    """Return the exact answer-free question snapshot shown to solvers."""
    value = sanitized_question(row)
    value.pop("user_guidance", None)
    value.pop("solution_skills", None)
    value.pop("language_variant", None)
    value.pop("question_snapshot_sha256", None)
    return value


def public_question_snapshot_sha256(row: sqlite3.Row) -> str:
    return sha256_text(compact_json(public_question_snapshot(row)))


def final_content_sha256(row: sqlite3.Row) -> str:
    question = json.loads(row["question_json"])
    return sha256_text(
        compact_json(
            {
                "question_snapshot_sha256": public_question_snapshot_sha256(row),
                "answer": str(question.get("answer", "")),
                "explanation_sha256": sha256_text(str(question.get("explanation", ""))),
            }
        )
    )


def store_question_annotation(
    state: State,
    *,
    key: str,
    run_id: str,
    annotation: dict[str, Any],
) -> None:
    """Store a staged annotation; never silently mutate a finalized question."""
    if not annotation:
        return
    annotation_id = uuid.uuid4().hex
    issue_codes = annotation.get("annotation_codes", [])
    if not isinstance(issue_codes, list):
        issue_codes = []
    proposed = annotation.get("proposed_revision")
    if not isinstance(proposed, dict):
        proposed = {}
    summary = str(annotation.get("summary") or "").strip()
    status = str(annotation.get("validity") or "unreviewed")
    with state.connect() as conn:
        conn.execute(
            "INSERT INTO question_annotations VALUES(?,?,?,?,?,?,?,?)",
            (
                annotation_id,
                key,
                run_id,
                status,
                compact_json([str(code) for code in issue_codes]),
                summary,
                compact_json(proposed),
                utc_now(),
            ),
        )


def _skill_tokens(value: str) -> set[str]:
    lowered = value.lower()
    latin = set(re.findall(r"[a-z0-9_+-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
    return latin | bigrams


def _skill_similarity(left: str, right: str) -> float:
    left_tokens = _skill_tokens(left)
    right_tokens = _skill_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _validated_skill_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    title = str(candidate.get("name") or candidate.get("title") or "").strip()
    description = re.sub(r"\s+", " ", str(candidate.get("description") or "")).strip()
    if not title or not description:
        raise ManagerError("解题 skill 必须有 name/title 和 description")
    if len(title) > 80 or len(description) > 600:
        raise ManagerError("解题 skill 的标题或描述过长")
    if "<" in description or ">" in description:
        raise ManagerError("解题 skill description 不得含尖括号")

    def string_list(field: str, *, required: bool = False, limit: int = 20) -> list[str]:
        raw = candidate.get(field, [])
        if not isinstance(raw, list):
            raise ManagerError(f"解题 skill 字段 {field} 必须是数组")
        values = [re.sub(r"\s+", " ", str(item)).strip() for item in raw]
        values = [item for item in values if item]
        if required and not values:
            raise ManagerError(f"解题 skill 字段 {field} 不得为空")
        if len(values) > limit or any(len(item) > 500 for item in values):
            raise ManagerError(f"解题 skill 字段 {field} 超出长度限制")
        return values

    requested_id = str(candidate.get("skill_id") or "").strip().lower()
    name = safe_component(requested_id, 64).lower() if requested_id else ""
    name = re.sub(r"[._]+", "-", name).strip("-")
    if not name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        name = "solution-" + sha256_text(title)[:12]
    normalized = {
        "skill_id": name,
        "name": title,
        "description": description,
        "applicability": string_list("applicability", required=True),
        "ordered_steps": string_list("ordered_steps", required=True),
        "verification_checks": string_list("verification_checks", required=True),
        "pitfalls": string_list("pitfalls"),
        "tags": string_list("tags", limit=12),
        "related_skill_id": str(candidate.get("related_skill_id") or "").strip(),
        "novelty_rationale": re.sub(
            r"\s+", " ", str(candidate.get("novelty_rationale") or "")
        ).strip(),
        "useful": bool(candidate.get("useful")),
        "novel": bool(candidate.get("novel")),
        "generalized": bool(candidate.get("generalized")),
        "action": str(candidate.get("action") or "none"),
    }
    if normalized["action"] not in {"create", "update", "none"}:
        raise ManagerError("解题 skill action 非法")
    return normalized


def render_solution_skill(candidate: dict[str, Any]) -> str:
    """Render a project skill with only the supported frontmatter keys."""
    value = _validated_skill_candidate(candidate)
    lines = [
        "---",
        "name: " + json.dumps(value["skill_id"], ensure_ascii=False),
        "description: " + json.dumps(value["description"], ensure_ascii=False),
        "---",
        "",
        f"# {value['name']}",
        "",
        "## 适用场景",
        "",
        *[f"- {item}" for item in value["applicability"]],
        "",
        "## 解题步骤",
        "",
        *[f"{index}. {item}" for index, item in enumerate(value["ordered_steps"], 1)],
        "",
        "## 必做复核",
        "",
        *[f"- {item}" for item in value["verification_checks"]],
    ]
    if value["pitfalls"]:
        lines.extend(
            ["", "## 常见误区", "", *[f"- {item}" for item in value["pitfalls"]]]
        )
    content = "\n".join(lines).rstrip() + "\n"
    if len(content.splitlines()) >= 500:
        raise ManagerError("解题 skill 超过 500 行")
    return content


def _active_skill_rows(state: State) -> list[sqlite3.Row]:
    with state.connect() as conn:
        return list(
            conn.execute(
                "SELECT * FROM solution_skills WHERE status='active' ORDER BY updated_at DESC"
            )
        )


def solution_skill_context_for_text(
    state: State, query: str, *, max_items: int | None = None
) -> list[dict[str, Any]]:
    config = state.config().get("solution_skills")
    if not isinstance(config, dict):
        config = {}
    if not config.get("enabled", True):
        return []
    limit = max_items if max_items is not None else int(config.get("max_context_skills") or 5)
    ranked: list[tuple[float, sqlite3.Row]] = []
    for skill in _active_skill_rows(state):
        metadata = json.loads(skill["metadata_json"])
        haystack = " ".join(
            [
                str(skill["name"]),
                str(skill["description"]),
                " ".join(map(str, metadata.get("tags", []))),
                " ".join(map(str, metadata.get("applicability", []))),
            ]
        )
        score = _skill_similarity(query, haystack)
        if score > 0:
            ranked.append((score, skill))
    ranked.sort(key=lambda item: (-item[0], str(item[1]["skill_id"])))
    result: list[dict[str, Any]] = []
    for score, skill in ranked[: max(0, limit)]:
        skill_path = state.solution_skills_root / str(skill["skill_id"]) / "SKILL.md"
        if not skill_path.is_file() or sha256_file(skill_path) != skill["current_sha256"]:
            continue
        result.append(
            {
                "skill_id": skill["skill_id"],
                "name": skill["name"],
                "description": skill["description"],
                "version": skill["current_version"],
                "sha256": skill["current_sha256"],
                "relevance": round(score, 4),
                "content": skill_path.read_text(encoding="utf-8"),
            }
        )
    return result


def solution_skill_context_for_question(
    state: State, row: sqlite3.Row, *, max_items: int | None = None
) -> list[dict[str, Any]]:
    question = json.loads(row["question_json"])
    query = " ".join(
        [str(row["subject"]), str(question.get("prompt", "")), str(question.get("skillTarget", ""))]
    )
    return solution_skill_context_for_text(state, query, max_items=max_items)


def record_solution_skill(
    state: State,
    *,
    candidate: dict[str, Any],
    source: dict[str, Any],
    verification_run_id: str | None,
    activate: bool,
    expected_base_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Version and optionally activate a useful, generalized solution skill.

    Novelty is retained as provenance metadata, but it is intentionally not an
    activation gate. Semantic consolidation and deterministic similarity checks
    remain responsible for preventing duplicate-library growth.
    """
    value = _validated_skill_candidate(candidate)
    if (
        value["action"] == "none"
        or not value["useful"]
        or not value["generalized"]
    ):
        return None
    config = state.config().get("solution_skills")
    config = config if isinstance(config, dict) else {}
    threshold = float(config.get("similarity_threshold") or 0.68)
    existing = {str(row["skill_id"]): row for row in _active_skill_rows(state)}
    related = value["related_skill_id"]
    if value["action"] == "update":
        if related not in existing:
            raise ManagerError("Teacher 请求更新不存在的解题 skill")
        value["skill_id"] = related
    else:
        comparison = " ".join(
            [value["name"], value["description"], *value["tags"], *value["applicability"]]
        )
        closest_id = ""
        closest_score = 0.0
        for skill_id, row in existing.items():
            metadata = json.loads(row["metadata_json"])
            haystack = " ".join(
                [
                    str(row["name"]),
                    str(row["description"]),
                    *map(str, metadata.get("tags", [])),
                    *map(str, metadata.get("applicability", [])),
                ]
            )
            score = _skill_similarity(comparison, haystack)
            if score > closest_score:
                closest_id, closest_score = skill_id, score
        if closest_score >= threshold:
            append_jsonl(
                state.skill_events_path,
                {
                    "event": "candidate_skipped_as_duplicate",
                    "candidate_name": value["name"],
                    "closest_skill_id": closest_id,
                    "similarity": round(closest_score, 4),
                    "source": source,
                    "created_at": utc_now(),
                },
            )
            return None
        if value["skill_id"] in existing:
            value["skill_id"] += "-" + sha256_text(value["name"] + value["description"])[:8]

    content = render_solution_skill(value)
    content_sha = sha256_text(content)
    skill_id = value["skill_id"]
    created = utc_now()
    lock = state.root / "locks" / "solution-skills.lock"
    with advisory_file_lock(lock):
        with state.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM solution_skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
            if expected_base_sha256 is not None and (
                prior is None or str(prior["current_sha256"]) != expected_base_sha256
            ):
                raise ManagerError("skill 基线 SHA 已变化；拒绝激活基于旧版本的修订")
            version = int(prior["current_version"] if prior else 0) + 1
            if prior:
                seen = conn.execute(
                    "SELECT MAX(version) AS n FROM solution_skill_versions WHERE skill_id=?",
                    (skill_id,),
                ).fetchone()["n"]
                version = max(version, int(seen or 0) + 1)
        metadata = {
            **value,
            "version": version,
            "sha256": content_sha,
            "source": source,
            "verification_run_id": verification_run_id,
            "updated_at": created,
        }
        version_dir = state.skill_versions_root / skill_id / f"v{version:04d}"
        atomic_write_text(version_dir / "SKILL.md", content)
        atomic_write_json(version_dir / "metadata.json", metadata)
        with state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM solution_skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
            if prior is None:
                conn.execute(
                    "INSERT INTO solution_skills VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        skill_id,
                        value["name"],
                        value["description"],
                        compact_json(value["tags"]),
                        version if activate else 0,
                        content_sha if activate else "",
                        "active" if activate else "candidate",
                        compact_json(metadata),
                        created,
                        created,
                    ),
                )
            elif activate:
                conn.execute(
                    "UPDATE solution_skills SET name=?,description=?,tags_json=?,"
                    "current_version=?,current_sha256=?,status='active',metadata_json=?,updated_at=? "
                    "WHERE skill_id=?",
                    (
                        value["name"],
                        value["description"],
                        compact_json(value["tags"]),
                        version,
                        content_sha,
                        compact_json(metadata),
                        created,
                        skill_id,
                    ),
                )
            conn.execute(
                "INSERT INTO solution_skill_versions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    skill_id,
                    version,
                    "active" if activate else "rejected",
                    content_sha,
                    content,
                    compact_json(metadata),
                    compact_json(source),
                    verification_run_id,
                    created,
                ),
            )
            conn.execute("COMMIT")
        if activate:
            visible_dir = state.solution_skills_root / skill_id
            atomic_write_text(visible_dir / "SKILL.md", content)
            atomic_write_json(visible_dir / "metadata.json", metadata)
        event = {
            "event": "version_activated" if activate else "version_rejected",
            "skill_id": skill_id,
            "version": version,
            "sha256": content_sha,
            "source": source,
            "verification_run_id": verification_run_id,
            "created_at": created,
        }
        append_jsonl(state.skill_events_path, event)
    return event


def maybe_extract_solution_skill(
    state: State,
    *,
    review: dict[str, Any],
    row: sqlite3.Row,
    run_id: str,
    teacher_meta: dict[str, Any],
) -> dict[str, Any] | None:
    candidate = review.get("skill_candidate")
    if not isinstance(candidate, dict):
        return None
    serialized = compact_json(candidate)
    question = json.loads(row["question_json"])
    forbidden_literals = [str(row["qid"]), str(question.get("prompt", ""))]
    forbidden_literals.extend(
        str(option.get("text", ""))
        for option in question.get("options", [])
        if isinstance(option, dict) and len(str(option.get("text", "")).strip()) >= 4
    )
    if any(value.strip() and value.strip() in serialized for value in forbidden_literals):
        raise ManagerError("解题 skill 候选包含题目 id、题干或专属选项文本")
    return record_solution_skill(
        state,
        candidate=candidate,
        source={
            "kind": "strict_teacher_consensus",
            "question_key": row["question_key"],
            "question_id": row["qid"],
            "question_snapshot_sha256": public_question_snapshot_sha256(row),
            "run_id": run_id,
            "teacher_response_sha256": str(teacher_meta.get("response_sha256", "")),
        },
        verification_run_id=run_id,
        activate=True,
    )


def historical_skill_evidence(row: sqlite3.Row) -> dict[str, Any]:
    """Build a compact, immutable record for retrospective skill curation."""
    question = public_question_snapshot(row)
    return {
        "question_key": str(row["question_key"]),
        "display_id": str(row["qid"]),
        "node_dir": str(row["node_dir"]),
        "source_kind": str(row["source_kind"]),
        "question_snapshot_sha256": public_question_snapshot_sha256(row),
        "question": {
            "subject": question.get("subject", ""),
            "prompt": question.get("prompt", ""),
            "options": question.get("options", []),
            "difficulty": question.get("difficulty", ""),
            "question_type": question.get("question_type", ""),
        },
        "verified_answer": str(row["teacher_answer"] or ""),
        "verified_solution": str(row["teacher_solution"] or ""),
        "verification": {
            "status": str(row["status"]),
            "run_id": str(row["current_run_id"] or ""),
            "verdict": str(row["teacher_verdict"] or ""),
        },
    }


def pack_skill_evidence(
    evidence: Sequence[dict[str, Any]], *, character_budget: int
) -> list[list[dict[str, Any]]]:
    """Pack every evidence row exactly once while keeping node order stable."""
    budget = max(20_000, int(character_budget))
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for item in evidence:
        size = len(compact_json(item))
        if current and current_size + size > budget:
            batches.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        batches.append(current)
    return batches


def active_skill_curation_context(state: State) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _active_skill_rows(state):
        metadata = json.loads(row["metadata_json"])
        result.append(
            {
                "skill_id": str(row["skill_id"]),
                "name": str(row["name"]),
                "description": str(row["description"]),
                "tags": list(metadata.get("tags", [])),
                "applicability": list(metadata.get("applicability", [])),
                "ordered_steps": list(metadata.get("ordered_steps", [])),
                "verification_checks": list(metadata.get("verification_checks", [])),
            }
        )
    return result


def validate_historical_skill_candidates(
    payload: dict[str, Any],
    *,
    evidence_by_key: dict[str, dict[str, Any]],
    existing_skill_ids: set[str],
    min_source_questions: int = 2,
    min_source_nodes: int = 1,
    lineage_candidates: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply deterministic evidence and pollution gates to curator output."""
    raw_candidates = payload.get("skill_candidates")
    if not isinstance(raw_candidates, list):
        raise ManagerError("历史 skill 回顾响应缺少 skill_candidates 数组")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    updated_ids: set[str] = set()
    all_forbidden: list[str] = []
    for evidence in evidence_by_key.values():
        display_id = str(evidence.get("display_id", "")).strip()
        if display_id:
            all_forbidden.append(display_id)
        question = evidence.get("question")
        if not isinstance(question, dict):
            continue
        prompt = str(question.get("prompt", "")).strip()
        if len(prompt) >= 12:
            all_forbidden.append(prompt)
        options = question.get("options", [])
        if isinstance(options, list):
            all_forbidden.extend(
                str(option.get("text", "")).strip()
                for option in options
                if isinstance(option, dict)
                and len(str(option.get("text", "")).strip()) >= 8
            )
        verified_answer = re.sub(
            r"\s+", " ", str(evidence.get("verified_answer", ""))
        ).strip()
        if len(verified_answer) >= 8:
            all_forbidden.append(verified_answer)
        verified_solution = re.sub(
            r"\s+", " ", str(evidence.get("verified_solution", ""))
        ).strip()
        if len(verified_solution) >= 40:
            all_forbidden.append(verified_solution)
        all_forbidden.extend(
            fragment.strip()
            for fragment in re.split(r"[。！？；;\n]", verified_solution)
            if len(fragment.strip()) >= 20
        )
    for index, item in enumerate(raw_candidates):
        label = f"candidate-{index + 1}"
        try:
            if not isinstance(item, dict):
                raise ManagerError("候选必须是 object")
            raw_keys = item.get("source_question_keys")
            if not isinstance(raw_keys, list):
                raise ManagerError("source_question_keys 必须是数组")
            source_keys = list(dict.fromkeys(str(key).strip() for key in raw_keys if str(key).strip()))
            if len(source_keys) < max(2, int(min_source_questions)):
                raise ManagerError("至少需要两道不同 final 题作为证据")
            unknown = [key for key in source_keys if key not in evidence_by_key]
            if unknown:
                raise ManagerError(f"包含不在回顾输入中的证据 key: {unknown[0]}")
            source_node_dirs = sorted(
                {
                    str(evidence_by_key[key].get("node_dir", ""))
                    for key in source_keys
                }
            )
            if len(source_node_dirs) < max(1, int(min_source_nodes)):
                raise ManagerError(
                    f"证据仅覆盖 {len(source_node_dirs)} 个节点，低于要求的 "
                    f"{max(1, int(min_source_nodes))} 个"
                )
            raw_candidate_ids = item.get("source_candidate_ids")
            if not isinstance(raw_candidate_ids, list):
                raise ManagerError("source_candidate_ids 必须是数组")
            source_candidate_ids = list(
                dict.fromkeys(
                    str(candidate_id).strip()
                    for candidate_id in raw_candidate_ids
                    if str(candidate_id).strip()
                )
            )
            if lineage_candidates is None:
                if source_candidate_ids:
                    raise ManagerError("批内发现阶段 source_candidate_ids 必须为空")
            else:
                if not source_candidate_ids:
                    raise ManagerError("跨批合并必须声明至少一个 source_candidate_id")
                unknown_candidate_ids = [
                    candidate_id
                    for candidate_id in source_candidate_ids
                    if candidate_id not in lineage_candidates
                ]
                if unknown_candidate_ids:
                    raise ManagerError(
                        "包含不存在的 source_candidate_id: "
                        + unknown_candidate_ids[0]
                    )
                lineage_keys = {
                    str(key)
                    for candidate_id in source_candidate_ids
                    for key in lineage_candidates[candidate_id].get(
                        "source_question_keys", []
                    )
                }
                unlinked = [key for key in source_keys if key not in lineage_keys]
                if unlinked:
                    raise ManagerError(
                        "source key 未由所声明的候选血缘支持: " + unlinked[0]
                    )
            candidate = item.get("skill_candidate")
            if not isinstance(candidate, dict):
                raise ManagerError("缺少 skill_candidate object")
            value = _validated_skill_candidate(candidate)
            label = value["name"]
            if not value["useful"] or not value["generalized"]:
                raise ManagerError("useful 和 generalized 必须均为 true")
            if not value["novelty_rationale"]:
                raise ManagerError("必须说明已有覆盖、差异和复用理由")
            related = value["related_skill_id"]
            if value["action"] == "update":
                if related not in existing_skill_ids:
                    raise ManagerError("update 引用了不存在的 active skill")
                if related in updated_ids:
                    raise ManagerError("同一 active skill 在一次回顾中只能更新一次")
                updated_ids.add(related)
            elif related:
                raise ManagerError("create 候选的 related_skill_id 必须为空")
            serialized = compact_json(value)
            polluted = next(
                (literal for literal in all_forbidden if literal and literal in serialized),
                None,
            )
            if polluted:
                raise ManagerError("候选仍含题目 id、题干、专属选项或过长原解片段")
            signature = sha256_text(
                compact_json(
                    {
                        "name": value["name"],
                        "description": value["description"],
                        "steps": value["ordered_steps"],
                    }
                )
            )
            if signature in seen_signatures:
                raise ManagerError("候选在当前响应中重复")
            seen_signatures.add(signature)
            reuse_rationale = re.sub(
                r"\s+", " ", str(item.get("reuse_rationale") or "")
            ).strip()
            if not reuse_rationale:
                raise ManagerError("reuse_rationale 不得为空")
            accepted.append(
                {
                    "source_question_keys": source_keys,
                    "source_candidate_ids": source_candidate_ids,
                    "source_node_dirs": source_node_dirs,
                    "reuse_rationale": reuse_rationale,
                    "skill_candidate": value,
                }
            )
        except ManagerError as exc:
            rejected.append({"index": index, "name": label, "error": str(exc)})
    return accepted, rejected


class Pipeline:
    def __init__(
        self,
        state: State,
        *,
        model: str | None,
        max_agent_processes: int | None,
        retries: int | None = None,
        timeout_seconds: int | None = None,
        isolation_mode: str | None = None,
        provider: str | None = None,
        api_mode: str | None = None,
    ) -> None:
        self.state = state
        self.runner = AgentRunner(
            state,
            model=model,
            max_processes=max_agent_processes,
            retries=retries,
            timeout_seconds=timeout_seconds,
            isolation_mode=isolation_mode,
            provider=provider,
            api_mode=api_mode,
        )

    def create_manifest(
        self, run_id: str, kind: str, request: dict[str, Any]
    ) -> Path:
        run_dir = self.state.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        prompt_names = [
            "generator-solver-prompt.md",
            "solver-prompt.md",
            "blind-recheck-prompt.md",
            "teacher-prompt.md",
            "skill-extractor-prompt.md",
            "skill-editor-prompt.md",
            "skill-history-curator-prompt.md",
            "skill-history-consolidator-prompt.md",
        ]
        prompts = {
            name: {
                "path": str(REFERENCE_ROOT / name),
                "sha256": sha256_file(REFERENCE_ROOT / name),
            }
            for name in prompt_names
            if (REFERENCE_ROOT / name).is_file()
        }
        atomic_write_json(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "kind": kind,
                "bank_root": str(self.state.bank),
                "state_root": str(self.state.root),
                "model_requested": self.runner.model,
                "provider_requested": getattr(self.runner, "provider_requested", "external"),
                "provider_resolved": getattr(self.runner, "provider", "external"),
                "execution_mode": getattr(self.runner, "execution_mode", "external"),
                "codex_cli_version": self.runner.cli_version,
                "isolation_mode": (
                    "api-payload-only"
                    if getattr(self.runner, "provider", "") == "api"
                    else getattr(self.runner, "isolation_mode", "external-runner")
                ),
                "prompts": prompts,
                "request": request,
                "replay_contract": "inputs and artifacts are hashed; model sampling may differ",
                "started_at": utc_now(),
            },
        )
        return run_dir

    def finish_manifest(self, run_dir: Path, result: dict[str, Any]) -> None:
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["finished_at"] = utc_now()
        manifest["result"] = result
        snapshot = self._writeback_snapshot(str(manifest.get("run_id", "")))
        manifest["writeback_snapshot"] = snapshot
        manifest["writeback_snapshot_sha256"] = sha256_text(compact_json(snapshot))
        inventory = artifact_inventory(run_dir, exclude=("manifest.json",))
        manifest["artifact_inventory"] = inventory
        manifest["artifact_root_sha256"] = sha256_text(compact_json(inventory))
        atomic_write_json(manifest_path, manifest)
        self._append_run_ledger(manifest_path, manifest)

    def _writeback_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.state.connect() as conn:
            questions = [dict(row) for row in conn.execute(
                "SELECT question_key,qid,node_dir,question_file,status,current_run_id,"
                "teacher_answer,teacher_solution,teacher_verdict,updated_at "
                "FROM questions WHERE current_run_id=? ORDER BY question_key",
                (run_id,),
            )]
            attempts = [dict(row) for row in conn.execute(
                "SELECT question_key,agent_id,prompt_sha256,response_sha256,created_at "
                "FROM attempts WHERE run_id=? ORDER BY question_key,agent_id",
                (run_id,),
            )]
            reviews = [dict(row) for row in conn.execute(
                "SELECT question_key,verdict,answer_consistent,teacher_answer,"
                "prompt_sha256,response_sha256,created_at FROM reviews "
                "WHERE run_id=? ORDER BY question_key",
                (run_id,),
            )]
            blind_rechecks = [dict(row) for row in conn.execute(
                "SELECT question_key,answer,question_valid,matched,"
                "question_snapshot_sha256,final_content_sha256,prompt_sha256,"
                "response_sha256,created_at FROM blind_rechecks "
                "WHERE run_id=? ORDER BY question_key",
                (run_id,),
            )]
            decisions = [dict(row) for row in conn.execute(
                "SELECT decision_id,question_key,source,answer,created_at FROM decisions "
                "WHERE run_id=? ORDER BY created_at,decision_id",
                (run_id,),
            )]
            skill_versions = [dict(row) for row in conn.execute(
                "SELECT skill_id,version,status,sha256,source_json,created_at "
                "FROM solution_skill_versions WHERE verification_run_id=? "
                "ORDER BY skill_id,version",
                (run_id,),
            )]
        file_hashes: dict[str, str] = {}
        for question in questions:
            for relative in (
                str(question["question_file"]),
                (Path(str(question["node_dir"])) / "answer_final.jsonl").as_posix(),
                (Path(str(question["node_dir"])) / "answer_review.jsonl").as_posix(),
            ):
                path = self.state.bank / relative
                if path.is_file():
                    file_hashes[relative] = sha256_file(path)
        skill_file_hashes: dict[str, str] = {}
        for version in skill_versions:
            version_path = (
                self.state.skill_versions_root
                / str(version["skill_id"])
                / f"v{int(version['version']):04d}"
                / "SKILL.md"
            )
            if version_path.is_file():
                skill_file_hashes[safe_rel(version_path, self.state.bank)] = sha256_file(
                    version_path
                )
            visible = self.state.solution_skills_root / str(version["skill_id"]) / "SKILL.md"
            if visible.is_file():
                skill_file_hashes[safe_rel(visible, self.state.bank)] = sha256_file(visible)
        return {
            "run_id": run_id,
            "questions": questions,
            "attempts": attempts,
            "reviews": reviews,
            "blind_rechecks": blind_rechecks,
            "decisions": decisions,
            "solution_skill_versions": skill_versions,
            "bank_file_sha256_at_finish": dict(sorted(file_hashes.items())),
            "solution_skill_file_sha256_at_finish": dict(sorted(skill_file_hashes.items())),
            "decisions_log_sha256_at_finish": (
                sha256_file(self.state.decisions_path)
                if self.state.decisions_path.is_file()
                else None
            ),
        }

    def _append_run_ledger(self, manifest_path: Path, manifest: dict[str, Any]) -> None:
        with advisory_file_lock(self.state.root / "locks" / "run-ledger.lock"):
            rows, errors = read_jsonl(self.state.ledger_path)
            if errors:
                raise ManagerError("run ledger 损坏: " + errors[0])
            previous = str(rows[-1].get("entry_sha256", "")) if rows else ""
            core = {
                "sequence": len(rows) + 1,
                "run_id": str(manifest.get("run_id", "")),
                "manifest": safe_rel(manifest_path, self.state.root),
                "manifest_sha256": sha256_file(manifest_path),
                "artifact_root_sha256": str(manifest.get("artifact_root_sha256", "")),
                "previous_entry_sha256": previous,
                "created_at": utc_now(),
            }
            append_jsonl(
                self.state.ledger_path,
                {**core, "entry_sha256": sha256_text(compact_json(core))},
            )

    def audit_rows(
        self,
        rows: Sequence[sqlite3.Row],
        *,
        run_id: str,
        run_dir: Path,
        guidance: str = "",
        guidance_by_key: dict[str, str] | None = None,
        prefilled: dict[str, dict[str, dict[str, Any]]] | None = None,
        auto_promote: bool = True,
        progress: Callable[[int, str], None] | None = None,
        batch_label: str = "batch-0001",
    ) -> dict[str, int]:
        if not rows:
            return {"final": 0, "disagreement": 0, "invalid": 0, "error": 0}
        requested_count = len(rows)
        rows = claim_question_rows(self.state, rows, run_id)
        if not rows:
            raise ManagerError("所选题目已被另一个 run 占用")
        if progress and len(rows) != requested_count:
            progress(5, f"跳过 {requested_count - len(rows)} 道被其他 run 占用的题")
        keys = [row["question_key"] for row in rows]
        guidance_by_key = guidance_by_key or {}
        question_skills = {
            str(row["question_key"]): solution_skill_context_for_question(self.state, row)
            for row in rows
        }
        questions = [
            sanitized_question(
                row,
                guidance_by_key.get(str(row["question_key"]), guidance),
                solution_skills=question_skills[str(row["question_key"])],
            )
            for row in rows
        ]
        question_images: list[Path] = []
        seen_images: set[Path] = set()
        for row, question in zip(rows, questions):
            image = question_node_image(self.state, row)
            if image:
                resolved = image.resolve()
                question["image_attachment"] = safe_rel(resolved, self.state.bank)
                if resolved not in seen_images:
                    question_images.append(resolved)
                    seen_images.add(resolved)
        batch_dir = run_dir / batch_label
        batch_dir.mkdir(parents=True, exist_ok=True)
        prefilled = prefilled or {}
        candidates: dict[str, dict[str, dict[str, Any]]] = {
            key: dict(prefilled.get(key, {})) for key in keys
        }
        candidate_sources: dict[str, dict[str, str]] = {
            key: {
                agent_id: ""
                for agent_id in candidates[key]
            }
            for key in keys
        }
        candidate_hashes: dict[str, dict[str, str]] = {
            key: {
                agent_id: sha256_text(compact_json(solution))
                for agent_id, solution in candidates[key].items()
            }
            for key in keys
        }
        counts = {"final": 0, "disagreement": 0, "invalid": 0, "error": 0}
        if prefilled:
            with self.state.connect() as conn:
                for key in keys:
                    for attempt in conn.execute(
                        "SELECT agent_id,response_sha256 FROM attempts "
                        "WHERE question_key=? AND run_id=?",
                        (key, run_id),
                    ):
                        if attempt["agent_id"] in candidates[key]:
                            candidate_sources[key][attempt["agent_id"]] = str(
                                attempt["response_sha256"]
                            )

        solver_template = load_prompt("solver-prompt.md")
        agents_to_run = [
            agent_id
            for agent_id in ("solver1", "solver2", "solver3")
            if not all(agent_id in candidates[key] for key in keys)
        ]

        def run_solver(agent_id: str) -> tuple[str, dict[str, Any], dict[str, Any], Path]:
            prompt = render_prompt(
                solver_template,
                {
                    "AGENT_ID": agent_id,
                    "SOLVER_LENS": SOLVER_LENSES[agent_id],
                    "QUESTION_BATCH_JSON": pretty_json({"questions": questions}),
                },
            )
            invocation_dir = batch_dir / agent_id
            payload, meta = self.runner.run(
                role=agent_id,
                prompt=prompt,
                schema_path=SCRIPT_ROOT / "solver_batch.schema.json",
                invocation_dir=invocation_dir,
                request={"questions": questions, "agent_id": agent_id},
                images=question_images,
                progress=(lambda msg: progress(20, f"{agent_id}: {msg}")) if progress else None,
            )
            return agent_id, payload, meta, invocation_dir

        try:
            if agents_to_run:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(agents_to_run), thread_name_prefix="solver"
                ) as executor:
                    futures = [executor.submit(run_solver, agent_id) for agent_id in agents_to_run]
                    completed = 0
                    for future in concurrent.futures.as_completed(futures):
                        agent_id, payload, meta, invocation_dir = future.result()
                        completed += 1
                        solutions = payload.get("solutions", [])
                        by_id = {
                            str(item.get("id", "")): item
                            for item in solutions
                            if isinstance(item, dict)
                        }
                        for key in keys:
                            if key not in by_id:
                                continue
                            candidates[key][agent_id] = by_id[key]
                            candidate_hashes[key][agent_id] = sha256_text(
                                compact_json(by_id[key])
                            )
                            candidate_sources[key][agent_id] = str(
                                meta.get("response_sha256", "")
                            )
                            store_attempt(
                                self.state,
                                key=key,
                                run_id=run_id,
                                agent_id=agent_id,
                                solution=by_id[key],
                                invocation_dir=invocation_dir,
                                meta=meta,
                            )
                        if progress:
                            progress(10 + int(50 * completed / max(1, len(agents_to_run))), f"{agent_id} 完成")
            ready_keys: list[str] = []
            for key in keys:
                missing = {"solver1", "solver2", "solver3"} - set(candidates[key])
                if missing:
                    update_question_status(
                        self.state,
                        [key],
                        "error",
                        run_id,
                        verdict=f"缺少候选: {sorted(missing)}",
                    )
                    counts["error"] += 1
                else:
                    ready_keys.append(key)
            if not ready_keys:
                return counts
            ready_key_set = set(ready_keys)

            teacher_request = {
                "questions": [
                    question
                    for question in questions
                    if str(question.get("id", "")) in ready_key_set
                ],
                "candidates": [
                    {
                        "id": key,
                        "solutions": [
                            {
                                "agent_id": agent_id,
                                **candidates[key][agent_id],
                                "candidate_sha256": candidate_hashes[key][agent_id],
                                "source_response_sha256": candidate_sources[key][agent_id],
                            }
                            for agent_id in ("solver1", "solver2", "solver3")
                        ],
                    }
                    for key in ready_keys
                ],
                "user_guidance": guidance,
            }
            teacher_prompt = render_prompt(
                load_prompt("teacher-prompt.md"),
                {"REVIEW_BATCH_JSON": pretty_json(teacher_request)},
            )
            teacher_dir = batch_dir / "teacher"
            if progress:
                progress(70, "Teacher 正在独立核验")
            teacher_payload, teacher_meta = self.runner.run(
                role="teacher",
                prompt=teacher_prompt,
                schema_path=SCRIPT_ROOT / "teacher_batch.schema.json",
                invocation_dir=teacher_dir,
                request=teacher_request,
                images=question_images,
                progress=(lambda msg: progress(75, f"teacher: {msg}")) if progress else None,
            )
            reviews = {
                str(item.get("id", "")): item
                for item in teacher_payload.get("reviews", [])
                if isinstance(item, dict)
            }
            question_payload_by_key = {
                str(question.get("id", "")): question for question in questions
            }
            for row in rows:
                key = row["question_key"]
                if key not in ready_key_set:
                    continue
                review = reviews.get(key)
                if not review:
                    update_question_status(
                        self.state,
                        [key],
                        "error",
                        run_id,
                        verdict="Teacher 缺少该题 review",
                    )
                    counts["error"] += 1
                    continue
                annotation = review.get("question_annotation")
                if isinstance(annotation, dict):
                    store_question_annotation(
                        self.state,
                        key=key,
                        run_id=run_id,
                        annotation=annotation,
                    )
                answers = [
                    normalize_answer(candidates[key][agent_id].get("answer"))
                    for agent_id in ("solver1", "solver2", "solver3")
                ]
                all_valid = all(
                    bool(candidates[key][agent_id].get("question_valid"))
                    for agent_id in ("solver1", "solver2", "solver3")
                )
                options = json.loads(row["question_json"]).get("options") or []
                option_ids = {
                    normalize_answer(option.get("id"))
                    for option in options
                    if isinstance(option, dict)
                }
                if options:
                    exact_guard = len(set(answers)) == 1 and answers[0] in option_ids
                else:
                    exact_guard = bool(review.get("answer_consistent"))
                feedback = review.get("agent_feedback", [])
                feedback_guard = (
                    isinstance(feedback, list)
                    and all(isinstance(item, dict) for item in feedback)
                    and len(feedback) == 3
                    and {str(f.get("agent_id")) for f in feedback} == {"solver1", "solver2", "solver3"}
                    and all(
                        bool(item.get("fully_correct"))
                        and isinstance(item.get("issues"), list)
                        and not item.get("issues")
                        for item in feedback
                    )
                )
                diagnostic_guard = all(
                    candidates[key][agent_id].get("diagnostic_issue_codes_checked", []) == []
                    and candidates[key][agent_id].get("diagnostic_focus_codes_checked", []) == []
                    for agent_id in ("solver1", "solver2", "solver3")
                )
                provided_skill_ids = {
                    str(item.get("skill_id", ""))
                    for item in question_skills.get(key, [])
                    if isinstance(item, dict)
                }
                skill_reference_guard = all(
                    isinstance(candidates[key][agent_id].get("solution_skill_ids_considered", []), list)
                    and set(
                        map(
                            str,
                            candidates[key][agent_id].get("solution_skill_ids_considered", []),
                        )
                    ).issubset(provided_skill_ids)
                    for agent_id in ("solver1", "solver2", "solver3")
                )
                question_payload = question_payload_by_key[key]
                snapshot_guard = (
                    str(question_payload.get("question_snapshot_sha256", ""))
                    == public_question_snapshot_sha256(row)
                )
                annotation_guard = (
                    isinstance(annotation, dict)
                    and annotation.get("validity") == "valid"
                    and not bool(annotation.get("revision_required"))
                )
                retry_contract = review.get("retry_feedback")
                retry_clear_guard = (
                    isinstance(retry_contract, dict)
                    and retry_contract.get("disposition") == "none"
                    and retry_contract.get("issue_codes") == []
                    and retry_contract.get("focus_codes") == []
                )
                effective_auto = bool(
                    auto_promote
                    and review.get("auto_promote")
                    and review.get("verdict") == "pass"
                    and review.get("answer_consistent")
                    and all_valid
                    and exact_guard
                    and feedback_guard
                    and diagnostic_guard
                    and skill_reference_guard
                    and snapshot_guard
                    and annotation_guard
                    and retry_clear_guard
                    and bool(answers[0])
                    and normalize_answer(review.get("teacher_answer")) == answers[0]
                )
                if effective_auto:
                    final_question = json.loads(row["question_json"])
                    final_question["answer"] = str(review.get("teacher_answer", ""))
                    final_question["explanation"] = str(review.get("teacher_solution", ""))
                    try:
                        validate_with_bank_contract(
                            self.state, final_question, node_dir=str(row["node_dir"])
                        )
                    except ManagerError as exc:
                        # Downgrade only this item when Teacher introduces a
                        # format violation; never abort its valid siblings.
                        effective_auto = False
                        review = dict(review)
                        review["auto_promote"] = False
                        review["process_review"] = (
                            str(review.get("process_review", "")).rstrip()
                            + f"\n自动写回被题库 validator 拒绝：{exc}"
                        ).strip()
                store_review(
                    self.state,
                    key=key,
                    run_id=run_id,
                    review=review,
                    invocation_dir=teacher_dir,
                    meta=teacher_meta,
                    effective_auto_promote=effective_auto,
                )
                if effective_auto:
                    accept_final(
                        self.state,
                        key,
                        run_id=run_id,
                        source="teacher_auto_consensus",
                        answer=str(review.get("teacher_answer", "")),
                        solution=str(review.get("teacher_solution", "")),
                    )
                    try:
                        maybe_extract_solution_skill(
                            self.state,
                            review=review,
                            row=row,
                            run_id=run_id,
                            teacher_meta=teacher_meta,
                        )
                    except ManagerError as exc:
                        append_jsonl(
                            self.state.skill_events_path,
                            {
                                "event": "candidate_rejected_by_manager",
                                "question_key": key,
                                "run_id": run_id,
                                "error": str(exc),
                                "created_at": utc_now(),
                            },
                        )
                    counts["final"] += 1
                else:
                    status = "invalid" if review.get("verdict") == "invalid_question" else "disagreement"
                    update_question_status(
                        self.state,
                        [key],
                        status,
                        run_id,
                        verdict=str(review.get("verdict", "")),
                        answer=str(review.get("teacher_answer", "")),
                        solution=str(review.get("teacher_solution", "")),
                    )
                    counts[status] += 1
            if progress:
                progress(95, "Teacher 审核已落盘")
            return counts
        except Exception as exc:
            unfinished = [
                key for key in keys if get_question_row(self.state, key)["status"] == "running"
            ]
            update_question_status(self.state, unfinished, "error", run_id, verdict=str(exc))
            if progress:
                progress(100, f"失败: {exc}")
            raise

    def audit(
        self,
        *,
        scope: ScopeValue,
        subject: str | None,
        qid_like: str | None,
        node_limit: int | None,
        question_limit: int | None,
        batch_size: int,
        include_disagreements: bool,
        force: bool,
        auto_promote: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        scan = scan_bank(self.state, scope, subject)
        files = resolve_scope(self.state.bank, scope)
        allowed_nodes = {safe_rel(path.parent, self.state.bank) for path in files}
        if node_limit is not None:
            allowed_nodes = set(sorted(allowed_nodes)[: max(0, node_limit)])
        statuses = ["pending", "error"]
        if include_disagreements:
            statuses.extend(["disagreement", "invalid"])
        if force:
            statuses = list(STATUSES)
        placeholders_status = ",".join("?" for _ in statuses)
        placeholders_nodes = ",".join("?" for _ in allowed_nodes) or "''"
        params: list[Any] = [*statuses, *sorted(allowed_nodes)]
        sql = (
            f"SELECT * FROM questions WHERE status IN ({placeholders_status}) "
            f"AND node_dir IN ({placeholders_nodes})"
        )
        if subject:
            sql += " AND subject=?"
            params.append(subject)
        if qid_like:
            sql += " AND qid LIKE ?"
            params.append(qid_like)
        sql += " ORDER BY node_dir,qid"
        with self.state.connect() as conn:
            rows = list(conn.execute(sql, params))
        if question_limit is not None:
            rows = rows[: max(0, question_limit)]
        preview = {
            "scan": scan,
            "selected_questions": len(rows),
            "selected_nodes": len({row["node_dir"] for row in rows}),
            "dry_run": dry_run,
        }
        if dry_run or not rows:
            return preview
        run_id = new_run_id("audit")
        run_dir = self.create_manifest(
            run_id,
            "audit",
            {
                "scope": scope,
                "subject": subject,
                "qid_like": qid_like,
                "batch_size": batch_size,
                "selected_questions": len(rows),
                "auto_promote": auto_promote,
            },
        )
        total = {"final": 0, "disagreement": 0, "invalid": 0, "error": 0}
        # Cross-node batching amortizes the substantial fixed context cost of each
        # Codex CLI call. Each item retains its own immutable question_key, so
        # routing back to the correct node remains deterministic.
        batches = [rows[start : start + batch_size] for start in range(0, len(rows), batch_size)]
        for index, batch in enumerate(batches, 1):
            label = f"batch-{index:04d}"
            node_count = len({row["node_dir"] for row in batch})
            print(
                f"[{index}/{len(batches)}] {batch[0]['node_dir']} 起 · "
                f"{len(batch)} 题 / {node_count} 节点",
                flush=True,
            )
            try:
                counts = self.audit_rows(
                    batch,
                    run_id=run_id,
                    run_dir=run_dir,
                    auto_promote=auto_promote,
                    batch_label=label,
                )
            except Exception as exc:
                print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
                total["error"] += len(batch)
                continue
            for key, value in counts.items():
                total[key] += value
        export_unresolved(self.state)
        result = {**preview, "run_id": run_id, "result": total}
        self.finish_manifest(run_dir, result)
        return result

    def blind_recheck(
        self,
        *,
        targets: Sequence[str],
        subject: str | None,
        batch_size: int,
        force: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Independently re-solve current finals without exposing stored answers.

        This is intentionally a separate one-agent delivery gate rather than a
        fourth participant in the original consensus.  It receives only the
        answer-free public snapshot, writes its own hashed invocation, and can
        never promote a question.  A mismatch demotes an existing seed for
        review; a generated mismatch is removed from the authoritative JSONL so
        the normal expansion command can generate a fresh replacement.
        """
        scopes = normalize_target_scopes(self.state.bank, targets)
        scan = scan_bank(self.state, scopes, subject)
        files = resolve_scope(self.state.bank, scopes)
        allowed_nodes = {safe_rel(path.parent, self.state.bank) for path in files}
        placeholders = ",".join("?" for _ in allowed_nodes) or "''"
        params: list[Any] = sorted(allowed_nodes)
        sql = (
            f"SELECT * FROM questions WHERE status='final' "
            f"AND node_dir IN ({placeholders})"
        )
        if subject:
            sql += " AND subject=?"
            params.append(subject)
        sql += " ORDER BY node_dir,qid"
        with self.state.connect() as conn:
            final_rows = list(conn.execute(sql, params))
        rows = [
            row
            for row in final_rows
            if force or latest_valid_blind_recheck(self.state, row) is None
        ]
        preview: dict[str, Any] = {
            "scan": scan,
            "targets": scopes,
            "eligible_finals": len(final_rows),
            "already_certified": len(final_rows) - len(rows),
            "selected_questions": len(rows),
            "batch_size": max(1, batch_size),
            "dry_run": dry_run,
        }
        if dry_run or not rows:
            return preview

        run_id = new_run_id("blind-recheck")
        run_dir = self.create_manifest(
            run_id,
            "blind-recheck",
            {
                "targets": scopes,
                "subject": subject,
                "selected_questions": len(rows),
                "batch_size": max(1, batch_size),
                "force": force,
                "answer_exposure": False,
            },
        )
        result_counts = {
            "passed": 0,
            "generated_rejected": 0,
            "existing_disagreement": 0,
            "error": 0,
        }
        template = load_prompt("blind-recheck-prompt.md")
        batches = [
            rows[start : start + max(1, batch_size)]
            for start in range(0, len(rows), max(1, batch_size))
        ]
        prepared_batches: list[dict[str, Any]] = []
        for index, batch in enumerate(batches, 1):
            label = f"batch-{index:04d}"
            questions: list[dict[str, Any]] = []
            image_paths: list[Path] = []
            seen_images: set[Path] = set()
            expected_hashes: dict[str, str] = {}
            for row in batch:
                question = sanitized_question(row, solution_skills=())
                # Make the absence of any hint or prior work explicit in the
                # persisted request; the prompt is hashed independently too.
                question["user_guidance"] = ""
                image_path = question_node_image(self.state, row)
                if image_path:
                    resolved = image_path.resolve()
                    question["image_attachment"] = safe_rel(resolved, self.state.bank)
                    if resolved not in seen_images:
                        image_paths.append(resolved)
                        seen_images.add(resolved)
                questions.append(question)
                expected_hashes[str(row["question_key"])] = final_content_sha256(row)
            request = {"questions": questions, "agent_id": "blind-recheck"}
            prompt = render_prompt(
                template,
                {"QUESTION_BATCH_JSON": pretty_json({"questions": questions})},
            )
            invocation_dir = run_dir / label / "solver-blind-recheck"
            prepared_batches.append(
                {
                    "index": index,
                    "label": label,
                    "batch": batch,
                    "request": request,
                    "prompt": prompt,
                    "invocation_dir": invocation_dir,
                    "image_paths": image_paths,
                    "expected_hashes": expected_hashes,
                }
            )

        max_parallel_batches = max(
            1,
            min(
                len(prepared_batches),
                int(getattr(self.runner, "max_processes", 1) or 1),
            ),
        )
        preview["max_parallel_batches"] = max_parallel_batches

        def invoke_blind_batch(
            prepared: dict[str, Any],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            index = int(prepared["index"])
            batch = prepared["batch"]
            print(
                f"[盲解 {index}/{len(prepared_batches)}] "
                f"{batch[0]['node_dir']} 起 · {len(batch)} 题",
                flush=True,
            )
            return self.runner.run(
                role="solver-blind-recheck",
                prompt=prepared["prompt"],
                schema_path=SCRIPT_ROOT / "solver_batch.schema.json",
                invocation_dir=prepared["invocation_dir"],
                request=prepared["request"],
                images=prepared["image_paths"],
            )

        invocation_results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        invocation_errors: dict[int, Exception] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel_batches,
            thread_name_prefix="blind-recheck",
        ) as executor:
            future_to_batch = {
                executor.submit(invoke_blind_batch, prepared): prepared
                for prepared in prepared_batches
            }
            for future in concurrent.futures.as_completed(future_to_batch):
                prepared = future_to_batch[future]
                index = int(prepared["index"])
                try:
                    invocation_results[index] = future.result()
                except Exception as exc:
                    invocation_errors[index] = exc

        # Apply model results in stable batch order.  Model calls run in
        # parallel, while source-file writeback remains deterministic.
        for prepared in prepared_batches:
            index = int(prepared["index"])
            label = str(prepared["label"])
            batch = prepared["batch"]
            invocation_dir = prepared["invocation_dir"]
            expected_hashes = prepared["expected_hashes"]
            try:
                if index in invocation_errors:
                    raise invocation_errors[index]
                payload, meta = invocation_results[index]
                raw_solutions = payload.get("solutions", [])
                if not isinstance(raw_solutions, list):
                    raise ManagerError("盲解响应 solutions 不是数组")
                solution_ids = [
                    str(item.get("id", ""))
                    for item in raw_solutions
                    if isinstance(item, dict)
                ]
                expected_ids = [str(row["question_key"]) for row in batch]
                if len(solution_ids) != len(set(solution_ids)):
                    raise ManagerError("盲解响应含重复 id")
                if set(solution_ids) != set(expected_ids):
                    missing = sorted(set(expected_ids) - set(solution_ids))
                    extra = sorted(set(solution_ids) - set(expected_ids))
                    raise ManagerError(
                        f"盲解响应 id 不完整：missing={missing[:3]} extra={extra[:3]}"
                    )
                solutions = {
                    str(item["id"]): item
                    for item in raw_solutions
                    if isinstance(item, dict)
                }
                evaluated: list[
                    tuple[sqlite3.Row, dict[str, Any], str, bool]
                ] = []
                for original_row in batch:
                    key = str(original_row["question_key"])
                    current = get_question_row(self.state, key)
                    expected_hash = expected_hashes[key]
                    if str(current["status"]) != "final":
                        raise ManagerError(f"盲解期间题目状态变化: {key}")
                    if final_content_sha256(current) != expected_hash:
                        raise ManagerError(f"盲解期间题面或最终答案变化: {key}")
                    solution = solutions[key]
                    expected_answer = normalize_answer(
                        json.loads(current["question_json"]).get("answer")
                    )
                    clean_diagnostics = (
                        solution.get("diagnostic_issue_codes_checked") == []
                        and solution.get("diagnostic_focus_codes_checked") == []
                        and solution.get("solution_skill_ids_considered") == []
                    )
                    complete_reasoning = bool(str(solution.get("solution", "")).strip()) and bool(
                        str(solution.get("independent_check", "")).strip()
                    )
                    matched = bool(
                        solution.get("question_valid")
                        and clean_diagnostics
                        and complete_reasoning
                        and expected_answer
                        and normalize_answer(solution.get("answer")) == expected_answer
                    )
                    evaluated.append((current, solution, expected_hash, matched))

                batch_results: list[dict[str, Any]] = []
                for current, solution, expected_hash, matched in evaluated:
                    key = str(current["question_key"])
                    store_blind_recheck(
                        self.state,
                        row=current,
                        run_id=run_id,
                        solution=solution,
                        matched=matched,
                        invocation_dir=invocation_dir,
                        meta=meta,
                        expected_final_sha256=expected_hash,
                    )
                    if matched:
                        result_counts["passed"] += 1
                        outcome = "passed"
                    else:
                        source_kind = str(current["source_kind"])
                        if source_kind == "generated":
                            remove_rejected_generated_final_from_source(self.state, current)
                            status = "invalid"
                            outcome = "generated_rejected"
                        else:
                            status = "disagreement"
                            outcome = "existing_disagreement"
                        with self.state.connect() as conn:
                            cursor = conn.execute(
                                """
                                UPDATE questions SET status=?,teacher_verdict=?,updated_at=?
                                WHERE question_key=? AND status='final' AND question_json=?
                                """,
                                (
                                    status,
                                    "blind_recheck_mismatch",
                                    utc_now(),
                                    key,
                                    current["question_json"],
                                ),
                            )
                        if cursor.rowcount != 1:
                            raise ManagerError(f"盲解淘汰写回发生并发变化: {key}")
                        result_counts[outcome] += 1
                    batch_results.append(
                        {
                            "question_key": key,
                            "id": current["qid"],
                            "source_kind": current["source_kind"],
                            "matched": matched,
                            "outcome": outcome,
                            "expected_final_sha256": expected_hash,
                            "response_sha256": str(meta.get("response_sha256", "")),
                        }
                    )
                atomic_write_json(
                    run_dir / label / "blind-recheck-results.json",
                    {"run_id": run_id, "results": batch_results},
                )
                print(
                    f"  盲解 batch-{index:04d} 已落盘: "
                    f"pass={sum(1 for item in batch_results if item['matched'])} "
                    f"mismatch={sum(1 for item in batch_results if not item['matched'])}",
                    flush=True,
                )
            except Exception as exc:
                result_counts["error"] += len(batch)
                atomic_write_json(
                    run_dir / label / "blind-recheck-error.json",
                    {"run_id": run_id, "error": str(exc), "question_count": len(batch)},
                )
                print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
        export_unresolved(self.state)
        result = {**preview, "run_id": run_id, "result": result_counts}
        self.finish_manifest(run_dir, result)
        return result

    def expand(
        self,
        *,
        scope: ScopeValue,
        subject: str | None,
        node_limit: int | None,
        auto_promote: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        scan = scan_bank(self.state, scope, subject)
        quotas = configured_quotas(self.state.config().get("quotas"))
        files = resolve_scope(self.state.bank, scope)
        nodes: list[
            tuple[
                Path,
                list[dict[str, Any]],
                dict[str, dict[str, int]],
                int,
                set[str],
            ]
        ] = []
        for qfile in files:
            rows, errors = read_jsonl(qfile)
            if errors:
                continue
            if subject and rows and str(rows[0].get("subject", "")) != subject:
                continue
            quota_eligible_ids = expansion_quota_eligible_ids(
                self.state, qfile, rows
            )
            deficits = quota_deficits(
                [
                    row
                    for row in rows
                    if str(row.get("id", "")) in quota_eligible_ids
                ],
                quotas,
            )
            safe_repairs = count_safe_format_repairs(rows)
            if (
                sum(sum(pool.values()) for pool in deficits.values()) > 0
                or any(not q.get("difficulty") or not q.get("pool") for q in rows)
                or safe_repairs > 0
            ):
                nodes.append(
                    (qfile, rows, deficits, safe_repairs, quota_eligible_ids)
                )
        nodes.sort(key=lambda item: safe_rel(item[0], self.state.bank))
        if node_limit is not None:
            nodes = nodes[: max(0, node_limit)]
        preview = {
            "scan": scan,
            "selected_nodes": len(nodes),
            "new_questions_after_classification": {
                "minimum": sum(
                    max(
                        0,
                        sum(sum(pool.values()) for pool in deficits.values())
                        - sum(
                            1
                            for row in rows
                            if str(row.get("id", "")) in quota_eligible_ids
                            and question_counts_toward_quota(row)
                            and (not row.get("difficulty") or not row.get("pool"))
                        ),
                    )
                    for _, rows, deficits, _, quota_eligible_ids in nodes
                ),
                "maximum": sum(
                    sum(sum(pool.values()) for pool in deficits.values())
                    for _, _, deficits, _, _ in nodes
                ),
            },
            "unclassified_existing_questions": sum(
                sum(
                    1
                    for row in rows
                    if not row.get("difficulty") or not row.get("pool")
                )
                for _, rows, _, _, _ in nodes
            ),
            "safe_format_repairs": sum(
                repairs for _, _, _, repairs, _ in nodes
            ),
            "quotas": quotas,
            "dry_run": dry_run,
        }
        if dry_run or not nodes:
            return preview
        run_id = new_run_id("expand")
        run_dir = self.create_manifest(
            run_id,
            "expand",
            {"scope": scope, "subject": subject, "selected_nodes": len(nodes)},
        )
        total = {
            "generated": 0,
            "final": 0,
            "disagreement": 0,
            "invalid": 0,
            "error": 0,
            "normalized": 0,
            "regeneration_needed": 0,
        }
        template = load_prompt("generator-solver-prompt.md")
        for index, (
            qfile,
            existing,
            deficits,
            _,
            quota_eligible_ids,
        ) in enumerate(nodes, 1):
            node_rel = safe_rel(qfile.parent, self.state.bank)
            node_dir = run_dir / f"node-{index:04d}-{sha256_text(node_rel)[:10]}"
            ref_path = qfile.parent / "reference.md"
            reference = ref_path.read_text(encoding="utf-8") if ref_path.exists() else ""
            with self.state.connect() as conn:
                prior_rejected_rows = list(
                    conn.execute(
                        "SELECT question_json,status FROM questions "
                        "WHERE node_dir=? AND source_kind='generated' "
                        "AND status IN ('disagreement','invalid','error') "
                        "ORDER BY updated_at DESC",
                        (node_rel,),
                    )
                )
            rejected_generated_questions: list[dict[str, str]] = []
            rejected_prompts: set[str] = set()
            for rejected_row in prior_rejected_rows:
                rejected_question = json.loads(rejected_row["question_json"])
                rejected_prompt = str(rejected_question.get("prompt", "")).strip()
                if not rejected_prompt or rejected_prompt in rejected_prompts:
                    continue
                rejected_prompts.add(rejected_prompt)
                if len(rejected_generated_questions) < 40:
                    rejected_generated_questions.append(
                        {
                            "difficulty": str(rejected_question.get("difficulty", "")),
                            "pool": str(rejected_question.get("pool", "")),
                            "prompt": rejected_prompt,
                            "status": str(rejected_row["status"]),
                        }
                    )
            request = {
                "node_dir": node_rel,
                "node_id": qfile.parent.name,
                "language_variant": language_variant_for_node(node_rel),
                "subject": str(existing[0].get("subject", qfile.parent.parent.name)) if existing else qfile.parent.parent.name,
                "reference": reference[:30000],
                "existing_questions": [
                    {
                        "id": q.get("id"),
                        "difficulty": q.get("difficulty", ""),
                        "pool": q.get("pool", ""),
                        "quota_eligible": (
                            str(q.get("id", "")) in quota_eligible_ids
                            and question_counts_toward_quota(q)
                        ),
                        "prompt": q.get("prompt", ""),
                        "options": q.get("options", []),
                    }
                    for q in existing[:40]
                ],
                "target_counts": quotas,
                "deficits_before_classification": deficits,
                "rejected_generated_questions": rejected_generated_questions,
            }
            request["solution_skills"] = solution_skill_context_for_text(
                self.state,
                " ".join(
                    [
                        str(request["subject"]),
                        reference,
                        *[
                            str(item.get("prompt", ""))
                            for item in request["existing_questions"]
                            if isinstance(item, dict)
                        ],
                    ]
                ),
            )
            prompt = render_prompt(template, {"NODE_REQUEST_JSON": pretty_json(request)})
            images = [p for p in sorted(qfile.parent.glob("question.*")) if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
            print(f"[{index}/{len(nodes)}] 生成 {node_rel}", flush=True)
            generated_rows: list[sqlite3.Row] = []
            generated_candidate_keys: set[str] = set()
            try:
                total["normalized"] += apply_safe_format_repairs(self.state, qfile)
                needs_generation = (
                    sum(sum(pool.values()) for pool in deficits.values()) > 0
                    or any(
                        not question.get("difficulty") or not question.get("pool")
                        for question in existing
                    )
                )
                if not needs_generation:
                    scan_bank(self.state, safe_rel(qfile, self.state.bank), subject)
                    continue
                payload, meta = self.runner.run(
                    role="generator-solver",
                    prompt=prompt,
                    schema_path=SCRIPT_ROOT / "generator_batch.schema.json",
                    invocation_dir=node_dir / "generator-solver",
                    request=request,
                    images=images[:1],
                )
                classifications = payload.get("classifications", [])
                classified_existing = classified_rows_for_generation(existing, classifications)
                # Classification is an independently validated writeback. Persist it
                # before checking generated siblings so one malformed new item cannot
                # strand all pre-existing questions in an unclassified state.
                apply_classifications(self.state, qfile, classifications)
                expected_counts = quota_deficits(
                    [
                        row
                        for row in classified_existing
                        if str(row.get("id", "")) in quota_eligible_ids
                    ],
                    quotas,
                )
                generated_items = [
                    item for item in payload.get("questions", []) if isinstance(item, dict)
                ]
                prompts = [str(item.get("prompt", "")).strip() for item in generated_items]
                existing_prompts = {str(item.get("prompt", "")).strip() for item in existing}
                if (
                    len(set(prompts)) != len(prompts)
                    or any(prompt in existing_prompts for prompt in prompts)
                    or any(prompt in rejected_prompts for prompt in prompts)
                ):
                    raise ManagerError("生成题与现有题、历史淘汰题或同批生成题重复")
                for generated in generated_items:
                    validate_generated_question(generated)
                selected_items, quota_overflow = cap_generated_to_deficits(
                    generated_items, expected_counts
                )
                prepared: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
                rejected: list[
                    tuple[str, str, dict[str, Any], dict[str, Any], str]
                ] = []
                overflow_ids = {id(item) for item in quota_overflow}
                for generated in [*selected_items, *quota_overflow]:
                    subject_name = str(request["subject"])
                    prompt_hash = sha256_text(str(generated.get("prompt", "")))[:12]
                    qid = f"pb_{subject_name}_{qfile.parent.name}_gen_{prompt_hash}"
                    qfile_rel = safe_rel(qfile, self.state.bank)
                    key = question_key(qfile_rel, qid)
                    if id(generated) not in overflow_ids:
                        generated_candidate_keys.add(key)
                    question = {
                        "id": qid,
                        "nodeId": qfile.parent.name,
                        "subject": subject_name,
                        "difficulty": generated["difficulty"],
                        "pool": generated["pool"],
                        "prompt": generated["prompt"],
                        "options": generated["options"],
                        "answer": generated["answer"],
                        "explanation": generated["solution"],
                        "skillTarget": generated.get("skillTarget", ""),
                        "hint": generated.get("hint", ""),
                    }
                    # Apply the same semantics-preserving punctuation repair
                    # used for legacy rows before the bank contract evaluates
                    # newly generated options such as "$g$，竖直向下".
                    repair_question_option_periods(question)
                    candidate = {
                        "id": key,
                        "answer": generated["answer"],
                        "solution": generated["solution"],
                        "independent_check": generated["independent_check"],
                        "question_valid": True,
                        "confidence": "medium",
                    }
                    if id(generated) in overflow_ids:
                        rejected.append(
                            (key, qid, question, candidate, "超过分类后该格子的精确缺口")
                        )
                    else:
                        try:
                            validate_with_bank_contract(
                                self.state, question, node_dir=node_rel
                            )
                        except ManagerError as exc:
                            rejected.append((key, qid, question, candidate, str(exc)))
                        else:
                            prepared.append((key, qid, question, candidate))
                prefilled: dict[str, dict[str, dict[str, Any]]] = {}
                for key, qid, question, candidate, rejection in rejected:
                    subject_name = str(request["subject"])
                    qfile_rel = safe_rel(qfile, self.state.bank)
                    upsert_question_row(
                        self.state,
                        key=key,
                        qid=qid,
                        node_dir=node_rel,
                        question_file=qfile_rel,
                        subject=subject_name,
                        question=question,
                        source_kind="generated",
                        status_if_new="invalid",
                    )
                    update_question_status(
                        self.state,
                        [key],
                        "invalid",
                        run_id,
                        verdict=f"bank_validator_rejected: {rejection}",
                    )
                    store_attempt(
                        self.state,
                        key=key,
                        run_id=run_id,
                        agent_id="solver1",
                        solution=candidate,
                        invocation_dir=node_dir / "generator-solver",
                        meta=meta,
                    )
                for key, qid, question, candidate in prepared:
                    subject_name = str(request["subject"])
                    qfile_rel = safe_rel(qfile, self.state.bank)
                    upsert_question_row(
                        self.state,
                        key=key,
                        qid=qid,
                        node_dir=node_rel,
                        question_file=qfile_rel,
                        subject=subject_name,
                        question=question,
                        source_kind="generated",
                        status_if_new="running",
                    )
                    prefilled[key] = {"solver1": candidate}
                    store_attempt(
                        self.state,
                        key=key,
                        run_id=run_id,
                        agent_id="solver1",
                        solution=candidate,
                        invocation_dir=node_dir / "generator-solver",
                        meta=meta,
                    )
                    generated_rows.append(get_question_row(self.state, key))
                total["generated"] += len(generated_items)
                total["invalid"] += len(rejected)
                if generated_rows:
                    counts = self.audit_rows(
                        generated_rows,
                        run_id=run_id,
                        run_dir=run_dir,
                        prefilled=prefilled,
                        auto_promote=auto_promote,
                        batch_label=f"node-{index:04d}-verification",
                    )
                    for key, value in counts.items():
                        total[key] += value
                scan_bank(self.state, safe_rel(qfile, self.state.bank), subject)
            except Exception as exc:
                unfinished = [
                    row["question_key"]
                    for row in generated_rows
                    if get_question_row(self.state, row["question_key"])["status"] == "running"
                ]
                if unfinished:
                    update_question_status(
                        self.state,
                        unfinished,
                        "error",
                        run_id,
                        verdict=f"expand_node_failed: {exc}",
                    )
                print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
                total["error"] += 1
            total["regeneration_needed"] += sum(
                1
                for key in generated_candidate_keys
                if str(get_question_row(self.state, key)["status"]) != "final"
            )
        export_unresolved(self.state)
        result = {**preview, "run_id": run_id, "result": total}
        self.finish_manifest(run_dir, result)
        return result

    def run_targets(
        self,
        *,
        targets: Sequence[str],
        mode: str,
        subject: str | None,
        batch_size: int,
        auto_promote: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Run one reproducible workflow over an exact directory/file list."""
        if mode not in {"full", "expand", "audit"}:
            raise ManagerError(f"非法运行模式: {mode}")
        scopes = normalize_target_scopes(self.state.bank, targets)
        result: dict[str, Any] = {
            "mode": mode,
            "targets": scopes,
            "target_count": len(scopes),
            "auto_promote": auto_promote,
            "dry_run": dry_run,
        }
        if mode in {"full", "expand"}:
            result["expand"] = self.expand(
                scope=scopes,
                subject=subject,
                node_limit=None,
                auto_promote=auto_promote,
                dry_run=dry_run,
            )
        if mode in {"full", "audit"}:
            # Passing the full list as one scope lets audit() batch questions
            # across arbitrary directories instead of paying four model calls
            # per directory.
            result["audit"] = self.audit(
                scope=scopes,
                subject=subject,
                qid_like=None,
                node_limit=None,
                question_limit=None,
                batch_size=max(1, batch_size),
                include_disagreements=False,
                force=False,
                auto_promote=auto_promote,
                dry_run=dry_run,
            )
        return result

    def curate_solution_skills(
        self,
        *,
        targets: Sequence[str],
        subject: str | None,
        character_budget: int,
        min_source_questions: int,
        min_source_nodes: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Review historical final solutions in two stages and grow the skill library."""
        scopes = normalize_target_scopes(self.state.bank, targets)
        scan = scan_bank(self.state, scopes, subject)
        files = resolve_scope(self.state.bank, scopes)
        allowed_nodes = {safe_rel(path.parent, self.state.bank) for path in files}
        placeholders = ",".join("?" for _ in allowed_nodes) or "''"
        params: list[Any] = sorted(allowed_nodes)
        sql = f"SELECT * FROM questions WHERE node_dir IN ({placeholders})"
        if subject:
            sql += " AND subject=?"
            params.append(subject)
        sql += " ORDER BY node_dir,qid"
        with self.state.connect() as conn:
            scope_rows = list(conn.execute(sql, params))
        final_rows = [row for row in scope_rows if str(row["status"]) == "final"]
        missing_verified = [
            str(row["question_key"])
            for row in final_rows
            if not str(row["teacher_answer"] or "").strip()
            or not str(row["teacher_solution"] or "").strip()
        ]
        if missing_verified:
            raise ManagerError(
                "final 题缺少核验答案或解法，不能用于 skill 回顾: "
                + missing_verified[0]
            )
        if not final_rows:
            raise ManagerError("指定目录没有可用于回顾的 final 题")
        evidence = [historical_skill_evidence(row) for row in final_rows]
        evidence_by_key = {str(item["question_key"]): item for item in evidence}
        evidence_key_hash = sha256_text(
            compact_json(sorted(evidence_by_key))
        )
        batches = pack_skill_evidence(
            evidence, character_budget=max(20_000, character_budget)
        )
        status_counts: dict[str, int] = {}
        for row in scope_rows:
            status = str(row["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        preview = {
            "targets": scopes,
            "subject": subject,
            "scan": scan,
            "scope_questions": len(scope_rows),
            "scope_nodes": len({str(row["node_dir"]) for row in scope_rows}),
            "status": dict(sorted(status_counts.items())),
            "final_evidence_questions": len(evidence),
            "excluded_nonfinal_questions": len(scope_rows) - len(evidence),
            "evidence_question_keys_sha256": evidence_key_hash,
            "curation_batches": len(batches),
            "character_budget": max(20_000, character_budget),
            "min_source_questions": max(2, min_source_questions),
            "min_source_nodes": max(1, min_source_nodes),
            "novelty_required": False,
            "dry_run": dry_run,
        }
        if dry_run:
            return preview

        run_id = new_run_id("skill-history")
        run_dir = self.create_manifest(
            run_id,
            "historical_solution_skill_curation",
            {
                **preview,
                "dry_run": False,
                "policy": "useful_and_generalized; novelty_is_metadata",
            },
        )
        scope_inventory = [
            {
                "question_key": str(row["question_key"]),
                "display_id": str(row["qid"]),
                "node_dir": str(row["node_dir"]),
                "source_kind": str(row["source_kind"]),
                "status": str(row["status"]),
                "question_snapshot_sha256": public_question_snapshot_sha256(row),
            }
            for row in scope_rows
        ]
        atomic_write_json(run_dir / "scope-inventory.json", scope_inventory)
        atomic_write_json(run_dir / "final-evidence.json", evidence)
        atomic_write_json(
            run_dir / "batch-plan.json",
            [
                {
                    "batch_id": f"batch-{index:04d}",
                    "question_count": len(batch),
                    "question_keys": [item["question_key"] for item in batch],
                    "question_keys_sha256": sha256_text(
                        compact_json(sorted(str(item["question_key"]) for item in batch))
                    ),
                }
                for index, batch in enumerate(batches, 1)
            ],
        )
        existing_skills = active_skill_curation_context(self.state)
        existing_ids = {str(item["skill_id"]) for item in existing_skills}
        batch_prompt_template = load_prompt("skill-history-curator-prompt.md")
        schema_path = SCRIPT_ROOT / "skill_history.schema.json"

        def curate_batch(
            index: int, batch: list[dict[str, Any]]
        ) -> dict[str, Any]:
            batch_id = f"batch-{index:04d}"
            request = {
                "mode": "historical_batch_discovery",
                "batch_id": batch_id,
                "novelty_required": False,
                "min_source_questions": max(2, min_source_questions),
                "min_source_nodes": max(1, min_source_nodes),
                "existing_skills": existing_skills,
                "verified_questions": batch,
            }
            prompt = render_prompt(
                batch_prompt_template,
                {"HISTORICAL_SKILL_REQUEST_JSON": pretty_json(request)},
            )
            invocation_dir = run_dir / "curators" / batch_id
            payload, meta = self.runner.run(
                role="skill-history-curator",
                prompt=prompt,
                schema_path=schema_path,
                invocation_dir=invocation_dir,
                request=request,
            )
            local_evidence = {
                str(item["question_key"]): item for item in batch
            }
            accepted, rejected = validate_historical_skill_candidates(
                payload,
                evidence_by_key=local_evidence,
                existing_skill_ids=existing_ids,
                min_source_questions=max(2, min_source_questions),
                min_source_nodes=max(1, min_source_nodes),
            )
            validation = {
                "batch_id": batch_id,
                "input_questions": len(batch),
                "accepted_candidates": len(accepted),
                "rejected_candidates": rejected,
                "response_sha256": str(meta.get("response_sha256", "")),
            }
            atomic_write_json(invocation_dir / "manager-validation.json", validation)
            return {
                **validation,
                "candidates": accepted,
            }

        try:
            batch_results: list[dict[str, Any]] = []
            worker_count = min(
                len(batches), max(1, int(getattr(self.runner, "max_processes", 1)))
            )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="skill-curator",
            ) as executor:
                future_map = {
                    executor.submit(curate_batch, index, batch): index
                    for index, batch in enumerate(batches, 1)
                }
                for future in concurrent.futures.as_completed(future_map):
                    result = future.result()
                    batch_results.append(result)
                    print(
                        f"[{len(batch_results)}/{len(batches)}] "
                        f"{result['batch_id']} 已回顾 {result['input_questions']} 题，"
                        f"候选 {result['accepted_candidates']}",
                        flush=True,
                    )
            batch_results.sort(key=lambda item: str(item["batch_id"]))
            atomic_write_json(run_dir / "batch-results.json", batch_results)
            batch_candidates: list[dict[str, Any]] = []
            for result in batch_results:
                for candidate_index, candidate in enumerate(result["candidates"], 1):
                    candidate_id = (
                        f"{result['batch_id']}-candidate-{candidate_index:02d}"
                    )
                    enriched = {
                        **candidate,
                        "candidate_id": candidate_id,
                        "batch_id": result["batch_id"],
                        "curator_response_sha256": result["response_sha256"],
                    }
                    batch_candidates.append(enriched)

            consolidated_candidates: list[dict[str, Any]] = []
            consolidation_rejected: list[dict[str, Any]] = []
            consolidation_meta: dict[str, Any] = {}
            lineage_candidates: dict[str, dict[str, Any]] = {}
            if batch_candidates:
                consolidation_request = {
                    "mode": "cross_batch_consolidation",
                    "novelty_required": False,
                    "min_source_questions": max(2, min_source_questions),
                    "min_source_nodes": max(1, min_source_nodes),
                    "coverage": {
                        "targets": scopes,
                        "scope_questions": len(scope_rows),
                        "final_evidence_questions": len(evidence),
                        "evidence_question_keys_sha256": evidence_key_hash,
                    },
                    "existing_skills": existing_skills,
                    "batch_candidates": batch_candidates,
                }
                consolidation_prompt = render_prompt(
                    load_prompt("skill-history-consolidator-prompt.md"),
                    {
                        "HISTORICAL_SKILL_REQUEST_JSON": pretty_json(
                            consolidation_request
                        )
                    },
                )
                consolidation_dir = run_dir / "consolidator"
                payload, consolidation_meta = self.runner.run(
                    role="skill-history-consolidator",
                    prompt=consolidation_prompt,
                    schema_path=schema_path,
                    invocation_dir=consolidation_dir,
                    request=consolidation_request,
                )
                stage_keys = {
                    str(key)
                    for item in batch_candidates
                    for key in item["source_question_keys"]
                }
                stage_evidence = {
                    key: evidence_by_key[key]
                    for key in sorted(stage_keys)
                    if key in evidence_by_key
                }
                lineage_candidates = {
                    str(item["candidate_id"]): item for item in batch_candidates
                }
                consolidated_candidates, consolidation_rejected = (
                    validate_historical_skill_candidates(
                        payload,
                        evidence_by_key=stage_evidence,
                        existing_skill_ids=existing_ids,
                        min_source_questions=max(2, min_source_questions),
                        min_source_nodes=max(1, min_source_nodes),
                        lineage_candidates=lineage_candidates,
                    )
                )
                atomic_write_json(
                    consolidation_dir / "manager-validation.json",
                    {
                        "input_batch_candidates": len(batch_candidates),
                        "accepted_candidates": len(consolidated_candidates),
                        "rejected_candidates": consolidation_rejected,
                        "response_sha256": str(
                            consolidation_meta.get("response_sha256", "")
                        ),
                    },
                )

            activated: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            record_rejected: list[dict[str, Any]] = []
            for item in consolidated_candidates:
                source_keys = list(map(str, item["source_question_keys"]))
                supporting_batches = sorted(
                    {
                        str(lineage_candidates[candidate_id]["batch_id"])
                        for candidate_id in item["source_candidate_ids"]
                    }
                )
                source = {
                    "kind": "historical_verified_solution_curation",
                    "curation_run_id": run_id,
                    "targets": scopes,
                    "scope_question_count": len(scope_rows),
                    "final_evidence_question_count": len(evidence),
                    "evidence_question_keys_sha256": evidence_key_hash,
                    "source_question_keys": source_keys,
                    "source_candidate_ids": item["source_candidate_ids"],
                    "source_question_snapshot_sha256": {
                        key: str(evidence_by_key[key]["question_snapshot_sha256"])
                        for key in source_keys
                    },
                    "source_node_dirs": item["source_node_dirs"],
                    "source_node_count": len(item["source_node_dirs"]),
                    "evidence_diversity": (
                        "cross_node"
                        if len(item["source_node_dirs"]) > 1
                        else "within_node_variants"
                    ),
                    "source_kind_counts": {
                        source_kind: sum(
                            1
                            for key in source_keys
                            if str(evidence_by_key[key].get("source_kind", ""))
                            == source_kind
                        )
                        for source_kind in sorted(
                            {
                                str(evidence_by_key[key].get("source_kind", ""))
                                for key in source_keys
                            }
                        )
                    },
                    "supporting_batches": supporting_batches,
                    "consolidator_response_sha256": str(
                        consolidation_meta.get("response_sha256", "")
                    ),
                    "novelty_required": False,
                    "reuse_rationale": item["reuse_rationale"],
                }
                try:
                    event = record_solution_skill(
                        self.state,
                        candidate=item["skill_candidate"],
                        source=source,
                        verification_run_id=run_id,
                        activate=True,
                    )
                    if event is None:
                        skipped.append(
                            {
                                "name": item["skill_candidate"]["name"],
                                "reason": "duplicate_or_gate",
                            }
                        )
                    else:
                        activated.append(event)
                except ManagerError as exc:
                    record_rejected.append(
                        {
                            "name": item["skill_candidate"].get("name", ""),
                            "error": str(exc),
                        }
                    )
            with self.state.connect() as conn:
                active_total = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM solution_skills WHERE status='active'"
                    ).fetchone()["n"]
                )
            result = {
                **preview,
                "dry_run": False,
                "run_id": run_id,
                "batch_candidates": len(batch_candidates),
                "consolidated_candidates": len(consolidated_candidates),
                "activated_or_updated": len(activated),
                "activated_events": activated,
                "skipped_duplicates": skipped,
                "curator_rejections": sum(
                    len(result["rejected_candidates"]) for result in batch_results
                ),
                "consolidator_rejections": consolidation_rejected,
                "record_rejections": record_rejected,
                "active_skill_total": active_total,
            }
            atomic_write_json(run_dir / "curation-summary.json", result)
            self.finish_manifest(run_dir, result)
            return result
        except Exception as exc:
            atomic_write_json(
                run_dir / "curation-error.json",
                {"error": str(exc), "created_at": utc_now()},
            )
            try:
                manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                if not manifest.get("finished_at"):
                    self.finish_manifest(run_dir, {**preview, "run_id": run_id, "error": str(exc)})
            except Exception:
                pass
            raise


def configured_quotas(value: Any) -> dict[str, dict[str, int]]:
    if value is None:
        value = DEFAULT_QUOTAS
    if not isinstance(value, dict):
        raise ManagerError("config.quotas 必须是 object")
    result: dict[str, dict[str, int]] = {}
    for difficulty in ("low", "mid", "high"):
        pools = value.get(difficulty)
        if not isinstance(pools, dict):
            raise ManagerError(f"config.quotas.{difficulty} 必须是 object")
        result[difficulty] = {}
        for pool in ("display", "exam"):
            count = pools.get(pool)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ManagerError(f"config.quotas.{difficulty}.{pool} 必须是非负整数")
            result[difficulty][pool] = count
    return result


def quota_deficits(
    rows: Sequence[dict[str, Any]],
    quotas: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, int]]:
    quotas = configured_quotas(quotas)
    result: dict[str, dict[str, int]] = {}
    eligible_rows = [row for row in rows if question_counts_toward_quota(row)]
    for difficulty in ("low", "mid", "high"):
        display = sum(
            1
            for q in eligible_rows
            if q.get("difficulty") == difficulty and q.get("pool") == "display"
        )
        exam = sum(
            1
            for q in eligible_rows
            if q.get("difficulty") == difficulty and q.get("pool") == "exam"
        )
        result[difficulty] = {
            "display": max(0, quotas[difficulty]["display"] - display),
            "exam": max(0, quotas[difficulty]["exam"] - exam),
        }
    return result


def question_counts_toward_quota(question: dict[str, Any]) -> bool:
    """Only structurally deliverable single-choice rows satisfy a quota slot."""
    options = question.get("options")
    return bool(
        str(question.get("prompt", "")).strip()
        and question.get("answer") in {"A", "B", "C", "D"}
        and isinstance(options, list)
        and len(options) == 4
        and [
            str(option.get("id", ""))
            for option in options
            if isinstance(option, dict)
        ]
        == ["A", "B", "C", "D"]
        and all(
            isinstance(option, dict) and str(option.get("text", "")).strip()
            for option in options
        )
    )


def expansion_quota_eligible_ids(
    state: State,
    question_file: Path,
    rows: Sequence[dict[str, Any]],
) -> set[str]:
    """Return source ids that may satisfy a future delivery quota.

    Pending/running rows still count during the initial expand-before-audit
    pass.  Once a source seed is known to be disagreement/invalid/error it
    remains in the working bank for audit, but no longer occupies a delivery
    quota slot; a later expand can therefore generate a clean replacement.
    """
    qfile_rel = safe_rel(question_file, state.bank)
    keys = {
        question_key(qfile_rel, str(row.get("id", ""))): str(row.get("id", ""))
        for row in rows
        if str(row.get("id", ""))
    }
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    with state.connect() as conn:
        status_by_key = {
            str(item["question_key"]): str(item["status"])
            for item in conn.execute(
                f"SELECT question_key,status FROM questions "
                f"WHERE question_key IN ({placeholders})",
                list(keys),
            )
        }
    return {
        qid
        for key, qid in keys.items()
        if status_by_key.get(key, "pending") not in UNRESOLVED_STATUSES
    }


def classified_rows_for_generation(
    rows: Sequence[dict[str, Any]], classifications: Any
) -> list[dict[str, Any]]:
    """Apply proposed blank-field classifications in memory and require completeness."""
    if not isinstance(classifications, list):
        raise ManagerError("生成器 classifications 必须是 array")
    ids = {str(row.get("id", "")) for row in rows}
    required = {
        str(row.get("id", ""))
        for row in rows
        if not row.get("difficulty") or not row.get("pool")
    }
    mapping: dict[str, tuple[str, str]] = {}
    for item in classifications:
        if not isinstance(item, dict):
            raise ManagerError("生成器 classification 必须是 object")
        qid = str(item.get("id", ""))
        if qid not in ids:
            raise ManagerError(f"生成器 classification 引用了未知题目: {qid}")
        if qid in mapping:
            raise ManagerError(f"生成器 classification 重复: {qid}")
        difficulty = str(item.get("difficulty", ""))
        pool = str(item.get("pool", ""))
        if difficulty not in {"low", "mid", "high"} or pool not in {"display", "exam"}:
            raise ManagerError(f"生成器 classification 非法: {qid}")
        mapping[qid] = (difficulty, pool)
    missing = required - set(mapping)
    if missing:
        raise ManagerError("生成器缺少现有题分类: " + ", ".join(sorted(missing)))
    result: list[dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        proposed = mapping.get(str(row.get("id", "")))
        if proposed and (not copy.get("difficulty") or not copy.get("pool")):
            copy["difficulty"], copy["pool"] = proposed
        result.append(copy)
    return result


def cap_generated_to_deficits(
    items: Sequence[dict[str, Any]], expected: dict[str, dict[str, int]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep at most the requested cell deficit while preserving model order."""
    used = {
        difficulty: {pool: 0 for pool in ("display", "exam")}
        for difficulty in ("low", "mid", "high")
    }
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for item in items:
        difficulty = str(item.get("difficulty", ""))
        pool = str(item.get("pool", ""))
        if used[difficulty][pool] < expected[difficulty][pool]:
            used[difficulty][pool] += 1
            selected.append(item)
        else:
            overflow.append(item)
    return selected, overflow


SENTENCE_CJK = re.compile(r"[\u4e00-\u9fff]")
SENTENCE_CLAUSE = re.compile(r"[，、；,;]")
SENTENCE_END = re.compile(r"[。！？.!?]\s*$")


def _looks_like_sentence_option(text: str) -> bool:
    value = text.strip()
    if not SENTENCE_CJK.search(value):
        return False
    if SENTENCE_CLAUSE.search(value):
        return True
    return len(SENTENCE_CJK.findall(value)) >= 10


def count_safe_format_repairs(rows: Sequence[dict[str, Any]]) -> int:
    """Count questions whose four sentence options need terminal punctuation."""
    count = 0
    for row in rows:
        if question_options_need_periods(row):
            count += 1
    return count


def question_options_need_periods(question: dict[str, Any]) -> bool:
    options = question.get("options")
    if not isinstance(options, list) or len(options) != 4:
        return False
    texts = [
        str(option.get("text", ""))
        for option in options
        if isinstance(option, dict) and str(option.get("text", "")).strip()
    ]
    return bool(
        len(texts) == 4
        and all(_looks_like_sentence_option(text) for text in texts)
        and any(not SENTENCE_END.search(text.strip()) for text in texts)
    )


def repair_question_option_periods(question: dict[str, Any]) -> bool:
    """Add terminal punctuation when all four options meet the bank's sentence rule."""
    if not question_options_need_periods(question):
        return False
    for option in question["options"]:
        text = str(option.get("text", "")).rstrip()
        if text and not SENTENCE_END.search(text):
            option["text"] = text + "。"
    return True


def apply_safe_format_repairs(state: State, qfile: Path) -> int:
    """Apply semantics-preserving option punctuation repairs under writeback lock."""
    lock_path = state.root / "locks" / "writeback.lock"
    with advisory_file_lock(lock_path):
        rows, errors = read_jsonl(qfile)
        if errors:
            raise ManagerError("格式规范化前发现损坏 JSONL: " + errors[0])
        changed_questions = 0
        for row in rows:
            if repair_question_option_periods(row):
                changed_questions += 1
        if changed_questions:
            atomic_write_jsonl(qfile, rows)
        return changed_questions


def apply_classifications(state: State, qfile: Path, classifications: Any) -> None:
    if not isinstance(classifications, list) or not classifications:
        return
    mapping = {
        str(item.get("id", "")): (item.get("difficulty"), item.get("pool"))
        for item in classifications
        if isinstance(item, dict)
        and item.get("difficulty") in {"low", "mid", "high"}
        and item.get("pool") in {"display", "exam"}
    }
    if not mapping:
        return
    lock_path = state.root / "locks" / "writeback.lock"
    with advisory_file_lock(lock_path):
        rows, errors = read_jsonl(qfile)
        if errors:
            raise ManagerError("分类写回前发现损坏 JSONL: " + errors[0])
        changed = False
        for row in rows:
            item = mapping.get(str(row.get("id", "")))
            if item and (not row.get("difficulty") or not row.get("pool")):
                row["difficulty"], row["pool"] = item
                changed = True
        if changed:
            atomic_write_jsonl(qfile, rows)


def validate_generated_question(item: dict[str, Any]) -> None:
    if item.get("difficulty") not in {"low", "mid", "high"}:
        raise ManagerError("生成题 difficulty 非法")
    if item.get("pool") not in {"display", "exam"}:
        raise ManagerError("生成题 pool 非法")
    if not str(item.get("prompt", "")).strip():
        raise ManagerError("生成题题干为空")
    options = item.get("options")
    if not isinstance(options, list) or len(options) != 4:
        raise ManagerError("生成题必须有 4 个选项")
    if [str(o.get("id", "")) for o in options if isinstance(o, dict)] != ["A", "B", "C", "D"]:
        raise ManagerError("生成题选项 id 必须依次为 A/B/C/D")
    if item.get("answer") not in {"A", "B", "C", "D"}:
        raise ManagerError("生成题 answer 非法")
    if not str(item.get("solution", "")).strip():
        raise ManagerError("生成题试做过程为空")


def validate_with_bank_contract(
    state: State,
    question: dict[str, Any],
    *,
    node_dir: str | None = None,
) -> None:
    """Apply a bank-provided per-question validator before source writeback."""
    validator_path = state.bank / "validate.py"
    if not validator_path.is_file():
        return
    cache_key = f"{validator_path}:{validator_path.stat().st_mtime_ns}"
    check_question = VALIDATOR_CACHE.get(cache_key)
    if check_question is None:
        try:
            namespace = runpy.run_path(str(validator_path), run_name="__qb_validator__")
        except Exception as exc:
            raise ManagerError(f"无法加载题库 validate.py: {exc}") from exc
        candidate = namespace.get("check_question")
        if not callable(candidate):
            raise ManagerError("题库 validate.py 未提供 check_question(q, line_no)")
        check_question = candidate
        VALIDATOR_CACHE.clear()
        VALIDATOR_CACHE[cache_key] = check_question
    try:
        signature = inspect.signature(check_question)
        accepts_locale = (
            "locale" in signature.parameters
            or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        )
        if accepts_locale:
            violations = check_question(
                question,
                1,
                locale=language_variant_for_node(node_dir or ""),
            )
        else:
            violations = check_question(question, 1)
    except Exception as exc:
        raise ManagerError(f"题库 check_question 执行失败: {exc}") from exc
    if violations:
        raise ManagerError("题目未通过题库 validate.py: " + "；".join(map(str, violations[:8])))


def latest_run_for_question(state: State, key: str) -> str | None:
    row = get_question_row(state, key)
    if row["current_run_id"]:
        return str(row["current_run_id"])
    with state.connect() as conn:
        attempt = conn.execute(
            "SELECT run_id FROM attempts WHERE question_key=? ORDER BY created_at DESC LIMIT 1", (key,)
        ).fetchone()
    return str(attempt["run_id"]) if attempt else None


def question_detail(state: State, key: str) -> dict[str, Any]:
    row = get_question_row(state, key)
    run_id = latest_run_for_question(state, key)
    with state.connect() as conn:
        attempts = list(
            conn.execute(
                "SELECT * FROM attempts WHERE question_key=? AND run_id=? ORDER BY agent_id",
                (key, run_id),
            )
        ) if run_id else []
        review = conn.execute(
            "SELECT * FROM reviews WHERE question_key=? AND run_id=?", (key, run_id)
        ).fetchone() if run_id else None
        jobs = list(
            conn.execute(
                "SELECT * FROM jobs WHERE question_key=? ORDER BY created_at DESC LIMIT 10", (key,)
            )
        )
        all_reviews = list(
            conn.execute(
                "SELECT * FROM reviews WHERE question_key=? ORDER BY created_at DESC,run_id DESC",
                (key,),
            )
        )
        all_attempts = list(
            conn.execute(
                "SELECT * FROM attempts WHERE question_key=? ORDER BY created_at DESC,run_id,agent_id",
                (key,),
            )
        )
        annotations = list(
            conn.execute(
                "SELECT * FROM question_annotations WHERE question_key=? "
                "ORDER BY created_at DESC",
                (key,),
            )
        )
    image = question_node_image(state, row)
    node = state.bank / row["node_dir"]
    final_rows, _ = read_jsonl(node / "answer_final.jsonl")
    final = next((item for item in final_rows if str(item.get("id", "")) == row["qid"]), None)
    return {
        "question_key": key,
        "id": row["qid"],
        "node_dir": row["node_dir"],
        "subject": row["subject"],
        "source_kind": row["source_kind"],
        "status": row["status"],
        "current_run_id": run_id,
        "question": json.loads(row["question_json"]),
        "attempts": [
            {
                "agent_id": item["agent_id"],
                "answer": item["answer"],
                "solution": item["solution"],
                "independent_check": item["independent_check"],
                "question_valid": bool(item["question_valid"]),
                "confidence": item["confidence"],
                "run_id": item["run_id"],
                "created_at": item["created_at"],
            }
            for item in attempts
        ],
        "review": (
            {
                "verdict": review["verdict"],
                "answer_consistent": bool(review["answer_consistent"]),
                "teacher_answer": review["teacher_answer"],
                "teacher_solution": review["teacher_solution"],
                "process_review": review["process_review"],
                "agent_feedback": json.loads(review["agent_feedback_json"]),
                "retry_feedback": json.loads(review["raw_json"]).get("retry_feedback"),
                "question_annotation": json.loads(review["raw_json"]).get("question_annotation"),
                "skill_candidate": json.loads(review["raw_json"]).get("skill_candidate"),
                "auto_promote": bool(review["auto_promote"]),
                "run_id": review["run_id"],
                "created_at": review["created_at"],
            }
            if review
            else None
        ),
        "final": final,
        "rounds": [
            {
                "run_id": historical_review["run_id"],
                "verdict": historical_review["verdict"],
                "answer_consistent": bool(historical_review["answer_consistent"]),
                "auto_promote": bool(historical_review["auto_promote"]),
                "teacher_answer": historical_review["teacher_answer"],
                "teacher_solution": historical_review["teacher_solution"],
                "process_review": historical_review["process_review"],
                "retry_feedback": json.loads(historical_review["raw_json"]).get("retry_feedback"),
                "attempts": [
                    {
                        "agent_id": attempt["agent_id"],
                        "answer": attempt["answer"],
                        "solution": attempt["solution"],
                        "independent_check": attempt["independent_check"],
                        "question_valid": bool(attempt["question_valid"]),
                        "confidence": attempt["confidence"],
                    }
                    for attempt in all_attempts
                    if attempt["run_id"] == historical_review["run_id"]
                ],
                "created_at": historical_review["created_at"],
            }
            for historical_review in all_reviews
        ],
        "annotations": [
            {
                "annotation_id": item["annotation_id"],
                "run_id": item["run_id"],
                "status": item["status"],
                "issue_codes": json.loads(item["issue_codes_json"]),
                "summary": item["summary"],
                "proposed_revision": json.loads(item["proposed_revision_json"]),
                "created_at": item["created_at"],
            }
            for item in annotations
        ],
        "has_image": image is not None,
        "jobs": [dict(item) for item in jobs],
        "updated_at": row["updated_at"],
    }


def accept_review_choice(
    state: State,
    key: str,
    *,
    source: str,
    requested_run_id: str | None,
    custom_answer: str = "",
    custom_solution: str = "",
) -> dict[str, Any]:
    """Resolve a human choice against one immutable current-run snapshot."""
    detail = question_detail(state, key)
    current_run_id = str(detail.get("current_run_id") or "") or None
    if requested_run_id and requested_run_id != current_run_id:
        raise ManagerError("页面中的 run 已过期；请刷新后再确认，未写入 answer_final")
    answer = custom_answer
    solution = custom_solution
    if source in {"solver1", "solver2", "solver3"}:
        candidate = next(
            (attempt for attempt in detail["attempts"] if attempt["agent_id"] == source),
            None,
        )
        if not candidate:
            raise ManagerError(f"当前 run 没有 {source} 输出")
        if candidate["run_id"] != current_run_id:
            raise ManagerError("候选输出与当前 run 不一致；请刷新")
        answer = candidate["answer"]
        solution = candidate["solution"]
    elif source == "teacher":
        review = detail.get("review")
        if not review:
            raise ManagerError("当前 run 没有 Teacher 输出")
        if review["run_id"] != current_run_id:
            raise ManagerError("Teacher 输出与当前 run 不一致；请刷新")
        answer = review["teacher_answer"]
        solution = review["teacher_solution"]
    elif source != "custom":
        raise ManagerError("source 只允许 solver1/solver2/solver3/teacher/custom")
    return accept_final(
        state,
        key,
        run_id=current_run_id,
        source=f"human_accept:{source}",
        answer=answer,
        solution=solution,
    )


def list_questions(
    state: State,
    *,
    statuses: Sequence[str],
    subject: str,
    query: str,
    limit: int,
    offset: int,
    review_bucket: str = "",
    node_dirs: set[str] | None = None,
    question_keys: set[str] | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if subject:
        clauses.append("subject=?")
        params.append(subject)
    if query:
        clauses.append("(qid LIKE ? OR node_dir LIKE ? OR question_json LIKE ?)")
        needle = f"%{query}%"
        params.extend([needle, needle, needle])
    if review_bucket == "seed":
        clauses.append("qid LIKE '%_seed_%'")
    elif review_bucket == "candidate":
        clauses.append("qid NOT LIKE '%_seed_%'")
    elif review_bucket:
        raise ManagerError("review_bucket 只允许 seed 或 candidate")
    if node_dirs is not None:
        if node_dirs:
            clauses.append("node_dir IN (" + ",".join("?" for _ in node_dirs) + ")")
            params.extend(sorted(node_dirs))
        else:
            clauses.append("0")
    if question_keys is not None:
        if question_keys:
            clauses.append("question_key IN (" + ",".join("?" for _ in question_keys) + ")")
            params.extend(sorted(question_keys))
        else:
            clauses.append("0")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = (
        " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'disagreement' THEN 1 "
        "WHEN 'invalid' THEN 2 WHEN 'error' THEN 3 WHEN 'pending' THEN 4 ELSE 5 END,"
        "updated_at DESC,node_dir,qid"
    )
    with state.connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM questions" + where, params).fetchone()["n"]
        rows = list(
            conn.execute(
                "SELECT * FROM questions" + where + order + " LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 500)), max(0, offset)],
            )
        )
    items = []
    for row in rows:
        question = json.loads(row["question_json"])
        prompt = str(question.get("prompt", ""))
        items.append(
            {
                "question_key": row["question_key"],
                "id": row["qid"],
                "node_dir": row["node_dir"],
                "subject": row["subject"],
                "status": row["status"],
                "prompt": prompt,
                "teacher_answer": row["teacher_answer"],
                "updated_at": row["updated_at"],
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_solution_skills(
    state: State, *, query: str, limit: int, offset: int
) -> dict[str, Any]:
    clauses = ["status='active'"]
    params: list[Any] = []
    if query:
        clauses.append("(skill_id LIKE ? OR name LIKE ? OR description LIKE ? OR tags_json LIKE ?)")
        needle = f"%{query}%"
        params.extend([needle, needle, needle, needle])
    where = " WHERE " + " AND ".join(clauses)
    page_limit = max(1, min(limit, 200))
    page_offset = max(0, offset)
    with state.connect() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM solution_skills" + where, params
            ).fetchone()["n"]
        )
        rows = list(
            conn.execute(
                "SELECT * FROM solution_skills" + where
                + " ORDER BY updated_at DESC,skill_id LIMIT ? OFFSET ?",
                [*params, page_limit, page_offset],
            )
        )
    items = [
        {
            "skill_id": row["skill_id"],
            "name": row["name"],
            "description": row["description"],
            "tags": json.loads(row["tags_json"]),
            "current_version": row["current_version"],
            "current_sha256": row["current_sha256"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": page_limit, "offset": page_offset}


def solution_skill_detail(state: State, skill_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_id):
        raise ManagerError("非法 skill_id")
    with state.connect() as conn:
        row = conn.execute(
            "SELECT * FROM solution_skills WHERE skill_id=?", (skill_id,)
        ).fetchone()
        if row is None:
            raise ManagerError(f"未知解题 skill: {skill_id}")
        versions = list(
            conn.execute(
                "SELECT * FROM solution_skill_versions WHERE skill_id=? "
                "ORDER BY version DESC",
                (skill_id,),
            )
        )
        jobs = list(
            conn.execute(
                "SELECT * FROM skill_jobs WHERE skill_id=? ORDER BY created_at DESC LIMIT 20",
                (skill_id,),
            )
        )
    visible = state.solution_skills_root / skill_id / "SKILL.md"
    content = visible.read_text(encoding="utf-8") if visible.is_file() else ""
    if row["status"] == "active" and (
        not visible.is_file() or sha256_text(content) != row["current_sha256"]
    ):
        raise ManagerError(f"解题 skill 文件缺失或 SHA 不匹配: {skill_id}")
    return {
        "skill_id": row["skill_id"],
        "name": row["name"],
        "description": row["description"],
        "tags": json.loads(row["tags_json"]),
        "status": row["status"],
        "current_version": row["current_version"],
        "current_sha256": row["current_sha256"],
        "content": content,
        "metadata": json.loads(row["metadata_json"]),
        "versions": [
            {
                "version": version["version"],
                "status": version["status"],
                "sha256": version["sha256"],
                "content": version["content"],
                "metadata": json.loads(version["metadata_json"]),
                "source": json.loads(version["source_json"]),
                "verification_run_id": version["verification_run_id"],
                "created_at": version["created_at"],
            }
            for version in versions
        ],
        "jobs": [dict(job) for job in jobs],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def summary(
    state: State,
    node_dirs: set[str] | None = None,
    question_keys: set[str] | None = None,
    review_title: str = "",
) -> dict[str, Any]:
    question_clauses: list[str] = []
    question_params: list[Any] = []
    job_clauses: list[str] = []
    job_params: list[Any] = []
    if node_dirs is not None:
        if node_dirs:
            placeholders = ",".join("?" for _ in node_dirs)
            question_clauses.append(f"node_dir IN ({placeholders})")
            question_params.extend(sorted(node_dirs))
            job_clauses.append(f"questions.node_dir IN ({placeholders})")
            job_params.extend(sorted(node_dirs))
        else:
            question_clauses.append("0")
            job_clauses.append("0")
    if question_keys is not None:
        if question_keys:
            placeholders = ",".join("?" for _ in question_keys)
            question_clauses.append(f"question_key IN ({placeholders})")
            question_params.extend(sorted(question_keys))
            job_clauses.append(f"questions.question_key IN ({placeholders})")
            job_params.extend(sorted(question_keys))
        else:
            question_clauses.append("0")
            job_clauses.append("0")
    question_where = " WHERE " + " AND ".join(question_clauses) if question_clauses else ""
    job_where = "".join(f" AND {clause}" for clause in job_clauses)
    with state.connect() as conn:
        by_status = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM questions" + question_where + " GROUP BY status",
            question_params,
        )}
        by_subject = {row["subject"]: row["n"] for row in conn.execute(
            "SELECT subject,COUNT(*) AS n FROM questions" + question_where
            + " GROUP BY subject ORDER BY subject",
            question_params,
        )}
        jobs = [dict(row) for row in conn.execute(
            "SELECT jobs.*,questions.qid AS question_id FROM jobs "
            "JOIN questions USING(question_key) "
            "WHERE jobs.status IN ('queued','running')" + job_where + " ORDER BY jobs.created_at",
            job_params,
        )]
        recent_jobs = [dict(row) for row in conn.execute(
            "SELECT jobs.*,questions.qid AS question_id FROM jobs "
            "JOIN questions USING(question_key) WHERE 1=1" + job_where
            + " ORDER BY jobs.updated_at DESC LIMIT 200",
            job_params,
        )]
        active_skill_jobs = [dict(row) for row in conn.execute(
            "SELECT skill_jobs.*,solution_skills.name AS skill_name FROM skill_jobs "
            "LEFT JOIN solution_skills USING(skill_id) "
            "WHERE skill_jobs.status IN ('queued','running') ORDER BY skill_jobs.created_at"
        )]
        recent_skill_jobs = [dict(row) for row in conn.execute(
            "SELECT skill_jobs.*,solution_skills.name AS skill_name FROM skill_jobs "
            "LEFT JOIN solution_skills USING(skill_id) "
            "ORDER BY skill_jobs.updated_at DESC LIMIT 200"
        )]
    for job in jobs:
        job.setdefault("item_id", job.get("question_key"))
        job.setdefault("item_label", job.get("question_id"))
    for job in recent_jobs:
        job.setdefault("item_id", job.get("question_key"))
        job.setdefault("item_label", job.get("question_id"))
    for job in [*active_skill_jobs, *recent_skill_jobs]:
        job["kind"] = "skill_revision"
        job["item_id"] = job.get("skill_id")
        job["item_label"] = job.get("skill_name") or job.get("skill_id")
    jobs = sorted([*jobs, *active_skill_jobs], key=lambda item: str(item.get("created_at", "")))
    recent_jobs = sorted(
        [*recent_jobs, *recent_skill_jobs],
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )[:200]
    return {
        "total": sum(by_status.values()),
        "status": {name: by_status.get(name, 0) for name in STATUSES},
        "subjects": by_subject,
        "active_jobs": jobs,
        "recent_jobs": recent_jobs,
        "state_root": str(state.root),
        "review_view": {
            "fixed": question_keys is not None,
            "title": review_title,
            "question_count": len(question_keys or ()),
        },
    }


def export_unresolved(state: State) -> dict[str, int]:
    state.ensure()
    placeholders = ",".join("?" for _ in UNRESOLVED_STATUSES)
    with state.connect() as conn:
        rows = list(
            conn.execute(
                f"SELECT question_key FROM questions WHERE status IN ({placeholders}) ORDER BY node_dir,qid",
                UNRESOLVED_STATUSES,
            )
        )
    unresolved: list[dict[str, Any]] = []
    reviews_by_node: dict[str, list[dict[str, Any]]] = {}
    authoritative_ids_by_node: dict[str, set[str]] = {}
    question_files = resolve_scope(state.bank, None)
    for question_file in question_files:
        node_dir = safe_rel(question_file.parent, state.bank)
        source_rows, source_errors = read_jsonl(question_file)
        if source_errors:
            raise ManagerError("export 前 questions.jsonl 损坏: " + source_errors[0])
        authoritative_ids_by_node[node_dir] = {
            str(item.get("id", "")) for item in source_rows
        }
    for item in rows:
        detail = question_detail(state, item["question_key"])
        unresolved.append(
            {
                "question_key": detail["question_key"],
                "id": detail["id"],
                "node_dir": detail["node_dir"],
                "subject": detail["subject"],
                "status": detail["status"],
                "question": detail["question"],
                "attempts": detail["attempts"],
                "teacher_review": detail["review"],
                "updated_at": detail["updated_at"],
            }
        )
    atomic_write_jsonl(state.bank / "错题集.jsonl", unresolved)
    with state.connect() as conn:
        reviewed_keys = [row["question_key"] for row in conn.execute(
            "SELECT DISTINCT question_key FROM reviews"
        )]
    for key in reviewed_keys:
        detail = question_detail(state, key)
        review = detail.get("review")
        if not review:
            continue
        row = get_question_row(state, key)
        # Rejected generated candidates intentionally remain in SQLite and the
        # unresolved audit log, but are not authoritative bank questions.  Do
        # not leak their stale reviews into answer_review.jsonl, where the
        # delivery validator correctly treats them as orphan records.
        if str(row["qid"]) not in authoritative_ids_by_node.get(
            detail["node_dir"], set()
        ):
            continue
        blind = latest_valid_blind_recheck(state, row)
        reviews_by_node.setdefault(detail["node_dir"], []).append(
            {
                "id": detail["id"],
                "question_key": key,
                "student_answers": {a["agent_id"]: a["answer"] for a in detail["attempts"]},
                "answer_consistent": review["answer_consistent"],
                "teacher_answer": review["teacher_answer"],
                "question_snapshot_sha256": public_question_snapshot_sha256(row),
                "teacher_solution_sha256": sha256_text(review["teacher_solution"]),
                "manager_status": detail["status"],
                "auto_promote": review["auto_promote"],
                "correct": review["verdict"] == "pass",
                "teacher_verdict": review["verdict"],
                "process_review": review["process_review"],
                "run_id": review["run_id"],
                "reviewed_on": review["created_at"],
                "blind_recheck": (
                    {
                        "status": "pass",
                        "matched": True,
                        "answer": blind["answer"],
                        "question_valid": bool(blind["question_valid"]),
                        "question_snapshot_sha256": blind[
                            "question_snapshot_sha256"
                        ],
                        "final_content_sha256": blind["final_content_sha256"],
                        "response_sha256": blind["response_sha256"],
                        "run_id": blind["run_id"],
                        "checked_on": blind["created_at"],
                    }
                    if blind is not None
                    else None
                ),
            }
        )
    for node_dir in sorted(authoritative_ids_by_node):
        node_reviews = reviews_by_node.get(node_dir, [])
        atomic_write_jsonl(
            state.bank / node_dir / "answer_review.jsonl",
            sorted(node_reviews, key=lambda item: str(item.get("id", ""))),
        )
    return {
        "unresolved": len(unresolved),
        "review_files": len(authoritative_ids_by_node),
    }


def verify_state(state: State) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not state.db_path.is_file():
        return {
            "ok": False,
            "questions": 0,
            "reviews": 0,
            "agent_attempt_meta": 0,
            "invocations": 0,
            "manifests": 0,
            "ledger_entries": 0,
            "errors": [f"状态数据库不存在: {state.db_path}"],
            "warnings": [],
        }
    database_uri = "file:" + urllib.parse.quote(str(state.db_path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(database_uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite integrity_check: {integrity}")
        rows = list(conn.execute("SELECT * FROM questions"))
        review_rows = list(conn.execute("SELECT * FROM reviews"))
        attempt_rows = list(conn.execute("SELECT * FROM attempts"))
        table_names = {
            str(item["name"])
            for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        skill_rows = (
            list(conn.execute("SELECT * FROM solution_skills"))
            if "solution_skills" in table_names
            else []
        )
        skill_version_rows = (
            list(conn.execute("SELECT * FROM solution_skill_versions"))
            if "solution_skill_versions" in table_names
            else []
        )
        blind_rows = (
            list(conn.execute("SELECT * FROM blind_rechecks"))
            if "blind_rechecks" in table_names
            else []
        )
    finally:
        conn.close()
    attempt_counts: dict[tuple[str, str], int] = {}
    attempts_by_pair: dict[tuple[str, str], list[sqlite3.Row]] = {}
    attempt_response: dict[tuple[str, str, str], str] = {}
    for attempt in attempt_rows:
        pair = (str(attempt["question_key"]), str(attempt["run_id"]))
        attempt_counts[pair] = attempt_counts.get(pair, 0) + 1
        attempts_by_pair.setdefault(pair, []).append(attempt)
        attempt_response[
            (str(attempt["run_id"]), str(attempt["question_key"]), str(attempt["agent_id"]))
        ] = str(attempt["response_sha256"])
    for row in rows:
        try:
            question = json.loads(row["question_json"])
        except json.JSONDecodeError as exc:
            errors.append(f"{row['question_key']}: question_json 损坏: {exc}")
            continue
        if not question.get("id") or not question.get("prompt"):
            errors.append(f"{row['question_key']}: 缺 id/prompt")
        if row["status"] == "final":
            final_path = state.bank / row["node_dir"] / "answer_final.jsonl"
            finals, final_errors = read_jsonl(final_path)
            errors.extend(final_errors)
            matches = [f for f in finals if str(f.get("id", "")) == row["qid"]]
            if len(matches) != 1:
                errors.append(f"{final_path}: {row['qid']} 应恰有一条 final，现为 {len(matches)}")
                continue
            source_path = state.bank / row["question_file"]
            source_rows, source_errors = read_jsonl(source_path)
            errors.extend(source_errors)
            source_matches = [
                item for item in source_rows if str(item.get("id", "")) == row["qid"]
            ]
            if len(source_matches) != 1:
                errors.append(
                    f"{source_path}: final 题 {row['qid']} 在 questions.jsonl 中应恰有一条，"
                    f"现为 {len(source_matches)}"
                )
                continue
            final = matches[0]
            source_question = source_matches[0]
            expected_answer = str(final.get("answer", ""))
            expected_solution = str(final.get("solution", ""))
            if str(source_question.get("answer", "")) != expected_answer:
                errors.append(
                    f"{source_path}: {row['qid']} 的 questions.answer 与 answer_final 不一致"
                )
            if str(source_question.get("explanation", "")) != expected_solution:
                errors.append(
                    f"{source_path}: {row['qid']} 的 questions.explanation 与 answer_final 不一致"
                )
            if compact_json(source_question) != compact_json(question):
                errors.append(
                    f"{row['question_key']}: SQLite question_json 与源 questions.jsonl 不一致"
                )
            if str(row["teacher_answer"] or "") != expected_answer:
                errors.append(f"{row['question_key']}: teacher_answer 与最终答案不一致")
            if str(row["teacher_solution"] or "") != expected_solution:
                errors.append(f"{row['question_key']}: teacher_solution 与最终解析不一致")
    legacy_review_contracts = 0
    for review in review_rows:
        pair = (str(review["question_key"]), str(review["run_id"]))
        n = attempt_counts.get(pair, 0)
        if n != 3:
            message = f"review {review['question_key']} / {review['run_id']} 需要 3 份 attempt，现为 {n}"
            if str(review["run_id"]).startswith("legacy-"):
                warnings.append("legacy " + message)
            else:
                errors.append(message)
        if bool(review["auto_promote"]) and not str(review["run_id"]).startswith("legacy-"):
            try:
                raw_review = json.loads(review["raw_json"])
            except json.JSONDecodeError as exc:
                errors.append(f"review {pair}: raw_json 损坏: {exc}")
                continue
            modern_review_contract = all(
                field in raw_review
                for field in ("retry_feedback", "question_annotation", "skill_candidate")
            )
            if not modern_review_contract:
                legacy_review_contracts += 1
                continue
            feedback = raw_review.get("agent_feedback")
            retry_feedback = raw_review.get("retry_feedback")
            annotation = raw_review.get("question_annotation")
            strict_review = (
                review["verdict"] == "pass"
                and bool(review["answer_consistent"])
                and isinstance(feedback, list)
                and len(feedback) == 3
                and all(
                    isinstance(item, dict)
                    and bool(item.get("fully_correct"))
                    and item.get("issues") == []
                    for item in feedback
                )
                and isinstance(retry_feedback, dict)
                and retry_feedback.get("disposition") == "none"
                and retry_feedback.get("issue_codes") == []
                and retry_feedback.get("focus_codes") == []
                and isinstance(annotation, dict)
                and annotation.get("validity") == "valid"
                and annotation.get("revision_required") is False
            )
            attempts = attempts_by_pair.get(pair, [])
            normalized_answers = [normalize_answer(item["answer"]) for item in attempts]
            strict_attempts = (
                len(attempts) == 3
                and all(bool(item["question_valid"]) for item in attempts)
                and bool(normalized_answers[0])
                and normalize_answer(review["teacher_answer"]) == normalized_answers[0]
            )
            question_row = next(
                (row for row in rows if row["question_key"] == review["question_key"]),
                None,
            )
            if question_row is not None:
                options = json.loads(question_row["question_json"]).get("options") or []
                if options:
                    option_ids = {
                        normalize_answer(item.get("id"))
                        for item in options
                        if isinstance(item, dict)
                    }
                    strict_attempts = strict_attempts and len(set(normalized_answers)) == 1 \
                        and normalized_answers[0] in option_ids
            if not strict_review or not strict_attempts:
                errors.append(f"review {pair}: auto_promote 不满足严格 3+1 证书门槛")
    if legacy_review_contracts:
        warnings.append(
            f"{legacy_review_contracts} 条历史 auto_promote 使用 v1 证书，"
            "无 v2 retry/annotation 字段；已按其原始 artifact 兼容核验"
        )

    question_rows_by_key = {str(row["question_key"]): row for row in rows}
    valid_blind_keys: set[str] = set()
    for blind in blind_rows:
        key = str(blind["question_key"])
        question_row = question_rows_by_key.get(key)
        if question_row is None:
            errors.append(f"blind recheck {key}: 对应题目不存在")
            continue
        invocation_dir = state.root / str(blind["invocation_dir"])
        invocation_root = (
            invocation_dir.parent
            if re.fullmatch(r"try-\d+", invocation_dir.name)
            else invocation_dir
        )
        prompt_path = invocation_root / "prompt.md"
        response_path = invocation_dir / "response.json"
        if not prompt_path.is_file() or sha256_file(prompt_path) != blind["prompt_sha256"]:
            errors.append(f"blind recheck {key}: prompt artifact 缺失或哈希不匹配")
        if not response_path.is_file() or sha256_file(response_path) != blind["response_sha256"]:
            errors.append(f"blind recheck {key}: response artifact 缺失或哈希不匹配")
        if not bool(blind["matched"]):
            continue
        expected_snapshot = public_question_snapshot_sha256(question_row)
        expected_final = final_content_sha256(question_row)
        expected_answer = normalize_answer(
            json.loads(question_row["question_json"]).get("answer")
        )
        if not bool(blind["question_valid"]):
            errors.append(f"blind recheck {key}: matched 记录却未确认 question_valid")
        if str(blind["question_snapshot_sha256"]) != expected_snapshot:
            errors.append(f"blind recheck {key}: 题面快照哈希不匹配")
        if str(blind["final_content_sha256"]) != expected_final:
            errors.append(f"blind recheck {key}: final content 哈希不匹配")
        if normalize_answer(blind["answer"]) != expected_answer:
            errors.append(f"blind recheck {key}: 独立答案与最终答案不一致")
        if (
            bool(blind["question_valid"])
            and str(blind["question_snapshot_sha256"]) == expected_snapshot
            and str(blind["final_content_sha256"]) == expected_final
            and normalize_answer(blind["answer"]) == expected_answer
        ):
            valid_blind_keys.add(key)
    uncertified_final_keys = {
        str(row["question_key"])
        for row in rows
        if str(row["status"]) == "final"
    } - valid_blind_keys
    if uncertified_final_keys:
        warnings.append(
            f"{len(uncertified_final_keys)} 道 final 尚无当前内容的独立盲解证书；"
            "交付 validate.py --delivery 会拒绝这些题"
        )

    versions_by_skill: dict[str, dict[int, sqlite3.Row]] = {}
    for version in skill_version_rows:
        skill_id = str(version["skill_id"])
        version_number = int(version["version"])
        versions_by_skill.setdefault(skill_id, {})[version_number] = version
        content = str(version["content"])
        if sha256_text(content) != version["sha256"]:
            errors.append(f"solution skill {skill_id} v{version_number}: DB content SHA-256 不匹配")
        version_path = state.skill_versions_root / skill_id / f"v{version_number:04d}" / "SKILL.md"
        if not version_path.is_file():
            errors.append(f"{version_path}: skill 历史版本文件缺失")
        elif sha256_file(version_path) != version["sha256"]:
            errors.append(f"{version_path}: skill 历史版本 SHA-256 不匹配")
    for skill in skill_rows:
        skill_id = str(skill["skill_id"])
        try:
            metadata = json.loads(skill["metadata_json"])
            tags = json.loads(skill["tags_json"])
        except json.JSONDecodeError as exc:
            errors.append(f"solution skill {skill_id}: metadata JSON 损坏: {exc}")
            continue
        if not isinstance(metadata, dict) or not isinstance(tags, list):
            errors.append(f"solution skill {skill_id}: metadata/tags 类型错误")
        current_version = int(skill["current_version"])
        current = versions_by_skill.get(skill_id, {}).get(current_version)
        if skill["status"] == "active" and current is None:
            errors.append(f"solution skill {skill_id}: current_version 不存在")
        elif current is not None and current["sha256"] != skill["current_sha256"]:
            errors.append(f"solution skill {skill_id}: current SHA 与版本链不一致")
        if skill["status"] != "active":
            continue
        visible = state.solution_skills_root / skill_id / "SKILL.md"
        if not visible.is_file():
            errors.append(f"{visible}: active skill 文件缺失")
            continue
        if sha256_file(visible) != skill["current_sha256"]:
            errors.append(f"{visible}: active skill SHA-256 不匹配")
        lines = visible.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != "---" or "---" not in lines[1:]:
            errors.append(f"{visible}: SKILL.md frontmatter 缺失")
        else:
            closing = lines[1:].index("---") + 1
            keys = {
                line.split(":", 1)[0].strip()
                for line in lines[1:closing]
                if ":" in line
            }
            if keys != {"name", "description"}:
                errors.append(f"{visible}: frontmatter 只能包含 name/description")
            if not any(line.strip() for line in lines[closing + 1 :]):
                errors.append(f"{visible}: skill 正文为空")

    invocation_files = list(state.runs_dir.rglob("invocation.json"))
    teacher_requests: list[tuple[Path, str, dict[str, Any]]] = []
    solver_inputs: dict[str, dict[str, Any]] = {}
    solver_question_fields = SOLVER_QUESTION_FIELDS
    for invocation_path in invocation_files:
        try:
            invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{invocation_path}: invocation 损坏: {exc}")
            continue
        invocation_dir = invocation_path.parent
        prompt_path = invocation_dir / "prompt.md"
        request_path = invocation_dir / "request.json"
        schema_path = invocation_dir / "output.schema.json"
        prompt_value = ""
        if not prompt_path.exists():
            errors.append(f"{prompt_path}: 缺少 prompt")
        else:
            prompt_value = prompt_path.read_text(encoding="utf-8")
            if sha256_file(prompt_path) != invocation.get("prompt_sha256"):
                errors.append(f"{prompt_path}: prompt SHA-256 不匹配")
        if not request_path.exists():
            errors.append(f"{request_path}: 缺少 request")
        else:
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"{request_path}: request JSON 损坏: {exc}")
            else:
                if sha256_text(compact_json(request)) != invocation.get("request_sha256"):
                    errors.append(f"{request_path}: request SHA-256 不匹配")
                role = str(invocation.get("role", ""))
                if role in {
                    "solver1",
                    "solver2",
                    "solver3",
                    "solver-blind-recheck",
                }:
                    modern_solver_contract = str(
                        invocation.get("contract_version") or ""
                    ) == SCHEMA_VERSION
                    if set(request) != {"questions", "agent_id"}:
                        errors.append(f"{request_path}: solver request 顶层字段不符合白名单")
                    expected_agent_id = (
                        "blind-recheck" if role == "solver-blind-recheck" else role
                    )
                    if str(request.get("agent_id", "")) != expected_agent_id:
                        errors.append(f"{request_path}: agent_id 与 invocation role 不一致")
                    if role != "solver-blind-recheck":
                        batch_key = invocation_path.parent.parent.resolve().as_posix()
                        solver_inputs.setdefault(batch_key, {})[role] = request.get("questions")
                    solver_questions = request.get("questions")
                    if not isinstance(solver_questions, list):
                        errors.append(f"{request_path}: questions 必须是数组")
                        solver_questions = []
                    for question in solver_questions:
                        if not isinstance(question, dict):
                            errors.append(f"{request_path}: solver question 不是 object")
                            continue
                        extra = set(question) - solver_question_fields
                        required = {
                            "id",
                            "display_id",
                            "prompt",
                            "options",
                            "question_type",
                            "user_guidance",
                        }
                        if modern_solver_contract:
                            required.update({"question_snapshot_sha256", "solution_skills"})
                        if extra or not required.issubset(question):
                            errors.append(f"{request_path}: solver question 字段不符合完整白名单")
                        options = question.get("options")
                        if not isinstance(options, list):
                            errors.append(f"{request_path}: solver options 必须是数组")
                            options = []
                        for option in options:
                            if not isinstance(option, dict) or set(option) != {"id", "text"}:
                                errors.append(f"{request_path}: solver option 含非白名单字段")
                        skills = question.get("solution_skills", [])
                        if modern_solver_contract and not isinstance(skills, list):
                            errors.append(f"{request_path}: solution_skills 必须是数组")
                            skills = []
                        allowed_skill_fields = {
                            "skill_id", "name", "description", "version", "sha256",
                            "relevance", "content",
                        }
                        for skill in skills:
                            if not isinstance(skill, dict) or set(skill) != allowed_skill_fields:
                                errors.append(f"{request_path}: solution_skill 字段不符合白名单")
                        feedback_value = question.get("verification_feedback")
                        if feedback_value is not None:
                            feedback_fields = {
                                "round", "issue_codes", "focus_codes", "observed_problems",
                                "required_checks", "safety_note", "feedback_sha256",
                            }
                            if not isinstance(feedback_value, dict) or set(feedback_value) != feedback_fields:
                                errors.append(f"{request_path}: verification_feedback 字段不符合白名单")
                            elif sha256_text(
                                compact_json(
                                    {key: value for key, value in feedback_value.items() if key != "feedback_sha256"}
                                )
                            ) != feedback_value.get("feedback_sha256"):
                                errors.append(f"{request_path}: verification_feedback SHA-256 不匹配")
                        if role == "solver-blind-recheck" and (
                            question.get("user_guidance") != ""
                            or question.get("solution_skills") != []
                            or "verification_feedback" in question
                        ):
                            errors.append(
                                f"{request_path}: 盲解 request 不得含 guidance、skill 或 Teacher feedback"
                            )
                    marker = "QUESTION_BATCH_JSON\n"
                    if marker not in prompt_value:
                        errors.append(f"{prompt_path}: 缺少 QUESTION_BATCH_JSON 边界")
                    else:
                        try:
                            embedded = json.loads(prompt_value.split(marker, 1)[1].strip())
                        except json.JSONDecodeError as exc:
                            errors.append(f"{prompt_path}: 嵌入题目 JSON 无法解析: {exc}")
                        else:
                            if embedded != {"questions": request.get("questions")}:
                                errors.append(f"{prompt_path}: 嵌入题面与 request 不一致")
                elif role == "teacher":
                    try:
                        run_id = invocation_path.relative_to(state.runs_dir).parts[0]
                    except (ValueError, IndexError):
                        run_id = ""
                    teacher_requests.append((request_path, run_id, request))
                    marker = "REVIEW_BATCH_JSON\n"
                    if marker not in prompt_value:
                        errors.append(f"{prompt_path}: 缺少 REVIEW_BATCH_JSON 边界")
                    else:
                        try:
                            embedded = json.loads(prompt_value.split(marker, 1)[1].strip())
                        except json.JSONDecodeError as exc:
                            errors.append(f"{prompt_path}: Teacher 嵌入 JSON 无法解析: {exc}")
                        else:
                            if embedded != request:
                                errors.append(f"{prompt_path}: Teacher 嵌入输入与 request 不一致")
        if not schema_path.exists():
            errors.append(f"{schema_path}: 缺少 output schema")
        elif sha256_file(schema_path) != invocation.get("schema_sha256"):
            errors.append(f"{schema_path}: schema SHA-256 不匹配")

    meta_files = list(state.runs_dir.rglob("meta.json"))
    batches: dict[str, dict[str, dict[str, Any]]] = {}
    for meta_path in meta_files:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{meta_path}: meta 损坏: {exc}")
            continue
        if meta.get("success"):
            response_path = meta_path.parent / "response.json"
            if not response_path.exists():
                errors.append(f"{response_path}: 成功调用缺少 response")
            elif sha256_file(response_path) != meta.get("response_sha256"):
                errors.append(f"{response_path}: response SHA-256 不匹配")
        events_path = meta_path.parent / "events.jsonl"
        if meta.get("events_sha256"):
            if not events_path.exists():
                errors.append(f"{events_path}: meta 声明了 events 但文件缺失")
            elif sha256_file(events_path) != meta.get("events_sha256"):
                errors.append(f"{events_path}: events SHA-256 不匹配")
        event_thread_ids: list[str] = []
        event_types: list[str] = []
        if events_path.exists():
            with events_path.open(encoding="utf-8") as handle:
                for line_no, raw in enumerate(handle, 1):
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        errors.append(f"{events_path}:{line_no}: 非 JSONL 事件: {exc}")
                        continue
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        event_thread_ids.append(str(event["thread_id"]))
                    event_types.append(str(event.get("type", "")))
                    item = event.get("item")
                    item_type = str(item.get("type", "")).lower() if isinstance(item, dict) else ""
                    if any(token in item_type for token in ("tool", "command_execution", "function_call", "mcp")):
                        errors.append(f"{events_path}:{line_no}: 隔离 Agent 发生工具调用 {item_type}")
        meta_thread_id = str(meta.get("thread_id") or "")
        provider_resolved = str(meta.get("provider_resolved") or "codex-cli")
        if meta.get("success"):
            if provider_resolved == "api":
                if event_thread_ids or meta_thread_id:
                    errors.append(f"{meta_path}: API 隔离调用不应复用 CLI thread_id")
                if not any(
                    event_type in {"manager.api_request_completed", "manager.batch_completed"}
                    for event_type in event_types
                ):
                    errors.append(f"{events_path}: API 成功调用缺少完成事件")
            else:
                if len(event_thread_ids) != 1:
                    errors.append(f"{events_path}: 成功调用应恰有一个 thread.started")
                elif meta_thread_id != event_thread_ids[0]:
                    errors.append(f"{meta_path}: thread_id 与 events 不一致")
        stderr_path = meta_path.parent / "stderr.log"
        if meta.get("stderr_sha256"):
            if not stderr_path.exists():
                errors.append(f"{stderr_path}: meta 声明了 stderr 但文件缺失")
            elif sha256_file(stderr_path) != meta.get("stderr_sha256"):
                errors.append(f"{stderr_path}: stderr SHA-256 不匹配")
        profile_value = meta.get("isolation_profile")
        isolation_mode = str(meta.get("isolation_mode", ""))
        command = meta.get("command")
        if not isinstance(command, list):
            errors.append(f"{meta_path}: command 不是数组")
            command = []
        if provider_resolved == "api":
            if command:
                errors.append(f"{meta_path}: API 调用不应记录本地执行命令")
            if isolation_mode != "api-payload-only":
                errors.append(f"{meta_path}: API 调用未声明 api-payload-only 隔离")
            if meta.get("store") is not False or meta.get("tools_enabled") is not False:
                errors.append(f"{meta_path}: API 调用未固定 store=false/tools=false")
            provider_request = meta_path.parents[1] / "provider-request.json"
            if not provider_request.is_file():
                errors.append(f"{provider_request}: API 调用缺少 provider request")
            else:
                if sha256_file(provider_request) != meta.get("provider_request_sha256"):
                    errors.append(f"{provider_request}: provider request SHA-256 不匹配")
                try:
                    provider_body = json.loads(provider_request.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    errors.append(f"{provider_request}: JSON 损坏: {exc}")
                else:
                    if provider_body.get("store") is not False:
                        errors.append(f"{provider_request}: store 必须为 false")
                    if any(key in provider_body for key in ("tools", "conversation", "previous_response_id")):
                        errors.append(f"{provider_request}: 含跨请求状态或工具字段")
                    text_format = provider_body.get("text", {}).get("format", {})
                    if text_format.get("type") != "json_schema" or text_format.get("strict") is not True:
                        errors.append(f"{provider_request}: 未启用 strict JSON schema")
                    serialized_provider = compact_json(provider_body).lower()
                    if "authorization" in serialized_provider or "bearer " in serialized_provider:
                        errors.append(f"{provider_request}: 错误持久化了 API 凭据字段")
        else:
            for required_flag in ("exec", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
                if required_flag not in command:
                    errors.append(f"{meta_path}: command 缺少 {required_flag}")
            try:
                sandbox_index = command.index("--sandbox")
                if command[sandbox_index + 1] != "read-only":
                    raise ValueError
            except (ValueError, IndexError):
                errors.append(f"{meta_path}: command 未固定 Codex read-only sandbox")
            declared_disabled = meta.get("disabled_agent_features")
            if isinstance(declared_disabled, list):
                for feature in declared_disabled:
                    if not any(
                        command[index : index + 2] == ["--disable", feature]
                        for index in range(max(0, len(command) - 1))
                    ):
                        errors.append(f"{meta_path}: command 未实际禁用 {feature}")
            elif meta.get("success"):
                warnings.append(f"{meta_path}: 旧调用未记录 disabled_agent_features")
            if isolation_mode == "soft" and meta.get("success"):
                warnings.append(f"{meta_path}: 使用 soft isolation，不具备 OS 题库读取隔离")
            if isolation_mode == "strict" and not profile_value:
                errors.append(f"{meta_path}: strict isolation 缺少 profile")
        if provider_resolved != "api" and profile_value:
            profile_path = state.root / str(profile_value)
            if not profile_path.exists():
                errors.append(f"{profile_path}: 严格隔离 profile 缺失")
            else:
                if sha256_file(profile_path) != meta.get("isolation_profile_sha256"):
                    errors.append(f"{profile_path}: 隔离 profile SHA-256 不匹配")
                profile = profile_path.read_text(encoding="utf-8")
                bank_literal = AgentRunner._seatbelt_path(state.bank)
                if f"(deny file-read-data (subpath {bank_literal}))" not in profile:
                    errors.append(f"{profile_path}: 未拒绝 bank file-read-data")
                if f"(allow file-read-data (subpath {bank_literal}))" in profile:
                    errors.append(f"{profile_path}: 错误地白名单了整个 bank")
                invocation_dir = meta_path.parents[1]
                for own_file in ("prompt.md", "request.json", "output.schema.json", "invocation.json"):
                    literal = AgentRunner._seatbelt_path(invocation_dir / own_file)
                    if f"(allow file-read-data (literal {literal}))" not in profile:
                        errors.append(f"{profile_path}: 未白名单自身 {own_file}")
                if not command or command[0] != "/usr/bin/sandbox-exec":
                    errors.append(f"{meta_path}: strict command 未经 sandbox-exec")
                if "-f" not in command or str(profile_path) not in command:
                    errors.append(f"{meta_path}: command 未引用记录的 isolation profile")
        if meta.get("success"):
            role = str(meta.get("role", ""))
            try:
                batch = meta_path.parents[2].resolve().as_posix()
            except IndexError:
                batch = meta_path.parent.resolve().as_posix()
            batches.setdefault(batch, {})[role] = meta

    for request_path, run_id, request in teacher_requests:
        candidates = request.get("candidates")
        if not isinstance(candidates, list):
            errors.append(f"{request_path}: Teacher candidates 必须是数组")
            continue
        for candidate_group in candidates:
            if not isinstance(candidate_group, dict):
                errors.append(f"{request_path}: candidate group 不是 object")
                continue
            key = str(candidate_group.get("id", ""))
            solutions = candidate_group.get("solutions")
            if not isinstance(solutions, list) or len(solutions) != 3:
                errors.append(f"{request_path}: {key} 必须有三份候选")
                continue
            agent_ids = {str(solution.get("agent_id", "")) for solution in solutions if isinstance(solution, dict)}
            if agent_ids != {"solver1", "solver2", "solver3"}:
                errors.append(f"{request_path}: {key} 候选 agent_id 集合不完整")
            for solution in solutions:
                if not isinstance(solution, dict):
                    continue
                agent_id = str(solution.get("agent_id", ""))
                candidate_hash = str(solution.get("candidate_sha256", ""))
                candidate_value = {
                    field: value
                    for field, value in solution.items()
                    if field not in {"agent_id", "candidate_sha256", "source_response_sha256"}
                }
                if sha256_text(compact_json(candidate_value)) != candidate_hash:
                    errors.append(f"{request_path}: {key}/{agent_id} candidate SHA-256 不匹配")
                expected_source = attempt_response.get((run_id, key, agent_id), "")
                if not expected_source or solution.get("source_response_sha256") != expected_source:
                    errors.append(f"{request_path}: {key}/{agent_id} source response 链接不匹配")

    for batch, role_inputs in solver_inputs.items():
        values = list(role_inputs.values())
        if len(values) >= 2 and any(value != values[0] for value in values[1:]):
            errors.append(f"{batch}: 同轮 solver 未收到完全相同的题面/feedback/skill 快照")

    for batch, roles in batches.items():
        solvers = [roles.get(agent_id) for agent_id in ("solver1", "solver2", "solver3")]
        if all(solvers):
            providers = {str(meta.get("provider_resolved") or "codex-cli") for meta in solvers if meta}
            if providers == {"api"}:
                response_ids = [
                    str(meta.get("provider_response_id") or "") for meta in solvers if meta
                ]
                if any(not response_id for response_id in response_ids):
                    errors.append(f"{batch}: API solver 缺少独立 provider response id")
                elif len(set(response_ids)) != 3:
                    errors.append(f"{batch}: 三个 API solver 未使用三个独立请求")
            else:
                thread_ids = [str(meta.get("thread_id") or "") for meta in solvers if meta]
                if any(not thread_id for thread_id in thread_ids):
                    warnings.append(f"{batch}: 成功 solver 调用缺少 thread_id，无法核验会话唯一性")
                elif len(set(thread_ids)) != 3:
                    errors.append(f"{batch}: 三个 solver 未使用三个唯一 thread_id")
            teacher = roles.get("teacher")
            if teacher:
                latest_solver_finish = max(str(meta.get("finished_at", "")) for meta in solvers if meta)
                if str(teacher.get("started_at", "")) < latest_solver_finish:
                    errors.append(f"{batch}: Teacher 在三个 solver 全部结束前启动")

    manifests = list(state.runs_dir.glob("*/manifest.json"))
    finished_manifest_hashes: dict[str, str] = {}
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{manifest_path}: manifest 损坏: {exc}")
            continue
        run_id = str(manifest.get("run_id", manifest_path.parent.name))
        if manifest.get("model_requested") is None:
            warnings.append(f"{run_id}: 未固定 --model，不能严格重放模型选择")
        if not manifest.get("finished_at"):
            warnings.append(f"{run_id}: run 尚未完成，跳过 artifact root 核验")
            continue
        expected_inventory = manifest.get("artifact_inventory")
        if not isinstance(expected_inventory, dict):
            warnings.append(f"{run_id}: 旧 manifest 无 artifact inventory")
        else:
            actual_inventory = artifact_inventory(manifest_path.parent, exclude=("manifest.json",))
            if expected_inventory != actual_inventory:
                missing = sorted(set(expected_inventory) - set(actual_inventory))
                added = sorted(set(actual_inventory) - set(expected_inventory))
                changed = sorted(
                    key
                    for key in set(expected_inventory) & set(actual_inventory)
                    if expected_inventory[key] != actual_inventory[key]
                )
                errors.append(
                    f"{run_id}: artifact inventory 不匹配 "
                    f"missing={missing[:3]} added={added[:3]} changed={changed[:3]}"
                )
            if sha256_text(compact_json(expected_inventory)) != manifest.get("artifact_root_sha256"):
                errors.append(f"{run_id}: artifact root SHA-256 不匹配")
        snapshot = manifest.get("writeback_snapshot")
        if not isinstance(snapshot, dict):
            warnings.append(f"{run_id}: 旧 manifest 无 writeback snapshot")
        elif sha256_text(compact_json(snapshot)) != manifest.get("writeback_snapshot_sha256"):
            errors.append(f"{run_id}: writeback snapshot SHA-256 不匹配")
        finished_manifest_hashes[run_id] = sha256_file(manifest_path)

    ledger_rows, ledger_errors = read_jsonl(state.ledger_path)
    errors.extend(ledger_errors)
    previous = ""
    ledger_manifests: dict[str, str] = {}
    for index, entry in enumerate(ledger_rows, 1):
        core = {key: value for key, value in entry.items() if key != "entry_sha256"}
        if entry.get("sequence") != index:
            errors.append(f"run ledger 第 {index} 条 sequence 错误")
        if entry.get("previous_entry_sha256", "") != previous:
            errors.append(f"run ledger 第 {index} 条前向哈希断裂")
        expected_entry_hash = sha256_text(compact_json(core))
        if entry.get("entry_sha256") != expected_entry_hash:
            errors.append(f"run ledger 第 {index} 条 entry SHA-256 不匹配")
        previous = str(entry.get("entry_sha256", ""))
        run_id = str(entry.get("run_id", ""))
        manifest_path = state.root / str(entry.get("manifest", ""))
        if not manifest_path.exists():
            errors.append(f"run ledger 第 {index} 条 manifest 不存在: {manifest_path}")
        elif sha256_file(manifest_path) != entry.get("manifest_sha256"):
            errors.append(f"run ledger 第 {index} 条 manifest SHA-256 不匹配")
        ledger_manifests[run_id] = str(entry.get("manifest_sha256", ""))
    for run_id, manifest_hash in finished_manifest_hashes.items():
        if ledger_manifests.get(run_id) != manifest_hash:
            errors.append(f"{run_id}: 完成的 manifest 未被 run ledger 正确锚定")
    if not meta_files:
        warnings.append("尚无原生 agent 调用 artifact；只有扫描或 legacy 导入")
    return {
        "ok": not errors,
        "questions": len(rows),
        "reviews": len(review_rows),
        "blind_rechecks": len(blind_rows),
        "blind_certified_finals": len(valid_blind_keys),
        "agent_attempt_meta": len(meta_files),
        "solution_skills": len(skill_rows),
        "solution_skill_versions": len(skill_version_rows),
        "invocations": len(invocation_files),
        "manifests": len(manifests),
        "ledger_entries": len(ledger_rows),
        "errors": errors,
        "warnings": warnings,
    }


class JobCoordinator:
    def __init__(self, state: State, pipeline: Pipeline) -> None:
        self.state = state
        self.pipeline = pipeline
        self._recover_orphaned_jobs()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="review-job"
        )

    def _recover_orphaned_jobs(self) -> None:
        """Fail jobs that belonged to the previous server process.

        Background workers are in-process threads. Therefore any queued/running
        row present before this coordinator exists cannot still have a live
        worker in this server process and must not block a retry forever.
        """
        recovered_at = utc_now()
        message = "服务曾重启，原后台任务已中断；可重新提交"
        error = "orphaned_job_recovered_on_server_start"
        with self.state.connect() as conn:
            orphaned = list(
                conn.execute(
                    "SELECT job_id,question_key,run_id FROM jobs "
                    "WHERE status IN ('queued','running')"
                )
            )
            orphaned_skills = list(
                conn.execute(
                    "SELECT job_id,skill_id,run_id FROM skill_jobs "
                    "WHERE status IN ('queued','running')"
                )
            )
            if not orphaned and not orphaned_skills:
                return
            if orphaned:
                conn.execute(
                    "UPDATE jobs SET status='failed',progress=100,message=?,error=?,updated_at=? "
                    "WHERE status IN ('queued','running')",
                    (message, error, recovered_at),
                )
            run_ids = [str(row["run_id"]) for row in orphaned if row["run_id"]]
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                conn.execute(
                    f"UPDATE questions SET status='error',teacher_verdict=?,updated_at=? "
                    f"WHERE status='running' AND current_run_id IN ({placeholders})",
                    [message, recovered_at, *run_ids],
                )
            if orphaned_skills:
                conn.execute(
                    "UPDATE skill_jobs SET status='failed',progress=100,message=?,error=?,updated_at=? "
                    "WHERE status IN ('queued','running')",
                    (message, error, recovered_at),
                )

    def enqueue_resolve(self, key: str, guidance: str) -> dict[str, Any]:
        question_row = get_question_row(self.state, key)
        guidance = guidance.strip()
        job_id = uuid.uuid4().hex
        run_id = new_run_id("resolve")
        created = utc_now()
        with self.state.connect() as conn:
            active = conn.execute(
                "SELECT job_id FROM jobs WHERE question_key=? AND status IN ('queued','running')",
                (key,),
            ).fetchone()
            if active:
                raise ManagerError(f"该题已有进行中的任务: {active['job_id']}")
            conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, key, "resolve", "queued", 0, "等待执行", guidance, run_id, None, created, created),
            )
        self.executor.submit(self._run, job_id, key, guidance, run_id)
        return {
            "job_id": job_id,
            "run_id": run_id,
            "status": "queued",
            "question_id": str(question_row["qid"]),
        }

    def _update(self, job_id: str, status: str, progress: int, message: str, error: str | None = None) -> None:
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?,progress=?,message=?,error=?,updated_at=? WHERE job_id=?",
                (status, max(0, min(progress, 100)), message, error, utc_now(), job_id),
            )

    def enqueue_skill_revision(
        self, skill_id: str, *, base_sha256: str, guidance: str
    ) -> dict[str, Any]:
        guidance = guidance.strip()
        if not guidance:
            raise ManagerError("修订 skill 必须提供 guidance")
        if len(guidance) > 20000:
            raise ManagerError("skill guidance 过长")
        detail = solution_skill_detail(self.state, skill_id)
        if not base_sha256 or base_sha256 != detail["current_sha256"]:
            raise ManagerError("skill 已有更新；请刷新后基于最新 SHA 修订")
        job_id = uuid.uuid4().hex
        run_id = new_run_id("skill-revision")
        created = utc_now()
        with self.state.connect() as conn:
            active = conn.execute(
                "SELECT job_id FROM skill_jobs WHERE skill_id=? "
                "AND status IN ('queued','running')",
                (skill_id,),
            ).fetchone()
            if active:
                raise ManagerError(f"该 skill 已有进行中的修订: {active['job_id']}")
            conn.execute(
                "INSERT INTO skill_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    skill_id,
                    "queued",
                    0,
                    "等待结构化修订",
                    guidance,
                    base_sha256,
                    run_id,
                    None,
                    created,
                    created,
                ),
            )
        self.executor.submit(
            self._run_skill_revision,
            job_id,
            skill_id,
            base_sha256,
            guidance,
            run_id,
        )
        return {"job_id": job_id, "skill_id": skill_id, "status": "queued"}

    def enqueue_skill_extraction(
        self,
        *,
        key: str,
        source_run_id: str | None,
        source: str,
        answer: str,
        solution: str,
    ) -> dict[str, Any]:
        placeholder = f"extract-{key[:16]}"
        job_id = uuid.uuid4().hex
        run_id = new_run_id("skill-extraction")
        created = utc_now()
        with self.state.connect() as conn:
            active = conn.execute(
                "SELECT job_id FROM skill_jobs WHERE skill_id=? "
                "AND status IN ('queued','running')",
                (placeholder,),
            ).fetchone()
            if active:
                return {"job_id": active["job_id"], "skill_id": placeholder, "status": "running"}
            conn.execute(
                "INSERT INTO skill_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    placeholder,
                    "queued",
                    0,
                    "等待提炼已确认解法",
                    "",
                    None,
                    run_id,
                    None,
                    created,
                    created,
                ),
            )
        self.executor.submit(
            self._run_skill_extraction,
            job_id,
            placeholder,
            key,
            source_run_id,
            source,
            answer,
            solution,
            run_id,
        )
        return {"job_id": job_id, "skill_id": placeholder, "status": "queued"}

    def _update_skill_job(
        self,
        job_id: str,
        status: str,
        progress: int,
        message: str,
        error: str | None = None,
        skill_id: str | None = None,
    ) -> None:
        assignments = "status=?,progress=?,message=?,error=?,updated_at=?"
        params: list[Any] = [
            status,
            max(0, min(progress, 100)),
            message,
            error,
            utc_now(),
        ]
        if skill_id:
            assignments += ",skill_id=?"
            params.append(skill_id)
        params.append(job_id)
        with self.state.connect() as conn:
            conn.execute(
                f"UPDATE skill_jobs SET {assignments} WHERE job_id=?",
                params,
            )

    def _run_skill_revision(
        self,
        job_id: str,
        skill_id: str,
        base_sha256: str,
        guidance: str,
        run_id: str,
    ) -> None:
        run_dir: Path | None = None
        try:
            self._update_skill_job(job_id, "running", 5, "正在读取当前 skill 版本")
            detail = solution_skill_detail(self.state, skill_id)
            if detail["current_sha256"] != base_sha256:
                raise ManagerError("skill 在任务启动前已更新；拒绝覆盖新版本")
            request = {
                "skill_id": skill_id,
                "base_version": detail["current_version"],
                "base_sha256": base_sha256,
                "current_content": detail["content"],
                "current_metadata": detail["metadata"],
                "user_guidance": guidance,
            }
            run_dir = self.pipeline.create_manifest(
                run_id,
                "skill_revision",
                {
                    "skill_id": skill_id,
                    "base_version": detail["current_version"],
                    "base_sha256": base_sha256,
                    "guidance_sha256": sha256_text(guidance),
                    "job_id": job_id,
                },
            )
            prompt = render_prompt(
                load_prompt("skill-editor-prompt.md"),
                {"SKILL_EDIT_REQUEST_JSON": pretty_json(request)},
            )
            self._update_skill_job(job_id, "running", 25, "修订 Agent 正在生成完整候选版本")
            payload, meta = self.pipeline.runner.run(
                role="skill-editor",
                prompt=prompt,
                schema_path=SCRIPT_ROOT / "skill_candidate.schema.json",
                invocation_dir=run_dir / "skill-editor",
                request=request,
            )
            candidate = payload.get("skill_candidate")
            if not isinstance(candidate, dict):
                raise ManagerError("skill-editor 未返回修订候选")
            candidate = dict(candidate)
            candidate.update(
                {
                    "action": "update",
                    "related_skill_id": skill_id,
                    "useful": True,
                    "novel": True,
                    "generalized": True,
                }
            )
            self._update_skill_job(job_id, "running", 70, "正在校验结构、基线和版本链")
            latest = solution_skill_detail(self.state, skill_id)
            if latest["current_sha256"] != base_sha256:
                raise ManagerError("skill 在生成期间已有更新；候选已保留在 run 中但未激活")
            event = record_solution_skill(
                self.state,
                candidate=candidate,
                source={
                    "kind": "user_guided_revision",
                    "job_id": job_id,
                    "run_id": run_id,
                    "base_sha256": base_sha256,
                    "guidance_sha256": sha256_text(guidance),
                    "editor_response_sha256": str(meta.get("response_sha256", "")),
                },
                verification_run_id=run_id,
                activate=True,
                expected_base_sha256=base_sha256,
            )
            if event is None:
                raise ManagerError("修订未形成可激活的新版本")
            result = {"job_id": job_id, "skill_event": event}
            self.pipeline.finish_manifest(run_dir, result)
            self._update_skill_job(job_id, "completed", 100, "skill 新版本已校验并激活")
        except Exception as exc:
            traceback.print_exc()
            if run_dir and (run_dir / "manifest.json").is_file():
                try:
                    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                    if not manifest.get("finished_at"):
                        self.pipeline.finish_manifest(run_dir, {"job_id": job_id, "error": str(exc)})
                except Exception:
                    pass
            self._update_skill_job(job_id, "failed", 100, "skill 修订失败", str(exc))

    def _run_skill_extraction(
        self,
        job_id: str,
        placeholder: str,
        key: str,
        source_run_id: str | None,
        source: str,
        answer: str,
        solution: str,
        run_id: str,
    ) -> None:
        run_dir: Path | None = None
        try:
            self._update_skill_job(job_id, "running", 5, "正在准备通用化提炼")
            row = get_question_row(self.state, key)
            existing = [
                {
                    "skill_id": skill["skill_id"],
                    "name": skill["name"],
                    "description": skill["description"],
                    "tags": json.loads(skill["tags_json"]),
                }
                for skill in _active_skill_rows(self.state)
            ]
            request = {
                "question": public_question_snapshot(row),
                "accepted_answer": answer,
                "accepted_solution": solution,
                "acceptance_source": source,
                "existing_skills": existing,
            }
            run_dir = self.pipeline.create_manifest(
                run_id,
                "human_accepted_skill_extraction",
                {
                    "question_key": key,
                    "source_run_id": source_run_id,
                    "acceptance_source": source,
                    "accepted_solution_sha256": sha256_text(solution),
                    "job_id": job_id,
                },
            )
            prompt = render_prompt(
                load_prompt("skill-extractor-prompt.md"),
                {"SKILL_EXTRACTION_REQUEST_JSON": pretty_json(request)},
            )
            self._update_skill_job(job_id, "running", 30, "提炼 Agent 正在判断通用性和复用价值")
            payload, meta = self.pipeline.runner.run(
                role="skill-extractor",
                prompt=prompt,
                schema_path=SCRIPT_ROOT / "skill_candidate.schema.json",
                invocation_dir=run_dir / "skill-extractor",
                request=request,
            )
            candidate = payload.get("skill_candidate")
            event = None
            if isinstance(candidate, dict):
                serialized = compact_json(candidate)
                question = json.loads(row["question_json"])
                forbidden = [str(row["qid"]), str(question.get("prompt", ""))]
                forbidden.extend(
                    str(option.get("text", ""))
                    for option in question.get("options", [])
                    if isinstance(option, dict) and len(str(option.get("text", "")).strip()) >= 4
                )
                if any(item.strip() and item.strip() in serialized for item in forbidden):
                    raise ManagerError("提炼候选仍含题面或专属选项文本")
                event = record_solution_skill(
                    self.state,
                    candidate=candidate,
                    source={
                        "kind": "human_accepted_solution",
                        "question_key": key,
                        "question_id": row["qid"],
                        "source_run_id": source_run_id,
                        "acceptance_source": source,
                        "extraction_run_id": run_id,
                        "extractor_response_sha256": str(meta.get("response_sha256", "")),
                    },
                    verification_run_id=run_id,
                    activate=True,
                )
            self.pipeline.finish_manifest(
                run_dir,
                {"job_id": job_id, "skill_event": event, "created": event is not None},
            )
            actual_skill_id = str(event.get("skill_id")) if event else None
            message = "已提炼并加入 skill 库" if event else "已检查：无需新增重复或低复用 skill"
            self._update_skill_job(
                job_id,
                "completed",
                100,
                message,
                skill_id=actual_skill_id,
            )
        except Exception as exc:
            traceback.print_exc()
            if run_dir and (run_dir / "manifest.json").is_file():
                try:
                    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                    if not manifest.get("finished_at"):
                        self.pipeline.finish_manifest(run_dir, {"job_id": job_id, "error": str(exc)})
                except Exception:
                    pass
            self._update_skill_job(job_id, "failed", 100, "skill 提炼失败", str(exc))

    def _run(self, job_id: str, key: str, guidance: str, run_id: str) -> None:
        try:
            self._update(job_id, "running", 5, "正在准备三个独立解题进程")
            row = get_question_row(self.state, key)
            run_dir = self.pipeline.create_manifest(
                run_id,
                "interactive_resolve",
                {"question_key": key, "guidance": guidance, "job_id": job_id},
            )

            def progress(value: int, message: str) -> None:
                self._update(job_id, "running", value, message)

            counts = self.pipeline.audit_rows(
                [row],
                run_id=run_id,
                run_dir=run_dir,
                guidance=guidance,
                auto_promote=True,
                progress=progress,
                batch_label=f"question-{key[:12]}",
            )
            export_unresolved(self.state)
            result = {"job_id": job_id, "counts": counts}
            self.pipeline.finish_manifest(run_dir, result)
            self._update(job_id, "completed", 100, "三次独立解题与 Teacher 核验已完成")
        except Exception as exc:
            traceback.print_exc()
            self._update(job_id, "failed", 100, "任务失败", str(exc))


class ReviewApplication:
    def __init__(
        self,
        state: State,
        *,
        model: str | None,
        max_agent_processes: int | None,
        isolation_mode: str | None = None,
        provider: str | None = None,
        api_mode: str | None = None,
        allowed_nodes: set[str] | None = None,
        allowed_question_keys: set[str] | None = None,
        review_title: str = "",
    ) -> None:
        self.state = state
        self.allowed_nodes = allowed_nodes
        self.allowed_question_keys = allowed_question_keys
        self.review_title = review_title
        self.pipeline = Pipeline(
            state,
            model=model,
            max_agent_processes=max_agent_processes,
            isolation_mode=isolation_mode,
            provider=provider,
            api_mode=api_mode,
        )
        self.jobs = JobCoordinator(state, self.pipeline)

    def checked_row(self, key: str) -> sqlite3.Row:
        row = get_question_row(self.state, key)
        if self.allowed_nodes is not None and str(row["node_dir"]) not in self.allowed_nodes:
            raise ManagerError("题目不在当前审题台范围内")
        if self.allowed_question_keys is not None and key not in self.allowed_question_keys:
            raise ManagerError("题目不在当前固定审阅清单内")
        return row


class ReviewHandler(http.server.BaseHTTPRequestHandler):
    server_version = "QuestionBankReview/1.0"
    app: ReviewApplication

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024:
            raise ManagerError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ManagerError("请求体必须是 JSON object")
        return value

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if path == "/api/summary":
                self._json(
                    200,
                    summary(
                        self.app.state,
                        self.app.allowed_nodes,
                        self.app.allowed_question_keys,
                        self.app.review_title,
                    ),
                )
                return
            if path == "/api/questions":
                raw_status = (
                    ""
                    if self.app.allowed_question_keys is not None
                    else query.get("status", ["disagreement,invalid,error,running"])[0]
                )
                statuses = [s for s in raw_status.split(",") if s in STATUSES]
                self._json(
                    200,
                    list_questions(
                        self.app.state,
                        statuses=statuses,
                        subject=query.get("subject", [""])[0],
                        query=query.get("q", [""])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                        review_bucket=query.get("review_bucket", [""])[0],
                        node_dirs=self.app.allowed_nodes,
                        question_keys=self.app.allowed_question_keys,
                    ),
                )
                return
            if path == "/api/skills":
                self._json(
                    200,
                    list_solution_skills(
                        self.app.state,
                        query=query.get("query", query.get("q", [""]))[0],
                        limit=int(query.get("limit", ["50"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                    ),
                )
                return
            skill_match = re.fullmatch(r"/api/skills/([a-z0-9]+(?:-[a-z0-9]+)*)", path)
            if skill_match:
                self._json(200, solution_skill_detail(self.app.state, skill_match.group(1)))
                return
            match = re.fullmatch(r"/api/questions/([0-9a-f]{64})", path)
            if match:
                self.app.checked_row(match.group(1))
                self._json(200, question_detail(self.app.state, match.group(1)))
                return
            image_match = re.fullmatch(r"/api/questions/([0-9a-f]{64})/image", path)
            if image_match:
                row = self.app.checked_row(image_match.group(1))
                image_path = question_node_image(self.app.state, row)
                if not image_path:
                    self._error(404, "无题图")
                    return
                data = image_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(image_path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=60")
                self.end_headers()
                self.wfile.write(data)
                return
            if path.startswith("/api/"):
                self._error(404, "未知 API")
                return
            self._serve_asset(path)
        except ManagerError as exc:
            self._error(404, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self._error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._error(403, "拒绝非本地 Origin")
            return
        try:
            path = urllib.parse.urlparse(self.path).path
            accept_match = re.fullmatch(r"/api/questions/([0-9a-f]{64})/accept", path)
            if accept_match:
                key = accept_match.group(1)
                self.app.checked_row(key)
                body = self._body()
                source = str(body.get("source", "teacher"))
                requested_run_id = str(body.get("run_id") or "") or None
                record = accept_review_choice(
                    self.app.state,
                    key,
                    source=source,
                    requested_run_id=requested_run_id,
                    custom_answer=str(body.get("answer", "")),
                    custom_solution=str(body.get("solution", "")),
                )
                export_unresolved(self.app.state)
                extraction = self.app.jobs.enqueue_skill_extraction(
                    key=key,
                    source_run_id=requested_run_id,
                    source=f"human_accept:{source}",
                    answer=str(record["answer"]),
                    solution=str(record["solution"]),
                )
                self._json(200, {"ok": True, "final": record, "skill_extraction": extraction})
                return
            resolve_match = re.fullmatch(r"/api/questions/([0-9a-f]{64})/resolve", path)
            if resolve_match:
                self.app.checked_row(resolve_match.group(1))
                body = self._body()
                result = self.app.jobs.enqueue_resolve(
                    resolve_match.group(1), str(body.get("guidance", ""))
                )
                self._json(202, result)
                return
            skill_revision_match = re.fullmatch(
                r"/api/skills/([a-z0-9]+(?:-[a-z0-9]+)*)/revisions", path
            )
            if skill_revision_match:
                body = self._body()
                result = self.app.jobs.enqueue_skill_revision(
                    skill_revision_match.group(1),
                    base_sha256=str(body.get("base_sha256", "")),
                    guidance=str(body.get("guidance", "")),
                )
                self._json(202, result)
                return
            self._error(404, "未知 API")
        except (ManagerError, json.JSONDecodeError, ValueError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self._error(500, str(exc))

    def _serve_asset(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        asset = (ASSET_ROOT / relative).resolve()
        try:
            asset.relative_to(ASSET_ROOT.resolve())
        except ValueError:
            self._error(403, "非法资源路径")
            return
        if not asset.is_file():
            asset = ASSET_ROOT / "index.html"
        data = asset.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(asset.name)[0] or "application/octet-stream") + ("; charset=utf-8" if asset.suffix in {".html", ".js", ".css"} else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def run_server(
    state: State,
    *,
    host: str,
    port: int,
    model: str | None,
    max_agent_processes: int | None,
    isolation_mode: str | None,
    provider: str | None,
    api_mode: str | None,
    scope: ScopeValue = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ManagerError("审题台含写入能力，只允许绑定本机回环地址")
    state.ensure()
    scan_bank(state, scope)
    allowed_nodes = None
    if scope:
        allowed_nodes = {
            safe_rel(path.parent, state.bank) for path in resolve_scope(state.bank, scope)
        }
    review_title = ""
    allowed_question_keys: set[str] | None = None
    configured_view = state.config().get("review_view")
    if configured_view is not None:
        if not isinstance(configured_view, dict):
            raise ManagerError("config.review_view 必须是 object")
        raw_keys = configured_view.get("question_keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise ManagerError("config.review_view.question_keys 必须是非空数组")
        allowed_question_keys = {str(key) for key in raw_keys}
        if len(allowed_question_keys) != len(raw_keys) or any(
            re.fullmatch(r"[0-9a-f]{64}", key) is None for key in allowed_question_keys
        ):
            raise ManagerError("config.review_view.question_keys 含重复或非法 question_key")
        with state.connect() as conn:
            present = {
                str(row["question_key"])
                for row in conn.execute(
                    "SELECT question_key FROM questions WHERE question_key IN ("
                    + ",".join("?" for _ in allowed_question_keys)
                    + ")",
                    sorted(allowed_question_keys),
                )
            }
        missing = sorted(allowed_question_keys - present)
        if missing:
            raise ManagerError("固定审阅清单含不存在的题目: " + ", ".join(missing[:3]))
        review_title = str(configured_view.get("title") or "固定审阅清单").strip()
    app = ReviewApplication(
        state,
        model=model,
        max_agent_processes=max_agent_processes,
        isolation_mode=isolation_mode,
        provider=provider,
        api_mode=api_mode,
        allowed_nodes=allowed_nodes,
        allowed_question_keys=allowed_question_keys,
        review_title=review_title,
    )
    handler = type("BoundReviewHandler", (ReviewHandler,), {"app": app})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    print(f"审题台: http://{host}:{port}", flush=True)
    print(f"题库: {state.bank}", flush=True)
    if scope:
        print(f"范围: {scope}", flush=True)
    if allowed_question_keys is not None:
        print(f"固定清单: {review_title} · {len(allowed_question_keys)} 题", flush=True)
    print("按 Ctrl-C 停止服务；后台重解任务会在当前进程中运行。", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止审题台…", flush=True)
    finally:
        server.shutdown()
        server.server_close()


def doctor(bank: Path, probe: bool = False) -> dict[str, Any]:
    codex = shutil.which("codex")
    api_available = bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())
    result: dict[str, Any] = {
        "bank": str(bank.resolve()),
        "codex_bin": codex,
        "codex_available": bool(codex),
        "api_key_available": api_available,
        "auto_provider": "api" if api_available else "codex-cli",
        "login": "unknown",
        "no_separate_api_key_required": False,
        "strict_isolation_available": AgentRunner.strict_isolation_backend() is not None,
        "strict_isolation_backend": AgentRunner.strict_isolation_backend(),
    }
    if not codex:
        result["action"] = (
            "OPENAI_API_KEY 已设置，将自动使用 Responses API"
            if api_available
            else "安装并登录 Codex CLI，或设置 OPENAI_API_KEY"
        )
        return result
    version = subprocess.run([codex, "--version"], capture_output=True, text=True, check=False, timeout=15)
    result["codex_version"] = (version.stdout or version.stderr).strip()
    login = subprocess.run(
        [codex, "login", "status"], capture_output=True, text=True, check=False, timeout=30
    )
    login_text = (login.stdout + "\n" + login.stderr).strip()
    result["login"] = login_text
    result["login_exit_code"] = login.returncode
    result["no_separate_api_key_required"] = login.returncode == 0
    if probe:
        result["probe_note"] = "doctor 不自动消耗模型额度；请用 audit --limit 1 做真实端到端探针"
    return result


def add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--bank", type=Path, required=True, help="题库根目录")
    subparser.add_argument("--state-dir", type=Path, help="默认 <bank>/.qb-review")


def add_agent_options(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--model", help="固定模型；API 默认 gpt-5.6-sol，CLI 省略则使用登录默认")
    subparser.add_argument(
        "--provider",
        choices=("auto", "api", "codex-cli"),
        help="auto 在 OPENAI_API_KEY 存在时用 API，否则用已登录 Codex CLI",
    )
    subparser.add_argument(
        "--api-mode",
        choices=("responses", "batch"),
        help="API 执行方式；responses 低延迟，batch 可排队但单次最长等待 24h",
    )
    subparser.add_argument("--max-agent-processes", type=int)
    subparser.add_argument("--retries", type=int)
    subparser.add_argument("--timeout-seconds", type=int)
    subparser.add_argument(
        "--isolation",
        choices=("strict", "soft"),
        help="strict 在 macOS 用 Seatbelt 阻断读取题库/其他 Agent；soft 仅用独立上下文",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="三个隔离解题 Agent + Teacher 的可审计题库审校系统"
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("doctor", help="检查 Codex CLI 与登录状态")
    add_common(p)
    p.add_argument("--probe", action="store_true")

    p = subs.add_parser("init", help="初始化审计状态")
    add_common(p)

    p = subs.add_parser("scan", help="扫描题库并导入兼容 legacy 产物")
    add_common(p)
    p.add_argument("--scope")
    p.add_argument("--subject")

    p = subs.add_parser("status", help="显示状态统计")
    add_common(p)

    p = subs.add_parser("audit", help="核验已有题目")
    add_common(p)
    add_agent_options(p)
    p.add_argument("--scope")
    p.add_argument("--subject")
    p.add_argument("--qid-like", help="仅处理题目 id 匹配该 SQL LIKE 模式的题，例如 %%_seed_%%")
    p.add_argument("--limit", type=int, help="最多处理的节点数")
    p.add_argument("--question-limit", type=int)
    p.add_argument("--batch-size", type=int, default=15)
    p.add_argument("--include-disagreements", action="store_true")
    p.add_argument("--force", action="store_true", help="包含 final；谨慎使用")
    p.add_argument("--no-auto-promote", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = subs.add_parser(
        "blind-recheck",
        help="剥离答案后用独立 Agent 重解 final，生成交付复核证书",
    )
    add_common(p)
    add_agent_options(p)
    p.add_argument(
        "--target",
        action="append",
        required=True,
        help="题库内目录或 questions.jsonl；可重复",
    )
    p.add_argument("--subject")
    p.add_argument("--batch-size", type=int, default=15)
    p.add_argument("--force", action="store_true", help="重新复核已有有效证书的 final")
    p.add_argument("--dry-run", action="store_true")

    p = subs.add_parser("expand", help="生成缺失的举一反三题并核验")
    add_common(p)
    add_agent_options(p)
    p.add_argument("--scope")
    p.add_argument("--subject")
    p.add_argument("--limit", type=int, help="最多处理的节点数")
    p.add_argument("--no-auto-promote", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = subs.add_parser("run", help="对一个或多个精确目录执行一键生成与审校")
    add_common(p)
    add_agent_options(p)
    p.add_argument(
        "--target",
        action="append",
        required=True,
        help="题库内目录或 questions.jsonl；可重复，也可传题库内绝对路径",
    )
    p.add_argument("--mode", choices=("full", "expand", "audit"), default="full")
    p.add_argument("--subject")
    p.add_argument("--batch-size", type=int, default=15)
    p.add_argument("--no-auto-promote", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = subs.add_parser(
        "curate-skills",
        help="回顾一个或多个目录中的 final 解法，合并并生成通用解题 skill",
    )
    add_common(p)
    add_agent_options(p)
    p.add_argument(
        "--target",
        action="append",
        required=True,
        help="题库内目录或 questions.jsonl；可重复，也可传题库内绝对路径",
    )
    p.add_argument("--subject")
    p.add_argument(
        "--character-budget",
        type=int,
        default=240000,
        help="每个历史回顾批次的近似输入字符预算",
    )
    p.add_argument(
        "--min-source-questions",
        type=int,
        default=2,
        help="每个 skill 至少需要的不同 final 题证据数（最小强制为 2）",
    )
    p.add_argument(
        "--min-source-nodes",
        type=int,
        default=1,
        help="每个 skill 至少覆盖的不同节点数；设为 2 可要求跨节点证据",
    )
    p.add_argument("--dry-run", action="store_true")

    p = subs.add_parser("serve", help="启动本地人工审题网页")
    add_common(p)
    add_agent_options(p)
    p.add_argument(
        "--scope",
        action="append",
        help="仅在网页中显示该目录或 glob 范围；多个范围可重复传入",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = subs.add_parser("export", help="导出错题集和 answer_review")
    add_common(p)

    p = subs.add_parser("verify", help="检查数据库、final 和 artifact 哈希")
    add_common(p)
    return parser


def state_from_args(args: argparse.Namespace) -> State:
    return State(args.bank, args.state_dir)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            print(pretty_json(doctor(args.bank, args.probe)), end="")
            return 0
        state = state_from_args(args)
        if args.command == "init":
            state.ensure()
            print(pretty_json({"ok": True, "state_root": str(state.root)}), end="")
            return 0
        if args.command == "scan":
            print(pretty_json(scan_bank(state, args.scope, args.subject)), end="")
            return 0
        if args.command == "status":
            state.ensure()
            print(pretty_json(summary(state)), end="")
            return 0
        if args.command in {
            "audit",
            "blind-recheck",
            "expand",
            "run",
            "curate-skills",
        }:
            state.ensure()
            pipeline = Pipeline(
                state,
                model=args.model,
                max_agent_processes=args.max_agent_processes,
                retries=args.retries,
                timeout_seconds=args.timeout_seconds,
                isolation_mode=args.isolation,
                provider=args.provider,
                api_mode=args.api_mode,
            )
            if args.command == "audit":
                result = pipeline.audit(
                    scope=args.scope,
                    subject=args.subject,
                    qid_like=args.qid_like,
                    node_limit=args.limit,
                    question_limit=args.question_limit,
                    batch_size=max(1, args.batch_size),
                    include_disagreements=args.include_disagreements,
                    force=args.force,
                    auto_promote=not args.no_auto_promote,
                    dry_run=args.dry_run,
                )
            elif args.command == "blind-recheck":
                result = pipeline.blind_recheck(
                    targets=args.target,
                    subject=args.subject,
                    batch_size=max(1, args.batch_size),
                    force=args.force,
                    dry_run=args.dry_run,
                )
            elif args.command == "expand":
                result = pipeline.expand(
                    scope=args.scope,
                    subject=args.subject,
                    node_limit=args.limit,
                    auto_promote=not args.no_auto_promote,
                    dry_run=args.dry_run,
                )
            elif args.command == "run":
                result = pipeline.run_targets(
                    targets=args.target,
                    mode=args.mode,
                    subject=args.subject,
                    batch_size=max(1, args.batch_size),
                    auto_promote=not args.no_auto_promote,
                    dry_run=args.dry_run,
                )
            else:
                result = pipeline.curate_solution_skills(
                    targets=args.target,
                    subject=args.subject,
                    character_budget=max(20_000, args.character_budget),
                    min_source_questions=max(2, args.min_source_questions),
                    min_source_nodes=max(1, args.min_source_nodes),
                    dry_run=args.dry_run,
                )
            print(pretty_json(result), end="")
            return 0
        if args.command == "serve":
            run_server(
                state,
                host=args.host,
                port=args.port,
                model=args.model,
                max_agent_processes=args.max_agent_processes,
                isolation_mode=args.isolation,
                provider=args.provider,
                api_mode=args.api_mode,
                scope=args.scope,
            )
            return 0
        if args.command == "export":
            print(pretty_json(export_unresolved(state)), end="")
            return 0
        if args.command == "verify":
            result = verify_state(state)
            print(pretty_json(result), end="")
            return 0 if result["ok"] else 1
        raise ManagerError(f"未知命令: {args.command}")
    except ManagerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
