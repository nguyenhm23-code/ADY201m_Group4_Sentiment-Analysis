"""Versioned Foody/Maps normalization, annotation import and leakage-aware splits.

Uses Python's standard library only. Raw crawler files are never modified.
"""
import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from contextlib import closing
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

VERSION = '1.2.0'
LOG = logging.getLogger(__name__)
TZ_VN = timezone(timedelta(hours=7))
LABELS = {'negative': 0, 'neutral': 1, 'positive': 2}
REVIEW_LABELS = set(LABELS) | {'mixed', 'uncertain'}
ASPECTS = {'food', 'price', 'service', 'ambience', 'location', 'delivery'}
EMPTY_TEXT = {'', 'không bình luận', 'không có bình luận', 'không có nội dung', 'none', 'null', 'nan'}
CITY_ALIASES = {'da-nang': 'Đà Nẵng', 'đà nẵng': 'Đà Nẵng',
    'ho-chi-minh-city': 'Hồ Chí Minh', 'ho-chi-minh': 'Hồ Chí Minh',
    'hồ chí minh': 'Hồ Chí Minh', 'tp. hồ chí minh': 'Hồ Chí Minh',
    'ha-noi': 'Hà Nội', 'hà nội': 'Hà Nội', 'can-tho': 'Cần Thơ',
    'cần thơ': 'Cần Thơ', 'quy-nhon': 'Quy Nhơn', 'quy nhơn': 'Quy Nhơn'}
DEFAULT_CONFIG = {'seed': 42, 'split_ratios': [0.8, 0.1, 0.1],
    'rating_scales': {'foody': 10, 'gmap': 5},
    'weak_thresholds': {'foody': {'negative_max': 4, 'positive_min': 7},
                        'gmap': {'negative_max': 2, 'positive_min': 4}},
    'near_duplicate_min_chars': 80, 'near_duplicate_similarity': 0.94,
    'near_duplicate_max_candidates': 100, 'long_duplicate_min_chars': 40,
    'restaurant_aliases': {}}
ARTIFACTS = ('reviews.jsonl', 'rejected.jsonl', 'train.jsonl', 'validation.jsonl',
    'test.jsonl', 'weak_train.jsonl', 'annotation_queue.jsonl', 'aspect_labels.jsonl',
    'reviews.sqlite', 'quality_report.json', 'foody.json', 'gmap.json', 'excluded_training.jsonl')


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def strict_json(value):
    def reject_constant(token):
        raise ValueError(f'Non-finite JSON number: {token}')
    def finite_float(token):
        result = float(token)
        if not math.isfinite(result):
            reject_constant(token)
        return result
    return json.loads(value, parse_constant=reject_constant, parse_float=finite_float)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            name = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if name:
            name.unlink(missing_ok=True)


def jsonl(path, rows):
    with Path(path).open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


def clean_text(value):
    text = unicodedata.normalize('NFC', html.unescape(str(value or '')))
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'</?(?:p|br|div|span|a|b|i|strong|em|ul|li)\b[^>]*>', ' ', text, flags=re.I)
    text = ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf') and c != '\u200d' else c for c in text)
    text = re.sub(r'\s+', ' ', text).strip()
    if text.casefold() in EMPTY_TEXT:
        return ''
    text = re.sub(r'(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b', '<EMAIL>', text)
    text = re.sub(r'(?i)\b(?:https?://|www\.)[^\s<>]+', '<URL>', text)
    text = re.sub(r'(?<!\w)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\d)', '<PHONE>', text)
    return text


def parse_number(raw, minimum, maximum):
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip().replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    value = float(text)
    return value if math.isfinite(value) and minimum <= value <= maximum else None


def parse_date(raw, iso=None):
    """Do not fabricate exact dates from relative or edited timestamps."""
    raw = str(raw or '').strip()
    flags = []
    edited = bool(re.search(r'chỉnh sửa|edited', raw, re.I))
    if edited:
        return None, 'edited_or_relative', ['edited_time_not_publication']
    for value in (iso, raw):
        if not value:
            continue
        value = str(value).strip()
        parsed = None
        precision = 'minute'
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[T ].*)?', value):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                precision = 'day' if len(value) == 10 else 'datetime'
            else:
                for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
                    try:
                        parsed = datetime.strptime(value, fmt)
                        precision = 'day' if fmt == '%d/%m/%Y' else ('second' if '%S' in fmt else 'minute')
                        break
                    except ValueError:
                        continue
        except ValueError:
            continue
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=TZ_VN)
                flags.append('timezone_assumed_Asia_Ho_Chi_Minh')
            return parsed.astimezone(TZ_VN).isoformat(), precision, flags
    relative = bool(re.search(r'trước|ago|hôm qua|hôm nay|yesterday|today', raw, re.I))
    return None, 'relative' if relative else 'unknown', flags


def canonical_url(value):
    parsed = urlsplit(str(value or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), '', ''))


def normalize_city(raw, address, source, url):
    # Foody's URL identifies the crawl region; legacy filenames/fields can be wrong.
    if source == 'foody' and url:
        slug = urlsplit(url).path.strip('/').split('/')[0]
        if slug in CITY_ALIASES:
            return CITY_ALIASES[slug], 'restaurant_url'
    candidate = str(raw or '').strip()
    if candidate and candidate.casefold() not in ('khong ro', 'không rõ', 'none', 'null'):
        # Search-area province names are not inferred to be city names.
        if candidate.casefold() in CITY_ALIASES:
            return CITY_ALIASES[candidate.casefold()], 'record'
    for token in str(address or '').split(','):
        key = re.sub(r'\s+\d{5,6}$', '', token.strip()).casefold()
        if key in CITY_ALIASES:
            return CITY_ALIASES[key], 'address'
    return None, None


def normalize(row, source, origin, config):
    if not isinstance(row, dict):
        raise ValueError('record_not_object')
    # A nested object/list must never become model text via str(value).
    for field in ('Bình Luận', 'URL Quán', 'Tên User', 'Tên Quán', 'Địa Chỉ',
                  'Thành Phố', 'Ngày Giờ', 'Ngày Đăng ISO',
                  'Thời Điểm Thu Thập', 'Thiết Bị'):
        if row.get(field) is not None and not isinstance(row[field], str):
            raise ValueError(f'invalid_field_type:{field}:expected_string_or_null')
    for field in ('ID Quán', 'ID Review'):
        if row.get(field) is not None and type(row[field]) not in (str, int):
            raise ValueError(f'invalid_field_type:{field}:expected_string_or_integer')
    for field in ('Điểm Đánh Giá', 'Điểm Trung Bình Quán', 'Thang Điểm'):
        if row.get(field) is not None and type(row[field]) not in (str, int, float, bool):
            raise ValueError(f'invalid_field_type:{field}:expected_rating_scalar')
    search_area = row.get('Khu Vực Tìm Kiếm')
    if isinstance(search_area, str):
        search_area = [search_area]
    if search_area is None:
        search_area = []
    if not isinstance(search_area, list) or any(not isinstance(area, str) for area in search_area):
        raise ValueError('invalid_field_type:Khu Vực Tìm Kiếm:expected_string_or_string_list')
    search_area = sorted({area.strip() for area in search_area if area.strip()})
    flags = []
    url = canonical_url(row.get('URL Quán'))
    native_place = str(row.get('ID Quán') or '').strip()
    if not native_place and not url:
        raise ValueError('missing_restaurant_identity')
    if source == 'foody' and url:
        place_key = url  # Slugs alone can collide across cities.
    else:
        place_key = native_place or url
    restaurant_id = source + ':' + digest(place_key)[:24]
    raw = str(row.get('Bình Luận') or '')
    cleaned = clean_text(raw)
    if not cleaned:
        flags.append('empty_or_placeholder_text')
    if cleaned != re.sub(r'\s+', ' ', unicodedata.normalize('NFC', raw)).strip():
        flags.append('text_normalized_or_redacted')
    signal = re.sub(r'<(?:PHONE|EMAIL|URL)>', ' ', cleaned)
    if not re.search(r'[\w\U0001F300-\U0001FAFF]', signal):
        flags.append('no_text_signal')
    scale = config['rating_scales'][source]
    supplied_scale = row.get('Thang Điểm')
    rating = parse_number(row.get('Điểm Đánh Giá'), 1 if source == 'gmap' else 0, scale)
    if supplied_scale is not None and supplied_scale != scale:
        flags.append('unexpected_rating_scale')
        rating = None
    if source == 'gmap' and rating is not None and not rating.is_integer():
        flags.append('non_integer_google_review_rating')
        rating = None
    if rating is None:
        flags.append('missing_or_invalid_rating')
    native_review = row.get('ID Review')
    if native_review:
        review_key = ['native', str(native_review)]
    else:
        review_key = ['fallback', row.get('Tên User'), row.get('Ngày Giờ'),
                      re.sub(r'\s+', ' ', unicodedata.normalize('NFC', raw)).strip()]
        flags.append('review_id_is_fallback')
    review_id = source + ':' + digest([restaurant_id, review_key])[:32]
    published, precision, date_flags = parse_date(row.get('Ngày Giờ'), row.get('Ngày Đăng ISO'))
    flags.extend(date_flags)
    collected, _, _ = parse_date(row.get('Thời Điểm Thu Thập'))
    city, city_source = normalize_city(row.get('Thành Phố'), row.get('Địa Chỉ'), source, url)
    if city is None:
        flags.append('city_unknown')
    if source == 'foody':
        raw_city = CITY_ALIASES.get(str(row.get('Thành Phố') or '').strip().casefold())
        if raw_city and city and raw_city != city:
            flags.append('record_city_mismatch')
        filename_city = Path(origin).name.removeprefix('foody_').removesuffix('_dataset.json')
        if filename_city in CITY_ALIASES and city and CITY_ALIASES[filename_city] != city:
            flags.append('filename_city_mismatch')
    weak = None
    if rating is not None:
        bounds = config['weak_thresholds'][source]
        weak = ('negative' if rating <= bounds['negative_max'] else
                'positive' if rating >= bounds['positive_min'] else 'neutral')
    vietnamese = bool(re.search(r'[ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]', cleaned))
    return {'schema_version': VERSION, 'review_id': review_id,
        'source': source, 'native_review_id': str(native_review) if native_review else None,
        'restaurant_id': restaurant_id, 'native_restaurant_id': native_place or None,
        'restaurant_url': str(row.get('URL Quán') or '') or None,
        'restaurant_name': row.get('Tên Quán'), 'address': row.get('Địa Chỉ'),
        'city': city, 'city_raw': row.get('Thành Phố'), 'city_source': city_source,
        'search_area': search_area,
        'text_raw': raw, 'text_clean': cleaned, 'text_sha256': digest(cleaned),
        'language_hint': 'vi' if vietnamese else 'und',
        'rating': rating, 'rating_raw': row.get('Điểm Đánh Giá'), 'rating_scale': scale,
        'rating_fraction': rating / scale if rating is not None else None,
        'restaurant_rating': parse_number(row.get('Điểm Trung Bình Quán'), 0, scale),
        'published_at': published, 'published_at_raw': row.get('Ngày Giờ'),
        'date_precision': precision, 'collected_at': collected,
        'device': row.get('Thiết Bị'), 'weak_sentiment': weak,
        'sentiment': None, 'label_source': None, 'aspects': [],
        'training_eligible': bool(cleaned) and 'no_text_signal' not in flags,
        'quality_flags': sorted(set(flags)), 'provenance': [origin]}


def read_config(path=None):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        override = strict_json(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(override, dict):
            raise ValueError('Configuration must be a JSON object.')
        unknown = set(override) - set(config)
        if unknown:
            raise ValueError(f'Unknown config keys: {sorted(unknown)}')
        config.update(override)
    if type(config['seed']) is not int:
        raise ValueError('seed must be an integer.')
    ratios = config['split_ratios']
    if not isinstance(ratios, list) or len(ratios) != 3 or any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in ratios) or not math.isclose(sum(ratios), 1):
        raise ValueError('split_ratios must be three positive numbers summing to 1.')
    if config['rating_scales'] != {'foody': 10, 'gmap': 5}:
        raise ValueError('Expected native rating scales: Foody 10, Google Maps 5.')
    thresholds = config['weak_thresholds']
    if not isinstance(thresholds, dict) or set(thresholds) != {'foody', 'gmap'}:
        raise ValueError('weak_thresholds must define foody and gmap.')
    for source in ('foody', 'gmap'):
        scale = config['rating_scales'][source]
        bounds = thresholds[source]
        if (not isinstance(bounds, dict) or set(bounds) != {'negative_max', 'positive_min'}
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in bounds.values())
            or not 0 <= bounds['negative_max'] < bounds['positive_min'] <= scale):
            raise ValueError('Invalid weak sentiment thresholds.')
    if type(config['near_duplicate_similarity']) not in (float, int) or not .8 <= config['near_duplicate_similarity'] <= 1:
        raise ValueError('near_duplicate_similarity must be between .8 and 1.')
    for key in ('near_duplicate_min_chars', 'near_duplicate_max_candidates', 'long_duplicate_min_chars'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f'{key} must be a positive integer.')
    if not isinstance(config['restaurant_aliases'], dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not k or not v
        for k, v in config['restaurant_aliases'].items()):
        raise ValueError('restaurant_aliases must map restaurant_id to a verified common entity ID.')
    return config


def discover_inputs(raw_dir):
    raw = Path(raw_dir).resolve()
    found = []
    for source, pattern in [('foody', '*dataset.json'), ('gmap', 'gmap_*.json')]:
        for path in sorted((raw / source).glob(pattern)):
            if path.name.endswith(('.status.json', '.place.json')):
                continue
            if path.resolve().is_relative_to(raw):
                found.append((source, path))
    # Existing Foody code wrote datasets beside the script/project root.
    for path in sorted(raw.glob('foody_*dataset.json')):
        if path.resolve().is_relative_to(raw):
            found.append(('foody', path))
    return found


def ingest(raw_dir, config):
    records, rejected, manifest = {}, [], []
    stats = Counter()
    for source, path in discover_inputs(raw_dir):
        payload = path.read_bytes()
        origin = str(path.relative_to(Path(raw_dir).resolve()))
        manifest.append({'path': origin, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)})
        try:
            data = strict_json(payload.decode('utf-8-sig'))
            if not isinstance(data, list):
                raise ValueError('Expected a JSON list of reviews.')
        except (ValueError, UnicodeError) as exc:
            rejected.append({'file': origin, 'row': None, 'reason': str(exc)})
            stats['invalid_files'] += 1
            continue
        for index, item in enumerate(data, 1):
            stats['input_rows'] += 1
            try:
                row = normalize(item, source, origin, config)
            except (ValueError, TypeError, AttributeError) as exc:
                rejected.append({'file': origin, 'row': index, 'reason': str(exc)})
                stats['rejected_rows'] += 1
                continue
            previous = records.get(row['review_id'])
            if previous:
                stats['duplicate_ids_removed'] += 1
                # Prefer later observed revisions, then the more complete text.
                rank = lambda r: (datetime.fromisoformat(r['collected_at']).timestamp() if r['collected_at'] else float('-inf'), len(r['text_clean']),
                                  not r['provenance'][0].endswith('.partial.json'))
                chosen = row if rank(row) > rank(previous) else previous
                chosen['provenance'] = sorted(set(previous['provenance'] + row['provenance']))
                chosen['quality_flags'] = sorted(set(previous['quality_flags'] + row['quality_flags']))
                if previous['text_sha256'] != row['text_sha256']:
                    chosen['quality_flags'].append('review_revision_conflict')
                records[row['review_id']] = chosen
            else:
                records[row['review_id']] = row
    return sorted(records.values(), key=lambda r: r['review_id']), rejected, manifest, stats


class DisjointSet:
    def __init__(self):
        self.parents = {}
    def find(self, key):
        self.parents.setdefault(key, key)
        root = key
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[key] != key:
            parent = self.parents[key]
            self.parents[key] = root
            key = parent
        return root
    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parents[max(a, b)] = min(a, b)


def group_and_split(rows, config):
    """Restaurant holdout + long duplicate-text components; no learned transform."""
    groups = DisjointSet()
    for restaurant, entity in config['restaurant_aliases'].items():
        groups.union(restaurant, 'entity:' + digest(entity)[:24])
    exact, same_restaurant, blocks = {}, {}, defaultdict(list)
    comparisons = 0
    for row in rows:
        restaurant = row['restaurant_id']
        groups.find(restaurant)
        text = row['text_clean'].casefold()
        row['duplicate_of'] = None
        if len(text) < config['long_duplicate_min_chars']:
            continue  # Common short opinions must not connect every restaurant.
        if text in exact:
            groups.union(restaurant, exact[text]['restaurant_id'])
            row['quality_flags'].append('exact_text_duplicate')
        else:
            exact[text] = row
        key = (restaurant, text)
        if key in same_restaurant:
            row['duplicate_of'] = same_restaurant[key]
            row['training_eligible'] = False
        else:
            same_restaurant[key] = row['review_id']
        if len(text) < config['near_duplicate_min_chars']:
            continue
        words = text.split()
        shingles = {' '.join(words[i:i+3]) for i in range(max(0, len(words)-2))}
        keys = sorted(digest(s)[:12] for s in shingles)[:5]
        candidates = {i for key in keys for i in blocks[key]}
        if len(candidates) > config['near_duplicate_max_candidates']:
            row['quality_flags'].append('near_duplicate_candidate_limit')
        for index in sorted(candidates)[:config['near_duplicate_max_candidates']]:
            other = rows[index]
            other_text = other['text_clean'].casefold()
            if min(len(text), len(other_text)) / max(len(text), len(other_text)) < config['near_duplicate_similarity']:
                continue
            comparisons += 1
            if SequenceMatcher(None, text, other_text, autojunk=False).ratio() >= config['near_duplicate_similarity']:
                groups.union(restaurant, other['restaurant_id'])
                row['quality_flags'].append('near_text_duplicate')
        for key in keys:
            blocks[key].append(row['_index'])
    for row in rows:
        group_id = groups.find(row['restaurant_id'])
        value = int(digest([config['seed'], group_id])[:16], 16) / 2**64
        train, validation, _ = config['split_ratios']
        row['split_group_id'] = group_id
        row['split'] = 'train' if value < train else 'validation' if value < train + validation else 'test'
        row.pop('_index', None)
        row['quality_flags'] = sorted(set(row['quality_flags']))
    return comparisons


def import_annotations(rows, path, *, payload=None):
    if not path:
        return []
    by_id = {r['review_id']: r for r in rows}
    seen, errors = set(), []
    if payload is None:
        payload = Path(path).read_bytes()
    lines = payload.decode('utf-8-sig').splitlines()
    staged = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = strict_json(line)
            if not item.get('sentiment') and not item.get('aspects'):
                continue  # Untouched annotation template.
            key = item['review_id']
            if key in seen:
                raise ValueError('Duplicate annotation ID.')
            seen.add(key)
            row = by_id.get(key)
            if row is None:
                raise ValueError('Unknown review_id.')
            if item.get('text_sha256') != row['text_sha256']:
                raise ValueError('Text changed or missing text_sha256; annotate current revision.')
            sentiment = item.get('sentiment')
            if sentiment is not None and sentiment not in REVIEW_LABELS:
                raise ValueError('Invalid sentiment.')
            aspects = item.get('aspects') or []
            if not isinstance(aspects, list):
                raise ValueError('aspects must be a list.')
            seen_aspects = set()
            for aspect in aspects:
                if aspect.get('aspect') not in ASPECTS or aspect.get('sentiment') not in REVIEW_LABELS:
                    raise ValueError('Invalid aspect annotation.')
                if aspect['aspect'] in seen_aspects:
                    raise ValueError('Duplicate aspect annotation.')
                seen_aspects.add(aspect['aspect'])
            if not isinstance(item.get('annotator'), str) or not item['annotator'].strip():
                raise ValueError('annotator is required for a human annotation.')
            staged.append((row, item))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append({'line': line_no, 'reason': str(exc)})
    if errors:
        raise ValueError('Invalid annotations: ' + json.dumps(errors, ensure_ascii=False))
    for row, item in staged:
        row['sentiment'] = item.get('sentiment')
        row['aspects'] = item.get('aspects') or []
        row['label_source'] = 'human' if row['sentiment'] else None
        row['annotator'] = item['annotator']
    return errors


def training_exports(rows):
    outputs = {key: [] for key in ('train', 'validation', 'test', 'weak_train', 'annotation_queue', 'aspect_labels')}
    for row in rows:
        if not row['training_eligible']:
            continue
        base = {'review_id': row['review_id'], 'restaurant_id': row['restaurant_id'],
                'split_group_id': row['split_group_id'], 'source': row['source'],
                'text': row['text_clean'], 'split': row['split']}
        if row['sentiment'] in LABELS:
            outputs[row['split']].append({**base, 'label': LABELS[row['sentiment']],
                'sentiment': row['sentiment'], 'label_source': 'human'})
        elif row['sentiment'] is None:
            outputs['annotation_queue'].append({'review_id': row['review_id'],
                'text_sha256': row['text_sha256'], 'text': row['text_clean'],
                'split': row['split'], 'sentiment': None, 'aspects': row['aspects'],
                'annotator': row.get('annotator')})
            if row['split'] == 'train' and row['weak_sentiment']:
                outputs['weak_train'].append({**base, 'label': LABELS[row['weak_sentiment']],
                    'sentiment': row['weak_sentiment'], 'label_source': 'rating_heuristic'})
        if row['aspects']:
            outputs['aspect_labels'].append({**base, 'aspects': row['aspects'], 'label_source': 'human'})
    return outputs


def write_database(path, rows, rejected):
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY, source TEXT NOT NULL, restaurant_id TEXT NOT NULL,
                city TEXT, text_clean TEXT NOT NULL, rating REAL, rating_scale INTEGER,
                published_at TEXT, date_precision TEXT, sentiment TEXT, weak_sentiment TEXT,
                label_source TEXT, split TEXT, split_group_id TEXT, training_eligible INTEGER,
                record_json TEXT NOT NULL);
            CREATE INDEX idx_restaurant ON reviews(restaurant_id);
            CREATE INDEX idx_split ON reviews(split, label_source);
            CREATE TABLE rejected (file TEXT, row_number INTEGER, reason TEXT);
        ''')
        for row in rows:
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                row['review_id'], row['source'], row['restaurant_id'], row['city'], row['text_clean'],
                row['rating'], row['rating_scale'], row['published_at'], row['date_precision'],
                row['sentiment'], row['weak_sentiment'], row['label_source'], row['split'],
                row['split_group_id'], int(row['training_eligible']), json.dumps(row, ensure_ascii=False)))
        db.executemany('INSERT INTO rejected VALUES (?,?,?)', [(r['file'], r['row'], r['reason']) for r in rejected])
        # Monthly observations only; unknown/relative times are not fabricated.
        db.executescript('''
            CREATE VIEW monthly_rating_observations AS
            SELECT source, city, substr(published_at,1,7) AS month,
                   COUNT(*) AS review_count, AVG(rating) AS average_rating, MAX(rating_scale) AS rating_scale
            FROM reviews WHERE published_at IS NOT NULL AND rating IS NOT NULL
            GROUP BY source, city, substr(published_at,1,7);
        ''')
        db.commit()


def summarize(rows, rejected, stats, exports, comparisons):
    flags = Counter(flag for row in rows for flag in row['quality_flags'])
    report = {'pipeline_version': VERSION, **dict(stats), 'normalized_rows': len(rows),
        'row_reconciliation_ok': stats['input_rows'] == len(rows) + stats['duplicate_ids_removed'] + stats['rejected_rows'],
        'restaurants': len({r['restaurant_id'] for r in rows}),
        'by_source': dict(Counter(r['source'] for r in rows)),
        'by_city': dict(Counter(r['city'] or 'unknown' for r in rows)),
        'by_split': dict(Counter(r['split'] for r in rows)),
        'split_groups': {s: len({r['split_group_id'] for r in rows if r['split'] == s})
                         for s in ('train', 'validation', 'test')},
        'quality_flags': dict(flags), 'eligible_text_rows': sum(r['training_eligible'] for r in rows),
        'exact_or_calendar_dates': sum(r['published_at'] is not None for r in rows),
        'weak_class_counts': dict(Counter(r['weak_sentiment'] or 'unlabeled' for r in rows if r['training_eligible'])),
        'exports': {name: len(data) for name, data in exports.items()},
        'near_duplicate_comparisons': comparisons, 'rejections': len(rejected), 'warnings': []}
    report['split_by_source'] = {split: dict(Counter(r['source'] for r in rows if r['split'] == split))
                               for split in ('train', 'validation', 'test')}
    for split in ('train', 'validation', 'test'):
        missing = set(LABELS) - {r['sentiment'] for r in exports[split]}
        if missing:
            report['warnings'].append(f'{split}: human labels missing classes {sorted(missing)}.')
        absent_sources = {r['source'] for r in rows} - set(report['split_by_source'][split])
        if absent_sources:
            report['warnings'].append(f'{split}: no restaurants from sources {sorted(absent_sources)}; collect more independent restaurants.')
    if flags['city_unknown']:
        report['warnings'].append('Unknown city is kept null; search region was not substituted.')
    if any(r['published_at'] is None for r in rows):
        report['warnings'].append('Relative/unknown dates excluded from monthly time view.')
    if rejected:
        report['warnings'].append('Some input files/rows were rejected; inspect rejected.jsonl.')
    report['warnings'].append('Weak labels are rating heuristics, not ground truth; do not evaluate on weak labels.')
    report['warnings'].append('Near-duplicate grouping is approximate; common short phrases are not merged across restaurants.')
    report['warnings'].append('Cross-source restaurant matching is not automatic; restaurant IDs are namespaced by source.')
    return report


def run_pipeline(raw_dir, output_dir, *, config_path=None, annotations=None):
    raw_dir, output_dir = Path(raw_dir).resolve(), Path(output_dir).resolve()
    if output_dir == raw_dir or raw_dir.is_relative_to(output_dir) or output_dir.is_relative_to(raw_dir):
        raise ValueError('raw and processed directories must be separate, non-nested directories.')
    config = read_config(config_path)
    rows, rejected, manifest, stats = ingest(raw_dir, config)
    if not rows:
        raise ValueError('No valid review records found. Existing processed snapshots were not changed.')
    for index, row in enumerate(rows):
        row['_index'] = index
    comparisons = group_and_split(rows, config)
    annotation_payload = Path(annotations).read_bytes() if annotations else None
    import_annotations(rows, annotations, payload=annotation_payload)
    exports = training_exports(rows)
    report = summarize(rows, rejected, stats, exports, comparisons)
    annotation_hash = hashlib.sha256(annotation_payload).hexdigest() if annotations else None
    implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    fingerprint = digest({'version': VERSION, 'implementation': implementation_hash,
                          'inputs': manifest, 'config': config, 'annotations': annotation_hash})
    run_id = fingerprint[:16]
    runs = output_dir / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    final = runs / run_id
    if not final.exists():
        stage = Path(tempfile.mkdtemp(prefix='.building-', dir=runs))
        try:
            jsonl(stage / 'reviews.jsonl', rows)
            for source in ('foody', 'gmap'):
                atomic_json(stage / f'{source}.json', [row for row in rows if row['source'] == source])
            jsonl(stage / 'excluded_training.jsonl', [row for row in rows if not row['training_eligible']])
            jsonl(stage / 'rejected.jsonl', rejected)
            for name, records in exports.items():
                jsonl(stage / f'{name}.jsonl', records)
            write_database(stage / 'reviews.sqlite', rows, rejected)
            atomic_json(stage / 'quality_report.json', report)
            atomic_json(stage / 'manifest.json', {'run_id': run_id, 'fingerprint': fingerprint,
                'pipeline_version': VERSION, 'config': config, 'inputs': manifest,
                'implementation_sha256': implementation_hash,
                'annotation_sha256': annotation_hash, 'label_map': LABELS,
                'artifacts': {name: file_signature(stage / name) for name in ARTIFACTS},
                'split_strategy': 'restaurant_and_long_duplicate_components_hash',
                'created_at': datetime.now(timezone.utc).isoformat()})
            stage.rename(final)
        except Exception:
            # Only remove the checked temporary directory created by this call.
            if stage.resolve().parent == runs and stage.name.startswith('.building-'):
                shutil.rmtree(stage)
            raise
    verify_snapshot(final, fingerprint)
    atomic_json(output_dir / 'latest.json', {'run_id': run_id, 'path': str(final),
        'quality_report': str(final / 'quality_report.json')})
    return {'run_id': run_id, 'output': str(final), **report}


def file_signature(path):
    checksum = hashlib.sha256()
    count = 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
            count += len(block)
    return {'sha256': checksum.hexdigest(), 'bytes': count}


def verify_snapshot(folder, fingerprint):
    """Do not silently reuse a snapshot changed by a user, crash or sync tool."""
    try:
        manifest = strict_json((folder / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('fingerprint') != fingerprint or set(manifest.get('artifacts', {})) != set(ARTIFACTS):
            raise ValueError('Manifest fingerprint or artifact list changed.')
        for name in ARTIFACTS:
            if (folder / name).is_symlink() or file_signature(folder / name) != manifest['artifacts'][name]:
                raise ValueError(f'Artifact changed: {name}')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f'Snapshot integrity check failed at {folder}: {exc}. '
                         'Restore the original snapshot or use a separate output directory.') from exc


def main(argv=None):
    folder = Path(__file__).resolve().parent
    root = folder.parent.parent if folder.name == 'processing' and folder.parent.name == 'src' else folder
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir', type=Path, default=root / 'data' / 'raw')
    parser.add_argument('--output-dir', type=Path, default=root / 'data' / 'processed')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--annotations', type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        result = run_pipeline(args.raw_dir, args.output_dir, config_path=args.config, annotations=args.annotations)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('rejections') else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
