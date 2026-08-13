# Question-bank consensus workflow

This repository includes a reusable Codex skill for large-scale question-bank
generation, three-way independent solving, single-pass Teacher verification,
replacement generation for rejected generated questions,
auditable final writeback, strict delivery validation, and browser-based human review.
Unresolved seed questions are never discarded: they stay in a Git-portable
review queue and appear by default in the website's **待审查** view.

## Included files

- `.agents/skills/question-bank-consensus-manager/`: manager skill, prompts,
  schemas, runner, review website, and tests.
- `practice-bank-expansion-pack/validate.py`: static JSONL
  and formatting validator.
- `practice-bank-expansion-pack/解题技能库-物理/`: 32
  versioned, reusable physics solution skills with provenance metadata.
- `practice-bank-expansion-pack/review-queue/unresolved.jsonl`: portable
  unresolved evidence used to reconstruct the human-review queue in a fresh clone.

Model credentials, `.qb-review` databases, and private invocation artifacts are
intentionally not included. The portable queue contains only the unresolved
question snapshot, three current attempts, Teacher review, and staged annotation.

## Use from Codex

Open a Codex task in a project that contains the skill and send a prompt like:

```text
使用 question-bank-consensus-manager skill。
题库根目录：/absolute/path/to/practice-bank-expansion-pack
目标目录：
- cn-nanjing-g11-2026/物理
- cn-guangxi-g11-2026/物理

先执行 doctor 和 dry-run；通过后运行完整流程。自动选择 API 或已登录的
Codex CLI，使用 strict isolation。只有三份独立解答和 Teacher 核验全部
通过时才同步写入 questions.jsonl 和内部 answer_final；seed 的 disagreement、
invalid、error 全部进入网页“待审查”，只有生成题可以淘汰并重新生成。
```

The full operating contract and commands are in
`.agents/skills/question-bank-consensus-manager/SKILL.md`.

## Prepare a clean delivery

After the manager run and `export`, create a new package outside the source bank:

```bash
python3 practice-bank-expansion-pack/validate.py \
  cn-nanjing-g11-2026/物理 \
  --prepare-delivery /absolute/path/to/clean-delivery
```

The package contains only `questions.jsonl` plus `answer_review.jsonl` for
Teacher-pass questions with matching question, answer, solver, and solution
hashes. Internal `answer_final.jsonl`, legacy `answers1/2/3.jsonl`, and model
artifacts are never copied. The delivery gate also checks formula formatting,
control characters, option structure, explanation residue, quotas, and answer
distribution. `review-queue/` remains in the source repository for human review
and is never copied into the clean delivery package.

## Verified state at publication

- Manager and validator tests run entirely offline; one localhost-socket test
  may be skipped by a restricted local sandbox.
- Nanjing physics validator: 93/93 nodes passed.
- Guangxi physics validator: 30/30 nodes passed.
- Shanghai physics clean delivery validator: 23/23 nodes passed (346 questions);
  four unresolved seed questions remain preserved for human review.
- Published solution skills: 32.
