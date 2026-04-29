FROM python:3.12-slim

ARG BUILD_VERSION=dev
ARG BUILD_DATE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUILD_VERSION=${BUILD_VERSION} \
    BUILD_DATE=${BUILD_DATE}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY alembic.ini /app/
COPY alembic /app/alembic

RUN pip install --no-cache-dir .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "--app-dir", "/app/src", "meshonator.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
