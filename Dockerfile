FROM python:3.12-slim

# ffmpeg is used from step 2 (vertical render) onward.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY clipbot ./clipbot
RUN pip install --no-cache-dir .

ENV WATCH_DIR=/clips \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/clips", "/data"]
EXPOSE 8765
CMD ["clipbot"]
