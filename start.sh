#!/bin/bash
# ============================================
# 一键启动「广告投放小助手」
# 用法(在 my-agent 目录下):
#     ./start.sh              # 用默认端口 18100
#     PORT=8100 ./start.sh    # 想换端口就这样指定
# 停止:在跑着它的终端里按 Ctrl+C
#
# 为什么默认用 18100 这种冷门端口:8100/5173 这类常用端口
# 容易被本机其它程序占用,VSCode 转发时会悄悄改用别的本地端口,
# 结果就是「浏览器打开是空白/一直转圈」。冷门端口能避开这个坑。
# ============================================
cd "$(dirname "$0")"

PORT="${PORT:-18100}"

# ---- 停掉旧实例 ----
# 注意 pkill 的坑:pattern 若写成 "uvicorn agent_server",-f 会匹配到
# 「正在执行 pkill 的这一行命令」本身,把自己也杀掉 → 旧实例反而没死,
# 下一步就报 [Errno 98] Address already in use。
# 用方括号技巧:正则 [u]vicorn 匹配 "uvicorn",但本行文本是 "[u]vicorn",不自匹配。
if pgrep -f "[u]vicorn agent_server" > /dev/null 2>&1; then
  echo "🛑 发现旧实例,正在停止…"
  pkill -f "[u]vicorn agent_server" 2>/dev/null
  for i in $(seq 1 10); do
    pgrep -f "[u]vicorn agent_server" > /dev/null 2>&1 || break
    sleep 0.5
  done
  # 还赖着不走就强制结束
  if pgrep -f "[u]vicorn agent_server" > /dev/null 2>&1; then
    echo "   (温和停止无效,强制结束)"
    pkill -9 -f "[u]vicorn agent_server" 2>/dev/null
    sleep 1
  fi
  echo "   已停止"
fi

# ---- 启动前先确认端口真的空了,免得报一串看不懂的 Errno 98 ----
if command -v python3 > /dev/null 2>&1; then
  if ! python3 - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
    sys.exit(0)          # 能绑上 = 端口是空的
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  then
    echo "❌ 端口 ${PORT} 还被别的程序占着,起不来。"
    echo "   看看是谁:  pgrep -af \"[p]ython.*${PORT}\""
    echo "   或者换端口:PORT=18200 ./start.sh"
    exit 1
  fi
fi

echo "🚀 广告投放小助手启动中 → http://localhost:${PORT}"
echo "   记得在 VSCode「端口」面板转发 ${PORT}"
echo "   (--reload 模式:改完代码自动生效,不用重启)"
echo "   日志同时写入 server.log(出事可回查谁动了广告)"
echo ""

# tee:日志一份打在屏幕上给你看,一份追加进 server.log 存档。
# 为什么重要:所有"谁在什么时候开关了哪条广告"都记在日志里,
# 只打屏幕的话,终端一关证据就没了,事后查不清。
exec ./venv/bin/uvicorn agent_server:app --host 0.0.0.0 --port "$PORT" --reload 2>&1 | tee -a server.log
