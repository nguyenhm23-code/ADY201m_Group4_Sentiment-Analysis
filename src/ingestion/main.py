"""Run Foody and Google Maps in two independent worker threads."""
import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .ingestion_runtime import CrawlCancelled, write_json
else:
    from ingestion_runtime import CrawlCancelled, write_json

LOG = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent if HERE.name == 'ingestion' and HERE.parent.name == 'src' else HERE


def run_jobs(jobs, stop_event, summary_path):
    """Submit all jobs before waiting; one failed source never cancels the other."""
    started = datetime.now(timezone.utc).isoformat()
    results = {}
    interrupted = False

    def worker(name, function):
        before = time.monotonic()
        LOG.info('[%s] Bắt đầu.', name)
        try:
            details = function()
            code = details.get('exit_code', 0)
            result = {'status': 'finished' if code == 0 else 'failed',
                      'exit_code': code, 'details': details}
        except CrawlCancelled as exc:
            result = {'status': 'interrupted', 'exit_code': 130, 'error': str(exc)}
        except Exception as exc:
            LOG.exception('[%s] Crawl gặp lỗi.', name)
            result = {'status': 'failed', 'exit_code': 1,
                      'error': f'{type(exc).__name__}: {exc}'}
        result['elapsed_seconds'] = round(time.monotonic() - before, 2)
        LOG.info('[%s] Kết thúc: %s.', name, result['status'])
        return result

    def save():
        write_json(summary_path, {'started_at': started,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'sources': results, 'interrupted': interrupted})

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='crawl') as pool:
        futures = {pool.submit(worker, name, fn): name for name, fn in jobs.items()}
        try:
            for future in as_completed(futures):
                results[futures[future]] = future.result()
                save()
        except KeyboardInterrupt:
            interrupted = True
            stop_event.set()
            LOG.warning('Đang dừng hai nguồn và đóng Chrome; chờ lệnh trình duyệt hiện tại kết thúc.')
            for future, name in futures.items():
                if name not in results:
                    results[name] = future.result()
            save()
        except Exception:
            stop_event.set()
            raise
    if interrupted or any(r['exit_code'] == 130 for r in results.values()):
        return 130
    return 1 if any(r['exit_code'] != 0 for r in results.values()) else 0


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Giá trị phải lớn hơn 0.')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('all', 'foody', 'gmap'), default='all')
    parser.add_argument('--gmap-config', type=Path, default=HERE / 'gmap_areas.json')
    parser.add_argument('--gmap-target', type=positive_int,
                        help='Tổng số quán Maps; mặc định theo gmap_areas.json.')
    parser.add_argument('--gmap-max-reviews', type=positive_int)
    parser.add_argument('--foody-url', action='append', help='URL khu vực Foody; có thể lặp nhiều lần.')
    parser.add_argument('--foody-target', type=positive_int, default=100,
                        help='Số quán tối đa mỗi URL khu vực Foody.')
    parser.add_argument('--output-dir', type=Path, default=PROJECT_ROOT / 'data' / 'raw')
    parser.add_argument('--gmap-profile-dir', type=Path,
                        help='Hồ sơ Maps riêng; không dùng hồ sơ Chrome đang mở.')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--chrome-major', type=positive_int, help='Ghi đè phiên bản Chrome cho cả hai nguồn.')
    parser.add_argument('--preprocess', action='store_true',
                        help='Chuẩn hóa và xuất dataset sau khi cả hai crawler kết thúc.')
    parser.add_argument('--processed-dir', type=Path)
    parser.add_argument('--preprocess-config', type=Path)
    parser.add_argument('--annotations', type=Path, help='Nhãn thủ công JSONL; dùng cùng --preprocess.')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s')
    stop_event = threading.Event()
    output = args.output_dir.resolve()

    def foody():
        if __package__:
            from .crawler import run_foody
        else:
            from crawler import run_foody
        report = run_foody(category_urls=args.foody_url, target_count=args.foody_target,
                          output_dir=output / 'foody', headless=args.headless,
                          chrome_major=args.chrome_major, stop_event=stop_event)
        return {'exit_code': 1 if report['errors'] or not report['reviews'] else 0, **report}

    def gmap():
        if __package__:
            from . import gmap_pipeline as pipeline
        else:
            import gmap_pipeline as pipeline
        config = pipeline.load_config(args.gmap_config)
        if args.gmap_target is not None:
            config['target_restaurants'] = args.gmap_target
        if args.gmap_max_reviews is not None:
            config['max_reviews_per_restaurant'] = args.gmap_max_reviews
        code = pipeline.run_pipeline(config, output / 'gmap', headless=args.headless,
            chrome_major=args.chrome_major, profile_dir=args.gmap_profile_dir or output / 'gmap' / '.chrome_profile',
            stop_event=stop_event)
        return {'exit_code': code, 'queue': str(output / 'gmap' / 'places_queue.json')}

    jobs = {}
    if args.source in ('all', 'foody'):
        jobs['foody'] = foody
    if args.source in ('all', 'gmap'):
        jobs['gmap'] = gmap
    summary = output / 'ingestion_status.json'
    code = run_jobs(jobs, stop_event, summary)
    if args.preprocess and code != 130:
        state = json.loads(summary.read_text(encoding='utf-8'))
        try:
            if __package__:
                from ..processing.cleaner import run_pipeline as preprocess
            else:
                import importlib.util
                spec = importlib.util.spec_from_file_location('training_cleaner', HERE.parent / 'processing' / 'cleaner.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                preprocess = module.run_pipeline
            result = preprocess(output, args.processed_dir or output.parent / 'processed',
                                config_path=args.preprocess_config, annotations=args.annotations)
            state['preprocessing'] = {'status': 'finished', 'output': result['output'],
                                      'normalized_rows': result['normalized_rows'],
                                      'exports': result['exports']}
            if result.get('rejections'):
                state['preprocessing']['status'] = 'partial'
                code = 1
            LOG.info('Dataset: %s', result['output'])
        except Exception as exc:
            LOG.exception('Tiền xử lý thất bại; dữ liệu crawl vẫn được giữ.')
            state['preprocessing'] = {'status': 'failed', 'error': str(exc)}
            code = 1
        write_json(summary, state)
    LOG.info('Kết quả từng nguồn: %s', summary)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
