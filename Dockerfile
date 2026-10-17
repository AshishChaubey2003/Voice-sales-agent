# Voice bot image — the service a visitor talks to over WebRTC.
# The text chat API is a separate service; see Dockerfile.api and DEPLOYMENT.md.
#
# Python 3.13 matches the environment the test suite and requirements.lock.txt
# were produced on. 3.12 also works, but regenerate the lock on 3.12 first:
# audioop-lts in the lock is a 3.13-only backport.
FROM python:3.13-slim

# - libgomp1: OpenMP runtime that onnxruntime (the embedding model) needs
# - libopus0: Opus audio codec for the WebRTC media path
# These are a conservative starting set; trim once you have tested the build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The embedding model cache. Baked below so cold starts do not re-download it.
    EMBEDDING_CACHE_DIR=/app/.models \
    PORT=7860

WORKDIR /app

# Install dependencies first, from the locked set, so this layer is cached
# across code changes and the image matches what the tests ran against.
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt

# Bake the embedding model into the image. Needs network at build time; if that
# is unavailable the build still succeeds and the model downloads on first use.
ARG BAKE_EMBEDDING_MODEL=true
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN if [ "$BAKE_EMBEDDING_MODEL" = "true" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='$EMBEDDING_MODEL', cache_dir='$EMBEDDING_CACHE_DIR')" \
      || echo "WARNING: embedding model not baked; it will download on first run"; \
    fi

# Application code last, so editing it does not invalidate the layers above.
COPY . .

# Run as a non-root user. /app (which holds the baked model cache at
# /app/.models) is chowned so it stays writable. UID 1000 matches the user
# Hugging Face Spaces runs containers as.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

# Reports unhealthy if the runner's HTTP server is not answering, so an
# orchestrator does not treat a dead voice bot as healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/', timeout=4).status < 500 else 1)" || exit 1

# Bind to 0.0.0.0 so the container port is reachable. ICE servers come from
# PIPECAT_ICE_SERVERS in the environment (see DEPLOYMENT.md) — without them a
# remote visitor cannot connect.
CMD ["python", "-m", "voice.bot", "-t", "webrtc", "--host", "0.0.0.0", "--port", "7860"]
