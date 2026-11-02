#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10 或更新版本"'
python3 - "$ROOT" <<'PY'
import hashlib,sys
from pathlib import Path
root=Path(sys.argv[1])
for line in (root/'SHA256SUMS').read_text().splitlines():
    digest,name=line.split('  ',1)
    p=root/name
    if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
        raise SystemExit('包校验失败，请重新解压：'+name)
print('包校验通过。仅在测试文档上运行；默认模型与约30秒协议不变。')
PY
NATIVE="$ROOT/bin/opsctl"
if [ -x "$ROOT/local-build/native/opsctl" ]; then NATIVE="$ROOT/local-build/native/opsctl"; fi
if ! "$NATIVE" selftest --models "$ROOT/models" --fixtures "$ROOT/source/native/fixtures/raw-window"; then
    printf '%s\n' '预编译自检失败，尝试用本机已有编译工具重建；不会安装软件。'
    sh "$ROOT/build_local.sh"
    NATIVE="$ROOT/local-build/native/opsctl"
fi
if ! command -v xdotool >/dev/null 2>&1; then
    if [ -x "$ROOT/local-bin/xdotool" ]; then PATH="$ROOT/local-bin:$PATH"; else PATH="$ROOT/bin:$PATH"; fi
    export PATH
fi
cd "$ROOT/source"
exec python3 -m scripts.linux_manual_demo --native "$NATIVE" --models "$ROOT/models" --output-root "$ROOT/runs" "$@"
