# CR Agent 进阶学习手册

> 本手册是基础学习手册（STUDY_GUIDE_FOR_INTERVIEW.md）的进阶版本。
> 基础手册教你"能跑、能讲"，进阶手册教你"能扛面试官深挖、能讲 unhappy path"。
>
> 前置条件：先完成基础手册的七天学习计划。

---

## 一、为什么需要进阶手册

基础手册教的是 happy path：
- diff 进来 → 正则检查 → LLM 分析 → 出报告

面试官不问 happy path。面试官问的是：

| 面试官的问题 | 对应的 unhappy path | 基础手册能答吗 |
|------------|--------------------|--------------| 
| "大 PR 怎么处理？" | 上下文爆炸 | 不能 |
| "Agent 无限循环怎么办？" | 死循环 | 不能 |
| "token 烧爆了怎么办？" | 成本失控 | 不能 |
| "lint 工具挂了怎么办？" | 工具失败 | 不能 |
| "GitHub 重复发 webhook 怎么办？" | 重复执行 | 不能 |
| "并发审查冲突怎么办？" | 竞态条件 | 不能 |
| "API key 泄露怎么办？" | 密钥泄露 | 不能 |
| "Agent 跑偏了怎么办？" | 范围漂移 | 不能 |

进阶手册逐个解决这些问题。每个问题都有对应代码和面试话术。

---

## 二、进阶七天计划

### 第 1 天：中间件链架构

**读这些文件（按顺序）**：
```
cr_agent/agent/middlewares/base.py              # 基类和链执行器
cr_agent/agent/middlewares/__init__.py          # build_default_chain()
```

**核心概念**：

中间件链是 Agent 的"管网"。每个中间件处理一个横切关注点，业务逻辑（graph.py 的节点）只管核心流程，不关心安全、压缩、预算这些事。

**并发隔离**：`MiddlewareContext` 通过 `contextvars.ContextVar` 绑定到当前执行上下文，每次 `graph.invoke` 拥有独立的 ctx 实例。即使同一个 chain 被并发调用，不同审查之间不会共享或覆盖中间件状态。`MiddlewareChain.reset()` 在当前上下文中创建新的 `MiddlewareContext` 并重置各中间件内部状态。

四个钩子时序：
```
用户输入 → [before_model] → LLM 调用 → [after_model] → 工具调用 → [before_tool] → 执行 → [after_tool]
              ↑                         ↑                          ↑                      ↑
        InputSanitization        LoopDetection               LoopDetection          OutputBudget
        ContextCompression       TokenBudget                                        ToolErrorHandling
```

**面试题**：

> "为什么用中间件而不是直接写在 graph 节点里？"

**参考答案**：
> 分离关注点。graph 节点只管业务流程（prepare → llm → tools → finalize），中间件管横切关注点（安全、压缩、预算、循环检测）。如果写在节点里，_llm_decide 函数会有 500 行混在一起。中间件可以独立测试、独立配置、按需插拔。比如确定性检查模式不需要 TokenBudget 中间件，去掉就行，不用改业务代码。

**代码作业**：写一个 LoggingMiddleware，在 before_model 和 after_tool 时打印日志。答案在 `base.py` 的 Middleware 基类里——继承它，override 两个方法。

---

### 第 2 天：上下文压缩 + 重要信息保留

**读这个文件**：
```
cr_agent/agent/middlewares/context_compression.py
```

**核心问题**：Agent 读 10 个文件，每个 500 行，工具输出累积到 80K token，LLM 上下文窗口装不下，审查崩溃。

**压缩策略**：
```
消息列表: [System, User(diff), ToolMsg×15, AIMsg×5, ...]  共 30 条
                    ↓ 压缩触发 (max_messages=20)
保留:     [System, User(diff), SummaryMsg, ToolMsg×8(最近)]  共 11 条
```

**什么不压缩（关键）**：
1. SystemMessage — Agent 的身份和规则，丢了就变傻
2. 第一条 HumanMessage — 包含 diff 和 PR 信息，丢了就不知道审查什么
3. 最近 N 条 — 当前工作上下文，丢了就不知道刚做了什么

**什么压缩**：
中间的工具结果和 AI 回复，用摘要替代："Context compressed: 15 earlier messages from tools: read_file, grep. Key findings preserved in state."

**面试题**：

> "压缩怎么确保重要信息不丢失？"

**参考答案**：
> 三层保护：1）System prompt 和初始 diff 消息被标记为 protected，永不压缩；2）确定性 findings 存在 AgentState 的 `deterministic_findings` 字段里，不在消息中，压缩不影响；3）_finalize 节点从 state 读取 findings 而非从消息历史解析，所以即使中间消息被压缩，最终报告仍然完整。生产系统会进一步用独立 LLM 做摘要而不是简单截断，但原理相同。

---

### 第 3 天：循环检测 + 跑偏控制

**读这些文件**：
```
cr_agent/agent/middlewares/loop_detection.py    # 循环检测
cr_agent/agent/middlewares/token_budget.py      # token 预算（跑偏的后果）
```

**循环场景**：
```
LLM: read_file("app.py")  → 看到 content
LLM: read_file("app.py")  → 同样 content
LLM: read_file("app.py")  → 还是同样 content
... 无限循环
```

**双层检测**：

| 层级 | 检测方式 | 触发条件 | 响应 |
|------|---------|---------|------|
| Layer 1 精确去重 | SHA-256(name + args) 哈希，滑动窗口 15 轮 | 同哈希 3 次 → warn，5 次 → hard stop | warn 注入提示；hard stop 剥离 tool_calls 强制结束 |
| Layer 2 频率统计 | Counter 统计每个工具名总调用次数 | 单工具 >30 次 | 加入 blocked_tools，before_tool 拦截 |

**跑偏控制**：
- TokenBudget：80% 时注入 "Budget alert: 80% used. Start synthesizing soon."
- 100% 时剥离 tool_calls + forced_finalize = True
- MAX_ITERATIONS = 15：硬性轮次上限

**面试题**：

> "Agent 跑偏了怎么办？比如它开始审查不相关的文件？"

**参考答案**：
> 三道防线：1）LoopDetection 检测重复行为——如果反复读同一文件，3 次警告 5 次强制结束；2）TokenBudget 控制总量——80% 时提醒收尾，100% 时强制产出报告；3）System prompt 里有预算约束："Do NOT read more than 10 files, do NOT review files not in the PR diff"。三层配合，从提示、经济、硬性三个维度限制跑偏。

---

### 第 4 天：工具失败降级 + 输出截断

**读这些文件**：
```
cr_agent/agent/middlewares/tool_error_handling.py   # 错误降级
cr_agent/agent/middlewares/output_budget.py          # 输出截断
cr_agent/agent/middlewares/tool_error_handling.py    # ToolErrorHandler 类
```

**失败场景**：
```
LLM: run_lint("ruff check .")
  → FileNotFoundError: ruff not installed
  → 没有中间件: 异常传播 → Agent 崩溃 → 审查失败
  → 有中间件: ToolErrorHandler 捕获 → 返回 ToolMessage(error) → Agent 继续
```

**fail-open vs fail-closed**：
```
fail-open (非关键工具):  lint 挂了 → 标记 "skipped" → 继续审查
fail-closed (关键操作):  generate_report 挂了 → 不能没有报告 → 降级为确定性 only
```

**输出截断**：
```
ruff check . → 50,000 字符 lint 输出
  → ToolOutputBudget 截断到 20,000
  → 附注: "[output truncated: 50000 → 20000 chars]"
  → LLM 知道看到的是部分输出，可以决定是否需要更精确的查询
```

**面试题**：

> "lint 命令失败了整个审查就崩溃吗？"

**参考答案**：
> 不会。ToolErrorHandler 用上下文管理器包裹工具执行，异常被捕获后转为 ToolMessage(status="error")，附带 recovery hint："Continue with available context, or choose an alternative tool."。Agent 看到错误后知道 lint 不可用，继续用确定性 findings + LLM 语义分析做审查，最终报告里 Static Analysis 部分标注 "skipped"。这叫 fail-open 策略——非关键工具失败不阻塞主流程。

---

### 第 5 天：幂等性 + 并发控制 + Trace ID

**读这些文件**：
```
cr_agent/observability/idempotency.py     # 幂等存储 + 并发限制
cr_agent/observability/tracing.py          # trace ID 链路追踪
cr_agent/github/webhook_server.py          # 集成点
```

**幂等性场景**：
```
GitHub 发 webhook → 审查开始（40s）
GitHub 没在 10s 内收到 200 → 重发 webhook
→ 第二个审查开始 → 两个审查并行 → 两条 PR 评论 → spam

解决: IdempotencyStore.try_acquire(repo, pr_number)
  → 第一个: status=IN_PROGRESS → acquire 成功
  → 第二个: status=IN_PROGRESS → acquire 失败 → 拒绝
```

**并发控制**：
```
PR #42 和 PR #43 同时触发 → 都允许（不同 PR，不同 key）
PR #44 也来了 → max_concurrent=3 → 第 4 个被拒绝

解决: _active_count >= _max_concurrent → return False
```

**Trace ID**：
```
trace=abc123 → webhook.received
trace=abc123 → review.started
trace=abc123 → LLM call done in 23s
trace=abc123 → review.complete, verdict=block

3 个并行审查的日志不会混淆，按 trace_id 过滤即可。
```

**面试题**：

> "GitHub 重复发 webhook 怎么办？你的审查会不会重复执行？"

**参考答案**：
> 三层去重：1）delivery ID 去重——每个 webhook 有唯一 X-GitHub-Delivery，处理过的 ID 存入 `OrderedDict`（LRU 淘汰，`threading.Lock` 保护并发安全），重复的直接返回；2）幂等存储——(repo, pr_number) 为 key，IN_PROGRESS 状态时拒绝新的审查，DONE 状态在 TTL 300s 内也拒绝；3）并发限制——max 3 个并行审查，超限拒绝。这样即使 GitHub 重投递 3 次，也只会执行 1 次审查。

> 后台任务 `_run_review` 包含 `except BaseException` 兜底，确保 `KeyboardInterrupt`/`SystemExit` 等也会释放幂等性锁，防止 PR 被永久阻塞（IN_PROGRESS 状态泄漏）。

---

### 第 6 天：安全深度 + 环境变量脱敏 + 记忆系统 + JSON 契约

**读这些文件**：
```
cr_agent/security/env_sanitizer.py    # 环境变量脱敏
cr_agent/agent/memory.py              # 记忆系统
cr_agent/core/contracts.py            # JSON 契约导出
```

**环境变量脱敏**：
```
Agent 进程有: OPENAI_API_KEY=sk-xxx, GH_TOKEN=ghp-xxx
LLM 说: run bash "env"
  → 没有脱敏: 子进程继承所有 env → API key 打印出来 → 泄露
  → 有脱敏: build_safe_env() → KEY/TOKEN/SECRET 替换为 [REDACTED] → 安全

白名单制: PATH, HOME, USER, LANG, TERM, SHELL 直接通过
黑名单制: 匹配 (KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE|API) 的变量 redacted
其余变量: 直接丢弃，不传入子进程
```

**记忆系统**：
```
第一次审查 owner/repo PR #42:
  → 发现 SQL injection, hardcoded secret
  → save_review_memory() 存到 .cr_agent_memory/owner_repo_42.json

第二次审查 owner/repo PR #43:
  → build_memory_context() 读取历史
  → 注入 prompt: "Previous reviews found: sql-injection (1x), hardcoded-secret (1x)"
  → LLM 重点检查这些模式
```

**JSON 契约**：
```bash
python -m cr_agent.core.contracts
# 导出 4 个 schema 文件到 contracts/
# finding.v1.schema.json
# review_report.v1.schema.json
# diff_metrics.v1.schema.json
# static_analysis.v1.schema.json
```

**面试题**：

> "你的 Agent 有记忆吗？怎么实现的？"

**参考答案**：
> 有。每次审查完成后 save_review_memory() 把 findings 的 rule_id 和 severity 存到 JSON 文件，按 (repo, pr_number) 索引。下次审查同一 repo 时 build_memory_context() 扫描所有历史文件，按频次排序 top 5 最常见的 finding pattern，注入 LLM prompt："Previous reviews of this repo commonly found: SQL injection (3x). Pay extra attention." 这让 Agent 有了仓库级的经验积累。生产环境会换成 Postgres + 向量检索做语义 recall，但原理相同。

---

### 第 7 天：面试模拟（进阶版）

### 自述练习（5 分钟，含工程深度）

> "我做了一个 AI Code Review Agent，用 LangGraph 状态机编排，6 层中间件链管理工具调用循环。
>
> 架构分三层：
> - 确定性规则引擎：30 条正则秒出安全问题
> - LLM 语义审查：通过条件边实现工具调用循环
> - 安全防护：prompt injection 中和、secret 脱敏、环境变量隔离
>
> 生产工程方面：
> - 上下文压缩防止 token 爆炸，保留 system prompt 和初始 diff
> - 双层循环检测：哈希去重 + 频率统计，防止 Agent 无限循环
> - Token 预算控制：80% 警告 100% 强制结束，防止 runaway cost
> - 工具失败 fail-open 降级：lint 挂了不崩溃，标记 skipped 继续
> - 幂等性 + 并发控制：webhook 重投递不重复执行，max 3 并发
> - Trace ID 链路追踪：全链路日志可按 trace_id 过滤
> - 记忆系统：历史审查结果注入下次审查的 prompt
>
> 316 个单元测试覆盖全部核心模块，45 例评测集实测注入成功率 0%。"

### 高频面试题清单（进阶 20 题）

**中间件架构（4 题）**

1. 你的中间件链有哪几层？顺序为什么是这样？
   > InputSanitization（先中和）→ ContextCompression（再压缩）→ LoopDetection（检查循环）→ ToolErrorHandling（捕获错误）→ ToolOutputBudget（截断输出）→ TokenBudget（最后算总量）。顺序由时序决定：输入要先净化才能给 LLM，输出要先截断才能算 token。

2. 中间件怎么做到可插拔？
   > Middleware 基类有 4 个可选 hook，子类只 override 需要的。MiddlewareChain 按列表顺序执行，add() 方法追加。build_default_chain() 组装默认链，换掉一个中间件只改这一行。

3. before_tool 返回非 None 会怎样？
   > 短路。该工具调用被跳过，返回一个 error ToolMessage 给 LLM。LLM 知道这个工具被拦截了，会尝试其他方案。

4. 中间件的状态怎么在 hook 之间传递？
   > MiddlewareContext 数据类。iteration、tool_call_history、total_tokens、blocked_tools 都在 ctx 里。ctx 通过 `contextvars.ContextVar` 绑定到当前执行上下文，每次 `graph.invoke` 拥有独立的 ctx 实例，并发 invoke 不会互相覆盖。`MiddlewareChain.reset()` 在当前上下文中创建新的 MiddlewareContext。

**上下文管理（3 题）**

5. 大 PR 50 个文件怎么处理？
   > diff 截断到 50K 字符传入 prompt。审查过程中 ContextCompressionMiddleware 在消息超过 20 条时触发压缩，保留 system prompt + 初始 diff + 最近 8 条消息。确定性 findings 在 state 里不受影响。TokenBudget 在 200K 时强制结束。

6. 压缩后 LLM 还能出完整报告吗？
   > 能。_finalize 从 state.deterministic_findings 读取（不在消息里），LLM findings 从 generate_report 工具调用的 args 里提取（在 AIMessage.tool_calls 里）。压缩只影响中间的工具结果消息，不影响最终报告组装。

7. 为什么不用真正的 LLM 做摘要？
   > 学习项目用简单截断 + 元信息摘要。生产用独立小模型做摘要（DeerFlow 的 SummarizationMiddleware），成本更低。原理相同：保护关键消息，压缩冗余。

**循环 + 跑偏（3 题）**

8. Agent 反复 read_file 同一文件怎么检测？
   > SHA-256(name + sorted(args)) 哈希存入滑动窗口 deque。同哈希出现 3 次注入 Hint，5 次剥离 tool_calls + forced_finalize。LLM 被迫产出最终回答。

9. Agent 调了 50 次 grep 但每次参数不同怎么办？
   > Layer 2 频率检测。Counter 统计工具名总调用次数，单工具 >30 次加入 blocked_tools。before_tool 拦截，返回 "Tool blocked due to overuse"。

10. Agent 开始审查 PR 以外的文件怎么办？
    > System prompt 约束 "Do NOT review files not in the PR diff"。TokenBudget 在 80% 时提醒收尾。MAX_ITERATIONS=15 硬性上限。三层限制：prompt 引导 + 经济激励 + 硬性截断。

**工具失败 + 降级（3 题）**

11. ruff 没安装怎么办？
    > ToolErrorHandler 捕获 FileNotFoundError，返回 ToolMessage(error) + recovery hint。Agent 继续，报告 Static Analysis 标注 "skipped"。

12. LLM API 挂了怎么办？
    > web/server.py 的 try/except 捕获后降级为 _deterministic_only()，只用正则规则出报告。CircuitBreaker 连续失败 5 次后开路 60s，防止持续重试。retry_with_backoff 对 transient 错误重试 3 次。

13. generate_report 工具自己挂了怎么办？
    > _finalize 有 fallback：如果找不到 generate_report 的 tool_call，用确定性 findings only 组装报告。verdict 由 determine_verdict(findings) 计算。不会完全没有输出。

**幂等 + 并发（3 题）**

14. 同一个 PR 的 webhook 被发了 3 次怎么办？
    > delivery ID 去重（第 2、3 次直接返回 duplicate）+ 幂等存储（第 1 次标记 IN_PROGRESS，后续 try_acquire 返回 False）。3 次只执行 1 次。

15. 3 个 PR 同时触发，你的 Agent 能并行吗？
    > 能。不同 (repo, pr_number) 是不同的 key。但 max_concurrent=3，第 4 个会被拒绝（Concurrency reject）。FastAPI BackgroundTasks 天然异步。

16. 幂等存储的 TTL 300s 是什么意思？
    > 审查完成后 status=DONE，在 300s（5 分钟）内如果同一个 PR 的 webhook 再来，仍然拒绝。防止短时间内重复审查同一 PR。300s 后 TTL 过期，允许重新审查（比如 PR 有新 push）。

**安全 + 可观测（4 题）**

17. 子进程会泄露 API key 吗？
    > 不会。build_safe_env() 白名单制：只有 PATH/HOME/USER/LANG 等安全变量通过，匹配 KEY/SECRET/TOKEN/PASSWORD 的变量替换为 [REDACTED]，其余变量直接丢弃。需要 GH_TOKEN 时通过 extra_env 显式注入。

18. trace ID 怎么贯穿全链路？
    > ContextVar 注入 structlog contextvars。webhook 收到时 new_trace_id() 生成 12 位 hex，bind 到日志上下文。background_tasks 里 re-bind。所有 structlog 输出自动带上 trace_id 字段。

19. structlog 比 print 好在哪？
    > 结构化输出（JSON key-value），可以按任意字段过滤。3 个并行审查的日志混在一起，按 trace_id=abc123 过滤就能看到一条完整链路。print 输出只能人眼扫描。

20. JSON 契约有什么用？
    > Pydantic model_json_schema() 导出 4 个 JSON Schema 文件。工具输出的 Finding 格式和报告生成的 Finding 格式有契约约束。CI 里可以验证工具输出是否匹配 schema。如果改了 Finding 模型，schema 变了，下游组件能立即发现不兼容。

---

## 三、项目文件速查表（进阶版）

| 想复习什么 | 看哪个文件 | 关键函数/类 |
|-----------|-----------|------------|
| 中间件基类 | `agent/middlewares/base.py` | `Middleware`, `MiddlewareChain`(`contextvars` 隔离), `build_default_chain()` |
| Prompt injection 防护 | `middlewares/input_sanitization.py` | `InputSanitizationMiddleware` |
| 上下文压缩 | `middlewares/context_compression.py` | `ContextCompressionMiddleware` |
| 循环检测 | `middlewares/loop_detection.py` | `LoopDetectionMiddleware`, `_hash_call()` |
| Token 预算 | `middlewares/token_budget.py` | `TokenBudgetMiddleware` |
| 工具错误降级 | `middlewares/tool_error_handling.py` | `ToolErrorHandler`, `ToolErrorHandlingMiddleware` |
| 输出截断 | `middlewares/output_budget.py` | `ToolOutputBudgetMiddleware` |
| 幂等性 | `observability/idempotency.py` | `IdempotencyStore`, `try_acquire()`, `release()` |
| Trace ID | `observability/tracing.py` | `new_trace_id()`, `with_trace_id` |
| 环境变量脱敏 | `security/env_sanitizer.py` | `build_safe_env()`, `SAFE_ENV_VARS` |
| 记忆系统 | `agent/memory.py` | `save_review_memory()`, `build_memory_context()` |
| JSON 契约 | `core/contracts.py` | `export_schemas()` |
| 熔断器 + 重试 | `observability/logger.py` | `CircuitBreaker`, `retry_with_backoff()` |
| Webhook 集成 | `github/webhook_server.py` | `handle_webhook()`, `_run_review()` |

---

## 四、面试话术对照表

| 面试官问 | 你的一句话回答 | 深挖时展开 |
|---------|--------------|-----------|
| "大 PR 怎么处理？" | "diff 截断 + 上下文压缩 + token 预算三重控制" | 展开压缩策略：保护 system prompt 和初始 diff，压缩中间消息 |
| "会不会无限循环？" | "双层循环检测：哈希去重 + 频率统计" | 展开阈值：3 次 warn 5 次 hard stop，30 次封顶 |
| "跑偏了怎么办？" | "prompt 约束 + 经济激励 + 硬性截断三层" | 展开预算：80% 警告 100% 强制，MAX_ITERATIONS=15 |
| "工具挂了怎么办？" | "fail-open 降级，转 ToolMessage 不崩溃" | 展开场景：lint 挂了标 skipped，LLM 挂了降级确定性 only |
| "重复 webhook 怎么办？" | "delivery ID 去重 + 幂等存储 + TTL" | 展开流程：IN_PROGRESS 拒绝，DONE+TTL 拒绝，TTL 后允许 |
| "并发冲突怎么办？" | "max 3 并发 + 不同 PR 不互斥" | 展开实现：thread-safe dict + Lock，_active_count 计数 |
| "API key 泄露吗？" | "白名单制环境变量，secret 模式 redacted" | 展开策略：PATH/HOME 通过，KEY/TOKEN 替换，其余丢弃 |
| "有记忆吗？" | "JSON 文件存历史 findings，注入下次 prompt" | 展开效果：同 repo 重复问题模式被记住，LLM 重点检查 |
| "能追踪一个请求吗？" | "trace ID 贯穿 webhook → LLM → comment" | 展开：ContextVar + structlog contextvars，按 trace_id 过滤 |
| "数据格式有保证吗？" | "Pydantic 导出 JSON Schema 契约" | 展开：4 个 schema 文件，CI 可验证工具输出匹配 |

---

## 五、升级后的简历描述

```
项目名称：基于 LangGraph 的自动化 Code Review Agent

技术栈：Python / LangGraph / FastAPI / Pydantic / OpenAI API

项目描述：
从零设计并实现了一个自动化代码审查 Agent，在 GitHub PR 提交后自动触发
审查。采用确定性规则 + LLM 双层审查架构，通过 6 层中间件链管理工具调用
循环，处理了上下文爆炸、无限循环、工具失败、重复执行等生产级 unhappy
path 场景。

个人职责：
1. 设计并实现基于 LangGraph 的 Agent 状态机，通过 6 层中间件链管理
   工具调用循环（InputSanitization → ContextCompression → LoopDetection
   → ToolErrorHandling → ToolOutputBudget → TokenBudget）
2. 实现上下文压缩机制：超过 20 条消息触发压缩，保留 system prompt 和
   初始 diff，防止大 PR 审查时 token 爆炸
3. 实现双层循环检测：SHA-256 哈希精确去重（3 次 warn / 5 次 hard stop）
   + 频率统计（30 次封顶），防止 Agent 无限循环
4. 实现 Token 预算控制：200K 上限，80% 警告 / 100% 强制 finalize
5. 实现工具失败 fail-open 降级：ToolErrorHandler 捕获异常转 ToolMessage，
   附 recovery hint，非关键工具失败不阻塞主流程
6. 实现幂等性 + 并发控制：(repo, pr) 级别去重，TTL 300s，max 3 并发
7. 实现 Trace ID 链路追踪：ContextVar 注入 structlog，全链路可追踪
8. 实现环境变量脱敏：subprocess 白名单制，KEY/TOKEN/SECRET redacted
9. 实现记忆系统：历史审查 findings 存 SQLite（90 天保留期），聚合
   blocker/major 高频模式注入下次审查 prompt
10. 导出 4 个 JSON Schema 契约，跨组件数据流有契约保证
11. 316 个单元测试覆盖中间件、幂等性、安全防护、熔断器等
12. 构建 45 例评测集（golden/对抗/注入/良性），LLM-as-judge 评 soft 维度

项目成果：
- 确定性检查 < 0.1s，LLM 审查约 40s
- 6 层中间件处理 11 个 unhappy path 场景
- 316 个单元测试全部通过
- 评测实测：注入成功率 0%、幻觉率 0%、Recall 97.6%、Verdict 准确率 91.7%
```
