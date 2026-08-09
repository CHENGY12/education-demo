# Question-bank consensus workflow

This repository includes a reusable Codex skill for large-scale question-bank
generation, three-way independent solving, Teacher verification, one safe retry,
auditable `answer_final` writeback, and browser-based human review.

## Included files

- `.agents/skills/question-bank-consensus-manager/`: manager skill, prompts,
  schemas, runner, review website, and tests.
- `question generation/practice-bank-expansion-pack/validate.py`: static JSONL
  and formatting validator.
- `question generation/practice-bank-expansion-pack/解题技能库-物理/`: 32
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
通过时才写入 answer_final；其余题进入网页审查。
```

The full operating contract and commands are in
`.agents/skills/question-bank-consensus-manager/SKILL.md`.

## Verified state at publication

- Manager tests: 41 passed; one localhost-socket test was skipped by the local
  sandbox.
- Nanjing physics validator: 93/93 nodes passed.
- Guangxi physics validator: 30/30 nodes passed.
- Published solution skills: 32.
