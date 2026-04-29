# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.10-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# Copy entire project preserving folder structure
COPY . .

RUN mkdir -p /data
ENV DB_PATH=/data/qa_assistant.db
# Add both /app and /app/apis to PYTHONPATH so all imports resolve
ENV PYTHONPATH=/app:/app/apis

EXPOSE 8000

CMD ["uvicorn", "apis.main:app", "--host", "0.0.0.0", "--port", "8000"]