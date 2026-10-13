"""Strict external CSV loading; samples never live in this repository."""
import csv
from pathlib import Path
import numpy as np
from features.feature_schema import schema_for


def read(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def keyed(rows, name):
    ids = [r['sample_id'] for r in rows]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError(f'{name}: duplicate or missing sample IDs')
    return dict(zip(ids, rows))


def load(directory, schema):
    directory = Path(directory)
    dim = schema_for(schema).vector_dim
    _, manifest = read(directory / 'dataset_manifest.csv')
    _, labels = read(directory / 'labels.csv')
    fields, vectors = read(directory / 'vma_vectors_l2.csv')
    if fields != ['sample_id'] + [f'v{i:04d}' for i in range(dim)]:
        raise ValueError(f'Expected {dim} vector columns for {schema}')
    m, l, v = [keyed(x, n) for x, n in ((manifest, 'manifest'), (labels, 'labels'), (vectors, 'vectors'))]
    if set(m) != set(l) or set(m) != set(v):
        raise ValueError('Manifest, labels and vectors must have identical sample IDs')
    records, values, excluded = [], [], []
    for sid in sorted(m):
        a, b = m[sid], l[sid]
        for field in ('trial_id', 'session_id', 'operation_label', 'label_id'):
            if not a.get(field) or a[field] != b.get(field):
                raise ValueError(f'{sid}: missing or mismatched {field}')
        if a.get('feature_schema_version', schema) != schema:
            raise ValueError(f'{sid}: mixed feature schemas')
        reason = None
        if a.get('is_mock', '0').lower() in ('1', 'true') or a.get('source') == 'mock':
            reason = 'mock'
        if a.get('quality_status', 'VALID') != 'VALID' or a.get('status', 'success') != 'success':
            reason = 'failed_or_invalid'
        if a.get('support_eligible', 'true').lower() != 'true' or a.get('window_quality', 'complete') != 'complete' or a.get('collection_quality', 'pass') != 'pass':
            reason = 'ineligible_or_incomplete'
        if '.control.' in a['operation_label'] or a['operation_label'] == 'settled_noop':
            reason = 'no_op_control'
        try:
            vector = np.asarray([float(v[sid][f'v{i:04d}']) for i in range(dim)])
            norm = np.linalg.norm(vector)
            if not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 0:
                reason = 'zero_or_nonfinite_vector'
        except ValueError:
            reason = 'nonnumeric_vector'
        if reason:
            excluded.append({'sample_id': sid, 'reason': reason})
            continue
        records.append({'sample_id': sid, 'trial_id': a['trial_id'], 'session_id': a['session_id'], 'label': a['operation_label']})
        values.append(vector / norm)
    profiles = {m[r['sample_id']].get('capture_profile') for r in records if m[r['sample_id']].get('capture_profile')}
    if len(profiles) > 1:
        raise ValueError('Do not mix capture profiles')
    if not records:
        raise ValueError('No valid samples')
    return records, np.asarray(values), excluded
