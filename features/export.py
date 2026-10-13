"""Export selected real, valid feature JSONs from a read-only SQLite manifest."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
import numpy as np
from .feature_schema import schema_for


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True, help='External captured workspace')
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--app', choices=['bilibili', 'huawei_browser'], required=True)
    p.add_argument('--schema', choices=['cross_app.vma.feature.v2', 'cross_app.vma.feature.v3_signed'], required=True)
    p.add_argument('--session', action='append', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    db = sqlite3.connect(f'file:{a.database.resolve().as_posix()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    marks = ','.join('?' for _ in a.session)
    rows = db.execute(f'''SELECT t.trial_id,t.session_id,t.operation_id,t.current_state,
                                f.derived_path,f.vector_dim,s.app_id
        FROM trial t JOIN session s ON s.session_id=t.session_id
        JOIN vma_feature f ON f.trial_id=t.trial_id AND f.feature_schema_version=?
        JOIN quality_log q ON q.quality_id=(SELECT MAX(q2.quality_id) FROM quality_log q2 WHERE q2.trial_id=t.trial_id)
        WHERE t.session_id IN ({marks}) AND s.app_id=?
          AND t.status='success' AND t.quality_status='VALID' AND q.valid=1 AND q.quality_status='VALID'
          AND t.source='hdc' AND t.is_mock=0 AND s.source='hdc' AND s.is_mock=0
          AND f.source='hdc' AND f.is_mock=0 AND q.source='hdc' AND q.is_mock=0
        ORDER BY t.trial_id''', (a.schema, *a.session, a.app)).fetchall()
    db.close()
    dim = schema_for(a.schema).vector_dim
    selected = []
    for row in rows:
        if '.control.' in row['operation_id']:
            continue
        if row['current_state'] != 'SAVE' or row['vector_dim'] != dim:
            raise ValueError('State or vector dimension mismatch')
        path = Path(row['derived_path'])
        if not path.is_absolute():
            path = a.root / path
        if not path.is_file():
            # Copied workspaces may retain a previous host's absolute paths.
            path = a.root / 'data/derived/vma_feature' / str(row['derived_path']).replace('\\', '/').split('/')[-1]
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload['sample_id'] != row['trial_id'] or payload['schema_version'] != a.schema or payload['app_id'] != a.app or payload['operation_id'] != row['operation_id']:
            raise ValueError('Feature ID/app/schema/label mismatch')
        if payload.get('is_mock') or payload.get('metadata', {}).get('is_mock'):
            raise ValueError('Mock feature is forbidden')
        vector = np.asarray(payload['l2_vector'], dtype=float)
        if len(vector) != dim or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
            raise ValueError('Invalid feature vector')
        profile = payload.get('metadata', {}).get('capture_profile', {})
        identity = profile.get('identity') if isinstance(profile, dict) else profile
        if not identity:
            raise ValueError('Missing capture profile identity')
        selected.append((dict(row), vector / np.linalg.norm(vector), identity))
    if not selected or len({x[2] for x in selected}) != 1:
        raise ValueError('Need nonempty data from one capture profile')
    labels = sorted({r['operation_id'].rsplit('.', 1)[-1] for r, _, _ in selected})
    a.output.mkdir(parents=True, exist_ok=True)
    fields = ['sample_id','trial_id','session_id','label_id','operation_label','source','quality_status','feature_schema_version','capture_profile']
    manifests = []
    for r, _, profile in selected:
        label = r['operation_id'].rsplit('.', 1)[-1]
        manifests.append({'sample_id': r['trial_id'], 'trial_id': r['trial_id'], 'session_id': r['session_id'],
                          'label_id': labels.index(label), 'operation_label': label, 'source': 'hdc',
                          'quality_status': 'VALID', 'feature_schema_version': a.schema, 'capture_profile': profile})
    for filename, columns in [('dataset_manifest.csv',fields),('labels.csv',fields[:5])]:
        with (a.output/filename).open('x',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(manifests)
    with (a.output/'vma_vectors_l2.csv').open('x',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow(['sample_id']+[f'v{i:04d}' for i in range(dim)])
        w.writerows([r['trial_id'], *[f'{v:.12g}' for v in vector]] for r,vector,_ in selected)
    print(f'Exported {len(selected)} valid samples, {len(labels)} labels, {dim} dimensions')


if __name__ == '__main__':
    main()
