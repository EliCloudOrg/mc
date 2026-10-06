# EliCloud MC 白名单服务 运行时镜像
#
# 设计要点（与 sso/Dockerfile 同一套取舍）：
#   1. 数据（SQLite）落在 /data（compose 里是挂卷），绝不进镜像层；
#   2. 容器内以 uid 1002:1003（宿主机 docker-admin）运行，挂卷目录才能被写入；
#   3. PIP_INDEX_URL 可覆盖：**默认官方 PyPI**。镜像若在 GitHub Actions 上构建（境外网络），
#      清华源会对出口 IP 间歇性 403（实测踩过）；在国内服务器本地构建时显式覆盖：
#        --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

# /data 是 SQLite 的落点；镜像内先建好并交给运行用户
RUN mkdir -p /data && chown -R 1002:1003 /data

USER 1002:1003

EXPOSE 8000

# 客户端 IP 由 app 自己从 X-Forwarded-For 末尾取值（见 app/deps.py::client_ip），
# 所以这里不开 --proxy-headers，行为显式可控。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
