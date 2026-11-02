#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cmake -S "$ROOT/source" -B "$ROOT/local-build" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build "$ROOT/local-build" --parallel 2
if ! command -v xdotool >/dev/null 2>&1; then
    mkdir -p "$ROOT/local-bin"
    c++ -std=c++17 "$ROOT/source/collector/linux/x11_control.cpp" -lX11 -ldl -o "$ROOT/local-bin/xdotool"
fi
"$ROOT/local-build/native/opsctl" selftest --models "$ROOT/models" --fixtures "$ROOT/source/native/fixtures/raw-window"
