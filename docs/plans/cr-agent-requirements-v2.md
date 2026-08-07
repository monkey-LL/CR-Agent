# CR Agent 需求文档 v2

> **项目**: 独立 Code Review Agent（不依赖任何框架）
> **版本**: v2.0 (基于实际实现更新)
> **日期**: 2026-08-07
> **代码规模**: 3733 行 · 41 个源文件 · 56 个单元测试

---

## 1. 项目概述

### 1.1 背景

Code Review 是软件研发中保障质量的关键环节。基于 LLM 的自动 CR Agent 可以补充人工审查，
在 PR 提交后即时产出结构化审查报告。

本项目从零独立构建，不依赖 DeerFlow 或任何 Agent 框架，使用 LangGraph + Python 原生实现
完整的 Agent 编排、中间件链、安全防护和生产级工程能力。

### 1.2 目标

构建一个自动化 Code Review Agent，在 GitHub PR 事件触发后：

1. 自动获取 PR 变更内容（diff、文件列表、元数据）
2. 对变更代码执行**确定性检查**（8 条正则安全规则 + lint + 类型检查）
3. 对变更代码执行**LLM 语义审查**（逻辑、安全、性能、可维护性）
4. 生成**结构化审查报告**（verdict + findings + metrics）
5. 将审查结果作为 PR comment 回写 GitHub

### 1.3 非目标

- 不自动修复代码（仅审查和建议）
- 不替代人工审查（补充而非替代）
- 不做 CI/CD 流水线集成（不做 pass/fail gate）

---

## 2. 功能需求

### 2.1 PR 事件触发

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-1.1 | PR 创建（opened）时自动触发审查 | P0 | 已实现 |
| FR-1.2 | PR 更新（synchronize）时自动触发审查 | P1 | 已实现 |
| FR-1.3 | PR 重开（reopened）时自动触发审查 | P2 | 已实现 |
| FR-1.4 | 通过 @code-reviewer 评论手动触发 | P0 | 未实现 |
| FR-1.5 | 同一 PR 多个事件不并发（幂等 + 串行） | P1 | 已实现 |

### 2.2 PR 信息获取

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-2.1 | 获取 PR diff（gh pr diff） | P0 | 已实现 |
| FR-2.2 | 获取 PR 文件列表和变更统计 | P0 | 已实现 |
| FR-2.3 | 获取 PR 元数据（标题、作者、base/head） | P0 | 已实现 |
| FR-2.4 | 读取变更文件完整内容（read_file 工具） | P1 | 已实现 |
| FR-2.5 | 读取仓库配置文件了解项目规范 | P2 | 部分 |

### 2.3 确定性检查

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-3.1 | 沙箱执行 lint 命令（ruff/eslint） | P0 | 已实现 |
| FR-3.2 | 沙箱执行类型检查（mypy/tsc） | P1 | 已实现 |
| FR-3.3 | diff 敏感模式扫描（8 条正则规则） | P1 | 已实现 |
| FR-3.4 | 收集为结构化 findings | P0 | 已实现 |
| FR-3.5 | 确定性检查失败不阻塞 LLM 审查 | P0 | 已实现 |

### 2.4 LLM 语义审查

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-4.1 | 审查逻辑正确性 | P0 | 已实现 |
| FR-4.2 | 审查安全风险 | P0 | 已实现 |
| FR-4.3 | 审查性能问题 | P1 | 已实现 |
| FR-4.4 | 审查可维护性 | P1 | 已实现 |
| FR-4.5 | 审查测试覆盖 | P2 | 部分 |
| FR-4.6 | 每个问题含 severity/file/line/描述/修复建议 | P0 | 已实现 |

### 2.5 审查报告

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-5.1 | Markdown 格式报告 | P0 | 已实现 |
| FR-5.2 | 执行摘要 | P0 | 已实现 |
| FR-5.3 | 审查结论（approve/request_changes/block） | P0 | 已实现 |
| FR-5.4 | 按 severity 排序 findings | P0 | 已实现 |
| FR-5.5 | 每个 finding 含 file:line + 修复建议 | P0 | 已实现 |
| FR-5.6 | 确定性检查结果摘要 | P1 | 已实现 |
| FR-5.7 | 中英双语支持 | P2 | 已实现（Web UI） |

### 2.6 PR 回写

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-6.1 | 审查报告作为 PR comment 发布 | P0 | 已实现 |
| FR-6.2 | 使用 gh pr comment CLI 发布 | P0 | 已实现 |
| FR-6.3 | 多次审查更新已有 comment | P1 | 部分 |

### 2.7 Agent 配置

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| FR-7.1 | System prompt 定义 Agent 审查人格 | P0 | 已实现 |
| FR-7.2 | 代码配置模型/工具/中间件 | P0 | 已实现 |
| FR-7.3 | GitHub 仓库绑定和触发条件 | P0 | 已实现 |

---

## 3. 生产级工程需求（v2 新增）

### 3.1 中间件链架构

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| PE-1.1 | 可插拔中间件链（4 种钩子） | P0 | 已实现 |
| PE-1.2 | InputSanitization（prompt injection 防护） | P0 | 已实现 |
| PE-1.3 | ContextCompression（上下文压缩） | P1 | 已实现 |
| PE-1.4 | LoopDetection（循环检测） | P0 | 已实现 |
| PE-1.5 | ToolErrorHandling（工具失败降级） | P0 | 已实现 |
| PE-1.6 | ToolOutputBudget（输出截断） | P1 | 已实现 |
| PE-1.7 | TokenBudget（token 预算控制） | P0 | 已实现 |

### 3.2 幂等性与并发

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| PE-2.1 | Webhook delivery ID 去重 | P0 | 已实现 |
| PE-2.2 | (repo, pr) 级幂等存储 | P0 | 已实现 |
| PE-2.3 | TTL 过期机制（300s） | P1 | 已实现 |
| PE-2.4 | 并发限制（max 3） | P1 | 已实现 |

### 3.3 安全防护

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| PE-3.1 | PR 内容 prompt injection 标签中和 | P0 | 已实现 |
| PE-3.2 | LLM 输出 secret 脱敏 | P0 | 已实现 |
| PE-3.3 | subprocess 环境变量白名单制 | P0 | 已实现 |
| PE-3.4 | 路径遍历拦截 | P1 | 已实现 |
| PE-3.5 | HMAC 常量时间比较 | P0 | 已实现 |

### 3.4 可观测性

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| PE-4.1 | Trace ID 链路追踪 | P0 | 已实现 |
| PE-4.2 | 结构化日志（structlog） | P0 | 已实现 |
| PE-4.3 | 熔断器（closed/open/half_open） | P1 | 已实现 |
| PE-4.4 | 指数退避重试 + jitter | P1 | 已实现 |

### 3.5 记忆与契约

| ID | 需求 | 优先级 | 状态 |
|----|------|--------|------|
| PE-5.1 | 历史审查 findings 存储 | P2 | 已实现 |
| PE-5.2 | 仓库级 pattern 注入 LLM prompt | P2 | 已实现 |
| PE-5.3 | JSON Schema 契约导出 | P1 | 已实现 |

---

## 4. 非功能需求

### 4.1 性能

| ID | 需求 | 目标 | 实测 |
|----|------|------|------|
| NFR-1.1 | 确定性检查延迟 | < 1s | < 0.1s |
| NFR-1.2 | LLM 审查延迟 | < 120s | ~40s |
| NFR-1.3 | Token 预算 | < 200K | 200K 硬限制 |

### 4.2 可靠性

| ID | 需求 | 状态 |
|----|------|------|
| NFR-2.1 | 确定性检查失败降级为仅 LLM | 已实现 |
| NFR-2.2 | LLM 失败重试 + 降级确定性 only | 已实现 |
| NFR-2.3 | 沙箱超时标注 "skipped" | 已实现 |
| NFR-2.4 | 熔断器防止持续重试 | 已实现 |

### 4.3 安全

| ID | 需求 | 状态 |
|----|------|------|
| NFR-3.1 | PR 代码不可信，不执行 | 已实现 |
| NFR-3.2 | 不泄露系统提示 | 已实现 |
| NFR-3.3 | Token 环境变量注入 | 已实现 |
| NFR-3.4 | 拒绝 prompt injection | 已实现 |
| NFR-3.5 | HMAC 签名验证 | 已实现 |

---

## 5. 验收标准

| ID | 验收标准 | 状态 |
|----|---------|------|
| AC-1 | PR 创建后 120s 内出现 comment | 达标（~40s） |
| AC-2 | comment 含摘要、结论、findings | 达标 |
| AC-3 | 每个 finding 有 file:line/severity/修复建议 | 达标 |
| AC-4 | 确定性检查结果包含在报告中 | 达标 |
| AC-5 | @code-reviewer 评论手动触发 | 未达标 |
| AC-6 | 不执行 PR 中的代码 | 达标 |
| AC-7 | Markdown 格式渲染正常 | 达标 |
| AC-8 | 多次审查不重复 comment | 部分达标 |
| AC-9 | 6 层中间件链正常工作 | 达标 |
| AC-10 | 56 个单元测试全部通过 | 达标 |
| AC-11 | Web UI 中英文 + 深浅色切换 | 达标 |

---

## 6. 技术约束

- Python 3.11+，不依赖 DeerFlow 或其他 Agent 框架
- LangGraph 作为状态机编排引擎
- OpenAI 兼容 API（当前使用 DeepSeek-V4-Flash）
- GitHub Webhook + gh CLI 作为 GitHub 集成方式
- FastAPI 作为 Web 服务和 Webhook 服务
- Pydantic v2 作为数据模型和 JSON Schema 来源

---

## 7. 项目结构

```
cr_agent/
├── core/               # 核心引擎
│   ├── models.py           # 数据模型（Finding, Severity, Verdict, Report）
│   ├── diff_parser.py      # Unified diff 解析器
│   ├── rules_engine.py     # 8 条确定性安全规则
│   └── contracts.py        # JSON Schema 契约导出
├── agent/              # Agent 编排
│   ├── graph.py            # LangGraph 状态机
│   ├── state.py            # 状态定义 + reducer
│   ├── prompts.py          # System prompt
│   ├── tools.py            # 3 个工具（lint, read_file, generate_report）
│   ├── memory.py           # 记忆系统
│   └── middlewares/        # 6 层中间件链
│       ├── base.py             # 基类 + 链执行器
│       ├── input_sanitization.py
│       ├── context_compression.py
│       ├── loop_detection.py
│       ├── token_budget.py
│       ├── tool_error_handling.py
│       └── output_budget.py
├── security/           # 安全防护
│   ├── sanitizer.py        # prompt injection + secret 脱敏 + path 验证
│   └── env_sanitizer.py    # 环境变量白名单制
├── sandbox/            # 沙箱执行
│   └── executor.py         # subprocess + timeout + 降级 + env 脱敏
├── github/             # GitHub 集成
│   ├── client.py           # HMAC + gh CLI + diff/comment
│   └── webhook_server.py   # FastAPI webhook + 幂等 + trace + memory
├── observability/      # 可观测性
│   ├── logger.py           # structlog + 熔断器 + 重试
│   ├── tracing.py          # trace ID 链路追踪
│   ├── idempotency.py      # 幂等 + 并发控制
│   └── metrics.py          # 指标采集
├── web/                # Web UI
│   ├── server.py           # FastAPI API + HTML 服务
│   └── index.html          # 中英文 + 深浅色切换
├── cli.py              # CLI 入口
└── __main__.py         # python -m cr_agent
```
