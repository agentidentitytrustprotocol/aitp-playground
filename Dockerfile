FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e .

COPY src/ ./src/
COPY agents/ ./agents/
COPY scenarios/ ./scenarios/

ENV PYTHONPATH=/app/src:/app/agents/base

EXPOSE 8000
CMD ["uvicorn", "aitp_playground.main:app", "--host", "0.0.0.0", "--port", "8000"]
