# Question-bank consensus workflow

This repository includes a reusable Codex skill for large-scale question-bank
generation, three-way independent solving, Teacher verification, one safe retry,
auditable final writeback, strict delivery validation, and browser-based human review.

## Included files

- `.agents/skills/question-bank-consensus-manager/`: manager skill, prompts,
  schemas, runner, review website, and tests.
- `practice-bank-expansion-pack/validate.py`: static JSONL
  and formatting validator.
- `practice-bank-expansion-pack/解题技能库-物理/`: 32
  versioned, reusable physics solution skills with provenance metadata.

Question sources, model credentials, `.qb-review` databases, and private run
artifacts are intentionally not included.

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
通过时才同步写入 questions.jsonl 和内部 answer_final；其余题进入网页审查。
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
distribution.

## Verified state at publication

- Manager and validator tests run entirely offline; one localhost-socket test
  may be skipped by a restricted local sandbox.
- Nanjing physics validator: 93/93 nodes passed.
- Guangxi physics validator: 30/30 nodes passed.
- Published solution skills: 32.
