# Codex 客户端直接运行 Prompt

把下面整段粘贴到 Codex 客户端，只替换“目标目录”列表。目录可以是题库根目录下的相对路径，也可以是绝对路径；一个和多个目录使用同一套写法。

```text
使用 $question-bank-consensus-manager，直接执行，不要只给计划或只模拟 Agent。

目标目录：
- {目录 1}
- {目录 2；没有就删除本行}

运行模式：full
自动写入一致且 Teacher 核验通过的答案：是
Provider：auto（有 OPENAI_API_KEY 则 API，否则 Codex CLI）
API 模式：responses（速度优先；只有我明确要求排队吞吐时才改 batch）
模型：API 默认 gpt-5.6-sol；CLI 使用当前登录默认模型
隔离：strict
解题并发：自动（CLI 3；API 9）
每批题数：15

请完成以下闭环：
1. 从目标目录推断唯一题库根目录；所有目标必须位于该根目录内并含有 questions.jsonl。路径有歧义时才询问我。
2. 使用本项目的 question-bank-consensus-manager runner。先 doctor，再用同一目标列表执行 run --dry-run；路径、题数和配额合理后立即执行正式 run，不需要二次确认。
   开始前完整读取题库根目录最新版 README.md、validate.py 及 Skill 的 delivery-issues-v1.md。`hk-*`/`hongkong` 目录标记为 `zh-Hant-HK`，题干、选项、解析、提示和解题过程一律使用香港繁体；其他当前大陆目录使用简体。
3. full 模式必须同时做举一反三生成与试做、另外两路独立解题、Teacher 独立重做和过程核验，也必须审校目录内原有题目。禁止在当前聊天上下文里假装三路独立解题。
4. 每个 --target 原样、逐项传给一键命令；使用 `--provider auto`。CLI 启用 strict 隔离；API 必须 `store=false` 且禁用 tools/conversation/previous response。检查 question 图片只含题面；若像素含答案、解析或批注，只暂停受影响目录。
5. 每题只做一次自动 3+1。Teacher 判为不一致后不自动重解：seed/已有题直接进入“待审查”；生成题保留首轮证据但不写入 `questions.jsonl`，下一次运行按仍未满足的配额生成不同的新题，并排除历史淘汰题面。不得把旧答案、Teacher comments 或具体解法传给替换生成 Agent。
6. 正式运行后执行 verify、export 和题库自带 validator。接受结果必须同步更新 `questions.jsonl` 的 answer/explanation；`answer_final.jsonl` 仅作内部兼容与审计，不能成为交付答案源。失败题保留在网页队列，不删除运行记录，不覆盖无关文件。
7. 启动 127.0.0.1:8765 审题网页并保持进程运行。默认“待审查”只含 status=disagreement 的 seed 题；其他 disagreement 放“候选不一致”；invalid/error/running 通过状态筛选查看；技能库单独可浏览和按提示生成版本。为每个目标目录重复传入一个 `serve --scope`。
8. 对严格一致且真正有通用复用价值的解法，去题目化、核验证据并去重后写入共享解题 skill 库；不要求方法全新。所有 solver 可独立参考同一 skill 快照。人工确认的分歧解法也异步判断是否值得提炼。
9. 若 API key 与 Codex CLI 登录都不可用，停止在模型调用前并告诉我配置方式；不得伪造结果。说明净化题面和明确附图会发送到所选云端 provider。
10. 若我要交付，在题库外创建一个全新空目录，运行 `validate.py <目标> --prepare-delivery <目录>`。只交付根部 `manifest.json`、节点内 `questions.jsonl` 与 `answer_review.jsonl`（原题图/reference 可选）；不得带 `answer_final.jsonl`、`answers1/2/3.jsonl`、`.qb-review`。对剥离答案/解析/首轮记录后的题面再做一次独立盲解；这不是分歧后的自动第二轮。报告被排除题、节点与全局 A/B/C/D 分布、繁简体结果和 manifest 复算结果；任何题面/答案/解析哈希不一致都必须停止交付。
```

如需先全部人工确认，只把这一行改成：

```text
自动写入一致且 Teacher 核验通过的答案：否
```

对应的确定性 CLI 入口为：

```bash
python3 "$SKILL_ROOT/scripts/qb_manager.py" run \
  --bank "$BANK_ROOT" \
  --target "$TARGET_1" \
  --target "$TARGET_2" \
  --mode full \
  --batch-size 15 \
  --provider auto \
  --isolation strict
```

“自动写入：否”时增加 `--no-auto-promote`。目标列表作为一个联合 scope 审校，解题请求可跨目录批处理，不会退化成每个目录各调用四次模型。
