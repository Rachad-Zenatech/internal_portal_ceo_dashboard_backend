FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    TESSERACT_CMD=/usr/bin/tesseract \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=7001

WORKDIR /app

# System packages required by OCR, PDF extraction, PostgreSQL drivers,
# and C extensions on EC2 Linux 2023 (x86_64 and AWS Graviton arm64).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libpq-dev \
    tesseract-ocr \
    poppler-utils \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better Docker build caching.
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source code.
COPY . .

# Create the persistent-data directories and a non-root application user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/data/upload_files \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8005 7001

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://localhost:{port}/health/live')" || exit 1

# Start FastAPI by default (MCP can be started by overriding the command)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
