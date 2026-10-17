FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies - includes real ffmpeg binary + tini (clean signal handling / no zombie processes)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    build-essential \
    ca-certificates \
    libssl-dev \
    libffi-dev \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p /app/data/scripts /app/data/logs

# Token / owner id bot.py ke andar hardcoded hain.
# Mount a volume on /app/data to keep scripts, logs and state across redeploys.
ENTRYPOINT ["tini", "--"]
CMD ["python", "bot.py"]
