# One image for the API and the workers: same code, different command.
#
# Keeping them identical means a worker can never be running a different build
# than the API that queued its work — which is exactly the class of bug that
# separate api/scheduler images invite.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# ffmpeg does the rendering; Node runs TikTok's request signer (there is no
# Python equivalent — see app/adapters/tiktok/signing.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl ca-certificates gnupg \
        fonts-liberation fonts-noto-cjk \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so editing source does not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Node deps for the signer.
COPY app/adapters/tiktok/signature/package.json app/adapters/tiktok/signature/package-lock.json \
     ./app/adapters/tiktok/signature/
RUN cd app/adapters/tiktok/signature && npm ci --omit=dev

# ...and the browser it drives. `npm ci` installs the Playwright library but
# never downloads a browser binary, and the signer launches one to compute the
# signature TikTok wants on the final publish call. Without it every publish
# uploaded the whole video and then failed on the last step — the upload made,
# no post to show for it, and nothing safe to retry automatically.
#
# The shared path matters: installed as root the browser lands in /root/.cache,
# which the appuser this image runs as cannot read.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN cd app/adapters/tiktok/signature \
    && npx playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY assets ./assets
COPY alembic.ini ./

# Runs as a non-root user; /data is the mounted volume it must be able to write.
#
# /models exists here, owned by appuser, so that the named volume compose mounts
# over it inherits that ownership. Docker only copies ownership from a path the
# image already has — mount a volume onto a path that does not exist and it is
# created root-owned, which a non-root process cannot write.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /models \
    && chown -R appuser:appuser /data /models /app
USER appuser

EXPOSE 8000

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
