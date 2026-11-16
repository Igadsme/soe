FROM python:3.12-slim-bookworm AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY backend/requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app
WORKDIR /app
# System ffmpeg/ffprobe are preferred; the bundled imageio-ffmpeg binary is the fallback
# (set INSTALL_SYSTEM_FFMPEG=false where Debian mirrors are unreachable).
ARG INSTALL_SYSTEM_FFMPEG=true
RUN if [ "$INSTALL_SYSTEM_FFMPEG" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*; \
    fi \
    && groupadd --gid 10001 soe && useradd --uid 10001 --gid soe --create-home soe \
    && mkdir /app/media && chown soe:soe /app/media
COPY --from=builder /opt/venv /opt/venv
COPY backend/app /app/app
COPY alembic.ini /app/alembic.ini
COPY backend/alembic /app/backend/alembic
COPY --chmod=755 infra/api/entrypoint.sh /app/entrypoint.sh
USER soe
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=4)"
ENTRYPOINT ["/app/entrypoint.sh"]
