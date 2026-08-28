# CR Agent 学习指南

> 一个独立的 Code Review Agent 项目，从零构建，不依赖 DeerFlow。
> 每个模块都有详细注释，按推荐顺序逐模块学习。

## 项目结构

```
cr_agent/
├── core/               # 核心引擎（不依赖 LLM，纯逻辑）
│   ├── models.py           # 数据模型：Finding, Severity, Verdict, Report
│   ├── diff_parser.py      # Unified diff 解析器：hunk、行号、变更统计
│   └── rules_engine.py     # 确定性规则引擎：30 条正则规则
├── agent/              # Agent 编排（LangGraph 状态机）
│   ├── state.py            # 状态定义（TypedDict + reducer）
│   ├── prompts.py          # System prompt + 审查触发 prompt
│   ├── tools.py            # 2 个工具：run_lint, read_file
│   ├── memory.py           # SQLite 审查记忆（仓库级经验积累）
│   ├── middlewares/        # 6 层中间件链
│   └── graph.py            # 状态机：prepare → llm ↔ tools → finalize
├── web/                # Web UI（FastAPI + 单 HTML）
│   ├── server.py           # API 端点 + 静态页面服务
│   └── index.html          # 深色/浅色主题 + 中英文切换
├── sandbox/            # 沙箱执行
│   └── executor.py         # subprocess + timeout + 降级
├── github/             # GitHub 集成
│   ├── client.py           # HMAC 验证、gh CLI、diff 获取、comment 回写
│   └── webhook_server.py   # FastAPI webhook 服务
├── security/           # 安全防护
│   └── sanitizer.py        # prompt injection 防护、secret 脱敏、path 验证
├── observability/      # 生产工程
│   └── logger.py           # structlog + retry + circuit breaker
├── cli.py              # CLI 入口
└── __main__.py         # python -m cr_agent
```

## 三种使用方式

| 方式 | 适合场景 | 启动命令 |
|------|---------|---------|
| Web UI | 交互式体验、演示 | `python -m uvicorn cr_agent.web.server:app --port 8088` |
| CLI | 脚本化、管道 | `python -m cr_agent --diff-file diff.patch --no-llm` |
| Webhook | GitHub 自动化 | `python -m uvicorn cr_agent.github.webhook_server:app --port 8088` |

## 学习路径（按推荐顺序）

### 第 1 步：启动 Web UI 体验

```bash
./start.sh web
# 或: python -m uvicorn cr_agent.web.server:app --port 8088
```

打开 http://localhost:8088，点「Load Sample」→「Review」，秒出 5 个安全问题。
右上角可切换中英文和深色/浅色主题。

### 第 2 步：核心引擎（不依赖 LLM，纯逻辑）

**文件**: `core/diff_parser.py`, `core/rules_engine.py`, `core/models.py`

学什么：
- Unified diff 格式是什么？如何用正则解析？
- 为什么先做确定性检查（正则），再交给 LLM？
- Severity 分级（blocker/major/minor/info）和 Verdict 决策逻辑
- 30 条规则的设计：hardcoded secret、SQL injection、eval、command injection 等

```bash
# 只运行确定性检查
echo '--- a/f.py
+++ b/f.py
@@ -1,1 +1,2 @@
+eval(user_input)' | python -m cr_agent --diff-stdin --no-llm
```

### 第 3 步：安全防护

**文件**: `security/sanitizer.py`

学什么：
- Prompt injection 攻击原理：PR 内容中的 `<system-reminder>` 标签
- 三层防御：标签中和 → secret 脱敏 → path 验证
- 为什么 secret masking 必须在多个层级应用（LLM 输出、工具输出、PR 评论）

### 第 4 步：Agent 编排引擎（精华）

**文件**: `agent/state.py`, `agent/graph.py`, `agent/tools.py`, `agent/prompts.py`

学什么：
- LangGraph StateGraph：节点（函数）+ 边（路由）+ 状态（共享内存）
- State reducer：`operator.add` 让消息追加而非覆盖
- 工具调用循环：LLM 说"我要读文件" → 我们执行 → LLM 看结果 → 继续
- 条件边：`has_tool_calls? → tools | finalize`，这是如何创建循环的
- Recursion limit：防止 Agent 陷入无限循环
- System prompt 设计五要素：角色 + 流程 + 输出格式 + 安全规则 + 预算

核心图结构：
```
START → prepare → llm → [has_tool_calls?]
                           ├── yes → tools → llm (循环)
                           └── no  → finalize → END
```

### 第 5 步：Web UI 实现

**文件**: `web/server.py`, `web/index.html`

学什么：
- FastAPI 同时服务 API 和 HTML
- 前端如何通过 fetch 调用 `/api/review` 触发审查
- CSS 变量实现深色/浅色主题切换
- i18n 字典实现中英文切换
- 单 HTML 文件无需 npm 构建，适合学习项目

### 第 6 步：沙箱执行

**文件**: `sandbox/executor.py`

学什么：
- subprocess 安全使用：timeout、capture_output
- 优雅降级：lint 不存在 → 标记 "skipped"，不崩溃
- 输出截断：保护 token 预算

### 第 7 步：GitHub 集成

**文件**: `github/client.py`, `github/webhook_server.py`

学什么：
- HMAC 签名验证：为什么需要、constant-time 比较防时序攻击
- Webhook 去重：GitHub 会重投递，用 delivery ID 去重
- 后台任务：webhook 10s 内返回 200，审查异步执行
- gh CLI：不用 API 库，GH_TOKEN 环境变量注入

### 第 8 步：生产级工程

**文件**: `observability/logger.py`

学什么：
- 指数退避重试：`base * 2^attempt + jitter`，为什么加 jitter
- 熔断器模式：closed → open → half_open → closed
- 结构化日志：JSON key-value 比文本好搜索

## 快速开始

```bash
# 1. 启动 Web UI
./start.sh web

# 2. 运行测试
./start.sh test

# 3. CLI 确定性检查（免费）
echo '--- a/f.py
+++ b/f.py
@@ -1,1 +1,2 @@
+eval(x)' | python -m cr_agent --diff-stdin --no-llm
```

## 核心概念速查

| 概念 | 文件 | 一句话解释 |
|------|------|-----------|
| Diff 解析 | core/diff_parser.py | 把 `@@ -30,7 +30,12 @@` 变成结构化数据 |
| 确定性规则 | core/rules_engine.py | 正则匹配 secret、SQL injection、eval 等 |
| Agent 状态 | agent/state.py | 所有节点共享的内存，reducer 合并更新 |
| 工具调用循环 | agent/graph.py | LLM 说"读文件" → 执行 → LLM 看结果 → 继续 |
| Prompt injection | security/sanitizer.py | PR 里的 `<system-reminder>` 标签被中和 |
| 熔断器 | observability/logger.py | 连续失败 5 次后停止调用，60s 后探针恢复 |
| HMAC 验证 | github/client.py | 用共享 secret 签名 webhook，防伪造 |
| Web UI 主题 | web/index.html | CSS 变量 + data-theme 属性切换深色/浅色 |
| Web UI i18n | web/index.html | data-i18n 属性 + 字典对象切换中英文 |
