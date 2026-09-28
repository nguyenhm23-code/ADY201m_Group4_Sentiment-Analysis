"""Read-only raw review counts; do not run preprocessing or change checkpoints."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import json
import hashlib
from collections import Counter
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.processing.cleaner import discover_inputs, strict_json, normalize, read_config

def main():
    raw = ROOT / 'data/raw'
    config = read_config(ROOT / 'configs/preprocessing.json')
    started = datetime.now(timezone(timedelta(hours=7))).isoformat()
    files = discover_inputs(raw)
    metrics = {s: {'files': 0, 'raw_rows': 0, 'valid_rows': 0, 'rejected_rows': 0,
                   'partial_files': 0, 'partial_rows': 0} for s in ('foody', 'gmap')}
    ids = {s: set() for s in metrics}
    restaurants = {s: set() for s in metrics}
    manifests, errors, changed = [], [], []
    for source, path in files:
        before = path.stat()
        payload = path.read_bytes()
        m = metrics[source]
        m['files'] += 1
        partial = path.name.endswith('.partial.json')
        m['partial_files'] += int(partial)
        record = {'source': source, 'path': path.relative_to(ROOT).as_posix(),
                  'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                  'mtime_ns': before.st_mtime_ns}
        try:
            rows = strict_json(payload.decode('utf-8-sig'))
            if not isinstance(rows, list):
                raise ValueError('Expected review array')
            record['raw_rows'] = len(rows)
            m['raw_rows'] += len(rows)
            if partial:
                m['partial_rows'] += len(rows)
            for index, row in enumerate(rows, 1):
                try:
                    item = normalize(row, source, str(path.relative_to(raw)), config)
                    m['valid_rows'] += 1
                    ids[source].add(item['review_id'])
                    restaurants[source].add(item['restaurant_id'])
                except (ValueError, TypeError, AttributeError) as exc:
                    m['rejected_rows'] += 1
                    errors.append({'file': record['path'], 'row': index, 'error': str(exc)})
        except (ValueError, UnicodeError) as exc:
            errors.append({'file': record['path'], 'error': str(exc)})
        manifests.append(record)
    for source, m in metrics.items():
        m['unique_reviews'] = len(ids[source])
        m['duplicate_rows'] = m['valid_rows'] - len(ids[source])
        m['restaurants_with_reviews'] = len(restaurants[source])
    for record in manifests:
        path = ROOT / record['path']
        if not path.exists() or path.stat().st_mtime_ns != record['mtime_ns']:
            changed.append(record['path'])
    if {p.relative_to(ROOT).as_posix() for _, p in discover_inputs(raw)} != {r['path'] for r in manifests}:
        changed.append('raw_file_inventory_changed')
    result = {'started_at': started,
              'finished_at': datetime.now(timezone(timedelta(hours=7))).isoformat(),
              'definition': 'Unique source-namespaced review_id using cleaner.normalize; include final and partial, exclude queue/status/place/diagnostics.',
              'by_source': metrics,
              'total_unique_reviews': sum(len(v) for v in ids.values()),
              'total_raw_rows': sum(m['raw_rows'] for m in metrics.values()),
              'errors': errors, 'files_changed_during_read': changed, 'inputs': manifests}
    (ROOT / 'reports/crawl_counts_20260928.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'inputs'}, ensure_ascii=True, indent=2))

if __name__ == '__main__':
    main()
