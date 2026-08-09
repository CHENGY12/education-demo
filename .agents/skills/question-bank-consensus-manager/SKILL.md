---
name: question-bank-consensus-manager
description: Orchestrate reproducible large-scale question-bank expansion and answer verification with three isolated solver agents, a fourth teacher agent, automatic Codex CLI or OpenAI API execution, one safe post-verification retry, an evolving solution-skill library, auditable artifacts, and a local human-review web console. Use when a directory tree contains questions.jsonl/reference.md/question images and needs 举一反三 generation, independent multi-agent solving, strict answer_final routing, disagreement review, skill extraction, or interactive re-solving.
---

# Question Bank Consensus Manager

Manage the whole question-bank lifecycle while keeping the three candidate solutions independent. Use the bundled Python runner; do not hand-edit aggregate state when the runner can perform the operation atomically.

## Non-negotiable isolation

1. Give solver 1, solver 2, and solver 3 separate invocations and artifact directories. The CLI provider uses three ephemeral processes and unique thread IDs. The API provider uses three self-contained Responses requests with `store=false`, no tools, no conversation, and no `previous_response_id`.
2. Give each solver only a sanitized question snapshot. Remove `answer`, `explanation`, other solvers' outputs, teacher reviews, and `answer_final` content.
   Treat attached `question.png/jpg` as trusted question-only input: strict filesystem isolation cannot remove an answer or handwritten annotation already visible in image pixels.
3. Use `--isolation strict` (the default). On macOS this adds a per-invocation Seatbelt policy: a solver may read its enumerated prompt/request/schema and explicit question image, but reads of source-bank files and sibling solver outputs are denied by the OS. Strict mode fails closed when that backend is unavailable; never silently downgrade it.
4. Do not start the teacher until all three solver outputs are closed and hashed.
5. Let the teacher inspect all three processes, independently recompute the answer, and explicitly decide whether auto-promotion is safe.
6. Auto-promote only when all three answers are equivalent, every material reasoning chain is correct, the question is valid, and the teacher returns `auto_promote=true`.
7. Treat a user's re-solve hint as guidance, not ground truth. Show it to all three fresh solvers identically.
8. When the first Teacher finds a repairable disagreement, retain that run and perform at most one new 3+1 round. The new solvers receive only canonical enum error categories and check categories; never pass the first answers, Teacher answer, concrete derivation, candidate count, or Agent identity.
9. Require `issues=[]` for every fully-correct solver, a valid option id for multiple choice, an unchanged public question snapshot, a valid/no-revision question annotation, and a clear retry contract before automatic final writeback.

The local runner enforces these boundaries. Do not replace it with three calls in one conversational context.

## Locate the bank and runner

Set `SKILL_ROOT` to this skill directory. Resolve `BANK_ROOT` to the directory whose descendants contain `questions.jsonl`. Never assume the current directory is the bank root when multiple candidates exist.

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" doctor --bank "$BANK_ROOT"
python3 "$SKILL_ROOT/scripts/qb_manager.py" init --bank "$BANK_ROOT"
python3 "$SKILL_ROOT/scripts/qb_manager.py" scan --bank "$BANK_ROOT"
```

`doctor` reports the automatically selected provider. If `OPENAI_API_KEY` exists, `auto` selects the Responses API; otherwise it selects the authenticated Codex CLI. The key is read only from the process environment and is never persisted. `init` creates `<BANK_ROOT>/.qb-review/`; `scan` indexes source JSONL and imports compatible legacy artifacts.

No separate API key does not mean offline: a `Logged in using ChatGPT` session still sends the sanitized prompt and any explicitly attached question image to Codex cloud. If the bank may not leave the machine, stop before `audit`/`expand` and connect an approved local provider implementation instead.

## Choose a workflow

### Run directly from one directory or a directory list

When the user supplies exact directories, prefer the repeatable `--target` entry point. It validates every path, unions all selected `questions.jsonl` files, expands each deficient node, and batches auditing across the whole list.

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" run \
  --bank "$BANK_ROOT" \
  --target "cn-nanjing-g11-2026/物理/10月阶段性检测_2" \
  --target "cn-nanjing-g11-2026/物理/10月阶段性检测_3" \
  --mode full \
  --batch-size 15 \
  --provider auto \
  --isolation strict
```

Use `--dry-run` first. Add `--no-auto-promote` when all results should wait for human confirmation. Read [direct-run-prompt.md](references/direct-run-prompt.md) when the user wants a single prompt that they can paste into the Codex client and edit only the target list.

### Audit questions already present

Run a small scope first, inspect the review console, then scale out.

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" audit \
  --bank "$BANK_ROOT" \
  --scope "cn-nanjing-g11-2026/物理" \
  --batch-size 15 \
  --max-agent-processes 3 \
  --isolation strict \
  --limit 2
```

Remove `--limit` after the sample passes. The command is resumable: completed questions are skipped, and each invocation is stored under `.qb-review/runs/<run-id>/`.

### Generate missing 举一反三 questions and audit them

Use `expand` when a node does not meet the low/mid/high and display/exam quotas described by the bank's own `README.md` or validator.

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" expand \
  --bank "$BANK_ROOT" \
  --scope "cn-nanjing-g11-2026/物理" \
  --max-agent-processes 3 \
  --isolation strict \
  --limit 1
```

The generator is also candidate solver 1 for newly generated questions. Solvers 2 and 3 receive only the generated stem and options. Teacher-approved questions are atomically added to `questions.jsonl` and `answer_final.jsonl`; unresolved generated questions remain in the review state until a human decision.

When the bank root provides `validate.py` with `check_question(q, line_no)`, the runner executes that per-question contract before **any** existing or generated question can enter `answer_final`; generated source insertion uses the same gate. After finishing a scope, also run the bank's aggregate quota validator:

```bash
python3 "$BANK_ROOT/validate.py" "cn-nanjing-g11-2026/物理"
```

### Open the review console

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" serve \
  --bank "$BANK_ROOT" \
  --scope "cn-nanjing-g11-2026/物理/*" \
  --host 127.0.0.1 \
  --port 8765 \
  --isolation strict
```

Open `http://127.0.0.1:8765`. Keep the server process running. The UI can:

- browse disagreements, invalid questions, errors, running jobs, and completed items;
- compare all solver processes with the teacher's verified solution;
- accept a solver or teacher result into the node's `answer_final.jsonl`;
- submit a hint or proposed method and enqueue three fresh isolated solves plus a new teacher review;
- continue browsing while queued jobs run and display completion notifications.
- paginate through arbitrarily large result sets instead of truncating the queue.
- default to **待审查**, containing only `seed` questions whose status is `disagreement`; place other disagreement items in **候选不一致**. Invalid/error/running/final remain available through the status filter and global job tray.
- browse the shared **解题技能库**, inspect immutable versions and provenance, and submit a guidance-based revision guarded by the current SHA-256.
- inspect the first and fallback 3+1 rounds plus staged question-quality annotations. Proposed question revisions are not silently applied to an already-finalized item.

Pass one `--scope` for every target directory or glob. This keeps unrelated banks out of the UI and also blocks detail, image, accept, and re-solve API calls for questions outside those scopes.

To pin the console to a durable hand-picked set, add a `review_view` object to
`<BANK_ROOT>/.qb-review/config.json` with a title and non-empty
`question_keys` array. The server validates every key, restricts the summary,
list, detail, image, accept, and re-solve routes to that exact set, and keeps
resolved items visible so the fixed list does not shrink during review.

Run only one `serve` process per state directory. If that process is restarted, abandoned queued/running rows are marked failed and become retryable rather than blocking forever.

## Preserve provenance

Do not delete `.qb-review` after a run. It contains the SQLite index plus request, prompt, provider response/events, stderr, normalized response, metadata, provider request/response id or CLI thread id, isolation declaration, model, timestamps, and SHA-256 records. Completed manifests inventory every run artifact; `run-ledger.jsonl` chains manifest hashes. The visible shared skill library is `<BANK_ROOT>/解题技能库/<skill-id>/SKILL.md`; immutable versions and source records live under `.qb-review/solution-skill-versions/` and `solution-skill-events.jsonl`.

Use these commands for checks and exports:

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" status --bank "$BANK_ROOT"
python3 "$SKILL_ROOT/scripts/qb_manager.py" verify --bank "$BANK_ROOT"
python3 "$SKILL_ROOT/scripts/qb_manager.py" export --bank "$BANK_ROOT"
```

`export` atomically refreshes `<BANK_ROOT>/错题集.jsonl` from unresolved records. Never derive truth from answer-string majority alone.

## Scale safely

- Filter with `--scope`, `--subject`, and `--limit` before a full run.
- Leave `--max-agent-processes` unset for provider-aware defaults: 3 for Codex CLI and 9 for API. Override only when the account and machine are intentionally provisioned.
- `--provider auto` is the default. Use `--provider api --api-mode responses` for low-latency API work. `--api-mode batch` is supported for asynchronous Batch transport, but is normally slower wall-clock than Responses and is not the speed default.
- Increase `--batch-size` to combine questions across nodes and reduce fixed call overhead; decrease it when prompts become too large or a subject needs more detailed reasoning.
- Resume after rate limits or interruption; do not restart completed batches.
- Use `--model` to pin a model. The manifest records the exact requested model and Codex CLI version.
- Edit `<BANK_ROOT>/.qb-review/config.json` `quotas` when a bank's low/mid/high × display/exam allocation differs from the defaults; invalid quota shapes fail closed.
- Never pass API keys on the command line or store credentials in the bank.

## Grow the solution-skill library conservatively

- Give all three solvers the same read-only snapshot of the few relevant active skills. A skill is optional evidence, never an answer authority.
- Activate an automatically proposed skill only after strict 3+1 final promotion, `useful=true`, `novel=true`, `generalized=true`, content-pollution checks, and deterministic similarity deduplication.
- After a human accepts a disputed solution, enqueue a post-resolution extraction. Its provenance is explicitly `human_accepted_solution` rather than a strict-consensus certificate.
- Render skill files deterministically with only `name` and `description` frontmatter. Never write the user's web guidance directly into `SKILL.md`; use the structured editor, validate it, version it, then atomically activate it.

## Prompt and data references

- Read [manager-prompt.md](references/manager-prompt.md) when the user wants a reusable manager prompt.
- The runner loads [generator-solver-prompt.md](references/generator-solver-prompt.md), [solver-prompt.md](references/solver-prompt.md), and [teacher-prompt.md](references/teacher-prompt.md) directly. Modify these files only as a versioned prompt change; their hashes enter every run manifest.
- Read [data-contract.md](references/data-contract.md) when integrating a new JSONL schema or external system.

## Completion report

Report the scanned, final, unresolved, invalid, error, and running counts; the run/state directory; the review URL; and whether Codex login or an API/local-provider fallback is required. Distinguish ephemeral context isolation, OS-enforced bank/sibling read isolation, the cloud trust boundary, and exact generative repeatability.
