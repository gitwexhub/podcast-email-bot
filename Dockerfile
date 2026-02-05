FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (ffmpeg for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (cloud-only, no PyTorch/faster-whisper)
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

# Copy application code
COPY src/ src/

# Create data directory for persistent storage
RUN mkdir -p /data/vault/content/podcasts /data/vault/content/articles /data/vault/content/threads
ENV VAULT_PATH=/data/vault

CMD ["python", "-m", "src.bot"]
