FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 brain

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY templates ./templates
COPY sql ./sql

RUN pip install . \
    && mkdir -p /data /app/brain_files \
    && chown -R brain:brain /data /app

USER brain

RUN git config --global safe.directory '*' \
    && git config --global init.defaultBranch main

ENV BRAIN_HOST=0.0.0.0 \
    BRAIN_PORT=8000 \
    BRAIN_FILES_DIR=/app/brain_files \
    DATABASE_URL=sqlite+aiosqlite:////data/brain_metadata.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["brain", "serve"]
