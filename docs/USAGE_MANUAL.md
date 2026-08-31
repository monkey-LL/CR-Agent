# CR Agent 使用手册

## 一、启动

### 1.1 启动 Web UI（推荐）

打开终端，输入两条命令：

```bash
cd CRagent
./start.sh web
```

看到下面的输出就是启动成功了：

```
🌐 启动 Web UI: http://localhost:8088
🤖 模型: DeepSeek-V4-Flash (https://antchat.alipay.com/v1)
INFO:     Uvicorn running on http://127.0.0.1:8088
```

### 1.2 打开浏览器

在浏览器地址栏输入：

```
http://localhost:8088
```

你会看到这个界面：

```
┌─────────────────────────────────────────────────────────────┐
│ [CR] CR Agent  [Code Review]         [中文][🌙]  ● online  │
├──────────────────────────┬──────────────────────────────────┤
│ INPUT — DIFF / PATCH     │ REVIEW REPORT                    │
│ [Load Sample]            │                                  │
│                          │  ┌────────────────────────────┐  │
│ PR 标题: [____________]   │  │  🔍 运行审查后在此查看结果   │  │
│ 作者:   [____________]   │  └────────────────────────────┘  │
│                          │                                  │
│ ┌──────────────────────┐ │                                  │
│ │  粘贴 diff 的文本框   │ │                                  │
│ │                      │ │                                  │
│ └──────────────────────┘ │                                  │
│                          │                                  │
│ [审查] [清空]  □ Use LLM │                                  │
│                          │                                  │
│ [security.sql-injection] │                                  │
│ [security.eval-usage]    │                                  │
│ ...规则标签...           │                                  │
└──────────────────────────┴──────────────────────────────────┘
```

### 1.3 停止服务

在终端按 `Ctrl + C` 即可停止。

### 1.4 端口被占用？

```bash
# 换个端口启动（比如 9090）
python -m uvicorn cr_agent.web.server:app --port 9090
```

然后浏览器访问 `http://localhost:9090`。

---

## 二、Web UI 使用

### 2.1 快速体验（30 秒搞定）

**不需要任何 API Key，免费体验：**

1. 打开 http://localhost:8088
2. 点击左上角蓝色的 **「Load Sample」** —— 会自动填入一段有问题的代码
3. 点击 **「Review」** 按钮
4. 右侧立刻出现审查报告，包含 5 个问题

你会看到类似这样的结果：

```
┌─ REVIEW REPORT ──────────────────────────────────────┐
│                                                       │
│  ⛔ BLOCK                                              │
│                                                       │
│  ┌─ Summary ──────────────────────────────────────┐  │
│  │ Deterministic review found 5 issue(s).         │  │
│  └────────────────────────────────────────────────┘  │
│                                                       │
│  FINDINGS                                             │
│                                                       │
│  ┌ [blocker] src/auth/login.py:32 ────────────────┐  │
│  │ Issue: Potential SQL injection via f-string     │  │
│  │ Fix: Use parameterized queries                  │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  ┌ [blocker] src/auth/login.py:33 ────────────────┐  │
│  │ Issue: Potential hardcoded secret detected      │  │
│  │ Fix: Use environment variables                  │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  ┌ [major] src/auth/login.py:35 ──────────────────┐  │
│  │ Issue: Debugger breakpoint left in code         │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  ┌─ Metrics ──────────────────────────────────────┐   │
│  │  1 Files  +5 Added  -1 Removed  5 Findings     │   │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  0.01s                                         0.01s  │
└───────────────────────────────────────────────────────┘
```

### 2.2 审查你自己的代码

1. 在你的项目目录下生成 diff：
   ```bash
   git diff main...HEAD
   ```
2. 复制输出的 diff 内容
3. 粘贴到 Web UI 左侧的文本框中
4. 填写 PR 标题和作者（可选）
5. 点击 **「Review」**

### 2.3 开启 LLM 语义分析

确定性检查（默认）只抓正则能匹配的问题。如果想发现逻辑错误、边界条件等问题：

1. 勾选 **「Use LLM」** 复选框
2. 点击 **「Review」**
3. 等待 10-40 秒（LLM 会调用 DeepSeek-V4-Flash 模型分析代码）

LLM 会额外发现正则抓不到的问题，比如：
- 输入校验缺失（`req.GET["pw"]` 没检查 key 是否存在）
- 密码不应该出现在 URL 查询参数中
- 异常没有 try/except 包裹

**注意：LLM 模式需要 10-40 秒，确定性模式秒出。**

### 2.4 切换中英文

点击右上角 **「中文」** 按钮，所有界面文字会切换为中文：
- 按钮变成「审查」「清空」「使用 LLM」
- 结果区域的标签变成「发现的问题」「修复建议」等

再点一次 **「EN」** 切回英文。

### 2.5 切换深色/浅色主题

点击右上角的 **🌙**（月亮图标）切换到浅色主题：
- 背景变成白色/浅灰
- 文字变成深色
- 图标变成 **☀**（太阳）

再点一次切回深色主题。

### 2.6 清空重来

点击 **「Clear」** 按钮会清空输入框和结果，可以重新粘贴新的 diff。

---

## 三、结果怎么看

### 3.1 Verdict（审查结论）

报告最上方是结论，有三种：

| 结论 | 颜色 | 含义 | 建议 |
|------|------|------|------|
| **BLOCK** | 红色 | 有 blocker 级问题 | 必须修复后才能合并 |
| **REQUEST_CHANGES** | 黄色 | 有 major 级问题 | 建议修复后合并 |
| **APPROVE** | 绿色 | 只有 minor/info 或无问题 | 可以合并 |

### 3.2 Finding（问题详情）

每个问题卡片包含：

```
┌ [blocker] src/auth/login.py:32           deterministic ─┐
│ Issue: Potential SQL injection via f-string             │
│ Fix: Use parameterized queries: cursor.execute(...)     │
└──────────────────────────────────────────────────────────┘
     ↑ 严重级别    ↑ 文件:行号                ↑ 来源
```

| 字段 | 说明 |
|------|------|
| 严重级别 | blocker（必须修）/ major（应该修）/ minor（建议修）/ info（仅供参考） |
| 文件:行号 | 问题在哪个文件的哪一行，可以直接定位 |
| Issue | 问题描述 |
| Fix | 修复建议 |
| 来源 | deterministic = 正则规则发现的，llm = AI 发现的 |

### 3.3 Metrics（统计指标）

底部显示：
- **Files**：变更了多少个文件
- **+Added**：新增了多少行
- **-Removed**：删除了多少行
- **Findings**：发现了多少个问题

### 3.4 耗时

右上角显示审查耗时，确定性模式通常 < 0.1s，LLM 模式通常 10-40s。

---

## 四、CLI 命令行使用

### 4.1 确定性检查（免费，秒出）

```bash
cd CRagent

# 审查一个 diff 文件
./start.sh cli --diff-file /tmp/my_changes.patch --no-llm

# 通过管道传入
git diff main...HEAD | ./start.sh cli --diff-stdin --no-llm
```

### 4.2 LLM 审查

```bash
./start.sh cli --diff-file /tmp/my_changes.patch
```

### 4.3 审查 GitHub PR

```bash
# 先认证 gh CLI（只需一次）
gh auth login

# 审查 PR #42
./start.sh cli --repo owner/repo --pr 42

# 审查并自动回写到 PR 评论
./start.sh cli --repo owner/repo --pr 42 --post-comment
```

### 4.4 查看帮助

```bash
./start.sh cli --help
```

---

## 五、Webhook 自动化使用

### 5.1 场景

你希望每次有人创建 PR 时，自动审查并回写评论，不需要手动操作。

### 5.2 启动 webhook 服务

```bash
cd CRagent
./start.sh webhook
```

### 5.3 配置 GitHub

在你的 GitHub 仓库：Settings → Webhooks → Add webhook

| 配置项 | 填写内容 |
|--------|---------|
| URL | `http://你的服务器IP:8088/webhook` |
| Content type | `application/json` |
| Secret | 任意字符串（比如 `my-secret-123`） |
| Which events | 选择 "Let me select individual events" → 勾选 **Pull requests** |

同时设置环境变量（在 start.sh 之前设置）：

```bash
export GITHUB_WEBHOOK_SECRET="my-secret-123"
./start.sh webhook
```

### 5.4 工作流程

```
1. 开发者在 GitHub 创建 PR
2. GitHub 自动发 webhook 到你的 CR Agent
3. CR Agent 自动获取 PR diff
4. 自动运行审查（确定性 + LLM）
5. 自动把审查报告写回 PR 评论
6. 开发者在 PR 页面看到审查结果
```

**开发者不需要做任何额外操作，创建 PR 后等 1-2 分钟就能看到审查评论。**

---

## 五点五、生产环境安全配置

### Web UI API Key 认证

默认情况下 Web UI 不需要认证，适合本地开发。如果部署到公网或共享网络，建议启用 API Key 认证：

```bash
export CR_WEB_API_KEY="your-secret-api-key"
./start.sh web
```

启用后：
- `/api/review` 请求需要携带 `X-API-Key: your-secret-api-key` 请求头
- 使用 `hmac.compare_digest` 做常数时间比较，防止时序攻击
- 未设置 `CR_WEB_API_KEY` 时自动跳过认证（并输出警告日志）

### Webhook 去重线程安全

Webhook delivery 去重使用 `threading.Lock` 保护 `OrderedDict` 操作，防止并发请求导致 `move_to_end` + `popitem` 竞态。

### 后台任务异常兜底

`_run_review` 后台任务包含 `except BaseException` 兜底，确保 `KeyboardInterrupt`、`SystemExit` 等异常也会释放幂等性锁，防止 PR 被永久阻塞。

---

## 六、运行测试

```bash
cd CRagent
./start.sh test
```

会运行 316 个测试，覆盖 diff 解析、规则引擎、安全防护、熔断器、重试、幂等性、沙箱、评测等。

---

## 七、常见问题

### Q: 点了 Review 没反应？

检查终端是否有报错。最常见的原因是端口被占用，换个端口重启。

### Q: LLM 模式报错？

LLM 模式已经配好了 DeepSeek-V4-Flash 模型和 API Key。如果报错，检查网络是否能访问 `https://antchat.alipay.com/v1`。LLM 报错时会自动降级为确定性检查，不会完全没结果。

### Q: 确定性模式和 LLM 模式有什么区别？

| | 确定性模式（不勾 LLM） | LLM 模式（勾 LLM） |
|---|---|---|
| 速度 | < 0.1 秒 | 10-40 秒 |
| 成本 | 免费 | 消耗 API 额度 |
| 能发现 | 密钥、SQL注入、eval、断点等 | 以上 + 逻辑错误、边界条件、性能问题 |
| 准确率 | 正则匹配，高精确率 | AI 分析，更全面但可能有误报 |

### Q: 支持哪些语言的代码？

确定性规则是语言无关的（正则匹配），Python、JavaScript、Go 等都能用。LLM 模式支持所有主流语言。

### Q: 支持哪些代码托管平台？

Webhook 模式目前只支持 GitHub。Web UI 和 CLI 模式不依赖任何平台——你手动粘贴 diff 就行。

### Q: Web UI 部署到公网安全吗？

默认不安全——`/api/review` 端点无认证，任何人都可以触发 LLM 审查消耗 API 额度。生产部署请设置 `CR_WEB_API_KEY` 环境变量，启用后请求需携带 `X-API-Key` 头认证。
