#!/bin/sh
set -eu
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PACKAGE"
if command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHA256SUMS
elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -c SHA256SUMS
else printf '%s\n' 'install/use the platform SHA-256 verification tool' >&2; exit 2
fi
