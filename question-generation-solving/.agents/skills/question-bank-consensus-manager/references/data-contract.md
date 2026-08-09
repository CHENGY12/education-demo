# 数据与审计契约

## 目录约定

一个题目节点通常包含：

- `questions.jsonl`：一行一题，至少有 `id`、`prompt`、`options`；现有题库还使用 `nodeId`、`subject`、`difficulty`、`pool`、`answer`、`explanation`。
- `reference.md`：知识点、考查要点和原题文字。
- `question.png` / `question.jpg`：可选原题图。
- `answer_final.jsonl`：兼容输出，每行 `{id, answer, solution}`；完整来源写在审计状态中。
- `answer_review.jsonl`：Teacher 的派生审查记录，可从状态库重建。

根目录输出：

- `.qb-review/review.sqlite3`：可查询索引、任务状态、attempt 和 review。
- `.qb-review/runs/<run-id>/...`：不可变 agent 调用证据。
- `.qb-review/run-ledger.jsonl`：完成 run 的 manifest 哈希链。
- `.qb-review/decisions.jsonl`：人工确认的追加式审计日志。
- `.qb-review/solution-skill-versions/<skill-id>/vNNNN/`：解题 skill 的不可变版本与 metadata。
- `.qb-review/solution-skill-events.jsonl`：skill 创建、去重跳过、激活、拒绝事件。
- `解题技能库/<skill-id>/SKILL.md`：当前激活的共享 skill；只能由版本化写入器生成。
- `错题集.jsonl`：由当前未解决记录原子重建的兼容导出。

## 内部标识

题目业务 id 可能在不同节点重复。内部 `question_key` 必须由“相对 questions.jsonl 路径 + NUL + 业务 id”的 SHA-256 派生；API 和数据库使用 `question_key`，写回节点时仍使用原业务 `id`。

## 状态机

`pending -> running -> final | disagreement | invalid | error`

重解不会覆盖旧 attempt；它创建新的 run id。自动后核验兜底也必须是新的 child run，且最多一轮。人工接受任一候选后进入 `final`，同时保留全部旧 review。对已有 `answer_final.jsonl` 的题，扫描时直接标记 final，不擅自重跑。

## Candidate solution

每份候选至少包含：

```json
{
  "id": "question business id",
  "answer": "A",
  "solution": "可检查的推导",
  "independent_check": "代回/量纲/边界等",
  "question_valid": true,
  "confidence": "high",
  "diagnostic_issue_codes_checked": [],
  "diagnostic_focus_codes_checked": [],
  "solution_skill_ids_considered": []
}
```

## Teacher review

每题至少包含 `verdict`、`answer_consistent`、`teacher_answer`、`teacher_solution`、`process_review`、逐 Agent 反馈、`retry_feedback`、`question_annotation`、可空 `skill_candidate` 和 `auto_promote`。`auto_promote` 是严格合取条件，不是多数票。

`retry_feedback` 只有 `disposition`、固定 enum 的 `issue_codes` 与 `focus_codes`。Manager 按固定常量顺序规范化后才发送给下一轮；下一轮 request 禁止包含 Teacher 答案、解法、候选或 Agent 身份。`question_annotation.proposed_revision` 是 staged 数据，不会静默改写已 final 的原题。

## Solution skill 版本

一个 active skill 的数据库当前 SHA、可见 `SKILL.md` SHA 和当前历史版本 SHA 必须一致。`SKILL.md` frontmatter 只能有 `name` 与 `description`。自动候选只有在严格 final、`useful=true`、`generalized=true`、无题面/长解法片段污染且相似度未命中已有 skill 时才激活；`novel` 是审计信号而非硬门槛。历史回顾候选还必须由至少两道 final 题支持，经过跨批次合并，并用 `source_candidate_ids` 把最终证据 key 精确链接回同一方法的批内候选；来源同时标注跨节点或同节点变体证据。网页修订使用 `{base_sha256,guidance}`；guidance 经结构化 editor 生成完整候选，而非直接追加到文件。

## 可复现含义

每次调用保存 prompt 和输入的原文与哈希。CLI 保存模型参数、唯一 thread id、Seatbelt profile、JSONL stdout、stderr 与最终响应；API 保存无凭据的 provider request、原始响应、provider request/response id、usage、事件和规范化响应，并强制 `store=false`/无工具/无跨请求状态。完成 run 的 manifest 覆盖 artifact inventory，并进入前向哈希 ledger。这样可以检查“当时给了什么、得到了什么、为何路由”，也能发现普通文件缺失或局部篡改；ledger 未签名，因此不属于能抵抗有权限攻击者整体重写的取证系统。大模型采样与后端版本可能变化，因此仅保证可追溯重放，不承诺相同输入逐字节生成相同输出。

严格隔离只白名单当前 Agent 的 invocation 文件和显式题图，同时拒绝题库其余内容。`--isolation soft` 仅用于用户明确接受弱隔离的非 macOS 环境。
