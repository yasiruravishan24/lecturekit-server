# Build from the folder that contains both lecturekit/ and server/ (the compose file does this):
#   docker build -f server/deploy/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/opt/hf HOME=/home/app DATA_DIR=/data WEB_DIST=/app/web/dist

# ffmpeg (decode audio), tesseract (OCR), LibreOffice Impress/Writer (PowerPoint and Word slides to PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr libreoffice-impress libreoffice-writer fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# an unprivileged user: LibreOffice runs on user files, so it gets no extra rights
RUN useradd --create-home --uid 1000 app && mkdir -p /data /app && chown app:app /data /app

WORKDIR /app
COPY lecturekit/requirements.txt /tmp/lecturekit-requirements.txt
COPY server/requirements.txt /tmp/server-requirements.txt
RUN pip install -r /tmp/lecturekit-requirements.txt -r /tmp/server-requirements.txt

# download the Whisper model now, so the first lecture does not wait for it
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" \
    && chmod -R a+rX /opt/hf

COPY lecturekit/lecturekit /app/lecturekit
COPY server/server /app/server

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# ONE worker process: a second one would break the single job queue (SERVER_PLAN.md section 10.1)
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
