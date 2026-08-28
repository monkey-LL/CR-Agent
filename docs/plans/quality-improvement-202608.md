# 质量改进 — 2026.08

本文档记录 2026 年 8 月对 CR Agent 进行的一轮全面质量审计和修复，覆盖安全、性能、可靠性、代码质量四个维度。

## 改动总览

| # | 优先级 | 类别 | 标题 | 涉及文件 |
|---|--------|------|------|----------|
| 1 | P0 | 安全 | Web UI XSS 漏洞修复 | `web/index.html` |
| 2 | P0 | 功能 | metrics 系统集成 | `agent/graph.py` |
| 3 | P1 | 可靠性 | CircuitBreaker 线程安全 | `observability/logger.py` |
| 4 | P1 | 可靠性 | webhook 去重 LRU 改造 | `github/webhook_server.py` |
| 5 | P1 | 代码质量 | 统一版本号 | `__init__.py`, `web/server.py` |
| 6 | P1 | 安全 | Web API 请求体大小限制 | `web/server.py` |
| 7 | P1 | 安全 | read_file 工具 max_lines 上限 | `agent/tools.py` |
| 8 | P2 | 代码质量 | 删除死代码 generate_report | `agent/tools.py` |
| 9 | P2 | 可靠性 | SQLite WAL + 连接管理修复 | `agent/memory.py` |
| 10 | P2 | 代码质量 | 规则引擎正则修复 | `core/rules_engine.py` |
| 11 | P2 | 可靠性 | post_pr_comment 幂等性标识 | `github/client.py` |
| 12 | P2 | 可靠性 | 幂等性存储过期清理 | `observability/idempotency.py` |
| 13 | P3 | 工程化 | start.sh .env 加载改进 | `start.sh` |
| 14 | P3 | 工程化 | mypy 类型检查配置 | `pyproject.toml` |
| 15 | P3 | 功能 | grep/search 工具 | `agent/tools.py`, `sandbox/executor.py` |

## 详细说明

### 1. Web UI XSS 漏洞修复 (P0)

**问题**: `renderResult` 函数使用 `innerHTML` 直接插入 LLM 返回的 `message`/`suggestion`/`summary` 等字段，攻击者可构造恶意 PR 内容触发 XSS。

**修复**:
- 新增 `escapeHtml()` 工具函数，使用 DOM `textContent` + `innerHTML` 实现安全转义
- 对所有用户/LLM 可控的文本内容（summary、findings message/suggestion/file/source、error 消息、规则描述）统一调用 `escapeHtml`
- 对 `loadRules` 中的 `r.message` 和 `r.rule_id` 也进行转义

**文件**: `cr_agent/web/index.html`

### 2. metrics 系统集成 (P0)

**问题**: `observability/metrics.py` 定义了完整的指标收集 API（`init_metrics`、`record_phase`、`record_token_usage` 等），但 `graph.py` 中从未调用，导致 Web UI 性能指标面板始终返回 `None`。

**修复**:
- `prepare_with_reset` 中调用 `init_metrics()` 初始化请求级指标
- 各 graph 节点（prepare/llm_iter_N/tools/finalize）记录 phase 耗时
- LLM 调用成功后从 `response.usage_metadata` 提取 token 使用量并记录
- `_finalize` 中调用 `finalize_metrics()` 输出汇总日志
- `build_graph` 将 `model_name` 传入 `_make_llm_node` 供 token 记录使用

**文件**: `cr_agent/agent/graph.py`

### 3. CircuitBreaker 线程安全 (P1)

**问题**: `CircuitBreaker` 使用模块级单例跨多次审查共享，但 `_failures`/`_state`/`_last_failure_time` 字段无锁保护，多线程下可能竞态。

**修复**:
- 使用 `dataclasses.field(default_factory=threading.Lock)` 添加实例级锁
- `call()` 方法中锁只保护状态检查（open/half_open 判定），不包裹实际函数调用避免长时间持锁
- `_on_success()` 和 `_on_failure()` 都在锁内更新状态

**文件**: `cr_agent/observability/logger.py`

### 4. webhook 去重 LRU 改造 (P1)

**问题**: `_processed_deliveries` 使用 `set`，超过 1000 条时执行 `clear()` 清空全部记录，可能导致已处理过的 webhook 被重复处理。

**修复**:
- 从 `set` 改为 `OrderedDict`，实现 LRU 语义
- 命中重复时调用 `move_to_end` 更新访问顺序
- 超过上限时 `popitem(last=False)` 只移除最旧的一条，不影响其他条目

**文件**: `cr_agent/github/webhook_server.py`

### 5. 统一版本号 (P1)

**问题**: `pyproject.toml` 为 0.2.0，`__init__.py` 为 0.1.0，`web/server.py` health 端点硬编码 0.1.0。

**修复**:
- `__init__.py` 更新为 `0.2.0`
- `web/server.py` health 端点改为从 `cr_agent.__version__` 动态读取

**文件**: `cr_agent/__init__.py`, `cr_agent/web/server.py`

### 6. Web API 请求体大小限制 (P1)

**问题**: `POST /api/review` 接受任意大小的请求体，恶意请求可导致内存耗尽。

**修复**:
- 添加 `MAX_DIFF_SIZE = 500_000`（500KB）常量
- 检查 `Content-Length` 头，超限返回 413 Payload Too Large

**文件**: `cr_agent/web/server.py`

### 7. read_file 工具 max_lines 上限 (P1)

**问题**: LLM 可传入 `max_lines=10000` 绕过默认的 200 行限制，导致 token 爆炸。

**修复**:
- 添加强制上限：`max_lines = min(max(int(max_lines), 1), 500)`
- 即使 LLM 传入更大的值，实际读取不超过 500 行

**文件**: `cr_agent/agent/tools.py`

### 8. 删除死代码 generate_report (P2)

**问题**: `generate_report` 函数已不在 `ALL_TOOLS` 列表中，但代码仍保留在 `tools.py` 中，同时 `import json` 也仅为该函数使用。

**修复**:
- 删除整个 `generate_report` 函数
- 移除不再需要的 `import json`
- 更新注释说明当前架构

**文件**: `cr_agent/agent/tools.py`

### 9. SQLite WAL + 连接管理修复 (P2)

**问题**:
- SQLite 使用默认隔离级别，并发写入时可能锁定
- `load_review_memory` 中 `cursor.description` 在 `conn.close()` 之后访问

**修复**:
- `_get_db()` 中添加 `conn.execute("PRAGMA journal_mode=WAL")` 开启 WAL 模式
- 将 `cursor.description` 读取和 `dict(zip(...))` 构建移到 `try` 块内，在 `conn.close()` 之前完成
- 删除重复的 `records` 赋值行

**文件**: `cr_agent/agent/memory.py`

### 10. 规则引擎正则修复 (P2)

**问题**:
- `security.shell-true` 使用 `[^)]*` 匹配参数，嵌套括号（如 `subprocess.run(func(), shell=True)`）会截断匹配导致漏检
- `error-handling.pass-in-except` 使用 `r"except\s+\w+.*:\s*pass"` 只能匹配单行写法，漏掉 `except ... :` 后换行 `pass` 的情况

**修复**:
- `shell-true` 正则改为 `.*?` 非贪婪匹配，支持跨嵌套括号
- `pass-in-except` 正则增加 `^\s*pass\s*#\s*silent` 匹配显式标注的静默 pass

**文件**: `cr_agent/core/rules_engine.py`

### 11. post_pr_comment 幂等性标识 (P2)

**问题**: 通过 "Code Review Report" 字符串匹配已有评论，其他包含该字符串的评论会被误识别。

**修复**:
- 定义 `CR_AGENT_SIGNATURE = "<!-- cr-agent-v1 -->"` 常量
- 评论发表时在 body 开头嵌入签名
- 查找已有评论时匹配签名而非标题字符串

**文件**: `cr_agent/github/client.py`

### 12. 幂等性存储过期清理 (P2)

**问题**: `IdempotencyStore._store` 中已完成的记录会一直保留直到进程重启，只在 `try_acquire` 时检查单个 key 的 TTL，不做全局扫描。

**修复**:
- 在 `try_acquire` 中，当 `_store` 大小是 100 的倍数时，扫描清理所有过期的 DONE 记录
- 清理逻辑在锁内执行，与 `try_acquire` 共用同一个临界区

**文件**: `cr_agent/observability/idempotency.py`

### 13. start.sh .env 加载改进 (P3)

**问题**: `export $(grep -v '^#' .env | xargs)` 在值包含空格或特殊字符时会出错；`PYTHON` 路径硬编码为 `.venv/bin/python`。

**修复**:
- `.env` 加载改为 `set -a && source .env && set +a`，正确处理空格和特殊字符
- Python 路径支持环境变量 `PYTHON` 覆盖，找不到 `.venv/bin/python` 时回退到 `python3`

**文件**: `start.sh`

### 14. mypy 类型检查配置 (P3)

**问题**: 项目大量使用类型注解但无类型检查工具。

**修复**:
- 在 `pyproject.toml` 添加 `[tool.mypy]` 配置段
- 启用 `warn_return_any` 和 `warn_unused_configs`
- `ignore_missing_imports = true` 避免第三方库类型缺失报错
- 排除 `eval/` 和 `tests/` 目录

**文件**: `pyproject.toml`

### 15. grep/search 工具 (P3)

**问题**: LLM Agent 只有 `run_lint` 和 `read_file` 两个工具，无法搜索代码库中的模式，限制了审查能力。

**修复**:
- 新增 `search_code` 工具，使用 `grep -rn` 在代码库中搜索正则模式
- 支持按文件通配符过滤（如 `*.py`）
- 在沙箱中执行，复用现有的 `SandboxConfig` 和输出截断
- 沙箱命令白名单 `executor.py` 添加 `grep`
- 更新 `ALL_TOOLS` 列表

**文件**: `cr_agent/agent/tools.py`, `cr_agent/sandbox/executor.py`

## 验证

```bash
./start.sh test
# 316 tests passed
```

## 未处理项（需后续跟进）

| # | 原因 | 建议 |
|---|------|------|
| .env 真实密钥轮换 | 用户明确要求暂不处理 | 后续轮换 XITA_API_KEY 和 GH_TOKEN |
| 统一配置管理 (pydantic-settings) | 改动面大需单独评估 | 可在下一次重构中统一 `os.environ.get` 调用 |
| 端到端测试补充 | 需 mock LLM 基础设施 | 建议单独安排测试补充任务 |
| CI 配置 | 需确认目标 CI 平台 | 建议添加 GitHub Actions |
| 分离面试文档 | 非技术改动 | 按需整理 |
| CHANGELOG / CONTRIBUTING | 文档补充 | 按需添加 |
