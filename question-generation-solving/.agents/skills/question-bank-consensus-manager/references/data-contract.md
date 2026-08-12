# 数据与审计契约

## 目录约定

一个题目节点通常包含：

- `questions.jsonl`：一行一题，至少有 `id`、`prompt`、`options`；现有题库还使用 `nodeId`、`subject`、`difficulty`、`pool`、`answer`、`explanation`。最终交付以这里的 `answer`/`explanation` 为权威；接受 final 时 manager 必须同步更新它们。
- `reference.md`：知识点、考查要点和原题文字。
- `question.png` / `question.jpg`：可选原题图。
- `answer_final.jsonl`：内部兼容/审计输出，每行 `{id, answer, solution}`；不进入交付包。
- `answer_review.jsonl`：Teacher 的派生审查记录，可从状态库重建；交付记录包含 `question_snapshot_sha256` 与 `teacher_solution_sha256`，用于证明复核题面、最终答案和 `questions.explanation` 没有漂移。

根目录输出：

- `.qb-review/review.sqlite3`：可查询索引、任务状态、attempt 和 review。
- `.qb-review/runs/<run-id>/...`：不可变 agent 调用证据。
- `.qb-review/run-ledger.jsonl`：完成 run 的 manifest 哈希链。
- `.qb-review/decisions.jsonl`：人工确认的追加式审计日志。
- `.qb-review/solution-skill-versions/<skill-id>/vNNNN/`：解题 skill 的不可变版本与 metadata。
- `.qb-review/solution-skill-events.jsonl`：skill 创建、去重跳过、激活、拒绝事件。
- `解题技能库/<skill-id>/SKILL.md`：当前激活的共享 skill；只能由版本化写入器生成。
- `错题集.jsonl`：由当前未解决记录原子重建的兼容导出。

每题的净化请求还含由 `node_dir` 确定的 `language_variant`。`hk-*`/`hongkong` 节点为 `zh-Hant-HK`，所有自然语言字段使用香港繁体；其他当前大陆节点为 `zh-Hans-CN`。该字段进入请求 artifact 及其哈希，但不改变原业务 JSONL schema，也不改变用于交付兼容的题面内容哈希。

## 内部标识

题目业务 id 可能在不同节点重复。内部 `question_key` 必须由“相对 questions.jsonl 路径 + NUL + 业务 id”的 SHA-256 派生；API 和数据库使用 `question_key`，写回节点时仍使用原业务 `id`。

## 状态机

`pending -> running -> final | disagreement | invalid | error`

用户从网页主动重解不会覆盖旧 attempt；它创建新的 run id。默认流程没有自动后核验兜底：已有题首轮不一致直接等待人工审查；生成题首轮失败不写入源文件，下一次扩题根据仍存在的配额缺口生成不同候选，并把旧失败题面列为禁重复项。历史版本留下的 `postverify` child run 只读兼容，不会被新流程创建。人工接受任一候选后进入 `final`，同时保留全部旧 review。对已有 `answer_final.jsonl` 的题，扫描时会兼容导入为 final，但 `verify` 与交付 validator 仍要求它和权威 `questions.jsonl` 完全一致；旧产物不因被导入就自动获得新交付证书。

## 交付契约

交付包由 `validate.py --prepare-delivery` 在题库外的新目录生成。每个有题节点只含：

- 必需：`questions.jsonl`、`answer_review.jsonl`；
- 可选：`question.png/jpg/jpeg`、`reference.md`。

交付包根目录必须另含 `manifest.json`，逐个记录 `questions.jsonl` 的相对路径、题数、SHA-256 和字节数，并汇总节点数、题数、难度、池和答案分布。validator 会复算整份清单；包内不得出现 `._*`、`.DS_Store`、`__MACOSX/` 或名称含 ` copy` 的目录。

不得包含 `answer_final.jsonl`、`answers1.jsonl`、`answers2.jsonl`、`answers3.jsonl`、`.qb-review` 或原始 Agent invocation。进入交付的每道题必须恰有一条 review，且 `teacher_verdict=pass`、`correct=true`、`answer_consistent=true`、`manager_status=final`、`auto_promote=true`，三名 solver 答案与 Teacher 相同、题面快照哈希相同、`questions.answer` 与 `teacher_answer` 相同、`questions.explanation` 与 Teacher 解法哈希相同。非 pass/final 题直接排除；标成 pass 却哈希或答案冲突时整批失败。

逐题静态契约还拒绝控制字符、数学环境外的 LaTeX/上下标、公式内裸 `%`、裸单位、题干重复选项、单字母选项、Markdown 表格选项、空公式、生成修补对话及内部审校用语。整批按科目检查 A/B/C/D 分布；修正分布只能交换完整选项并重新核验，不能孤立修改答案字母。

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

`retry_feedback` 是为兼容现有审计/UI 保留的结构化路由诊断，只有 `disposition`、固定 enum 的 `issue_codes` 与 `focus_codes`；新流程不会把它发送给 solver，也不会据此自动启动第二轮。通过题必须为 `none`，不一致题使用 `human_review`，题面需改使用 `question_revision`。`question_annotation.proposed_revision` 是 staged 数据，不会静默改写已 final 的原题。

## Solution skill 版本

一个 active skill 的数据库当前 SHA、可见 `SKILL.md` SHA 和当前历史版本 SHA 必须一致。`SKILL.md` frontmatter 只能有 `name` 与 `description`。自动候选只有在严格 final、`useful=true`、`generalized=true`、无题面/长解法片段污染且相似度未命中已有 skill 时才激活；`novel` 是审计信号而非硬门槛。历史回顾候选还必须由至少两道 final 题支持，经过跨批次合并，并用 `source_candidate_ids` 把最终证据 key 精确链接回同一方法的批内候选；来源同时标注跨节点或同节点变体证据。网页修订使用 `{base_sha256,guidance}`；guidance 经结构化 editor 生成完整候选，而非直接追加到文件。

## 可复现含义

每次调用保存 prompt 和输入的原文与哈希。CLI 保存模型参数、唯一 thread id、Seatbelt profile、JSONL stdout、stderr 与最终响应；API 保存无凭据的 provider request、原始响应、provider request/response id、usage、事件和规范化响应，并强制 `store=false`/无工具/无跨请求状态。完成 run 的 manifest 覆盖 artifact inventory，并进入前向哈希 ledger。这样可以检查“当时给了什么、得到了什么、为何路由”，也能发现普通文件缺失或局部篡改；ledger 未签名，因此不属于能抵抗有权限攻击者整体重写的取证系统。大模型采样与后端版本可能变化，因此仅保证可追溯重放，不承诺相同输入逐字节生成相同输出。

严格隔离只白名单当前 Agent 的 invocation 文件和显式题图，同时拒绝题库其余内容。`--isolation soft` 仅用于用户明确接受弱隔离的非 macOS 环境。
