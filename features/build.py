"""Construct cross-app features from an external JSON window manifest."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from collector.snapshot import VmaRegion, VmaSnapshot
from .vma_feature import build_vma_feature


def snapshot(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return VmaSnapshot(snapshot_id=data['snapshot_id'], app_id=data['app_id'], phase=data['phase'],
                       timestamp=data['timestamp'], regions=tuple(VmaRegion(**r) for r in data['regions']),
                       source=data.get('source', 'hdc'), is_mock=data.get('is_mock', False),
                       pid=data.get('pid'), metadata=data.get('metadata', {}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--schema', choices=['cross_app.vma.feature.v2', 'cross_app.vma.feature.v3_signed'], required=True)
    a = p.parse_args()
    m = json.loads(a.manifest.read_text(encoding='utf-8'))
    def read(value):
        path = Path(value)
        return snapshot(path if path.is_absolute() else a.manifest.parent / path)
    feature = build_vma_feature(sample_id=m['sample_id'], app_id=m['app_id'], operation_id=m['operation_id'],
                               baselines=[read(x) for x in m['baselines']], action=read(m['action']),
                               post_action=read(m['post_action']), post_actions=[read(x) for x in m.get('additional_post_actions', [])],
                               schema_version=a.schema, metadata=m.get('metadata', {}))
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8') as f:
        json.dump(asdict(feature), f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
