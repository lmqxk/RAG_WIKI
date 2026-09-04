# 规智库后端 API 镜像
# 国内网络默认走 daocloud 代理拉取官方镜像，可经 build args 覆盖
ARG DOCKER_REGISTRY=docker.m.daocloud.io
FROM ${DOCKER_REGISTRY}/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --index-url "${PIP_INDEX_URL}" .

# 项目按源码路径推导 PROJECT_ROOT（backend/src/backend/config.py -> parents[3]），
# 必须保持 /app/backend/src 布局并从源码导入，否则 storage 目录定位错误。
ENV PYTHONPATH=/app/backend/src

EXPOSE 8008

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8008"]
