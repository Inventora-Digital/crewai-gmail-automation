# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY README.md ./

# Install project
RUN pip install --upgrade pip setuptools wheel \
 && pip install . \
 && pip install uvicorn

EXPOSE 8080

CMD ["uvicorn", "gmail_crew_ai.server:app", "--host", "0.0.0.0", "--port", "8080"]

