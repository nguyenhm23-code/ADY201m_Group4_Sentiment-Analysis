"""Train two text-only baselines on a frozen preprocessing snapshot.

Default: compare on human validation labels, evaluate selected model on human test.
--weak-baseline: train using rating labels; never report evaluation metrics.
"""
import argparse
import hashlib
import json
import logging
import shutil
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LABELS = {'negative': 0, 'neutral': 1, 'positive': 2}
ROOT = Path(__file__).resolve().parents[2]


def read_rows(path):
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f'{path.name}:{number}: expected object.')
        for key in ('review_id', 'restaurant_id', 'split_group_id', 'text', 'source'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f'{path.name}:{number}: missing {key}.')
        if type(item.get('label')) is not int or item['label'] not in LABELS.values():
            raise ValueError(f'{path.name}:{number}: label must be 0, 1 or 2.')
        if LABELS.get(item.get('sentiment')) != item['label']:
            raise ValueError(f'{path.name}:{number}: sentiment/label mismatch.')
        rows.append(item)
    return rows


def load_dataset(snapshot, weak=False):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('label_map') != LABELS:
        raise ValueError('Snapshot label_map does not match supported classes.')
    names = ('weak_train',) if weak else ('train', 'validation', 'test')
    data, seen_ids, seen_groups, seen_restaurants, seen_texts = {}, set(), {}, {}, {}
    for name in names:
        rows = read_rows(snapshot / f'{name}.jsonl')
        expected_split = 'train' if name == 'weak_train' else name
        for row in rows:
            required_source = 'rating_heuristic' if weak else 'human'
            if row.get('label_source') != required_source or row.get('split') != expected_split:
                raise ValueError(f'{name}: incompatible label_source or split; holdout must use human labels.')
            if row['review_id'] in seen_ids:
                raise ValueError('Duplicate review_id across dataset exports.')
            seen_ids.add(row['review_id'])
            for key, index in ((row['split_group_id'], seen_groups), (row['restaurant_id'], seen_restaurants)):
                if key in index and index[key] != expected_split:
                    raise ValueError('Restaurant/group leakage across splits.')
                index[key] = expected_split
            text_key = row['text'].casefold().strip()
            if len(text_key) >= 40:
                if text_key in seen_texts and seen_texts[text_key] != expected_split:
                    raise ValueError('Long duplicate text crosses splits; rebuild the dataset.')
                seen_texts[text_key] = expected_split
        if not rows:
            raise ValueError(f'{name}.jsonl is empty. Import human annotations first; --weak-baseline only trains a provisional model.')
        classes = {r['label'] for r in rows}
        if weak:
            if len(classes) < 2:
                raise ValueError('Weak training needs at least two observed classes.')
        elif classes != set(LABELS.values()):
            raise ValueError(f'{name}: all three human sentiment classes are required; missing {set(LABELS.values()) - classes}.')
        data[expected_split] = rows
    return data, manifest


def build_models(seed):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.svm import LinearSVC
    # Character n-grams support Vietnamese spelling/emoji without language-specific tokenizers.
    def pipeline(classifier):
        return Pipeline([('tfidf', TfidfVectorizer(analyzer='char', ngram_range=(2, 5),
            min_df=1, max_features=100000, sublinear_tf=True)), ('classifier', classifier)])
    return {
        'logistic_regression': pipeline(LogisticRegression(C=1, class_weight='balanced', max_iter=2000, random_state=seed)),
        'linear_svm': pipeline(LinearSVC(C=1, class_weight='balanced', max_iter=5000, random_state=seed)),
    }


def evaluate(model, rows):
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
    actual = [r['label'] for r in rows]
    predicted = model.predict([r['text'] for r in rows])
    return {'rows': len(rows), 'accuracy': float(accuracy_score(actual, predicted)),
        'macro_f1': float(f1_score(actual, predicted, labels=[0, 1, 2], average='macro', zero_division=0)),
        'confusion_matrix': confusion_matrix(actual, predicted, labels=[0, 1, 2]).tolist(),
        'classification_report': classification_report(actual, predicted, labels=[0, 1, 2],
            target_names=list(LABELS), output_dict=True, zero_division=0)}


def train(snapshot, output_dir, *, weak=False, seed=42):
    data, manifest = load_dataset(snapshot, weak)
    import joblib
    import sklearn
    models = build_models(seed)
    x = [r['text'] for r in data['train']]
    y = [r['label'] for r in data['train']]
    report = {'dataset_run_id': manifest.get('run_id'), 'dataset_fingerprint': manifest.get('fingerprint'),
        'mode': 'weak_baseline_not_evaluated' if weak else 'human_holdout',
        'seed': seed, 'sklearn_version': sklearn.__version__, 'label_map': LABELS,
        'training_rows': len(x), 'training_class_counts': dict(Counter(y)),
        'input_features': ['text'], 'validation': {}, 'test_selected_model': None,
        'notes': ['No claim of generalization from weak labels.'] if weak else [
            'Models use fixed parameters; selection uses validation macro-F1 only.',
            'Test is evaluated only for the selected model; do not repeatedly tune against it.',
            'Three classes present is a minimal check, not proof of sufficient sample size or representative coverage.']}
    for name, model in models.items():
        model.fit(x, y)
        if not weak:
            report['validation'][name] = evaluate(model, data['validation'])
    if not weak:
        selected = max(sorted(models), key=lambda name: report['validation'][name]['macro_f1'])
        report['selected_model'] = selected
        report['test_selected_model'] = evaluate(models[selected], data['test'])
        report['test_by_source'] = {source: evaluate(models[selected], [r for r in data['test'] if r['source'] == source])
                                    for source in sorted({r['source'] for r in data['test']})}
    report['dataset_files_sha256'] = {name: hashlib.sha256((Path(snapshot) / name).read_bytes()).hexdigest()
                                     for name in (['weak_train.jsonl'] if weak else ['train.jsonl', 'validation.jsonl', 'test.jsonl'])}
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    stage = Path(tempfile.mkdtemp(prefix='.building-', dir=output_dir))
    final = output_dir / run_id
    try:
        for name, model in models.items():
            joblib.dump(model, stage / f'{name}.joblib')
        (stage / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        stage.rename(final)
    except Exception:
        if stage.resolve().parent == output_dir and stage.name.startswith('.building-'):
            shutil.rmtree(stage)
        raise
    return final, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, help='Frozen directory data/processed/runs/<run_id>.')
    parser.add_argument('--processed-dir', type=Path, default=ROOT / 'data/processed')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data/models')
    parser.add_argument('--weak-baseline', action='store_true')
    args = parser.parse_args(argv)
    try:
        snapshot = args.snapshot
        if snapshot is None:
            pointer = json.loads((args.processed_dir / 'latest.json').read_text(encoding='utf-8'))
            run_id = pointer['run_id']
            if not isinstance(run_id, str) or not run_id.isalnum():
                raise ValueError('Invalid snapshot run_id.')
            snapshot = args.processed_dir / 'runs' / run_id
        path, report = train(snapshot, args.output_dir, weak=args.weak_baseline)
        print(json.dumps({'output': str(path), 'mode': report['mode'], 'training_rows': report['training_rows']}, indent=2))
        return 0
    except ImportError as exc:
        logging.error('Missing modeling dependency: %s. Install requirements.txt.', exc)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        logging.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
