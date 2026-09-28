import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from src.modeling import model


class ModelingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'manifest.json').write_text(json.dumps({'label_map': model.LABELS, 'run_id': 'fixture'}))
        for split in ('train', 'validation', 'test'):
            rows = []
            for sentiment, label in model.LABELS.items():
                rows.append({'review_id': f'{split}-{label}', 'restaurant_id': f'{split}-{label}',
                    'split_group_id': f'{split}-{label}', 'source': 'foody', 'split': split,
                    'text': ['rất tệ dở không ngon', 'cũng bình thường', 'rất ngon tốt tuyệt vời'][label] + ' ' + split,
                    'sentiment': sentiment, 'label': label, 'label_source': 'human'})
            self.write(split, rows)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, split, rows):
        (self.root / f'{split}.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')

    def test_empty_gold_explains_annotations_needed(self):
        self.write('train', [])
        with self.assertRaisesRegex(ValueError, 'human annotations'):
            model.load_dataset(self.root)

    def test_rejects_weak_holdout_and_group_leakage(self):
        rows = model.read_rows(self.root / 'test.jsonl')
        rows[0]['label_source'] = 'rating_heuristic'
        self.write('test', rows)
        with self.assertRaisesRegex(ValueError, 'human labels'):
            model.load_dataset(self.root)
        rows[0]['label_source'] = 'human'
        rows[0]['split_group_id'] = 'train-0'
        self.write('test', rows)
        with self.assertRaisesRegex(ValueError, 'leakage'):
            model.load_dataset(self.root)

    def test_rejects_label_mismatch(self):
        rows = model.read_rows(self.root / 'test.jsonl')
        rows[0]['label'] = 2
        self.write('test', rows)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            model.load_dataset(self.root)

    @unittest.skipUnless(importlib.util.find_spec('sklearn'), 'scikit-learn not installed')
    def test_two_models_and_weak_mode_no_metrics(self):
        path, report = model.train(self.root, self.root / 'models')
        self.assertEqual(2, len(list(path.glob('*.joblib'))))
        self.assertEqual(2, len(report['validation']))
        self.assertEqual(3, report['test_selected_model']['rows'])
        rows = model.read_rows(self.root / 'train.jsonl')
        for row in rows:
            row['label_source'] = 'rating_heuristic'
        self.write('weak_train', rows)
        _, weak = model.train(self.root, self.root / 'models', weak=True)
        self.assertEqual({}, weak['validation'])
        self.assertIsNone(weak['test_selected_model'])


if __name__ == '__main__':
    unittest.main()
