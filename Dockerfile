# Stage 1: build the SPA
FROM node:22-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
# npm ci is too strict across npm versions for the wasm optional deps
# (@emnapi/*) that tailwind's oxide pulls in; install still honors the lock.
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: python runtime with Tesseract + language packs
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-spa tesseract-ocr-ita tesseract-ocr-fra \
        tesseract-ocr-por tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/ server/
COPY --from=web /build/dist/ web/dist/

# /data is provided by a platform volume (Railway) or compose volume; a
# Dockerfile VOLUME directive is rejected by Railway's builder.
ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=8000
EXPOSE 8000
# IPv4 bind: the public edge connects over IPv4, and this container's
# [::] bind is v6-only (no v4-mapped addresses). Only services that
# RECEIVE private-mesh traffic (the vision sidecar) must bind ::; the
# app only makes outbound v6 connections, which don't depend on the
# listen socket.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
