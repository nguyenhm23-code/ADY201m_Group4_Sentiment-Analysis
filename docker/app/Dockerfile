FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY docker/app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY src /app/src
COPY configs /app/configs
# Browser crawling runs on the host; this image processes MinIO -> SQLite/JSONL.
CMD ["python", "-m", "src.storage.lake", "process", "--config", "configs/preprocessing.json"]
