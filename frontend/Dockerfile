# 规智库前端镜像：构建生产包并启动
# 国内网络默认走 daocloud 代理拉取官方镜像，可经 build args 覆盖
ARG DOCKER_REGISTRY=docker.m.daocloud.io
FROM ${DOCKER_REGISTRY}/library/node:22-slim

# 注意：vinext/vite 属于 devDependencies，构建与启动都需要，不能设 NODE_ENV=production
WORKDIR /app

ARG NPM_REGISTRY=https://registry.npmmirror.com

COPY package.json package-lock.json ./
# lock 与 npmmirror 的平台可选依赖存在差异，用 install 自动补齐
RUN npm install --no-audit --no-fund --registry="${NPM_REGISTRY}"

COPY . .

ARG NEXT_PUBLIC_API_PORT=8008
ENV NEXT_PUBLIC_API_PORT=${NEXT_PUBLIC_API_PORT}

RUN npm run build

EXPOSE 3000

CMD ["npm", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
