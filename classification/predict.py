"""Cosine inference for exported models, without any device connection."""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from .algorithms import norm_rows


def load_model(path, schema):
    path = Path(path)
    model = json.loads(path.read_text(encoding='utf-8'))
    if model['feature_schema'] != schema:
        raise ValueError('Input feature schema does not match model')
    with (path.parent / model['center_csv']).open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    dim = model['vector_dim']
    if len(rows) != model['center_count']:
        raise ValueError('Center count mismatch')
    for i, row in enumerate(rows):
        if int(row['center_id']) != i or row['operation_label'] != model['center_to_operation'][str(i)]:
            raise ValueError('Center ID/label mapping mismatch')
    centers = np.asarray([[float(r[f'v{i:04d}']) for i in range(dim)] for r in rows])
    if not np.isfinite(centers).all():
        raise ValueError('Nonfinite center')
    return model, norm_rows(centers)


def predict(vectors, model, centers):
    vectors = np.asarray(vectors, dtype=float)
    if vectors.ndim != 2 or vectors.shape[1] != model['vector_dim'] or not np.isfinite(vectors).all():
        raise ValueError('Vector dimension/finite validation failed')
    scores = norm_rows(vectors) @ centers.T
    indices = np.argmax(scores, axis=1)
    return [(model['center_to_operation'][str(int(i))], float(scores[j, i])) for j, i in enumerate(indices)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', required=True); p.add_argument('--schema', required=True)
    p.add_argument('--vectors', required=True); p.add_argument('--output', required=True)
    a = p.parse_args()
    model, centers = load_model(a.model, a.schema)
    with open(a.vectors, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    predictions = predict([[float(r[f'v{i:04d}']) for i in range(model['vector_dim'])] for r in rows], model, centers)
    with open(a.output, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f); w.writerow(['sample_id', 'operation_label', 'cosine_similarity'])
        w.writerows((r['sample_id'], label, score) for r, (label, score) in zip(rows, predictions))


if __name__ == '__main__':
    main()
