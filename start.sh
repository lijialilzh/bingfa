#!/usr/bin/env bash
# 一键重启阅片并发测试面板：先杀掉旧进程，再启动新进程
cd "$(dirname "$0")"

# 杀掉旧的 server.py 进程（忽略未找到的错误）
pkill -f "python server.py" 2>/dev/null
pkill -f "server.py" 2>/dev/null
sleep 1

# 启动新进程
.venv/bin/python server.py
