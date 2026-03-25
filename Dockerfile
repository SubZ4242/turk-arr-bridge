FROM python:3.12-slim

LABEL maintainer="TurkARRBridge"
LABEL description="Torznab Proxy Bridge für türkische Serien/Filme mit Titel-Übersetzung"

WORKDIR /app

# System-Dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Persistent config directory
RUN mkdir -p /config
VOLUME /config

# Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App Code
COPY bridge.py .

# Health Check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9696/health || exit 1

EXPOSE 9696

# Gunicorn für Production
CMD ["gunicorn", \
     "--bind", "0.0.0.0:9696", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "bridge:app"]
