# CR Agent 新人入职指南

> 基于 Understand-Anything 知识图谱自动生成 | 生成日期: 2026-08-12 | Git commit: cdd422ac

---

## 项目概览

**CR Agent** 是一个基于 LangGraph 的自动化代码审查 Agent。它在 GitHub PR 提交后自动触发审查，采用确定性规则 + LLM 双层审查架构。

**技术栈**: Python 3.11+ / LangGraph / FastAPI / Pydantic v2 / uvicorn / pytest / structlog / OpenAI 兼容 API（支持 DeepSeek/GPT）

**核心设计理念**:
- **双层审查**: 确定性正则规则（快速、高精度）+ LLM 语义分析（逻辑、架构、微妙问题）
- **LLM 不在控制**: 图结构和中间件链决定工具是否执行、是否循环或结束，LLM 只提供建议
- **生产级安全**: prompt injection 防御、密钥脱敏、沙箱执行、路径遍历防护
- **可观测性优先**: 结构化日志、trace ID 传播、熔断器、重试、幂等控制

---

## 架构分层

项目由 10 个逻辑层组成，从核心引擎到外部集成逐层展开:

### 1. 核心引擎层 (`cr_agent/core/`)
审查的数据基础和规则检测逻辑。
- `diff_parser.py` — 解析统一 diff 格式，提取结构化 DiffHunk
- `models.py` — Pydantic v2 数据模型: Severity / Verdict / Finding / ReviewReport
- `rules_engine.py` — 25+ 正则规则检测安全漏洞、调试代码、错误处理等
- `contracts.py` — 导出 JSON Schema 契约文件

### 2. Agent 编排层 (`cr_agent/agent/`)
Agent 的核心控制流，这是项目最核心的模块。
- `graph.py` — LangGraph 状态机: prepare → llm ↔ tools → finalize
- `state.py` — AgentState TypedDict，`Annotated[list, operator.add]` 实现消息追加
- `prompts.py` — 系统提示词（安全规则、输出格式、预算约束）
- `tools.py` — 三个工具定义: run_lint / read_file / generate_report
- `memory.py` — SQLite + JSON 双后端审查记忆系统
- `middlewares/` — 6 个中间件组成的防护链

### 3. 中间件链 (`cr_agent/agent/middlewares/`)
生产级 Agent 的核心防线，顺序至关重要:
1. `InputSanitizationMiddleware` — 中和 prompt injection 标签
2. `ContextCompressionMiddleware` — 压缩历史消息防止 token 爆炸
3. `LoopDetectionMiddleware` — 检测无限工具调用循环
4. `ToolErrorHandlingMiddleware` — 工具失败优雅降级
5. `ToolOutputBudgetMiddleware` — 截断超长工具输出
6. `TokenBudgetMiddleware` — 80% 警告 / 100% 强制结束

### 4. 安全防护层 (`cr_agent/security/`)
三层防御: prompt injection 中和、密钥脱敏（API key/token/Bearer）、路径遍历验证。`env_sanitizer.py` 构建安全子进程环境。

### 5. 沙箱执行层 (`cr_agent/sandbox/`)
命令白名单 + shlex 分割（禁止 `shell=True`）+ shell 元字符正则拒绝 + 超时控制。fail-open 设计: lint 失败返回 skipped 而非崩溃。

### 6. GitHub 集成层 (`cr_agent/github/`)
- `client.py` — gh CLI 封装: HMAC 验证、PR diff 获取、评论幂等更新
- `webhook_server.py` — FastAPI webhook 入口: HMAC → 幂等 → 异步审查

### 7. 可观测性层 (`cr_agent/observability/`)
- `logger.py` — structlog + 熔断器 + 指数退避重试 + 阶段计时
- `tracing.py` — ContextVar 绑定 trace ID 到日志上下文
- `metrics.py` — 请求级隔离指标: 阶段耗时、token 用量、工具调用
- `idempotency.py` — 内存/Redis 双后端幂等存储

### 8. Web UI 层 (`cr_agent/web/`)
FastAPI REST API + 前端页面。端点: `POST /api/review`、`GET /api/rules`、`GET /api/health`

### 9. 契约层 (`contracts/`)
JSON Schema 定义跨组件数据格式（finding / review_report / diff_metrics / static_analysis）

### 10. 配置层
`pyproject.toml`（依赖+lint+pytest）、`.env.example`（环境变量模板）、`start.sh`（启动脚本）、`cli.py` + `__main__.py`（入口）

---

## 关键概念

### 确定性规则 + LLM 双层审查
规则引擎用正则扫描 diff 新增行，检测 hardcoded secret、SQL injection、command injection 等 25+ 类问题。这些高精度模式交给正则，LLM 聚焦逻辑错误、架构问题和微妙的安全隐患——各司其职。

### LangGraph 状态机: prepare → llm ↔ tools → finalize
```
START → prepare (确定性检查 + 提示词构建)
      → llm (绑定工具 + 中间件拦截)
      ↔ tools (沙箱执行 + 错误处理)     ← 条件边: _should_continue
      → finalize (合并发现 + 生成报告)
      → END
```
关键: LLM 只建议调用哪个工具，中间件和图结构决定是否执行。

### `Annotated[list, operator.add]` Reducer
AgentState 中的 `messages` 字段使用 `operator.add` reducer——节点返回的消息被**追加**到历史而非覆盖。这是 LangGraph 状态流的核心机制。

### 中间件四阶段钩子
`before_model` / `after_model` / `before_tool` / `after_tool`。`before_tool` 返回非 None 会短路阻断后续中间件和实际工具执行。模式借鉴自 Express/Koa。

### 幂等性设计
webhook 重发和并发推送是生产环境的核心问题。内存后端用 `threading.Lock` + TTL，Redis 后端用 `SET NX + EX` 原子操作实现分布式锁。同一 (repo, pr_number) 在审查进行中或完成后 TTL 内拒绝重复。

### 沙箱安全
`subprocess.run(args=[...])` 而非 `shell=True`，`_FORBIDDEN_PATTERNS` 正则拦截 `;`、`|`、`&&`、`$()`、反引号。命令白名单仅允许 ruff/mypy/eslint/tsc/gh 等已知工具。

### trace ID 传播
`contextvars.ContextVar` 绑定 UUID 到 structlog contextvars，实现跨日志行的单次审查追踪。多个并发审查的日志不再混淆。

---

## 学习路径（引导式 Tour）

按以下顺序阅读代码，从"这是什么"到"它怎么工作":

| 步骤 | 标题 | 关键文件 | 学习重点 |
|------|------|---------|---------|
| 1 | 项目概览 | `README.md` | 定位、技术栈、快速启动 |
| 2 | 应用入口 | `cli.py`, `__main__.py` | 审查完整流程: 获取 diff → 规则/LLM → 报告 |
| 3 | 核心数据模型 | `core/models.py` | Severity/Verdict/Finding 词汇表 + `determine_verdict()` |
| 4 | Diff 解析与规则引擎 | `diff_parser.py`, `rules_engine.py` | 确定性审查层: 25+ 正则规则仅检查新增行 |
| 5 | Agent 状态机编排 | `agent/graph.py`, `state.py` | prepare→llm↔tools→finalize + reducer 语义 |
| 6 | 提示词与工具 | `prompts.py`, `tools.py` | 系统提示词安全规则 + @tool 装饰器 |
| 7 | 中间件链架构 | `middlewares/base.py` 等 4 个 | 四阶段钩子 + 顺序重要性 + 短路阻断 |
| 8 | 安全防护体系 | `sanitizer.py`, `env_sanitizer.py` | 三层防御: injection/secret/path |
| 9 | 沙箱执行器 | `sandbox/executor.py` | 白名单 + shlex + 元字符拒绝 |
| 10 | GitHub 集成 | `github/client.py`, `webhook_server.py` | HMAC → 幂等 → 异步审查 |
| 11 | 可观测性 | `logger.py`, `tracing.py`, `metrics.py`, `idempotency.py` | 熔断器/重试/trace/metrics/幂等 |
| 12 | Web UI | `web/server.py`, `index.html` | REST API + 前端交互 |
| 13 | 契约与文档 | `contracts/`, 设计文档 | JSON Schema + 架构决策记录 |

---

## 文件地图

### 核心引擎层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `cr_agent/core/rules_engine.py` | complex | 25+ 正则规则，安全漏洞+调试+错误处理检测 |
| `cr_agent/core/models.py` | moderate | Severity/Verdict/Confidence 枚举 + Finding/ReviewReport 模型 |
| `cr_agent/core/diff_parser.py` | moderate | 统一 diff 解析，DiffHunk + 变更指标计算 |
| `cr_agent/core/contracts.py` | simple | 导出 Pydantic 模型为 JSON Schema |

### Agent 编排层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `cr_agent/agent/graph.py` | complex | LangGraph 状态机，6 个节点函数 |
| `cr_agent/agent/memory.py` | complex | SQLite + JSON 双后端审查记忆 |
| `cr_agent/agent/prompts.py` | moderate | 系统提示词 + 用户提示词构建 |
| `cr_agent/agent/tools.py` | moderate | run_lint / read_file / generate_report |
| `cr_agent/agent/state.py` | simple | AgentState TypedDict |

### 中间件链
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `middlewares/context_compression.py` | complex | 消息超阈值时摘要压缩 |
| `middlewares/base.py` | moderate | Middleware 基类 + MiddlewareChain 执行器 |
| `middlewares/loop_detection.py` | moderate | 去重哈希 + 频率分析 |
| `middlewares/token_budget.py` | moderate | tiktoken 计数 + 80%/100% 阈值 |
| `middlewares/tool_error_handling.py` | moderate | 异常捕获 → ToolMessage 降级 |
| `middlewares/input_sanitization.py` | simple | 标签中和 + 密钥脱敏 |
| `middlewares/output_budget.py` | simple | 截断超长输出 |

### 安全防护层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `security/sanitizer.py` | moderate | sanitize_input / mask_secrets / validate_path |
| `security/env_sanitizer.py` | simple | 白名单环境变量 + 密钥替换 |

### 沙箱执行层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `sandbox/executor.py` | complex | 命令白名单 + shlex + 元字符拒绝 + 超时 |

### GitHub 集成层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `github/client.py` | complex | gh CLI: HMAC/diff/评论幂等更新 |
| `github/webhook_server.py` | complex | FastAPI webhook: HMAC→幂等→异步审查 |

### 可观测性层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `observability/idempotency.py` | complex | 内存/Redis 双后端幂等存储 |
| `observability/logger.py` | moderate | structlog + 熔断器 + 重试 |
| `observability/metrics.py` | moderate | ContextVar 请求级指标隔离 |
| `observability/tracing.py` | simple | trace ID ContextVar 传播 |

### Web UI 层
| 文件 | 复杂度 | 职责 |
|------|--------|------|
| `web/server.py` | moderate | FastAPI REST API + 静态文件服务 |
| `web/index.html` | complex | 前端交互界面（725 行） |

---

## 复杂度热点

新开发者在修改以下文件时应格外小心:

1. **`agent/graph.py`** (complex, 262 行) — Agent 编排核心，6 个节点函数 + 中间件集成。修改图结构需要理解 LangGraph 条件边和状态 reducer 语义。

2. **`agent/memory.py`** (complex, 276 行) — 双存储后端（SQLite + JSON），线程安全锁。修改存储 schema 需要同步两个后端。

3. **`core/rules_engine.py`** (complex, 316 行) — 25+ 正则规则，修改规则需要考虑误报率。仅检查新增行的行号追踪逻辑容易出错。

4. **`github/client.py`** (complex, 206 行) — gh CLI 封装 + HMAC 验证 + 评论幂等更新。幂等更新逻辑涉及查找现有评论并 PATCH 更新。

5. **`github/webhook_server.py`** (complex, 146 行) — 生产入口: HMAC → 幂等 → 异步审查。修改生命周期需要同步 idempotency.release() 调用。

6. **`sandbox/executor.py`** (complex, 202 行) — 安全关键模块。修改白名单或禁止模式需要安全审查。

7. **`observability/idempotency.py`** (complex, 292 行) — 双后端幂等存储。Redis 原子操作逻辑修改需要分布式锁知识。

8. **`middlewares/context_compression.py`** (complex, 189 行) — 消息压缩涉及保护系统提示词和初始用户消息的逻辑，容易遗漏边界情况。

---

## 快速启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY / GH_TOKEN

# 2. 启动 Web UI
./start.sh web
# 浏览器打开 http://localhost:8088

# 3. 运行测试
./start.sh test

# 4. CLI 审查
python -m cr_agent.cli --repo owner/repo --pr 42
python -m cr_agent.cli --diff-file path/to/diff.patch    # 本地 diff
python -m cr_agent.cli --no-llm                          # 仅确定性规则
```

---

## 数据流概览

```
GitHub PR → webhook_server.py
  → HMAC 验证 (github/client.py)
  → 幂等检查 (observability/idempotency.py)
  → 异步审查 (_run_review)
    → get_pr_diff (github/client.py)
    → build_memory_context (agent/memory.py)
    → graph.invoke()
      → prepare: parse_diff + run_deterministic_checks + build_review_prompt
      → llm: ChatOpenAI.bind_tools + middleware chain (before_model/after_model)
      → tools: run_lint (sandbox) / read_file (validate_path) / generate_report
      → finalize: merge findings + determine_verdict + ReviewReport
    → post_pr_comment (幂等更新)
    → save_review_memory (SQLite)
  → idempotency.release()
```

---

*本指南由 [Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) 知识图谱自动生成。如需更新，运行 `/understand --full` 重新分析后再次生成。*
