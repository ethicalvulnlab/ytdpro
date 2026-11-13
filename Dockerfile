FROM python:3.12-slim

RUN set -e; \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(uname -m)" in \
        x86_64)  DENO_ARCH="x86_64-unknown-linux-gnu";; \
        aarch64) DENO_ARCH="aarch64-unknown-linux-gnu";; \
        *) echo "unsupported arch"; exit 1;; \
    esac; \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip; \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    chmod +x /usr/local/bin/deno; \
    rm -f /tmp/deno.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY templates/ templates/

RUN mkdir -p /downloads /data

ENV YT_SETTINGS_FILE=/data/settings.json
ENV YT_DOWNLOAD_DIR=/downloads
ENV YT_HOST=0.0.0.0
ENV YT_PORT=8080

VOLUME ["/downloads", "/data"]
EXPOSE 8080

CMD ["python", "-u", "server.py"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/version', timeout=4)" || exit 1
