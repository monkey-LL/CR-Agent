# CR Agent — AI Code Review Agent

基于 LangGraph 的自动化代码审查 Agent，在 GitHub PR 提交后自动触发审查，采用确定性规则 + LLM 双层审查架构，并配套标注数据集评测体系验证审查质量。

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API key

# 2. 启动 Web UI
./start.sh web
# 浏览器打开 http://localhost:8088

# 3. 运行测试
./start.sh test
```

## 项目结构

```
cr_agent/
├── core/               # 核心引擎（diff 解析、30 条确定性规则、数据模型）
├── agent/              # Agent 编排（LangGraph 状态机 + 6 层中间件链 + SQLite 审查记忆）
├── security/           # 安全防护（prompt injection、secret 脱敏、env 隔离）
├── sandbox/            # 沙箱执行（subprocess + env 白名单）
├── github/             # GitHub 集成（webhook、HMAC、gh CLI）
├── observability/      # 可观测性（trace ID、熔断器、重试、幂等）
├── web/                # Web UI（中英文 + 深浅色切换）
├── cli.py              # CLI 入口
└── eval/               # 评测框架（45 例标注数据集 + LLM-as-judge）
```

## 技术栈

- Python 3.11+ / LangGraph / FastAPI / Pydantic v2
- OpenAI 兼容 API（支持 DeepSeek/GPT 等）
- GitHub Webhook + gh CLI

## 文档

- [启动教程](docs/QUICKSTART.md)
- [使用手册](docs/USAGE_MANUAL.md)
- [评测指南](docs/EVAL_GUIDE.md)
- [基础学习手册](docs/STUDY_GUIDE_FOR_INTERVIEW.md)
- [进阶学习手册](docs/ADVANCED_STUDY_GUIDE.md)
- [需求文档 v2](docs/plans/cr-agent-requirements-v2.md)
- [设计文档 v2](docs/plans/cr-agent-design-v2.md)
- [简历参考](docs/RESUME.md)

## 测试

```bash
./start.sh test
# 316 tests passed
```

## 评测

```bash
# 确定性层评测（无 LLM，零成本，秒级）
python -m eval.run_eval --mode deterministic --datasets eval/datasets/golden eval/datasets/adversarial

# LLM 层评测（需 API key）
python -m eval.run_eval --mode llm --datasets eval/datasets/golden eval/datasets/injection
```

数据集共 45 例，分四类：golden（23）/ adversarial（4）/ injection（8）/ benign（10）。
最新实测：确定性层 P/R/F1 = 1.0；LLM 层 Recall 97.6%、Verdict 准确率 91.7%、注入成功率 0%、幻觉率 0%。详见 [评测指南](docs/EVAL_GUIDE.md)。

## License

MIT
