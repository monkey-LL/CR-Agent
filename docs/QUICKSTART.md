# CR Agent 启动教程

## 环境准备

项目依赖 Python 3.11+，需要 langgraph、langchain-openai、structlog、fastapi 等库。

```bash
cd CRagent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

推荐使用 `./start.sh`（会自动设置 PYTHONPATH 并加载 `.env`）。

---

## 体验 1：Web UI（推荐入口）

最直观的体验方式，浏览器打开即用。

### 1.1 启动 Web 服务

```bash
./start.sh web
# 或手动:
python -m uvicorn cr_agent.web.server:app --port 8088
```

### 1.2 打开浏览器

访问 http://localhost:8088

界面功能：
- 左侧：粘贴 diff，填写 PR 标题和作者
- 右侧：审查结果（verdict + findings + metrics）
- 右上角：中英文切换按钮、深色/浅色主题切换按钮
- 点击「Load Sample」一键加载示例 diff
- 勾选「Use LLM」可启用 LLM 语义分析（需要 API key）

### 1.3 快速体验

1. 打开 http://localhost:8088
2. 点左上角「Load Sample」
3. 点「Review」按钮
4. 右侧秒出结果：确定性 findings + verdict

### 1.5 生产环境安全配置（可选）

如果 Web UI 暴露在公网或非可信网络，建议设置 API Key 认证：

```bash
export CR_WEB_API_KEY="your-secret-api-key"
./start.sh web
```

设置后，`/api/review` 端点需要通过 `X-API-Key` 请求头认证。未设置时默认不启用认证（适合本地开发）。

### 1.4 端口被占用？

```bash
# 换成任意空闲端口
python -m uvicorn cr_agent.web.server:app --port 9090
```

---

## 体验 2：CLI 命令行

适合脚本化或管道使用。

### 2.1 确定性检查（免费，秒出）

```bash
# 创建测试 diff
cat > /tmp/test_pr.patch << 'EOF'
--- a/src/auth/login.py
+++ b/src/auth/login.py
@@ -30,7 +30,10 @@ def login(email):
     import os
     password = os.environ.get("DB_PASS")
-    query = "SELECT * FROM users"
+    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
+    api_key = "hardcoded-secret-value-here"
+    print("debug: login called")
+    breakpoint()
     return cursor.fetchone()
EOF

# 运行审查（--no-llm = 不调 LLM，纯正则规则）
python -m cr_agent --diff-file /tmp/test_pr.patch --no-llm --verbose
```

### 2.2 通过管道传入

```bash
git diff main...HEAD | python -m cr_agent --diff-stdin --no-llm
```

### 2.3 完整 LLM 审查（需要 API Key）

```bash
export OPENAI_API_KEY="sk-your-key"
python -m cr_agent --diff-file /tmp/test_pr.patch --model gpt-4o-mini
```

### 2.4 审查真实 GitHub PR

```bash
# 先认证 gh CLI
gh auth login

# 审查 PR（不回写评论）
python -m cr_agent --repo owner/repo --pr 42 --model gpt-4o-mini

# 审查并自动回写 PR 评论
python -m cr_agent --repo owner/repo --pr 42 --model gpt-4o-mini --post-comment
```

---

## 体验 3：Webhook 服务（自动化）

GitHub PR 创建时自动触发审查，结果回写到 PR 评论。

### 3.1 启动 webhook 服务

```bash
export GITHUB_WEBHOOK_SECRET="your-webhook-secret"
export OPENAI_API_KEY="sk-your-key"
./start.sh webhook
# 或手动:
python -m uvicorn cr_agent.github.webhook_server:app --port 8088
```

### 3.2 验证服务

```bash
curl http://localhost:8088/health
# {"status":"ok"}
```

### 3.3 GitHub App 配置

在仓库 Settings → Webhooks：
- URL: `http://your-host:8088/webhook`
- Content type: `application/json`
- Secret: 和 `GITHUB_WEBHOOK_SECRET` 一致
- Events: `Pull requests`

### 3.4 工作流程

```
开发者创建 PR → GitHub webhook → CR Agent 自动审查 → PR 评论
```

---

## 体验 4：运行测试

```bash
# 所有测试（316 个）
./start.sh test
# 或:
python -m pytest tests/ -v

# 核心测试（diff 解析 + 规则引擎 + 安全防护）
python -m pytest tests/test_core.py -v

# 可观测性测试（熔断器 + 重试）
python -m pytest tests/test_observability.py -v
```

---

## 体验 5：运行评测

```bash
# 确定性层评测（无 LLM，零成本，秒级）
python -m eval.run_eval --mode deterministic \
  --datasets eval/datasets/golden eval/datasets/adversarial

# LLM 层评测（需 API key）
python -m eval.run_eval --mode llm \
  --datasets eval/datasets/golden eval/datasets/injection
```

详见 [评测指南](EVAL_GUIDE.md)。

---

## 命令速查

| 场景 | 命令 |
|------|------|
| Web UI | `./start.sh web` |
| 确定性检查（免费） | `python -m cr_agent --diff-file diff.patch --no-llm` |
| 完整 LLM 审查 | `python -m cr_agent --diff-file diff.patch --model gpt-4o-mini` |
| 审查 GitHub PR | `python -m cr_agent --repo owner/repo --pr 42` |
| 审查 + 回写评论 | `python -m cr_agent --repo owner/repo --pr 42 --post-comment` |
| Webhook 服务 | `./start.sh webhook` |
| 运行测试 | `./start.sh test` |
| 确定性层评测 | `python -m eval.run_eval --mode deterministic` |

## 注意事项

1. **Web UI** 是最简单的体验入口，浏览器打开即用
2. **确定性模式**（`--no-llm` 或不勾 LLM）不需要任何 API key，秒出结果
3. **LLM 模式**需要设置 `OPENAI_API_KEY`，支持 OpenAI / DeepSeek / 任何兼容 API
4. **GitHub PR 模式**需要 `gh` CLI 已安装并认证
5. **Webhook 模式**需要设置 `GITHUB_WEBHOOK_SECRET` 和 GitHub App
6. **Web UI 生产部署**建议设置 `CR_WEB_API_KEY` 防止未授权 LLM 调用
7. 端口被占用时，`--port` 换成任意空闲端口
