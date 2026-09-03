# 使用微软官方 Playwright 镜像（已内置 Python + Chromium + 所有系统依赖，无需 apt）
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# 安装 Python 依赖（国内镜像加速）
COPY requirements-docker.txt .
RUN pip install -r requirements-docker.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目文件
COPY engine.py server.py index.html config.json ./

EXPOSE 9000

CMD ["python", "server.py"]
