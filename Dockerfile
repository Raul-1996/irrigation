# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for Pillow and curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-dev zlib1g-dev libpng-dev \
    curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Record build-time git info into VERSION (best-effort, .git may be absent)
ARG GIT_COMMIT=unknown
ARG GIT_BRANCH=unknown
RUN printf '\ngit_commit=%s\ngit_branch=%s\nbuild_date=%s\n' \
    "$GIT_COMMIT" "$GIT_BRANCH" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> VERSION || true

# Create non-root user and ensure writable directories
RUN adduser --disabled-password --gecos '' appuser \
 && mkdir -p /app/media /app/backups \
 && chown -R appuser:appuser /app

EXPOSE 8080

# Healthcheck to /health (optional)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/ || exit 1

ENV TESTING=0

USER appuser

CMD ["python","run.py"]


