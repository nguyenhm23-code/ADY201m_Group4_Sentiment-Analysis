"""Publish and restore verified raw snapshots in a private MinIO bucket.

Run from the project root: python -m src.storage.lake --help
Uploads use content-addressed blobs, then a manifest, then a latest pointer.
Interrupted uploads cannot make an incomplete snapshot the latest snapshot.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from src.processing.cleaner import discover_inputs, run_pipeline, strict_json, verify_snapshot, ARTIFACTS

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
HASH = re.compile(r'^[0-9a-f]{64}$')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def make_client():
    """Read credentials only from environment; do not log credentials."""
    from minio import Minio
    import urllib3

    access = os.environ.get('MINIO_ACCESS_KEY') or os.environ.get('MINIO_ROOT_USER')
    secret = os.environ.get('MINIO_SECRET_KEY') or os.environ.get('MINIO_ROOT_PASSWORD')
    if not access or not secret:
        raise ValueError('Set MINIO_ACCESS_KEY and MINIO_SECRET_KEY (or MINIO_ROOT_USER/PASSWORD).')
    endpoint = os.environ.get('MINIO_ENDPOINT', 'localhost:9000')
    secure = os.environ.get('MINIO_SECURE', 'true').lower()
    if secure not in {'true', 'false'}:
        raise ValueError('MINIO_SECURE must be true or false.')
    return Minio(endpoint, access_key=access, secret_key=secret, secure=secure == 'true',
                 http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=60),
                                                retries=urllib3.Retry(total=2, backoff_factor=0.5)))


def put_bytes(client, bucket, key, payload):
    client.put_object(bucket, key, io.BytesIO(payload), len(payload), content_type='application/json')


def get_bytes(client, bucket, key, maximum=None):
    response = client.get_object(bucket, key)
    try:
        payload = response.read() if maximum is None else response.read(maximum + 1)
        if maximum is not None and len(payload) > maximum:
            raise ValueError(f'Object exceeds the expected size: {key}')
        return payload
    finally:
        response.close()
        response.release_conn()


def valid_relative_path(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('Invalid snapshot path.')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or value != str(path):
        raise ValueError('Snapshot path must be a normalized relative path.')
    name = path.name
    allowed = (len(path.parts) == 2 and path.parts[0] == 'foody' and name.endswith('dataset.json'))
    allowed |= (len(path.parts) == 2 and path.parts[0] == 'gmap' and name.startswith('gmap_')
                and name.endswith('.json') and not name.endswith(('.status.json', '.place.json')))
    allowed |= len(path.parts) == 1 and name.startswith('foody_') and name.endswith('dataset.json')
    if not allowed:
        raise ValueError(f'Not a supported raw dataset path: {value}')
    return path


def validate_manifest(manifest, snapshot_id):
    if not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported snapshot manifest.')
    if set(manifest) != {'schema_version', 'files'} or not isinstance(manifest['files'], list) or not manifest['files']:
        raise ValueError('Snapshot has no files or invalid metadata.')
    if not HASH.fullmatch(snapshot_id) or sha(encoded(manifest)) != snapshot_id:
        raise ValueError('Manifest SHA256 does not match the requested snapshot.')
    seen = set()
    for entry in manifest['files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'bytes'}:
            raise ValueError('Invalid manifest file entry.')
        valid_relative_path(entry['path'])
        key = entry['path'].casefold()
        if key in seen:
            raise ValueError('Duplicate snapshot path (including case-only duplicates).')
        seen.add(key)
        if not isinstance(entry['sha256'], str) or not HASH.fullmatch(entry['sha256']):
            raise ValueError('Invalid file checksum.')
        if type(entry['bytes']) is not int or entry['bytes'] < 0:
            raise ValueError('Invalid file length.')
    return manifest


def upload_snapshot(client, bucket, raw_dir):
    raw = Path(raw_dir).resolve()
    files = discover_inputs(raw)
    if not files:
        raise ValueError('No raw review dataset files found.')
    # Freeze each read before uploading. A crawler may atomically replace its file
    # afterwards; the manifest always describes exactly the bytes uploaded here.
    with tempfile.TemporaryDirectory(prefix='ady-lake-upload-') as scratch:
        entries = []
        for _, path in files:
            if not path.resolve().is_relative_to(raw) or not path.is_file():
                raise ValueError('Raw input leaves the raw directory.')
            relative = path.relative_to(raw).as_posix()
            valid_relative_path(relative)
            payload = path.read_bytes()
            value = strict_json(payload.decode('utf-8-sig'))
            if not isinstance(value, list):
                raise ValueError(f'Expected a list of reviews: {relative}')
            checksum = sha(payload)
            (Path(scratch) / checksum).write_bytes(payload)
            entries.append({'path': relative, 'sha256': checksum, 'bytes': len(payload)})
        manifest = {'schema_version': SCHEMA_VERSION, 'files': sorted(entries, key=lambda item: item['path'])}
        snapshot_id = sha(encoded(manifest))
        validate_manifest(manifest, snapshot_id)
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        for checksum in sorted({entry['sha256'] for entry in entries}):
            client.fput_object(bucket, f'raw/blobs/{checksum}.json', str(Path(scratch) / checksum),
                               content_type='application/json')
            if sha(get_bytes(client, bucket, f'raw/blobs/{checksum}.json',
                             maximum=(Path(scratch) / checksum).stat().st_size)) != checksum:
                raise ValueError('Uploaded raw object checksum mismatch.')
        put_bytes(client, bucket, f'raw/snapshots/{snapshot_id}/manifest.json', encoded(manifest))
        put_bytes(client, bucket, 'raw/latest.json', encoded({'snapshot_id': snapshot_id}))
    return {'snapshot_id': snapshot_id, 'files': len(entries), 'bucket': bucket}


def verify_cache(folder, manifest):
    raw = folder / 'raw'
    for entry in manifest['files']:
        path = raw / entry['path']
        if not path.resolve().is_relative_to(raw.resolve()) or not path.is_file():
            raise ValueError('Cached snapshot file is missing or leaves its directory.')
        if path.stat().st_size != entry['bytes'] or sha(path.read_bytes()) != entry['sha256']:
            raise ValueError(f'Cached snapshot file has changed: {entry["path"]}')
    expected = {item['path'] for item in manifest['files']}
    found = {path.relative_to(raw).as_posix() for path in raw.rglob('*') if path.is_file()}
    if expected != found:
        raise ValueError('Cached snapshot contains unlisted files.')


def download_snapshot(client, bucket, cache_dir, snapshot_id=None):
    cache = Path(cache_dir).resolve()
    if snapshot_id is None:
        pointer = json.loads(get_bytes(client, bucket, 'raw/latest.json', maximum=4096))
        snapshot_id = pointer.get('snapshot_id') if isinstance(pointer, dict) else None
    if not isinstance(snapshot_id, str) or not HASH.fullmatch(snapshot_id):
        raise ValueError('Expected a full 64-character snapshot ID.')
    manifest = json.loads(get_bytes(client, bucket, f'raw/snapshots/{snapshot_id}/manifest.json', maximum=16 * 1024 * 1024))
    validate_manifest(manifest, snapshot_id)
    snapshots = cache / 'snapshots'
    snapshots.mkdir(parents=True, exist_ok=True)
    final = snapshots / snapshot_id
    if final.exists():
        verify_cache(final, manifest)
        return {'snapshot_id': snapshot_id, 'raw_dir': str(final / 'raw'), 'files': len(manifest['files'])}
    stage = Path(tempfile.mkdtemp(prefix='.downloading-', dir=snapshots))
    try:
        for entry in manifest['files']:
            payload = get_bytes(client, bucket, f'raw/blobs/{entry["sha256"]}.json', maximum=entry['bytes'])
            if len(payload) != entry['bytes'] or sha(payload) != entry['sha256']:
                raise ValueError(f'Raw object checksum mismatch: {entry["path"]}')
            path = stage / 'raw' / entry['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (stage / 'manifest.json').write_bytes(encoded(manifest))
        stage.rename(final)
    except Exception:
        if stage.resolve().parent == snapshots and stage.name.startswith('.downloading-'):
            shutil.rmtree(stage)
        raise
    return {'snapshot_id': snapshot_id, 'raw_dir': str(final / 'raw'), 'files': len(manifest['files'])}


def process_snapshot(client, bucket, cache_dir, output_dir, *, snapshot_id=None, config_path=None, annotations=None):
    restored = download_snapshot(client, bucket, cache_dir, snapshot_id)
    result = run_pipeline(restored['raw_dir'], output_dir, config_path=config_path, annotations=annotations)
    return {'raw_snapshot': restored, 'processing': result}


def upload_processed(client, bucket, snapshot_dir):
    """Verify a frozen cleaner run, upload it, read it back, publish pointer last."""
    folder = Path(snapshot_dir).resolve()
    manifest = strict_json((folder / 'manifest.json').read_text(encoding='utf-8'))
    fingerprint = manifest.get('fingerprint')
    if not isinstance(fingerprint, str) or not HASH.fullmatch(fingerprint):
        raise ValueError('Invalid processed fingerprint.')
    verify_snapshot(folder, fingerprint)
    prefix = f'processed/snapshots/{fingerprint}'
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    # Freeze reads before upload; files could otherwise change after verification.
    payloads = {name: (folder / name).read_bytes() for name in ARTIFACTS}
    for name, payload in payloads.items():
        expected = manifest['artifacts'][name]
        if len(payload) != expected['bytes'] or sha(payload) != expected['sha256']:
            raise ValueError(f'Processed artifact changed before upload: {name}')
    payloads['manifest.json'] = encoded(manifest)
    for name, payload in payloads.items():
        key = f'{prefix}/{name}'
        content_type = ('application/x-sqlite3' if name.endswith('.sqlite') else
                        'application/x-ndjson' if name.endswith('.jsonl') else 'application/json')
        client.put_object(bucket, key, io.BytesIO(payload), len(payload), content_type=content_type)
        actual = get_bytes(client, bucket, key, maximum=len(payload))
        if len(actual) != len(payload) or sha(actual) != sha(payload):
            raise ValueError(f'Uploaded processed object checksum mismatch: {name}')
    pointer = encoded({'fingerprint': fingerprint, 'run_id': manifest['run_id'], 'prefix': prefix})
    put_bytes(client, bucket, 'processed/latest.json', pointer)
    if get_bytes(client, bucket, 'processed/latest.json', maximum=4096) != pointer:
        raise ValueError('Processed latest pointer verification failed.')
    return {'bucket': bucket, 'prefix': prefix, 'files': len(payloads), 'verified_sha256': True}


def load_env_file(path):
    """Optional explicit local .env; process environment takes precedence; no logging."""
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('Invalid .env assignment.')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key.startswith('MINIO_'):
            os.environ.setdefault(key, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Read local MINIO_* settings without printing credentials.')
    parser.add_argument('--bucket')
    actions = parser.add_subparsers(dest='action', required=True)
    upload = actions.add_parser('upload', help='Publish local crawl datasets as a complete snapshot.')
    upload.add_argument('--raw-dir', type=Path, default=ROOT / 'data' / 'raw')
    processed = actions.add_parser('upload-processed', help='Publish and verify a cleaner run under processed/.')
    processed.add_argument('--snapshot-dir', type=Path, required=True)
    for name in ('download', 'process'):
        command = actions.add_parser(name)
        command.add_argument('--cache-dir', type=Path, default=ROOT / 'data' / 'lake_cache')
        command.add_argument('--snapshot-id', help='Default: last completely published snapshot.')
        if name == 'process':
            command.add_argument('--output-dir', type=Path, default=ROOT / 'data' / 'processed')
            command.add_argument('--config', type=Path)
            command.add_argument('--annotations', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        args.bucket = args.bucket or os.environ.get('MINIO_BUCKET', 'ady-raw')
        client = make_client()
        if args.action == 'upload':
            result = upload_snapshot(client, args.bucket, args.raw_dir)
        elif args.action == 'download':
            result = download_snapshot(client, args.bucket, args.cache_dir, args.snapshot_id)
        elif args.action == 'upload-processed':
            result = upload_processed(client, args.bucket, args.snapshot_dir)
        else:
            result = process_snapshot(client, args.bucket, args.cache_dir, args.output_dir,
                snapshot_id=args.snapshot_id, config_path=args.config, annotations=args.annotations)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('processing', {}).get('rejections', 0):
            return 1
        return 0
    except Exception as exc:
        print(f'Lake operation failed ({type(exc).__name__}): {exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
