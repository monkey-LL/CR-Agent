#!/bin/bash
# CR Agent 一键启动脚本
# 用法:
#   ./start.sh web      → 启动 Web UI (默认)
#   ./start.sh webhook  → 启动 GitHub Webhook 服务
#   ./start.sh cli      → CLI 模式 (需传参数)
#   ./start.sh test     → 运行测试

cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)"

PYTHON=".venv/bin/python"

# 加载 .env 文件（如果存在）
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

MODE=${1:-web}

case "$MODE" in
  web)
    echo "启动 Web UI: http://localhost:8088"
    echo "模型: ${CR_MODEL:-DeepSeek-V4-Flash} (${OPENAI_BASE_URL:-default})"
    exec $PYTHON -m uvicorn cr_agent.web.server:app --port 8088
    ;;
  webhook)
    echo "启动 GitHub Webhook 服务: http://localhost:8088/webhook"
    echo "模型: ${CR_MODEL:-DeepSeek-V4-Flash} (${OPENAI_BASE_URL:-default})"
    exec $PYTHON -m uvicorn cr_agent.github.webhook_server:app --port 8088
    ;;
  cli)
    shift
    exec $PYTHON -m cr_agent "$@"
    ;;
  test)
    exec $PYTHON -m pytest tests/ -v
    ;;
  *)
    echo "用法: ./start.sh [web|webhook|cli|test]"
    echo "  web      启动 Web UI (默认)"
    echo "  webhook  启动 Webhook 服务"
    echo "  cli      CLI 模式, 如: ./start.sh cli --diff-file x.patch --no-llm"
    echo "  test     运行测试"
    exit 1
    ;;
esac