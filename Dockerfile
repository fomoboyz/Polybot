FROM python:3.12-slim

WORKDIR /app

# System deps for py-clob-client's crypto (eth-account, secp256k1 via coincurve).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libgmp-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY polybot ./polybot
COPY config.example.yaml ./config.example.yaml

# Persist SQLite + kill-switch file across container restarts.
VOLUME ["/app/state"]

ENV PYTHONUNBUFFERED=1 \
    POLYBOT_CONFIG=/app/config.yaml

ENTRYPOINT ["python", "-m", "polybot"]
