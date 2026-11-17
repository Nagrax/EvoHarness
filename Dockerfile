FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        gnupg \
        grep \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && npx -y playwright@latest install --with-deps chromium \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
COPY requirements-organization.txt .
COPY feishu-daily-agent/requirements.txt ./feishu-daily-agent/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r feishu-daily-agent/requirements.txt

COPY agents ./agents
COPY server ./server
COPY evoharness ./evoharness
COPY integration ./integration
COPY feishu-daily-agent ./feishu-daily-agent
COPY ops ./ops
COPY frontend ./frontend
COPY README.md .

RUN mkdir -p /workspace
COPY .mcp.json /workspace/.mcp.json

WORKDIR /workspace

ENV HOST=0.0.0.0
ENV PORT=7860
EXPOSE 7860

# 优雅停机：uvicorn 收到 SIGTERM 会停止接受新请求并等待在途请求收尾
# （DE-23 与 docs/digital-employee/OPERATIONS.md 的单活/优雅停机口径一致）。
STOPSIGNAL SIGTERM

# 健康探针：/api/health 返回真实组件状态（server/app.py），非"进程存在"式心跳。
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# 默认启动 Web 服务（部署形态）；CLI 形态：
#   docker run -it --entrypoint python <image> -m agents.main
ENTRYPOINT ["python", "-m", "server.app"]
