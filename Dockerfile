# Prepared for a future release; local development uses Vite + Python source.
FROM node:22-slim AS frontend
WORKDIR /app/frontend
RUN npm install --global pnpm@10.33.0
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
# The hashed runtime export includes the optional Redis client for the bundled Compose cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip install --no-cache-dir setuptools==84.1.0 \
    && groupadd --gid 10001 watcher \
    && useradd --uid 10001 --gid watcher --create-home watcher \
    && mkdir -p /app/data && chown watcher:watcher /app/data
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend /app/src/newapi_watch/web-dist ./src/newapi_watch/web-dist
RUN pip install --no-cache-dir --no-build-isolation --no-deps .
USER 10001:10001
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health/live', timeout=3)"
ENTRYPOINT ["python", "-m", "newapi_watch"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8088"]
