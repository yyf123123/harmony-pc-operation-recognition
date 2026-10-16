#!/bin/sh
set -eu
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OPSCTL=${OPSCTL:-"$PACKAGE/build-local/native/opsctl"}
"$OPSCTL" doctor --models "$PACKAGE/models"
"$OPSCTL" selftest --models "$PACKAGE/models" --fixtures "$PACKAGE/fixtures/raw-window"
