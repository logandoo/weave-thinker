#
# Weave Thinker 一体化镜像：node 构建前端 → python 运行时（后端直接服务 static/）。
# 构建（仓库根为上下文）：
#   docker build -t weave-thinker:local .
#   docker build --build-arg WITH_BROWSER=1 -t weave-thinker:browser .   # 含 Chromium（浏览器工具）
# 构建后由 docker-compose.yml 编排 PostgreSQL 与运行时卷。

# ── Stage 1: 前端构建 ────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── Stage 2: 运行时 ──────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS runtime

ARG WITH_BROWSER=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# WeasyPrint(PDF) 运行库 + CJK 字体（中文导出）+ ffmpeg（语音转写转码）+ Chromium 运行库（可选）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libharfbuzz-subset0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fonts-noto-cjk \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY docker/ /app/docker/
COPY --from=frontend-build /build/frontend/dist /app/backend/static

# 可选：安装 Playwright Chromium（浏览器工具 / E2E；镜像体积显著增大）
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        python -m playwright install --with-deps chromium; \
    else \
        echo "WITH_BROWSER=0 — 跳过 Chromium（浏览器工具不可用，其余功能不受影响）"; \
    fi

# 运行时目录（先于 named volume 挂载创建，保证卷初始化时继承 appuser 属主）
RUN mkdir -p /app/user_workspaces \
             /app/backend/agent_memories \
             /app/backend/audio_files \
             /app/backend/output_files \
             /app/backend/skins_custom \
             /app/backend/Fonts \
    && chmod +x /app/docker/entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app \
    && if [ -d /ms-playwright ]; then chown -R appuser:appuser /ms-playwright; fi

USER appuser

EXPOSE 8158

HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
