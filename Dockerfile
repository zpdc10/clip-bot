FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY clipbot ./clipbot
RUN pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV WATCH_DIR=/clips \
    DATA_DIR=/data \
    WEB_PORT=8765 \
    PUID=99 \
    PGID=100 \
    PYTHONUNBUFFERED=1

VOLUME ["/clips", "/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT','8765'), timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["clipbot"]
