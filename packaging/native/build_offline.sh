#!/bin/sh
set -eu
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD=${OPS_BUILD_DIR:-"$PACKAGE/build-local"}
if [ -n "${OPS_TOOLCHAIN:-}" ]; then
  cmake -S "$PACKAGE/source" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DHARMONY_OPS_PLATFORM_BRIDGE="$PACKAGE/operator/platform_bridge.cpp" -DCMAKE_TOOLCHAIN_FILE="$OPS_TOOLCHAIN" -DOHOS_ARCH="${OPS_OHOS_ARCH:-arm64-v8a}"
else
  cmake -S "$PACKAGE/source" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DHARMONY_OPS_PLATFORM_BRIDGE="$PACKAGE/operator/platform_bridge.cpp"
fi
cmake --build "$BUILD" --parallel 2
printf '%s\n' "Built: $BUILD/native/opsctl"
