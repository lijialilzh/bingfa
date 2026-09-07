# 测试平台 - Docker 部署说明

## 一、需要上传到服务器的文件

```
Dockerfile
engine.py
server.py
index.html
config.json
record.py
ui_test.py
requirements-docker.txt
```

## 二、服务器要求

- 已安装 Docker（`docker --version` 能正常输出）
- 能访问被测系统 `192.168.108.109:8000`
- 内存建议 32GB+

## 三、部署步骤

### 1. 上传文件

```bash
mkdir -p ~/peizhi
# 在本机执行（把文件传到服务器）
scp Dockerfile engine.py server.py index.html config.json record.py ui_test.py requirements-docker.txt 用户名@服务器IP:~/peizhi/
```

### 2. 构建镜像

```bash
cd ~/peizhi
docker build -t peizhi-test .
```

> 构建需要几分钟（下载 Python 镜像 + Chromium）。如果下载慢，可配置 Docker 镜像加速器。

### 3. 启动容器

```bash
docker run -d --name peizhi-test -p 19000:19000 --restart=always peizhi-test
```

### 4. 访问面板

浏览器打开：`http://服务器IP:19000/`

登录账号：`master`，密码：`Tuixiang2026`

## 四、常用命令

```bash
# 查看容器状态
docker ps

# 查看日志
docker logs -f peizhi-test

# 停止容器
docker stop peizhi-test

# 删除容器
docker rm peizhi-test

# 重新构建（改了代码后）
docker build -t peizhi-test . && docker rm -f peizhi-test && docker run -d --name peizhi-test -p 19000:19000 --restart=always peizhi-test
```

## 五、注意事项

1. **内存**：100 个浏览器上下文约需 5~10GB，容器默认不限制内存，但宿主机要够
2. **网络**：容器内访问 `192.168.108.109:8000` 走宿主机网络，内网互通即可
3. **超时**：100 并发下服务器响应慢，面板上「图像加载超时上限」建议设 180 秒
