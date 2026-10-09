# ============================================================
#  Dockerfile - Universal Media Downloader Bot (Railway)
# ============================================================
#
#  Build:  docker build -t media-downloader-bot .
#  Run:    docker run --env-file .env media-downloader-bot
#  Logs:   docker logs -f media-downloader-bot
#
#  IMPORTANT
#  The git tag below MUST equal the pip version of
#  bgutil-ytdlp-pot-provider in requirements.txt (2.0.2).
#  A major-version mismatch makes the PO Token provider reject
#  every request, so YouTube fails on datacenter IPs.
# ============================================================

FROM python:3.11-slim

# Keep these two in sync with requirements.txt
ARG BGUTIL_VERSION=2.9.5
ARG BGUTIL_TAG=2.0.2

# Deno runs the PO Token server
ENV DENO_INSTALL=/root/.deno
ENV PATH="$DENO_INSTALL/bin:$PATH"
ENV DENO_DIR=/root/.cache/deno
ENV DENO_NO_PROMPT=1
ENV DENO_NO_UPDATE_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh -s v${BGUTIL_VERSION} \
    && deno --version

# PO Token server - same tag as the pip plugin
RUN git clone --single-branch --branch ${BGUTIL_TAG} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /opt/bgutil-ytdlp-pot-provider

WORKDIR /opt/bgutil-ytdlp-pot-provider/server
RUN deno install --allow-scripts=npm:canvas --frozen \
    && deno cache --frozen src/main.ts

# --- Python dependencies ---
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && echo "=== Verification ===" \
    && python --version \
    && yt-dlp --version \
    && deno --version \
    && ffmpeg -version 2>&1 | head -1 \
    && python -c "import importlib.metadata as m; \
print('bgutil plugin:', m.version('bgutil-ytdlp-pot-provider'))"

# --- Application ---
COPY bot.py start.sh ./
RUN chmod +x start.sh && mkdir -p /app/downloads

# Railway routes HTTP here; the bot serves / and /health on it.
EXPOSE 8080

CMD ["./start.sh"]
