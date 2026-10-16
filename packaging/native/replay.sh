#!/bin/sh
set -eu
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OPSCTL=${OPSCTL:-"$PACKAGE/build-local/native/opsctl"}
if [ "$#" -ne 1 ]; then printf '%s\n' 'usage: replay.sh NEW_OUTPUT_DIRECTORY' >&2; exit 2; fi
if [ -e "$1" ]; then printf '%s\n' 'output directory must not exist' >&2; exit 2; fi
mkdir -p "$1"
for APP in wps bilibili huawei_browser; do
  "$OPSCTL" replay --models "$PACKAGE/models" --app "$APP" --manifest "$PACKAGE/fixtures/raw-window/manifest.csv" --output "$1/$APP.json" --vector-output "$1/$APP.csv" --stages "$1/$APP-stages"
done
