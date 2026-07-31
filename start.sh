#!/bin/bash
# ============================================
# 一键启动「广告投放小助手」
# 用法:在 VSCode 终端里进入 my-agent 目录,敲:  ./start.sh
# 停止:在跑着它的终端里按 Ctrl+C
# ============================================
cd "$(dirname "$0")"

# 若已有旧实例在跑,先请它退位(避免端口打架)
pkill -f "uvicorn agent_server:app" 2>/dev/null && echo "(已停掉旧实例)" && sleep 1

echo "🚀 广告投放小助手启动中 → http://localhost:8100"
echo "   (--reload 模式:改完代码自动生效,不用重启)"
exec ./venv/bin/uvicorn agent_server:app --host 0.0.0.0 --port 8100 --reload
