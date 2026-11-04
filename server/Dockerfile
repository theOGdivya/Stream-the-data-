FROM python:3.13-slim

# OMP_THREAD_LIMIT: one thread per Tesseract call (parallel calls are ~50x faster this way).
# MPLCONFIGDIR: matplotlib (report figures) needs a writable cache folder.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data OMP_THREAD_LIMIT=1 MPLCONFIGDIR=/tmp/matplotlib
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr libglib2.0-0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aqserver ./aqserver

RUN useradd --system --uid 1000 --home /app app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["uvicorn", "aqserver.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
