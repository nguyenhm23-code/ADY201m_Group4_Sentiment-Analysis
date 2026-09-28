"""Run the affected Maps places in the user's normal Windows session.

python tests/live_gmap_check.py --login --max-reviews 100
Outputs are isolated under data/validation/gmap; raw crawl data is preserved.
"""
import argparse
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ingestion import GGMap as gmap, gmap_pipeline as pipeline
from src.ingestion.ingestion_runtime import close_driver

PLACE_IDS = [
    '0x31752f3f52ce6b9d:0x2f7c030139c58cdb',
    '0x316f6d001b3eb75f:0xa16275953ae399a4',
    '0x3168532aa82ab9f1:0x5f471336cc2918b1',
    '0x316f6d00f63807fb:0x5f316758b50053de',
    '0x31752f3830aad8c9:0x32f1cdda07359c6',
    '0x316f6da4b58d3b59:0x38e9d47a7d905175',
    '0x31752facc18a645b:0x7b48594407b5780b',
    '0x316f6d03908b946d:0xc11760da2b7716d2',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-dir', type=Path, default=ROOT/'data/raw/gmap/.chrome_profile')
    parser.add_argument('--max-reviews', type=int, default=100)
    parser.add_argument('--login', action='store_true', help='Chờ bạn đăng nhập trong Chrome trước khi kiểm thử.')
    args = parser.parse_args()
    if args.max_reviews < 1:
        parser.error('--max-reviews phải lớn hơn 0.')
    queue_path = ROOT/'data/raw/gmap/places_queue.json'
    queue = json.loads(queue_path.read_text(encoding='utf-8'))
    missing = [key for key in PLACE_IDS if key not in queue['places']]
    if missing:
        parser.error('Thiếu quán trong hàng đợi kiểm thử: '+', '.join(missing))
    output = ROOT/'data/validation/gmap'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(), logging.FileHandler(output/'run.log', encoding='utf-8')])
    gmap.write_json(output/'places_queue.json', {'version':1, 'queries':{}, 'places':{
        key:{**queue['places'][key], 'status':'pending', 'attempts':0} for key in PLACE_IDS}})
    gmap.write_json(output/'environment.json', {'profile_dir':str(args.profile_dir.resolve()),
        'source_sha256':hashlib.sha256(Path(gmap.__file__).read_bytes()).hexdigest(),
        'requested_reviews':args.max_reviews, 'fresh_output':True})
    holder = []
    def browser(restart=False):
        if restart and holder:
            close_driver(holder.pop())
        if not holder:
            holder.append(gmap.make_driver(profile_dir=args.profile_dir))
        return holder[0]
    try:
        if args.login:
            browser().get('https://www.google.com/maps/?hl=vi')
            input('Đăng nhập Google trong Chrome vừa mở. Xong, nhấn Enter để kiểm thử 8 quán: ')
        config = {'target_restaurants':8, 'max_reviews_per_restaurant':args.max_reviews,
            'areas':[{'name':'Hồ Chí Minh'},{'name':'Bình Định'}],
            'idle_seconds':25, 'max_place_seconds':300}
        return pipeline._run_pipeline(config,output,browser,crawl_only=True)
    finally:
        if holder:
            close_driver(holder.pop())
        summary = []
        for path in output.glob('gmap_*.status.json'):
            state=json.loads(path.read_text(encoding='utf-8'))
            summary.append({'name':state.get('place',{}).get('name'),
                **{key:state.get(key) for key in ('status','count','stop_reason','error','diagnostics')}})
        gmap.write_json(output/'summary.json', {'places':summary})
        print('Kết quả và diagnostics:',output)


if __name__=='__main__':
    raise SystemExit(main())
