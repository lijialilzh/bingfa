#!/usr/bin/env bash
# 一键重启阅片并发测试面板：先杀掉旧进程，再后台启动新进程
cd "$(dirname "$0")"

# 杀掉占用 9000 端口的旧进程（比按名字杀更可靠）
if lsof -ti:9000 >/dev/null 2>&1; then
  lsof -ti:9000 | xargs kill -9 2>/dev/null
  sleep 1
fi

# 后台启动新进程，完全脱离终端
# setsid 创建新会话，彻底断开与终端的关联（Linux 推荐）
if command -v setsid >/dev/null 2>&1; then
  setsid .venv/bin/python server.py < /dev/null > server.log 2>&1 &
else
  nohup .venv/bin/python server.py < /dev/null > server.log 2>&1 &
fi
PID=$!
disown "$PID" 2>/dev/null

sleep 1
if kill -0 "$PID" 2>/dev/null; then
  echo "=============================================="
  echo "  服务已启动 (PID: $PID)"
  echo "  访问面板: http://localhost:9000/"
  echo "  查看日志: tail -f server.log"
  echo "  停止服务: lsof -ti:9000 | xargs kill -9"
  echo "=============================================="
else
  echo "启动失败，请查看 server.log"
  tail -20 server.log
  exit 1
fi

