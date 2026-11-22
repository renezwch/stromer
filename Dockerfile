FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app/ /app/
# Unprivileged user; the data directory on the host must belong to uid 1000 (user pi).
USER 1000:1000
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8095
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8095/health',timeout=4)"
CMD ["python", "/app/collector.py"]
