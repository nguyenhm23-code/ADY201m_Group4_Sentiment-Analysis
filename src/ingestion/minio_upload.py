"""Upload batches to MinIO, optionally process them, and keep recovery checkpoints."""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from minio import Minio


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUCKET_NAME = "foody-raw-data"
BATCH_SIZE = 2_000
VIETNAM_TIME = timezone(timedelta(hours=7))
load_dotenv(PROJECT_ROOT / ".env")


class BatchUploadError(RuntimeError):
    """Dừng crawler khi không thể lưu bộ đệm hoặc upload dữ liệu."""


def write_json(path, data):
    """Ghi file tạm rồi thay thế để không để lại JSON đang ghi dở."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def create_minio_client(bucket_name=BUCKET_NAME):
    access_key = os.getenv("MINIO_ROOT_USER")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD")
    if not access_key or not secret_key:
        raise ValueError("Cần khai báo MINIO_ROOT_USER và MINIO_ROOT_PASSWORD trong .env")
    client = Minio(
        os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        access_key=access_key,
        secret_key=secret_key,
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
    return client


def upload_folder():
    """Tên thư mục: ngày-tháng-năm upload, theo giờ Việt Nam (UTC+7)."""
    return datetime.now(VIETNAM_TIME).strftime("%d-%m-%Y")


def record_key(record):
    """Một bình luận của một quán chỉ được xếp vào hàng đợi một lần."""
    review_id = record.get("ID Bình Luận")
    identity = [record.get("URL Quán"), str(review_id)]
    if review_id is None:
        identity = record
    content = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MinioBatchUploader:
    """Bộ đệm dùng chung cho mọi thành phố, có checkpoint riêng với crawler.

    Lưu tên object trước khi gửi. Nếu gửi thành công nhưng chưa kịp cập nhật
    checkpoint, lần chạy sau ghi lại đúng object đó thay vì tạo bản sao.
    Chỉ chạy một crawler trên cùng thư mục checkpoint tại một thời điểm.
    """

    def __init__(self, state_file, bucket_name=BUCKET_NAME, batch_size=BATCH_SIZE,
                 *, process_batch=None, log_dir=None):
        if batch_size <= 0:
            raise ValueError("batch_size phải lớn hơn 0")
        self.state_file = Path(state_file)
        self.batch_file = self.state_file.with_name("minio_pending_batch.json")
        self.bucket_name = bucket_name
        self.batch_size = batch_size
        self.process_batch = process_batch
        self.log_dir = Path(log_dir) if log_dir is not None else self.state_file.parent / "ingestion_logs"
        self.log_file = self.log_dir / "events.jsonl"
        self.state = {"pending": [], "uploaded_keys": [], "object_name": None, "batch_count": 0}
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.state.setdefault("duplicate_rows_skipped", 0)
        self.seen = set(self.state["uploaded_keys"])
        self.seen.update(record_key(record) for record in self.state["pending"])
        self.client = create_minio_client(bucket_name)

    def _event(self, event, **details):
        entry = {
            "timestamp": datetime.now(VIETNAM_TIME).isoformat(), "event": event,
            "bucket": self.bucket_name, "object_name": self.state["object_name"],
            **details,
        }
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(f"[Ingestion] {json.dumps(entry, ensure_ascii=False)}", flush=True)
        return entry

    def add_records(self, records):
        """Lưu các record mới; gửi ngay mỗi khi đủ một lô."""
        try:
            # Hoàn tất lô bị gián đoạn ở lần chạy trước, kể cả lô cuối < 2.000.
            self._upload_ready_batches()
            duplicates = 0
            added = 0
            for record in records:
                key = record_key(record)
                if key in self.seen:
                    duplicates += 1
                    self.state["duplicate_rows_skipped"] += 1
                    continue
                self.state["pending"].append(record)
                added += 1
                self.seen.add(key)
                if len(self.state["pending"]) >= self.batch_size:
                    self._upload_batch(self.batch_size)
            write_json(self.state_file, self.state)
            if added or duplicates:
                self._event("records_queued", new_rows=added, duplicate_rows=duplicates,
                            duplicate_rows_skipped_total=self.state["duplicate_rows_skipped"],
                            pending_rows=len(self.state["pending"]))
        except Exception as error:
            raise BatchUploadError(
                f"Không thể hoàn tất lô MinIO/pipeline/DB; hãy chạy lại crawler để tiếp tục: {error}"
            ) from error

    def flush(self):
        """Gửi phần dư khi crawler kết thúc bình thường."""
        try:
            self._upload_ready_batches()
            if self.state["pending"]:
                self._upload_batch(len(self.state["pending"]))
        except Exception as error:
            raise BatchUploadError(f"Xử lý phần dư thất bại; hãy chạy lại crawler: {error}") from error

    def _upload_ready_batches(self):
        if self.state["object_name"]:
            self._upload_batch(min(self.batch_size, len(self.state["pending"])))
        while len(self.state["pending"]) >= self.batch_size:
            self._upload_batch(self.batch_size)

    def _upload_batch(self, count):
        batch = self.state["pending"][:count]
        if not self.state["object_name"]:
            self.state["object_name"] = f"{upload_folder()}/foody_batch_{uuid4().hex}.json"
        batch_dir = self.log_dir / "batches" / Path(self.state["object_name"]).stem
        stage = "checkpoint_payload"
        try:
            # Persist the payload and object name before any network operation.
            write_json(self.state_file, self.state)
            write_json(self.batch_file, batch)
            started = time.monotonic()
            started_at = datetime.now(VIETNAM_TIME).isoformat()
            self._event("batch_started", raw_rows=count, started_at=started_at)
            stage = "minio_upload"
            self.client.fput_object(
                self.bucket_name, self.state["object_name"], str(self.batch_file),
                content_type="application/json",
            )
            uploaded_at = datetime.now(VIETNAM_TIME).isoformat()
            self._event("minio_uploaded", raw_rows=count, uploaded_at=uploaded_at)
            stats = {"raw_rows": count}
            if self.process_batch is not None:
                stage = "pipeline_database"
                stats = self.process_batch(
                    self.client, self.bucket_name, self.state["object_name"],
                    self.batch_file, batch_dir,
                )
            stage = "completion_log"
            summary = {
                "status": "completed", "mode": "pipeline_database" if self.process_batch else "upload_only",
                "bucket": self.bucket_name, "object_name": self.state["object_name"],
                "started_at": started_at, "uploaded_at": uploaded_at,
                "completed_at": datetime.now(VIETNAM_TIME).isoformat(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "crawler_duplicates_skipped_total": self.state["duplicate_rows_skipped"],
                **stats,
            }
            write_json(batch_dir / "summary.json", summary)
            self._event("batch_completed", **summary)
            # Acknowledge ONLY after DB commit and successful completion logging.
            # Build a new state so failed writes do not acknowledge in memory either.
            stage = "checkpoint"
            completed_state = {
                **self.state,
                "uploaded_keys": self.state["uploaded_keys"] + [record_key(record) for record in batch],
                "pending": self.state["pending"][count:], "object_name": None,
                "batch_count": self.state["batch_count"] + 1,
            }
            write_json(self.state_file, completed_state)
            self.state = completed_state
        except Exception as error:
            try:
                self._event("batch_failed", stage=stage, raw_rows=count,
                            error_type=type(error).__name__, error=str(error))
            except OSError:
                print(f"[Ingestion] Cannot write failure log: {error}", file=sys.stderr, flush=True)
            raise
        self.batch_file.unlink(missing_ok=True)


def upload_dataset_to_minio(file_name="foody_merged_dataset.json", bucket_name=BUCKET_NAME):
    """Giữ hàm upload file riêng cho những nơi đang gọi hàm cũ."""
    path = PROJECT_ROOT / "data" / "raw" / file_name
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file JSON: {path}")
    client = create_minio_client(bucket_name)
    object_name = f"{upload_folder()}/{path.stem}_{uuid4().hex}{path.suffix}"
    client.fput_object(bucket_name, object_name, str(path), content_type="application/json")
    print(f"[MinIO] Đã upload: {bucket_name}/{object_name}", flush=True)
    return object_name


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    upload_dataset_to_minio()
