# CR Agent 后端系分文档

> **项目**: CR Agent — 基于 LangGraph 的 AI Code Review Agent
> **文档类型**: 系统分析设计(后端系分)
> **版本**: v0.2.0(对应代码包 `cr-agent==0.2.0`)
> **编写日期**: 2026-08-24
> **代码基线**: 41 个源文件 · 约 3733 行 Python · 9 个测试文件 / 156 个测试函数
> **前置文档**: [需求文档 v2](./cr-agent-requirements-v2.md) · [设计文档 v2](./cr-agent-design-v2.md)

> **本文档与既有文档的关系**: 本文以**代码实际实现**为准。需求/设计 v2 文档与代码存在若干 drift,本文已对齐代码,并在「6. 风险分析」中单列「文档/代码漂移」风险。读者如发现本文与设计 v2 不一致,**以本文为准**。

---

## 1. 需求价值「必需」

### 1.1 背景

Code Review 是软件研发中保障质量的关键环节,但人工 CR 成本高、响应慢、覆盖不均。基于 LLM 的自动 CR Agent 能在 PR 提交后**即时**产出结构化审查报告,补充而非替代人工审查。

CR Agent 从零独立构建,**不依赖 DeerFlow 或任何 Agent 框架**,使用 LangGraph + Python 原生实现完整的 Agent 编排、中间件链、安全防护与生产级工程能力(幂等、熔断、重试、链路追踪、契约)。目标是在学习/展示一个"生产级 AI Agent 形态"的同时,产出可实际工作的 GitHub PR 代码审查服务。

### 1.2 目标

构建一个自动化 Code Review Agent,在 GitHub PR 事件触发后:

1. 自动获取 PR 变更内容(diff、文件列表、元数据);
2. 对变更代码执行**确定性检查**(正则安全/质量规则 + 沙箱 lint/类型检查);
3. 对变更代码执行 **LLM 语义审查**(逻辑、安全、性能、并发、可维护性、API 兼容性);
4. 生成**结构化审查报告**(verdict + findings + metrics),verdict 由代码按 findings 严重度确定,**不可被 LLM 改写**;
5. 将审查结果作为 PR 评论回写 GitHub,并沉淀为仓库级"审查记忆"以指导后续审查。

### 1.3 价值

| 维度 | 价值 |
|------|------|
| 研发效率 | PR 提交后约 40s 产出审查报告,降低人工 CR 等待成本 |
| 质量基线 | 确定性规则秒出、覆盖 OWASP 常见风险点,补人工盲区 |
| 工程示范 | 完整展示独立 Agent 的生产级能力:编排、中间件、安全、可观测、幂等、契约 |
| 成本可控 | 双层架构(免费确定性 + 按 token 计费 LLM),默认可走纯确定性路线 |

### 1.4 非目标

- 不自动修复代码(仅审查和建议);
- 不替代人工审查;
- 不做 CI/CD 流水线集成(不做 pass/fail gate);
- 不内置 Web 构建工具链(Web UI 为单 HTML + 内嵌 CSS/JS)。

---

## 2. 项目文档「必需」

| 类型 | 链接/说明 |
|------|-----------|
| 需求文档 | [docs/plans/cr-agent-requirements-v2.md](./cr-agent-requirements-v2.md)(功能/非功能/验收标准,56→156 测试已超出) |
| 设计文档 | [docs/plans/cr-agent-design-v2.md](./cr-agent-design-v2.md)(架构/状态机/中间件/安全/幂等/记忆设计,部分内容与代码 drift) |
| 后端系分 | 本文档 |
| 前端系分 | 无独立前端系分;Web UI 为单页 HTML(`cr_agent/web/index.html`),中英文 + 深浅色切换,按需 |
| 设计稿 | 无 |
| 上游系统 | GitHub(Webhook 推送 PR 事件)、「待补充」 |
| 下游系统 | GitHub(回写 PR 评论)、Ant 内部 OpenAI 兼容 API(`antchat.alipay.com/v1`)、可选 Redis(幂等后端)、「待补充」 |

> **待补充**:真实部署环境下的上游/下游系统全名、网络可达性、SLA 等于上线前由运维补充。

---

## 3. 需求功能点「必需」

> 优先级口径:P0 = 阻断核心流程/必须具备;P1 = 重要、影响质量或可靠性;P2 = 增强、可后置。状态以代码为准。

### 功能点 1:多入口触发审查
- 描述:支持 GitHub Webhook、Web API、CLI 三种入口触发审查,共享同一套 LangGraph 编排。
- 优先级:**P0**
- 状态:已实现;但 **Webhook 入口存在致命 bug(见 6.2-R1)**,需修复后方可投产。

### 功能点 2:确定性检查(规则引擎)
- 描述:对 diff 新增行执行正则规则扫描,产出高置信度 findings;当前共 28 条规则,覆盖安全(blocker/major)、调试、异常处理、可维护性、代码质量、JS/TS、类型安全。
- 优先级:**P0**
- 状态:已实现;规则数已从设计文档的 8 条扩展到 28 条。

### 功能点 3:沙箱静态分析
- 描述:在沙箱中执行 lint/类型检查(ruff/eslint/mypy/tsc 等),结果以 `StaticAnalysisResult` 收集;沙箱失败/超时优雅降级为 `skipped`,不阻塞 LLM 审查。
- 优先级:**P0**
- 状态:已实现;沙箱为命令白名单 + 环境脱敏 + cwd 校验(**非 OS 级隔离**,见 6.4-R8)。

### 功能点 4:LLM 语义审查
- 描述:基于 system prompt 的人格与安全规则,调用 LLM 对 diff 做六维语义审查,产出带 severity/file/line/message/suggestion/confidence 的 findings;LLM 通过 `generate_report` 工具提交结果,但 **verdict 由代码按严重度重算,忽略 LLM 传入的 verdict**。
- 优先级:**P0**
- 状态:已实现;模型为 OpenAI 兼容 API(默认 DeepSeek-V4-Flash)。

### 功能点 5:结构化报告与回写
- 描述:合并确定性 + LLM findings,按严重度排序,生成 markdown 报告回写 PR 评论;支持幂等更新已有"Code Review Report"评论。
- 优先级:**P0**
- 状态:已实现;多次审查更新已有评论为「部分」(依赖 gh CLI 评论查找,见 6.5-R13)。

### 功能点 6:中间件链(6 层)
- 描述:可插拔中间件链,覆盖输入中和、上下文压缩、循环检测、token 预算、工具失败降级、输出截断四种钩子。
- 优先级:**P0**
- 状态:已实现。

### 功能点 7:幂等与并发控制
- 描述:delivery ID 去重(内存)、(repo, pr) 级幂等存储(IDLE/IN_PROGRESS/DONE + TTL 300s)、全局并发上限 3;支持内存与 Redis 双后端。
- 优先级:**P0**
- 状态:已实现;多实例下内存后端失效,需 Redis(见 6.3-R5)。

### 功能点 8:可观测性(链路/熔断/重试/指标)
- 描述:trace_id 链路追踪 + structlog 结构化日志、熔断器(5 次失败开/60s 恢复)、指数退避重试(3 次 + jitter)、单次审查指标(token/耗时/成本估算)。
- 优先级:**P1**
- 状态:已实现;但无 Prometheus/标准导出(见 7.2)。

### 功能点 9:安全防护(三层纵深)
- 描述:输入中和(prompt injection 标签)、输出脱敏(secret mask)、环境隔离(白名单 + 脱敏)、subprocess 安全(命令白名单 + shell=False + 超时降级)。
- 优先级:**P0**
- 状态:已实现;隔离强度与 `extra_env` 注入存在安全缝隙(见 6.4-R8/R9)。

### 功能点 10:记忆系统
- 描述:按仓库沉淀历史审查 findings/verdict,统计 Top 5 高频 pattern 注入 LLM prompt 引导重点检查。
- 优先级:**P2**
- 状态:已实现,采用 **SQLite(主)+ JSON(fallback)** 双写(非设计文档的纯 JSON)。

### 功能点 11:契约导出
- 描述:基于 Pydantic v2 自动导出 4 个 JSON Schema(finding/review_report/diff_metrics/static_analysis),供上下游对齐。
- 优先级:**P1**
- 状态:已实现。

### 功能点 12:Web UI 与中英双语
- 描述:单页 Web UI,展示规则列表 + 触发审查 + 指标面板,中英文 + 深浅色切换。
- 优先级:**P2**
- 状态:已实现;**Web API 无鉴权与限流(见 6.5-R12)**。

---

## 4. 系统设计(架构和建模)

### 4.1 系统整体架构

```
   GitHub PR Event                Web API / CLI
        │                              │
        ▼                              ▼
 ┌──────────────────────────┐   ┌─────────────────────┐
 │ FastAPI Webhook (8088)   │   │ FastAPI Web / CLI   │
 │ HMAC → delivery 去重     │   │ (无鉴权/同步)         │
 │ → IdempotencyStore       │   │                     │
 │ → trace_id → bg task     │   └──────────┬──────────┘
 └─────────────┬────────────┘              │
               │                           │
               └───────────┬───────────────┘
                           ▼
          ┌────────────────────────────────────────┐
          │  cr_agent.agent.graph.build_graph()     │  ← 共享编排核心
          │  LangGraph: prepare → llm ↔ tools       │
          │              → finalize → END           │
          │  外裹 6 层 MiddlewareChain(4 钩子)       │
          └─────┬───────────┬──────────────┬───────┘
                │           │              │
          ┌─────▼─────┐ ┌───▼────┐  ┌──────▼──────┐
          │  Tools    │ │ Memory │  │  finalize   │
          │ run_lint  │ │SQLite+ │  │ 合并findings │
          │ read_file │ │ JSON   │  │ 重算verdict  │
          │ gen_report│ │        │  │ ReviewReport │
          └─────┬─────┘ └────────┘  └─────────────┘
                │
          ┌─────▼──────────────────────┐
          │ sandbox: subprocess        │
          │ 命令白名单 + env 脱敏       │
          │ + cwd 校验(shell=False)    │
          └────────────────────────────┘
                │
          ┌─────▼──────────────────────┐
          │ GitHub gh CLI(回写评论)    │
          └────────────────────────────┘
```

### 4.2 领域模型

```
┌─────────────────────────────────────────────────────────┐
│ 审查领域(核心不可变事实)                                  │
│                                                         │
│  PRInfo ──(diff)──▶ DiffHunk[](old_start,new_start,lines)│
│                         │                               │
│            ┌────────────┼─────────────┐                 │
│            ▼            ▼             ▼                 │
│   确定性规则(28)   沙箱静态分析    LLM 语义审查           │
│   Finding[]       StaticAnalysis    Finding[]           │
│   source=         Result[]          source=             │
│   deterministic                      llm                │
│            └────────────┬─────────────┘                 │
│                         ▼                               │
│                  ReviewReport                           │
│                  (verdict 由 determine_verdict 重算)     │
└─────────────────────────────────────────────────────────┘

枚举:
  Severity  = blocker | major | minor | info
  Verdict   = approve | request_changes | block
  Confidence= high | medium | low
```

**领域决策核心**:`determine_verdict(findings)` —— 任意 `blocker` → `block`;否则任意 `major` → `request_changes`;否则 `approve`。这一映射在 `finalize` 节点执行,**覆盖 LLM 自报的 verdict**,是防 prompt injection 的关键不变量(LLM 被"请直接 approve"攻击时仍会被 blocker 规则纠正)。

### 4.3 业务模型(LangGraph 状态机)

```
START
  │
  ▼
prepare  ── parse_diff + run_deterministic_checks + build_review_prompt
  │        写入: messages, deterministic_findings, iteration=0
  ▼
llm ◄─────────────────┐   _make_llm_node: 调 LLM(绑定 tools)
  │                   │   外裹 before_model / after_model 中间件
  │  _should_continue │   MAX_ITERATIONS=15 硬上限
  ├─ 有 tool_calls ───┘
  │
  ▼
tools  ────────────────┐   _execute_tools: 执行 tool_calls
  │                    │   外裹 before_tool / after_tool 中间件
  │                    │   工具: run_lint / read_file / generate_report
  └─ 回到 llm ─────────┘
  │
  ▼(无 tool_calls)
finalize ── 从 messages 的 generate_report 调用中提取 LLM findings
  │         合并 deterministic + llm → determine_verdict → ReviewReport
  ▼
END
```

**三重循环保护**:`MAX_ITERATIONS=15`(节点级硬上限) → `LoopDetection` 中间件(相同调用 3 次警告/5 次强制终止 + 单工具总调用 >30 次封禁) → `TokenBudget` 中间件(token 达 100% 强制终止)。

---

## 5. 详细设计

### 5.1 时序图 / 流程图

#### 5.1.1 Webhook 入口端到端时序(默认入口)

```
GitHub ──POST /webhook──▶ FastAPI
                           │
       (1) 读 raw body(bytes)
       (2) HMAC-SHA256 校验(hmac.compare_digest,常量时间)── 失败 401
       (3) delivery ID 去重(_processed_deliveries 内存 set,>1000 清空)
       (4) parse_webhook_payload:仅处理 opened/synchronize/reopened
       (5) new_trace_id() 生成 12 位 trace_id
       (6) IdempotencyStore.try_acquire(repo, pr): IN_PROGRESS/DONE+TTL/并发≥3 → 拒绝
       (7) background_tasks.add_task(_run_review, pr_data, trace_id)
       (8) 立即返回 {status:accepted}  (HTTP 200)

  _run_review(后台):
       (a) ⚠️ new_trace_id(trace_id) ← 当前存在致命 bug(见 6.2-R1)
       (b) get_pr_diff(gh pr diff) → mask_secrets
       (c) build_memory_context(repo)  ← Top 5 高频 pattern
       (d) build_graph(CR_MODEL).invoke({diff, pr_info, memory_context})
       (e) ReviewReport(**result["report"]) → to_markdown()
       (f) post_pr_comment(幂等:查找"Code Review Report"评论 → PATCH 或新建)
       (g) save_review_memory(repo, pr, findings, verdict)
       (h) idempotency.release(repo, pr, success=True)   ← ⚠️ bug 下不会执行
       异常 → logger.error("review.failed") + release(success=False)
```

#### 5.1.2 中间件链四钩子时序

```
before_model : InputSanitization(中和标签) → ContextCompression(消息>20 压缩,保留 8 条)
                 ↓ (LLM 调用)
after_model  : InputSanitization(mask_secrets) → LoopDetection(去重+频率) → TokenBudget(预算)
                 ↓ (检查 tool_calls)
before_tool  : LoopDetection(blocked_tools 拦截,唯一可短路的钩子)
                 ↓ (工具执行)
after_tool   : ToolErrorHandling(仅含"Traceback"触发) → ToolOutputBudget(截断 20K)
                 ↓
              ToolMessage
```

#### 5.1.3 幂等状态机(IdempotencyStore)

```
            try_acquire(空闲/不存在)
   IDLE/缺失 ───────────────────────▶ IN_PROGRESS  (active_count +1)
                                          │
                                   release(success)
                                          ▼
                                        DONE  (active_count -1)
                                          │
                            now - completed_at < 300s ──▶ try_acquire 拒绝
                            now - completed_at ≥ 300s ──▶ 允许(覆盖为 IN_PROGRESS)
并发保护:_active_count ≥ 3(max_concurrent) ──▶ 拒绝
后端:无 REDIS_URL → 内存(threading.Lock);有 REDIS_URL → Redis(SET NX EX,fail-open)
```

### 5.2 数据模型

#### 5.2.1 领域对象(`core/models.py`,Pydantic v2)

**Finding**(单条发现)

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| rule_id | str | 是 | — | 规则/来源标识,如 `security.hardcoded-secret` / `llm.unknown` |
| severity | Severity | 是 | — | blocker/major/minor/info |
| file | str\|None | 否 | None | 文件路径 |
| line | int\|None | 否 | None | 行号 |
| message | str | 是 | — | 问题描述 |
| suggestion | str | 是 | — | 修复建议 |
| confidence | Confidence | 否 | medium | high/medium/low |
| source | Literal["deterministic","llm"] | 否 | "deterministic" | 来源 |

**ReviewReport**(报告,会作为 PR 评论发布)

| 字段 | 类型 | 必填 | 默认 |
|------|------|------|------|
| schema_version | str | 否 | "cr-agent.report.v1" |
| verdict | Verdict | 是 | — |
| summary | str | 是 | — |
| findings | list[Finding] | 否 | [] |
| static_analysis | list[StaticAnalysisResult] | 否 | [] |
| metrics | DiffMetrics | 否 | DiffMetrics() |

**DiffMetrics**:`files_changed`/`lines_added`/`lines_removed`(均 int,默认 0)
**StaticAnalysisResult**:`tool`(str,必填)、`status`(pass/fail/skipped,默认 skipped)、`issues`(int)、`output`(str\|None)

#### 5.2.2 Agent 状态(`agent/state.py`)

| 字段 | 类型 | Reducer | 写入方 |
|------|------|---------|--------|
| messages | list[BaseMessage] | operator.add | prepare/llm/tools |
| diff | str | 覆盖 | 入口 |
| pr_info | dict | 覆盖 | 入口 |
| deterministic_findings | list[Finding] | operator.add | prepare |
| llm_findings | list[Finding] | operator.add | **无节点写入**(见 6.2-R2) |
| static_analysis | list[StaticAnalysisResult] | operator.add | **无节点写入** |
| file_contents | dict[str,str] | 覆盖 | **无节点写入** |
| report | dict\|None | 覆盖 | finalize |
| iteration | int | 覆盖 | llm |
| memory_context | str | 覆盖 | 入口 |

#### 5.2.3 记忆存储(`agent/memory.py`,SQLite)

```sql
CREATE TABLE reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  findings_count INTEGER DEFAULT 0,
  finding_types TEXT,   -- JSON 数组(rule_id 去重)
  severities TEXT,      -- JSON 数组
  reviewed_at TEXT NOT NULL,
  trace_id TEXT
);
-- 索引:idx_reviews_repo(repo), idx_reviews_repo_pr(repo, pr_number)
```
并发:`_db_lock = threading.Lock()`(全局锁,见 6.2-R4);fallback:`{repo}/{pr}.json` 文件。

#### 5.2.4 确定性规则(28 条,`core/rules_engine.py`)

| 分组 | 数量 | 严重度 | 代表性 rule_id |
|------|------|--------|----------------|
| 安全(注入/执行) | 8 | BLOCKER | hardcoded-secret、sql-injection、command-injection、eval-usage、exec-usage、pickledeserialize、yaml-unsafe-load、shell-true |
| 安全(弱实现) | 5 | MAJOR | weak-hash、verify-false、insecure-random、assert-sensitive、tempfile-race |
| 调试残留 | 2 | MAJOR/MINOR | breakpoint、print-statement |
| 异常处理 | 3 | MAJOR/MINOR | bare-except、pass-in-except、broad-except |
| 可维护性 | 3 | MAJOR/MINOR/INFO | todo-comment、mutable-default、global-statement |
| 代码质量 | 3 | MINOR/INFO | long-line、import-star、unused-import-common |
| JS/TS 专用 | 3 | MAJOR/MINOR | js-innerhtml、js-document-write、js-var-declaration |
| 类型安全 | 1 | INFO | any-annotation |

扫描入口 `run_deterministic_checks(hunks)`:逐 hunk 逐新增行,`re.IGNORECASE`,只查新增行(跳过上下文/删除行),命中→`Finding(confidence=HIGH, source="deterministic")`,结果按 `{blocker:0,major:1,minor:2,info:3}` + file + line 排序。

> 已知问题:`error-handling.pass-in-except` 依赖 `\n` 匹配,但逐行扫描无换行符 → **死规则**(见 6.2-R3)。

### 5.3 配置变更

| 配置项 | 位置 | 默认 | 说明 |
|--------|------|------|------|
| OPENAI_API_KEY / XITA_API_KEY | 环境变量 | — | LLM 鉴权 key |
| OPENAI_BASE_URL | 环境变量 | — | OpenAI 兼容 endpoint |
| CR_MODEL | 环境变量 | DeepSeek-V4-Flash | 审查模型 |
| GITHUB_WEBHOOK_SECRET | 环境变量 | — | HMAC 密钥 |
| GH_TOKEN | 环境变量 | — | GitHub Token(经 `extra_env` 注入子进程) |
| CR_WEB_API_KEY | 环境变量 | — | Web UI API Key 认证(未设置则跳过认证) |
| CR_MEMORY_DIR | 环境变量 | .cr_agent_memory | 记忆目录 |
| CR_MEMORY_DB | 环境变量 | {CR_MEMORY_DIR}/memory.db | SQLite 路径 |
| REDIS_URL | 环境变量 | — | 有则用 Redis 幂等后端;无则内存 |

- **DRM 配置**:无(本项目无动态配置中心接入);「待补充」生产是否接入。
- **DB 变更**:首次运行自动 `CREATE TABLE IF NOT EXISTS`(SQLite 本地文件,无迁移脚本);生产若迁 Postgres 需补迁移「待补充」。

### 5.4 接口相关

#### 5.4.1 对外 HTTP 接口

**① Webhook(`/webhook`,POST,`github/webhook_server.py`)**

| 项 | 内容 |
|----|------|
| 请求头 | `X-Hub-Signature-256`、`X-GitHub-Event`、`X-GitHub-Delivery` |
| 请求体 | GitHub webhook 原始 JSON(bytes) |
| 响应 | `200 {status:accepted, pr, repo, trace}` / `401 Invalid signature` / `{status:duplicate}` / `{status:ignored}` / `{status:rejected, reason}` |
| `GET /health` | `{status:ok}` |

**② Web API(`web/server.py`)**

| 路径 | 方法 | 请求 | 响应 |
|------|------|------|------|
| `/` | GET | — | index.html |
| `/api/health` | GET | — | `{status, version}` |
| `/api/rules` | GET | — | `[{rule_id,severity,message}, ...]` |
| `/api/review` | POST | `ReviewRequest{diff:str, pr_title:"Untitled PR", pr_author:"unknown", use_llm:false, model:"DeepSeek-V4-Flash"}` | `{verdict, summary, findings, metrics, markdown, elapsed_seconds, review_metrics}`;空 diff → `400 {error}` |

**③ CLI(`cli.py`)**:`--repo --pr` / `--diff-file` / `--diff-stdin`,`--no-llm` / `--post-comment` / `--model` / `--verbose`;输出 `report.to_markdown()` 至终端。

#### 5.4.2 内部编排/能力接口(供三入口复用)

| 接口 | 签名 | 说明 |
|------|------|------|
| `build_graph` | `(model_name="DeepSeek-V4-Flash", temperature=0.1) -> CompiledGraph` | 构造 LangGraph,内部建 LLM + 默认中间件链 |
| `graph.invoke` | `({"diff","pr_info","memory_context"}) -> {"report": dict}` | 同步执行编排 |
| `run_deterministic_checks` | `(hunks: list[DiffHunk]) -> list[Finding]` | 纯规则审查(Web API/CLI 的 `--no-llm` 走此路径) |
| `determine_verdict` | `(findings) -> Verdict` | blocker→block / major→request_changes / else→approve |
| 工具 | `run_lint(command, cwd=".")`、`read_file(path, max_lines=200)`、`generate_report(verdict, summary, findings, ...)` | 被 LLM 调用 |
| `IdempotencyStore` | `try_acquire(repo, pr, trace_id="")->bool`、`release(repo, pr, success=True)`、`get_status(...)` | 幂等存储 |
| `build_memory_context` | `(repo: str) -> str` | 注入 prompt 的历史审查上下文 |
| `export_schemas` | `() -> None` | 导出 4 个 JSON Schema 到 `contracts/` |

#### 5.4.3 契约(JSON Schema,`contracts/`)

| 文件 | 来源模型 | required |
|------|----------|----------|
| finding.v1.schema.json | Finding | [rule_id, severity, message, suggestion] |
| review_report.v1.schema.json | ReviewReport | [verdict, summary] |
| diff_metrics.v1.schema.json | DiffMetrics | [] |
| static_analysis.v1.schema.json | StaticAnalysisResult | [tool] |

> 已知问题:schema 无 `$id`/`$schema`,缺权威版本标识;`ReviewReport` 与 `Finding` schema 各自内联子类型,存在重复定义(见 6.2-R14)。

---

## 6. 风险分析

### 6.1 业务风险

| 风险 | 说明 | 影响 |
|------|------|------|
| 审查结论误导 | LLM 可能漏报或误报;确定性规则存在误报(如 `verify-false` 匹配 `is_verify=False`)、漏报(SQL 拼接仅查 f-string 不查 .format/%) | 开发者据报告做决策,误报扰民/漏报放行风险 |
| 不可被当作 gating | 项目明确不做 CI gate;若误用为 pass/fail 门槛,LLM 波动会卡合并 | 流程风险需文档与产品约束 |
| 评论噪音 | 多次审查、大量 findings 刷屏 PR | 开发者体验下降,需控制评论粒度 |

### 6.2 技术风险(致命数量级)

| 编号 | 风险 | 类别 | 严重度 |
|------|------|------|--------|
| **R1** | **`webhook_server.py:96` `new_trace_id(trace_id)` 调用零参函数 `new_trace_id()`,必抛 `TypeError`;该调用在 `try:` 之前 → 异常不被捕获、`idempotency.release()` 不执行、锁永久 IN_PROGRESS、后台任务静默失败。所有走 Webhook 入口的审查 100% 失败,PR 永不出现评论** | 正确性 | **致命** |
| R2 | `AgentState.llm_findings / static_analysis / file_contents` 三个字段带 reducer 但无节点写入;`finalize` 改从 messages 的 `generate_report` 调用中挖 findings,与状态设计意图不符 | 正确性/可维护 | 高 |
| R3 | 确定性规则 `error-handling.pass-in-except` 依赖 `\n`,但 `run_deterministic_checks` 逐行扫描无换行符 → 死规则 | 正确性 | 中 |
| R4 | 内存记忆 `_db_lock` 为全局 `threading.Lock`,所有写串行,高并发 webhook 下为瓶颈;且 SQLite 连接无池化 | 性能 | 中 |

### 6.3 技术风险(幂等/可靠性)

| 编号 | 风险 | 类别 | 严重度 |
|------|------|------|--------|
| R5 | `IdempotencyStore` 用 `threading.Lock`,多 worker/多实例下幂等与并发控制**完全失效**;须强制 Redis 后端,但 Redis 版 `GET→SET NX→INCR` 三步非原子,高并发可超 `max_concurrent`,且 Redis 故障 fail-open(保护失效) | 可靠性 | 高 |
| R6 | ~~delivery 去重 `_processed_deliveries` 为纯内存 set:进程重启丢失、多实例不共享、超 1000 全量清空 → 重放攻击(仅 TTL 300s 部分缓解)~~ **已优化:改用 `OrderedDict` LRU 淘汰 + `threading.Lock` 保护并发安全;多实例/重启丢失仍需 Redis 后端** | 安全/可靠 | ~~高~~ 中(并发安全已修复) |
| R7 | ~~后台任务 `_run_review` `except Exception` 吞没所有异常,无重试/告警/DLQ;`get_pr_diff`/`get_pr_info`/`post_pr_comment` 均**不检查 gh CLI returncode**,`get_pr_info` 失败会抛 `JSONDecodeError`~~ **已修复:新增 `except BaseException` 兜底,确保幂等性锁总被释放,然后 `raise` 传播** | 可靠性 | ~~高~~ 已修复 |

### 6.4 技术风险(安全)

| 编号 | 风险 | 类别 | 严重度 |
|------|------|------|--------|
| R8 | **沙箱非 OS 级隔离**:subprocess + 命令白名单 + cwd 校验,无 namespace/cgroups/seccomp/chroot,与 agent 同用户/同 FS/同网络;白名单含 `cat`/`python3`/`node`/`npm`/`npx`/`pnpm`/`go`/`cargo` → 可读 `~/.ssh/id_rsa`、经 `npm postinstall`/`python3 setup.py` 实现 RCE;`SandboxConfig.allowed_cwd` 默认 None 无边界 | 安全 | 高 |
| R9 | `extra_env` 经 `build_safe_env(extra)` 完全绕过环境脱敏:为支持 `gh` 注入真实 `GH_TOKEN`,恶意脚本可经 `process.env` 读取并经 stdout 回流 LLM | 安全 | 高 |
| R10 | `mask_secrets` 正则覆盖窄:漏 `glpat-`/`xoxb-`/`sk_live_`/JWT(`eyJ...`)/PEM 私钥/`.env` 内容;`sk-` 不含连字符 → Anthropic `sk-ant-...` 脱敏不全;`ghp_` 要求恰 36 位可能漏匹配 | 安全 | 中 |
| R11 | `run_command` 输出`{(stdout+stderr)[:2000]}` 未自动 mask,依赖调用方补;若遗忘则 secret 原样进 LLM 上下文 | 安全 | 中 |

### 6.5 技术风险(入口/可观测/工程)

| 编号 | 风险 | 类别 | 严重度 |
|------|------|------|--------|
| R12 | **Web API `POST /api/review` 零鉴权、无限流**:任何人可发超大 diff 烧 LLM 配额、注入 prompt、DoS | 安全/成本 | 高 |
| R13 | `post_pr_comment` 幂等依赖 gh CLI 评论查找;失败时静默跳过查找直接新建 → 可能产生重复评论 | 正确性 | 中 |
| R14 | 中间件链 `run_*` 方法**无 try-except**,任一中间件钩子抛异常 → 整条审查失败(仅工具层有 `ToolErrorHandler` 兜底);`ToolErrorHandlingMiddleware` 只在结果含 `"Traceback"` 时触发,与 `ToolErrorHandler` 生成的 `"Error executing ..."` 格式衔接不上 → 两套错误机制有缝 | 健壮性 | 中 |
| R15 | 熔断器非线程安全(`_failures`/`_state` 无锁),half_open 无并发控制、可能放行多探测;`retry_with_backoff` 用 `time.sleep` 阻塞、不适配 async;trace `with_trace_id` 未 reset 自定义 `_trace_id` ContextVar(轻微泄漏) | 健壮性 | 中 |
| R16 | 日志用 `ConsoleRenderer`(非 JSON),无 Prometheus/Sentry/OTel 导出,无全局聚合(P50/P99/错误率/QPS);无标准监控接入 | 可观测 | 中 |
| R17 | LLM 自报 verdict 与代码重算的最终 verdict 可能不一致,但报告未标注 `degraded`/不一致,用户无感;Web API LLM 失败静默降级确定性,响应亦无降级标志 | 信息透明 | 中 |

### 6.6 文档/代码漂移风险

| 编号 | 风险 | 严重度 |
|------|------|--------|
| D1 | **设计文档 `docs/plans/cr-agent-design-v2.md:477-478` 明文写有 `OPENAI_API_KEY=...` 真实 key**(已随仓库提交),属密钥泄露,须立即吊销并清理 git 历史 | **高/安全** |
| D2 | 设计文档:测试 56 条 → 实际 156 条;规则 8 条 → 实际 28 条;记忆纯 JSON → 实际 SQLite+JSON。文档对外不可作权威,易误导接手者 | 中 |
| D3 | 设计文档「三层去重」描述 delivery ID 去重位于幂等层,实际 delivery 去重在 `webhook_server.py` 内存 set,幂等层只做 (repo,pr)+TTL+并发 | 中 |

---

## 7. 三板斧设计「必填」

> 现状评估:本项目已实现部分三板斧能力(熔断、重试、幂等、trace 链路、降级),但**监控「发现能力」与灰度「发布能力」偏弱**,需在本系分对应工作内补齐,标记为「待补/待补」。

### 7.1 灰度能力设计

| 灰度批次 | 执行人 | 时间 | 策略 |
|----------|--------|------|------|
| 本地/CLI 自测 | 后端 | T+0 | 先用 `--diff-file`/`--no-llm` 在本地验证 28 条规则与报告渲染 |
| Web API 灰度 | 后端 | T+1 | 开放 Web API 仅内网可达,小流量触发 `use_llm=true` 观察 |
| Webhook 内灰 | 后端 | T+2 | 绑定 1 个测试仓库 webhook,验证端到端回写评论(须先修复 R1) |
| Webhook 全量 | 后端+运维 | T+3 | 绑定目标仓库,观察 1 个迭代周期后转默认开启 |

> 灰度开关:当前**无**按仓库/百分比的灰度开关,需新增配置(如 `CR_ENABLED_REPOS` 白名单或开关中心)。「待补充」灰度开关实现方式与责任人。

### 7.2 发现能力设计(监控)

#### 监控设计

| 用例 | 监控项 | 监控改动 | 监控地址 |
|------|--------|----------|----------|
| 审查成功率 | `review.complete` vs `review.failed` 比率 | **待补**:接 SLS/云监控/Pyroscope,新增成功率大盘 | 「待补充」 |
| 审查耗时 | prepare/llm/tools/finalize 各阶段 `elapsed_ms` | 已采集(ReviewMetrics),**待补**导出至监控 | 「待补充」 |
| LLM 成本 | token 用量 + `estimated_cost_usd` | 已采集,模型单价硬编码,**待补**导出与告警 | 「待补充」 |
| 幂等拒绝率 | `try_acquire` 拒绝次数/原因 | **待补**:`get_status` 已有,需打点 | 「待补充」 |
| 熔断器状态 | open/half_open 切换次数 | **待补**:无 `is_open()` 暴露(见 R15) | 「待补充」 |
| PR 评论成功率 | `post_pr_comment` 成功/失败 | **待补**:`returncode` 未检查(R7),需先补错误检测 | 「待补充」 |

#### 风险点(时序风险对照)

| 时序 action | 风险点 | 类别 | 发现能力 | 责任人 |
|-------------|--------|------|----------|--------|
| webhook `_run_review` | R1 致命 bug 致审查静默失败 | 资金/研发安全 | **当前无**(失败仅 logger.error) | 后端 |
| LLM 调用 | LLM 限流/超时 | 可靠性 | 熔断器+重试(已实现),但无告警 | 后端 |
| 沙箱执行 | R8 非隔离 RCE | 安全 | 无;依赖 LLM 不主动作恶+白名单 | 后端+安全 |
| 幂等释放 | R1 下 release 不执行锁泄漏 | 可靠性 | 无;需监控 IN_PROCESS 滞留 | 后端 |
| 评论回写 | gh CLI 失败 | 可靠性 | **无**(returncode 未检查) | 后端 |

### 7.3 应急能力设计

| 触发事件 | 应急方式 | 业务影响 |
|----------|----------|----------|
| 致命 bug R1 黑屏 | 修复 `new_trace_id` 调用(改为 `set_trace_id` 或 `new_trace_id()` 不传参后单独 set);上线前必须热修 | webhook 审查暂停,期间 PR 无自动评论 |
| LLM 持续 5xx | 熔断器自动 open,60s 后半开探测;Web API 自动降级确定性 | 仅确定性规则,语义审查缺失,报告降级 |
| LLM 配额耗尽 | 默认 `use_llm=false`/CLI `--no-llm` 走纯确定性 | 同上 |
| 幂等 Redis 故障 | fail-open(放行),风险:重复评论/重复审查 | 重复评论噪音、成本上升 |
| 沙箱执行高危 | 沙箱超时/拒绝返回 `(-1, skipped)`,不阻塞 | 该工具结果缺失,LLM 基于已有 diff 审查 |
| 密钥泄露 D1 | 立即吊销设计文档中的明文 key,清理 git 历史,轮换 `.env` | 短期服务不可用至新 key 就绪 |
| PR 评论风暴 | **待补**评论数/频率限制开关 | 关闭自动回写,改为只落库 |

> 应急预案落地度:**降级路径充分**(确定性 fallback、fail-open、skipped),但**告警/熔断可见性/灰度开关不足**,需在本期补齐。

---

## 8. 高可用设计

### 8.1 流量引入
- 入口:Webhook(GitHub 主动推送)与 Web API(被动调用)均经 FastAPI/uvicorn 单端口 8088。
- 异步化:Webhook 收到事件后**立即返回 accepted**,审查走 `BackgroundTasks` 异步执行,不阻塞 GitHub 回调(避免 webhook 超时重试)。
- 「待补充」生产 SLB/网关配置、HTTPS 终止、内网外网隔离。

### 8.2 限流配置
- **架构级并发**:`IdempotencyStore.max_concurrent=3`(全局同时在审上限)。
- **TTL 去重**:同一 (repo, pr) 300s 内只审一次。
- 缺失:**Web API 无 QPS 限流、无鉴权(R12)**;无全局限流中间件。需补「待补充」。
- 熔断器:`threshold=5 / recovery=60s`,防 LLM 持续故障下重复重试。

### 8.3 负载均衡
- 单进程 uvicorn 即可承载(审查为 IO 密集 + LLM 等待)。
- 多实例前置需**强制 Redis 幂等后端**(否则 R5 幂等失效)、`GH_TOKEN` 与 `GITHUB_WEBHOOK_SECRET` 一致、「待补充」是否有状态(Session/记忆 SQLite 共享问题——SQLite 本地文件不可多实例共享,见 R4,需迁共享 DB「待补充」)。

### 8.4 分布式架构
- 当前为**单机有状态**(记忆 SQLite 本地、幂等内存/可选 Redis、delivery 去重内存)。
- 多实例改造点(R5/R6/R4):delivery 去重迁 Redis;幂等已支持 Redis 但需修原子性;记忆迁 Postgres「待补充」;trace 若跨服务需 OTel「待补充」。
- 降级:LLM 失败 → 确定性;沙箱失败 → skipped;Redis 失败 → fail-open(幂等降级,折中)。

---

## 9. 上线预案「必需」

> 本系分对应工作 = 「v0.2 上线前的修复与加固」。发布须严格按顺序,前置未完成不得进入下一批。

| 步骤 | 描述 | 具体信息 | 验证方法 | 完成情况 |
|------|------|----------|----------|----------|
| 0 | 安全应急:吊销泄露 key | 吊销设计文档 D1 明文 `OPENAI_API_KEY`,清理 git 历史,轮换 `.env` | 工单 + 历史 grep 无残留 | ⬜ |
| 1 | 热修致命 bug R1 | 修 `webhook_server.py:96` 的 `new_trace_id(trace_id)` 调用(新增 `set_trace_id(tid)` 或不传参后单独 set) | 单测 mock webhook → 后台任务不抛 TypeError、release 被调用 | ⬜ |
| 2 | 修复幂等释放路径 | 确保 `_run_review` 所有异常路径都 `release`,trace 绑定置于 try 内 | 测试:构造 `get_pr_diff` 失败 → 锁正确回到 DONE | ⬜ |
| 3 | Web API 鉴权+限流 R12 | `/api/review` 加 API key 校验 + 限流(如 slowapi) | 未带 key → 401;超频 → 429 | ⬜ |
| 4 | 沙箱加固 R8/R9 | 收紧白名单(移除 `cat`/`head`/`wc` 默认、`npx`/`pnpm` 需配置显式开启);强制 `allowed_cwd`;`run_command` 返回前自动 `mask_secrets` | 安全用例回归;读 `~/.ssh` 被拒 | ⬜ |
| 5 | 多实例幂等 R5/R6 | 生产强制 `REDIS_URL`;用 Lua 脚本把 `GET→SET NX→INCR` 改原子;release `DECR` 加 `>=0` 保护 | 并发压测不超 `max_concurrent` | ⬜ |
| 6 | gh CLI 错误检测 R7 | `get_pr_diff/info/comment` 检查 `returncode`,失败显式异常并纳入 review.failed 指标 | gh 故障注入 → 评论失败可观测 | ⬜ |
| 7 | 监控接入(7.2) | 接 SLS/云监控,导出成功率/耗时/成本/熔断/拒绝率大盘 | 监控面板可见 | ⬜ |
| 8 | 内灰发布 7.1 | 绑定 1 个测试仓库 webhook 观察 1 迭代 | PR 自动出现评论、无重复、无锁滞留 | ⬜ |
| 9 | 全量发布 | 绑定目标仓库 | 灰度指标达标后转默认 | ⬜ |

**依赖关系**:0→1→2 必须先做(救系统于黑屏);3/4/5 可并行;6→7;8 依赖 1-7 全绿;9 依赖 8 观察。「待补充」每步具体负责人与时间窗口。

---

## 10. 研发计划「必需」

> 本期聚焦"修复 + 加固 + 可观测",目标是让 v0.2 从"能跑"到"能上线"。以下时间节点为建议排期,实际以排期系统为准「待补充」。

| 阶段 | 内容 | 建议时间 |
|------|------|----------|
| 系分评审 | 本文评审 + 安全/运维对齐 D1 应急 | 「待补充」 |
| 热修(R0-R2) | key 轮换、修 R1 致命 bug、修释放路径 | 评审后 +2d 内 |
| 安全加固(R8/R9/R10/R12) | 沙箱收紧、Web API 鉴权限流、mask 补全 | +1w |
| 可靠性(R5/R6/R7) | Redis 原子化、delivery 共享、gh 错误检测 | +1w |
| 可观测(R15/R16) | 日志 JSON 化、监控接入、告警 | +0.5w |
| 联调测试 | 补 webhook 端到端用例、并发压测、安全回归 | +0.5w |
| 测试 | 全量 156+ 测试通过 + 新增回归 | +0.5w |
| 内灰发布 | 测试仓库灰度 1 迭代 | +1w |
| 全量上线 | 目标仓库转默认 | 灰度达标后 |

**测试策略补充**:现有 9 个测试文件 / 156 个测试函数覆盖 core/observability/sandbox/security/github/web_api/rules/memory/idempotency。本次需新增:**webhook 端到端 happy/unhappy 用例**(当前缺,直接关联 R1)、**并发幂等压测**、**沙箱逃逸回归集**、**mask_secrets 新增 pattern 用例**。

---

## 附录 A:与设计文档 v2 的关键 drift 汇总

| 维度 | 设计文档 v2 | 代码实际(本文基准) |
|------|-------------|----------------------|
| 版本 | v2.0 | pyproject `0.2.0` |
| 测试数 | 56 | 156(grep `def test_`) |
| 确定性规则 | 8 条 | 28 条 |
| 记忆系统 | 纯 JSON 文件 | SQLite(主)+ JSON(fallback) |
| 幂等 delivery 去重 | 在幂等层 | 在 `webhook_server` 内存 set(幂等层只做 repo+pr+TTL+并发) |
| 沙箱描述 | subprocess + env 白名单 | + 命令白名单 + cwd 校验(但非 OS 隔离) |
| 明文 key | 设计文档含真实 key D1 | 须立即处理 |

## 附录 B:核心阈值常量速查

| 模块 | 常量 | 值 |
|------|------|-----|
| graph | MAX_ITERATIONS | 15 |
| LoopDetection | warn_threshold / hard_limit / window_size / 频率上限 | 3 / 5 / 15 / 30 |
| TokenBudget | max_tokens / warn 比例 | 200000 / 0.8 |
| ContextCompression | max_messages / keep_recent | 20 / 8 |
| ToolOutputBudget | max_chars | 20000 |
| ToolErrorHandling | max_error_length | 500 |
| Idempotency | ttl_seconds / max_concurrent | 300 / 3 |
| CircuitBreaker | threshold / recovery_timeout | 5 / 60s |
| retry_with_backoff | max_retries / base_delay / max_delay | 3 / 1.0s / 30.0s |
| sandbox | timeout / max_output_chars | 30s / 2000 |
| prompts | diff 截断 | 50000 字符 |
| metrics | 模型 DeepSeek-V4-Flash 单价 | $0.14/$0.28 每百万 token |
