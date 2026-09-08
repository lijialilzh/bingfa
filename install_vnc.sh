#!/usr/bin/env bash
# 安装 VNC 录制依赖（网页内嵌浏览器功能）
# 在服务器上执行一次即可

set -e

echo "=== 1. 安装系统软件包（Xvfb 虚拟显示 + x11vnc）==="
sudo apt-get update
sudo apt-get install -y xvfb x11vnc

echo "=== 2. 安装 Python 依赖（websockify）==="
cd "$(dirname "$0")"
if [ -d ".venv" ]; then
  .venv/bin/python -m pip install websockify -i https://pypi.tuna.tsinghua.edu.cn/simple
else
  python3 -m pip install websockify -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

echo "=== 3. 下载 noVNC 静态文件 ==="
if [ ! -d "novnc" ]; then
  curl -sL --max-time 120 -o /tmp/novnc.tar.gz \
    "https://ghproxy.net/https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz"
  mkdir -p /tmp/novnc_extract
  tar -xzf /tmp/novnc.tar.gz -C /tmp/novnc_extract
  cp -r /tmp/novnc_extract/noVNC-1.5.0 novnc
  echo "noVNC 已下载到 novnc/ 目录"
else
  echo "novnc/ 目录已存在，跳过下载"
fi

echo ""
echo "=== 安装完成 ==="
echo "重启服务后，录制页面的「网页内嵌浏览器」功能即可使用"
echo "重启命令：./start.sh"
