# Portable human-review queue

`unresolved.jsonl` preserves unresolved questions that must remain available to
the review website even when they are intentionally excluded from a clean
machine-consumable delivery.

- Seed rows with `disagreement`, `invalid`, or `error` are mandatory human-review
  items and appear by default under **待审查**. They are never treated as rejected.
- Non-seed rows are generated candidates. Disagreements appear under
  **候选不一致**; invalid/error candidates remain accessible through the status
  filter and may be replaced by later generation.
- Each row carries the content-bound `question_key`, question snapshot, current
  attempts, Teacher review, and staged annotation. It does not contain API keys
  or raw provider invocation artifacts.
- `qb_manager.py scan` and `serve` import this file idempotently into a fresh
  `.qb-review` state. Path/key drift, duplicate keys, source-content drift, and
  non-unresolved statuses are rejected. A stale row can never demote a database
  question that is already `final`.
- `qb_manager.py export` rewrites this file atomically. Human acceptance first
  passes the bank's per-question validator, writes the accepted seed back to its
  authoritative `questions.jsonl`, and then removes it from this queue.

This directory is source-side review evidence. `validate.py --prepare-delivery`
does not copy it into a clean delivery package.
