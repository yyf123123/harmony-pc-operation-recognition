"""Evaluate external VMA CSVs and export centers with training-only mappings."""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from . import algorithms
from .data import load


def export(directory, app, schema, method, centers, mapping, labels):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'centers.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['center_id', 'operation_label'] + [f'v{i:04d}' for i in range(centers.shape[1])])
        for i, center in enumerate(centers):
            w.writerow([i, labels[int(mapping[i])], *[f'{x:.12g}' for x in center]])
    model = {'schema_version': 'cross_app.vma-cluster-model.v1', 'app_id': app, 'method': method,
             'feature_schema': schema, 'vector_dim': centers.shape[1], 'center_count': len(centers),
             'center_csv': 'centers.csv', 'center_to_operation': {str(i): labels[int(mapping[i])] for i in range(len(mapping))},
             'threshold': None, 'fit_scope': 'all valid data after evaluation; not an independently tested model'}
    (directory / 'model.json').write_text(json.dumps(model, indent=2) + '\n', encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', required=True, choices=['bilibili', 'huawei_browser', 'wps'])
    p.add_argument('--data', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--schema', required=True, choices=['wps.vma.feature.v1', 'cross_app.vma.feature.v2', 'cross_app.vma.feature.v3_signed'])
    p.add_argument('--group', choices=['trial', 'session'], default='trial')
    p.add_argument('--folds', type=int, default=5, help='0 means leave-one-group-out')
    p.add_argument('--seed', type=int, default=20261003)
    a = p.parse_args()
    if (a.app == 'wps') != (a.schema == 'wps.vma.feature.v1'):
        p.error('WPS uses its frozen feature schema; cross-app apps use v2 or signed v3')
    records, vectors, excluded = load(a.data, a.schema)
    labels = sorted({r['label'] for r in records})
    y = np.asarray([labels.index(r['label']) for r in records])
    key = a.group + '_id'
    groups = np.asarray([r[key] for r in records])
    unique = sorted(set(groups))
    if a.folds == 0:
        folds = [[g] for g in unique]
    else:
        if a.folds < 2 or len(unique) < a.folds:
            p.error('Insufficient groups for requested folds')
        grouping_records = [{**r, 'trial_id': r[key]} for r in records]
        folds = algorithms.make_folds(grouping_records, labels, a.folds, a.seed)
    if len(folds) < 2:
        p.error('Need at least two groups')
    result = {'app_id': a.app, 'feature_schema': a.schema, 'sample_count': len(records),
              'grouping': key, 'seed': a.seed, 'fold_groups': folds, 'excluded': excluded,
              'random_uniform_accuracy': 1 / len(labels), 'methods': {},
              'scope': 'offline known-application operation classification; no device switching validation'}
    a.output.mkdir(parents=True, exist_ok=True)
    for method in ('class_centroid_cosine', 'spherical_kmeans'):
        predictions = np.full(len(y), -1)
        fold_scores, rows = [], []
        for number, heldout in enumerate(folds, 1):
            test = np.isin(groups, heldout); train = ~test
            if len(set(y[train])) != len(labels):
                raise ValueError('A training fold lacks an operation class')
            centers, mapping = algorithms.fit_model(vectors[train], y[train], labels, method, a.seed + number * 997)
            clusters = np.argmax(vectors[test] @ centers.T, axis=1)
            pred = mapping[clusters]; predictions[test] = pred
            score = algorithms.metrics(y[test], pred, labels)
            score.update({'fold': number, 'test_samples': int(test.sum()), 'train_samples': int(train.sum()),
                          'test_groups': heldout, 'mapping_train_only': {str(i): labels[int(mapping[i])] for i in range(len(mapping))}})
            fold_scores.append(score)
            for idx, cluster, output in zip(np.flatnonzero(test), clusters, pred):
                rows.append({'sample_id': records[idx]['sample_id'], 'trial_id': records[idx]['trial_id'],
                             'session_id': records[idx]['session_id'], 'fold': number, 'true_label': labels[y[idx]],
                             'predicted_label': labels[int(output)], 'cluster_id': int(cluster)})
        result['methods'][method] = {'pooled': algorithms.metrics(y, predictions, labels), 'folds': fold_scores}
        target = a.output / method
        centers, mapping = algorithms.fit_model(vectors, y, labels, method, a.seed)
        export(target, a.app, a.schema, method, centers, mapping, labels)
        with (target / 'predictions.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (a.output / 'evaluation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({m: r['pooled']['accuracy'] for m, r in result['methods'].items()}, indent=2))


if __name__ == '__main__':
    main()
