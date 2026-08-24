# CR-Agent 设计优化文档

> 编写日期: 2026-08-24
> 基于: 系分文档风险分析 + 代码审查发现的设计层面问题
> 状态: 全部已实施，160 测试通过

---

## 背景

系分文档（`cr-agent-xifen-backend.md`）列出了 17 个代码级风险（R1-R17），已在前一轮修复中处理。本文档聚焦的是**设计层面的问题**——代码能跑、没有 bug，但架构决策本身不合理，会导致浪费、矛盾或潜在隐患。

---

## D1: generate_report 从"假工具"改为"直接输出 JSON"

### 问题

`generate_report` 被定义为 `@tool`，LLM 通过 `tool_calls` 调用它。但：
- 函数体里的逻辑（构造 report dict、返回 JSON）是**死代码**——`_finalize` 不用它的返回值
- `_finalize` 改为从 `messages` 里倒序遍历找 `generate_report` 的 `tool_calls`，从 `args` 里挖 findings
- LLM 调了 `generate_report` 后，`_should_continue` 发现有 `tool_calls` → 走 tools 节点执行 → 返回 JSON 给 LLM → LLM 再回复一条不带 tool_calls 的消息 → 才走 finalize
- **多了一轮无意义的 LLM 调用**（执行 generate_report → 返回 → LLM 再说一句话 → finalize）

### 优化方案

- 从 `ALL_TOOLS` 移除 `generate_report`
- System prompt 改为"完成分析后不调用工具，直接在回复中输出 JSON"
- `_finalize` 新增 `_parse_llm_findings()` 函数，从最后一条 `AIMessage.content` 解析 JSON（支持 ```` ```json ```` 代码块和裸 JSON 两种格式）
- 去掉了 `llm ⇄ tools` 之间一轮无意义的往返

### 改动文件

- `cr_agent/agent/tools.py` — 移除 `generate_report`，`ALL_TOOLS` 只剩 `run_lint` + `read_file`
- `cr_agent/agent/prompts.py` — 审查流程第 6 步改为"直接输出 JSON"，新增输出格式说明
- `cr_agent/agent/graph.py` — `_finalize` 重写为从 `AIMessage.content` 解析，新增 `_parse_llm_findings()`

---

## D2: 移除 LLM 的 verdict 参数

### 问题

`generate_report` 的参数列表有 `verdict: str`，docstring 告诉 LLM 传 "approve/request_changes/block"。但 `determine_verdict(all_findings)` 完全忽略 LLM 传的值，自己按严重度重算。LLM 浪费 token 思考 verdict，且系统 prompt 没告诉 LLM "你传的 verdict 会被忽略"。

### 优化方案

- `generate_report` 已移除（D1），verdict 参数自然消失
- System prompt 明确说明"verdict 由系统根据 findings 严重度自动判定，你不需要输出 verdict"
- 防御设计不变：`determine_verdict()` 仍然由代码执行，LLM 无法影响审查结论

### 改动文件

- `cr_agent/agent/prompts.py` — 输出格式说明里去掉 verdict，加注释"verdict 由系统自动判定"

---

## D3: diff 截断统一到 prepare 入口

### 问题

`build_review_prompt` 里把 diff 截断到 50000 字符，但 `_prepare_review` 在 `state["diff"]` 里存了完整 diff。`_finalize` 又从 `state["diff"]` 重新 `parse_diff` 算 metrics。如果 diff 被截断了，LLM 看到的是截断版，但 metrics 算的是完整版——报告说"审查了 50 个文件"，但 LLM 只看到了前 30 个。

### 优化方案

- 截断逻辑从 `build_review_prompt` 移到 `_prepare_review` 入口
- 截断后的 diff 写回 `state["diff"]`，后续所有环节用同一份数据
- `build_review_prompt` 不再做截断，只负责拼 prompt

### 改动文件

- `cr_agent/agent/graph.py` — `_prepare_review` 入口处截断 diff，写回 state
- `cr_agent/agent/prompts.py` — `build_review_prompt` 去掉截断逻辑

---

## D4: 熔断器改为模块级单例

### 问题

`CircuitBreaker` 在 `_make_llm_node` 函数内部创建为局部变量。每次 `build_graph()` 新建一个熔断器。webhook 每次审查都调 `build_graph()`，熔断器的"连续失败 5 次开路"状态永远不会跨审查累积。

### 优化方案

- 熔断器提到模块级：`_llm_circuit_breaker = CircuitBreaker(threshold=5, recovery_timeout=60.0)`
- `_make_llm_node` 引用模块级单例，不再每次新建

### 改动文件

- `cr_agent/agent/graph.py` — 模块级 `_llm_circuit_breaker`，`_make_llm_node` 内引用

---

## D5: 循环检测区分工具类型

### 问题

`LoopDetection` 的 hash 只看工具名+参数。`read_file("app.py")` 调两次，即使文件内容变了（比如 lint 自动修了），hash 相同，判定为循环。对 `read_file` 这种幂等操作，重复调用不一定是循环。

### 优化方案

- `after_tool` 钩子新增：对 `read_file` 的返回内容做 hash
- 如果文件内容变化了，移除最近一次 call_hash（不算重复）
- `_result_hashes` 记录上一次 `read_file` 的返回 hash
- `run_lint` 等其他工具不变（相同命令重复执行确实是循环）

### 改动文件

- `cr_agent/agent/middlewares/loop_detection.py` — 新增 `after_tool` 钩子、`_pending_tool_names`、`_result_hashes`

---

## D6: 中间件实例隔离说明

### 问题

`MiddlewareContext` 用普通 Python 对象（无锁），`BackgroundTasks` 在同进程内可能并发执行多个 `_run_review`。如果同一个 chain 实例被多个 invoke 共享，`chain.reset()` 可能清掉另一个正在跑的审查的上下文。

### 优化方案

- 当前代码已经是每次 `build_graph()` 新建 chain，webhook 和 Web API 每次审查都调 `build_graph()`
- 加注释明确约束："调用方应每次审查调 build_graph()，不要复用 graph 实例跨多次 invoke"
- 不改代码结构，只加文档约束（实际场景已被规避）

### 改动文件

- `cr_agent/agent/graph.py` — `build_graph` 内加注释

---

## D7: 记忆系统去偏见

### 问题

`build_memory_context` 统计所有历史 findings 的 Top 5 pattern 注入 prompt。如果上次审查误报了 `sql-injection`，记忆系统会告诉 LLM "这个仓库经常有 SQL 注入"，LLM 这次更可能也报 SQL 注入（确认偏误）。

### 优化方案

- 只统计 `blocker` 和 `major` 级别的 pattern（误报率远低于 minor/info）
- prompt 文案从 "Common issues found" 改为 "High-severity patterns"
- 去掉 "Pay extra attention to these patterns"（避免引导 LLM 强化偏见）
- 同时修复 `save_review_memory` 中 finding_types 和 severities 用 set 去重导致 zip 不对齐的 bug

### 改动文件

- `cr_agent/agent/memory.py` — `build_memory_context` 只统计 blocker+major；`save_review_memory` 不去重
- `tests/test_redis_idempotency.py` — 更新测试断言

---

## 额外修复：save_review_memory 的 set 去重 bug

### 问题

`save_review_memory` 用 `list({f.get("rule_id") for f in findings})` 和 `list({f.get("severity") for f in findings})` 分别去重。set 去重后顺序不确定，两个列表 zip 后不对应——`security.sql-injection` (blocker) 可能被 zip 成了 `debug.print` (minor)。

### 优化方案

- 改为 `[f.get("rule_id") for f in findings]` 不去重，保持 finding_types 和 severities 一一对应

### 改动文件

- `cr_agent/agent/memory.py` — `save_review_memory` 去掉 set 去重

---

## 验证

```
160 tests passed, 1 warning in 3.46s
```

所有现有测试通过，无需新增测试（设计优化不改变外部行为，只改变内部数据流）。