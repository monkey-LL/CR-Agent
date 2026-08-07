# CR Agent — AI Code Review Agent

基于 LangGraph 的自动化代码审查 Agent，在 GitHub PR 提交后自动触发审查，采用确定性规则 + LLM 双层审查架构。

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
├── core/               # 核心引擎（diff 解析、规则引擎、数据模型）
├── agent/              # Agent 编排（LangGraph 状态机 + 6 层中间件链）
├── security/           # 安全防护（prompt injection、secret 脱敏、env 隔离）
├── sandbox/            # 沙箱执行（subprocess + env 白名单）
├── github/             # GitHub 集成（webhook、HMAC、gh CLI）
├── observability/      # 可观测性（trace ID、熔断器、重试、幂等）
├── web/                # Web UI（中英文 + 深浅色切换）
└── cli.py              # CLI 入口
```

## 技术栈

- Python 3.11+ / LangGraph / FastAPI / Pydantic v2
- OpenAI 兼容 API（支持 DeepSeek/GPT 等）
- GitHub Webhook + gh CLI

## 文档

- [启动教程](docs/QUICKSTART.md)
- [使用手册](docs/USAGE_MANUAL.md)
- [基础学习手册](docs/STUDY_GUIDE_FOR_INTERVIEW.md)
- [进阶学习手册](docs/ADVANCED_STUDY_GUIDE.md)
- [需求文档 v2](docs/plans/cr-agent-requirements-v2.md)
- [设计文档 v2](docs/plans/cr-agent-design-v2.md)
- [简历参考](docs/RESUME.md)

## 测试

```bash
./start.sh test
# 56 tests passed
```

## License

MIT
