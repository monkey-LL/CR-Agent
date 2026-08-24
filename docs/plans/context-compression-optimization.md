# 上下文压缩优化文档

> 编写日期: 2026-08-24
> 参考: Claude Code `/compact`、LangChain `ConversationSummaryBufferMemory`、OpenAI Assistants API
> 状态: 已实施，160 测试通过

---

## 背景

原有 `ContextCompressionMiddleware` 存在三个设计问题，与业界做法存在差距。本次优化参考 Claude Code `/compact` 和 LangChain 的上下文管理策略，进行三项改进。

---

## C1: 触发条件从消息条数改为 token 数

### 原有问题

按消息条数（20 条）触发压缩。20 条短消息可能只有 5K token（不需要压缩），20 条长消息可能 80K token（早该压缩）。条数不能反映真实 token 占用。

### 业界做法

- LangChain `ConversationSummaryBufferMemory`：按 `max_token` 触发
- OpenAI Assistants API：按 token 数截断
- Claude Code `/compact`：手动触发，但内部按 token 判断是否需要

### 优化方案

- 复用 `tiktoken`（与 `TokenBudgetMiddleware` 同一个编码器）精确计数
- tiktoken 不可用时回退到字符数 ÷ 4 估算
- `max_tokens=50000` 作为默认阈值（约为 128K 上下文窗口的 40%，留足 LLM 回复空间）
- 每次 `before_model` 时计算当前 messages 的 token 数，超阈值才触发

### 改动

- `ContextCompressionMiddleware.__init__`：`max_messages` → `max_tokens`
- 新增 `_count_tokens()` 方法
- `before_model`：`len(messages) <= self.max_messages` → `self._count_tokens(messages) <= self.max_tokens`
- `build_default_chain()`：`max_messages=20` → `max_tokens=50000`

---

## C2: 结构化摘要按工具类型分别提取

### 原有问题

无 LLM fallback 时，对所有工具输出统一取"第一行前 100 字符"。lint 返回 500 行输出只取第一行，关键错误可能在第 200 行——信息损失大。

### 业界做法

- Claude Code `/compact`：用主 LLM 生成摘要，能理解语义
- LangChain：用 LLM 做 chain-of-thought 摘要

### 优化方案

本项目不默认用 LLM 做摘要（避免额外 API 调用），但提升正则提取的质量：

| 工具类型 | 原有提取 | 优化后提取 |
|---------|---------|-----------|
| `read_file` | "read: app.py" | "Files read: app.py (200 lines), utils.py (150 lines)" |
| `run_lint` | 第一行前 100 字符 | "Lint results: lint passed \| error at line 42 \| error at line 87"（取前 3 条错误行） |
| AIMessage (含 findings) | 前 100 字符 | "LLM analysis: produced 5 findings"（解析 JSON 统计数量） |
| AIMessage (普通分析) | 前 100 字符 | 前 150 字符（增大保留量） |
| 错误消息 | 前 80 字符 | 提取含 "Error" 的行（最多 3 条） |

### 改动

- `_extract_tool_info` 完全重写，按 `ToolMessage` 内容特征分类提取
- `read_file` 结果提取行数
- `run_lint` 结果区分 passed/failed，failed 时取错误行
- AIMessage 用 `_contains_findings()` 判断是否含审查结果 JSON

---

## C3: 标记含 findings 的 AIMessage 为不可压缩

### 原有问题

D1 优化后 LLM 在 content 里直接输出 JSON findings。如果这条消息被压缩，findings 会丢失——`_finalize` 从最后一条 AIMessage 解析 JSON，但中间的 findings 消息被压成摘要后无法恢复。

### 业界做法

- Claude Code：标记关键决策消息为 "pinned"，不参与压缩
- LangChain：`return_messages=True` 保留关键消息

### 优化方案

- 新增 `_contains_findings(msg)` 方法：检查 AIMessage 的 content 是否包含 `"findings"` 和 `"severity"` 字段
- `before_model` 分类消息时，含 findings 的 AIMessage 加入 `protected` 列表，不参与压缩
- 日志中输出 `protected=N` 便于调试

### 改动

- 新增 `_contains_findings()` 方法
- `before_model` 消息分类逻辑增加第三个类别：`protected`（system + 首条用户 + 含 findings 的 AIMessage）

---

## 参数速查

| 参数 | 原值 | 新值 | 说明 |
|------|------|------|------|
| 触发条件 | `max_messages=20` | `max_tokens=50000` | 按 token 数触发 |
| 保留最近 | `keep_recent=8` | `keep_recent=8` | 不变 |
| 不可压缩 | system + 首条用户 | system + 首条用户 + **含 findings 的 AIMessage** | 新增保护类别 |

---

## 验证

```
160 tests passed, 1 warning in 4.47s
```

测试更新：
- `test_compresses_when_too_many_messages` → `test_compresses_when_too_many_tokens`，用长内容 + 小 token 阈值触发
- `test_no_compression_when_under_limit`：`max_messages=20` → `max_tokens=50000`