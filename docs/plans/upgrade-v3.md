# CR Agent 升级文档 v3 — 实用性提升

> 升级日期: 2026-08-25
> 升级目标: 提升项目实际可用性，消除已知痛点

## U1: GitHub API 替代 gh CLI 依赖

### 问题

`get_pr_diff`、`get_pr_info`、`post_pr_comment` 三个函数全部依赖 `gh` 命令行工具。
在未安装 gh CLI 的环境中，CLI 模式和 webhook 模式都无法工作。

### 方案

改用 `httpx` 直调 GitHub REST API，消除外部命令行依赖：

| 操作 | 旧方式 | 新方式 |
|------|--------|--------|
| 获取 diff | `gh pr diff N --repo R` | `GET /repos/{R}/pulls/{N}` + `Accept: application/vnd.github.v3.diff` |
| 获取 PR 信息 | `gh pr view N --json ...` | `GET /repos/{R}/pulls/{N}` (JSON) |
| 列出评论 | `gh pr view N --json comments` | `GET /repos/{R}/issues/{N}/comments` |
| 创建评论 | `gh pr comment N --body ...` | `POST /repos/{R}/issues/{N}/comments` |
| 更新评论 | `gh api ... --method PATCH` | `PATCH /repos/{R}/issues/comments/{id}` |

### 改动文件

- `cr_agent/github/client.py` — 重写，移除 `subprocess`，改用 `httpx`
- `tests/test_github.py` — mock 从 `subprocess.run` 改为 `httpx.get/post/patch`

### 设计考量

- `httpx` 是项目已有依赖（FastAPI 间接引入），不增加新依赖
- Token 通过 `GH_TOKEN` 环境变量传入，不出现在代码或日志中
- 幂等性逻辑保留：先列出评论查找 "Code Review Report" 标题，存在则更新而非创建
- 密钥脱敏逻辑保留：`get_pr_diff` 返回前仍经过 `mask_secrets`

---

## U2: 确定性规则与 LLM 审查结果去重

### 问题

同一个 bug 会被确定性规则引擎和 LLM 各报一次，例如 SQL 注入：
- 规则引擎报 `[blocker] file.py:8 — Potential SQL injection via f-string`
- LLM 报 `[blocker] file.py:6 — get_user 函数使用 f-string 拼接 SQL 查询`

最终报告里有两条指向同一问题的发现，体验不好。

### 方案

在 `_finalize` 阶段合并前执行 `_deduplicate_findings`，按以下策略匹配：

```
匹配条件（从严到松）：
1. 同 file + 同 line + 同 rule_id → 精确匹配
2. 同 file + 同 line + rule_id 类别前缀相同 → 跨来源匹配
   例: "security.sql-injection" 和 "llm.sql-injection" 视为同类
3. 同 file + 同 line + message 关键词重叠 ≥2 → 兜底匹配

合并策略：保留信息更丰富的那条
- 优先保留 suggestion 更长的
- suggestion 长度相同时，保留 confidence 更高的
```

### 改动文件

- `cr_agent/agent/graph.py` — 新增 `_deduplicate_findings`、`_findings_match`、`_category_from_rule_id`，在 `_finalize` 中调用

### 设计考量

- 行号未知的 finding 不参与去重（保留两条，宁多不漏）
- 去重后日志记录原始数量和去重后数量，便于观察
- 去重只发生在 `_finalize`，不影响中间件的 `forced_finalize` 逻辑

---

## U3: 增量审查（hunk 压缩）

### 问题

大 PR 的 diff 可能几万字符，全部传给 LLM 会导致：
- token 消耗激增
- 可能超出模型上下文窗口
- 审查速度变慢

### 方案

当 diff 超过 `INCREMENTAL_REVIEW_THRESHOLD`（10,000 字符）时，启用 hunk 压缩：

```
原始 diff:
  --- a/file.py
  +++ b/file.py
  @@ -1,50 +1,55 @@
   <50 行上下文>
   +新增行1
   <30 行上下文>
   +新增行2
   <20 行上下文>

压缩后:
  --- file.py (lines around 1) ---
   上下文行  ← 变更行前 2 行
  +新增行1
   上下文行  ← 变更行后 2 行
  ...
  +新增行2
   上下文行  ← 变更行后 2 行
```

关键设计：
- **确定性规则引擎不受影响**：在完整 diff 上运行，不丢精度
- **只压缩传给 LLM 的 diff**：减少 token，但 LLM 仍能看到变更行 + 2 行上下文
- **保留文件路径和行号信息**：LLM 报告的 line 号仍然准确
- 压缩率取决于上下文行占比，纯变更行多的 PR 压缩率低，大文件小改动的 PR 压缩率高

### 改动文件

- `cr_agent/agent/graph.py` — 新增 `_compress_diff_for_llm`，在 `_prepare_review` 中调用

### 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INCREMENTAL_REVIEW_THRESHOLD` | 10,000 | diff 字符数超过此值时启用压缩 |
| `HUNK_CONTEXT_LINES` | 2 | 每个 hunk 变更行前后保留的上下文行数 |

---

## 测试验证

```
162 tests passed in 4.34s
```

所有测试通过，包括：
- `test_github.py` — 6 个 GitHub API 客户端测试（mock httpx）
- `test_core.py` — diff 解析、规则引擎、数据模型
- `test_production.py` — 中间件链、幂等性、环境隔离
- `test_web_api.py` — Web API 端到端测试
