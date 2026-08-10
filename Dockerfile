# Signal Desk — always-on container (Railway / Render / Fly / any VPS)
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY odte_scanner ./odte_scanner
COPY config.yaml ./config.yaml

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

EXPOSE 8787

# Persist outputs via a volume mount in production
RUN mkdir -p /app/outputs

CMD ["python", "-m", "odte_scanner", "ui", "--host", "0.0.0.0", "--port", "8787"]
