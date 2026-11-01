#!/bin/sh
# One build entry; no Python, package downloads, service registration or sudo.
set -eu
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD=${OPS_BUILD_DIR:-"$PACKAGE/build-local"}
cd "$PACKAGE"
# Only the documented platform bridge is editable. All core/model/script hashes
# must still match; never regenerate the release SHA manifest after adaptation.
CORE_SUMS=$(mktemp)
trap 'rm -f "$CORE_SUMS"' EXIT HUP INT TERM
awk '$0 !~ /  operator\/platform_bridge.cpp$/ {print}' SHA256SUMS > "$CORE_SUMS"
if command -v sha256sum >/dev/null 2>&1; then sha256sum -c "$CORE_SUMS" > /dev/null
elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -c "$CORE_SUMS" > /dev/null
else printf '%s\n' '缺少 SHA-256 校验工具，请使用内网已有工具；不跳过核心校验。' >&2; exit 2
fi
sh "$PACKAGE/operator/build_offline.sh"
if [ "$(cat "$BUILD/ops-build-mode.txt")" = cross ]; then
  printf '%s\n' '交叉构建通过；未在本机执行目标程序。现有服务启动时会自动做目标侧回放自检。' > "$BUILD/READY_REPORT.txt"
else
  CHECK=$(mktemp -d "$BUILD/ready-check.XXXXXX")
  OPSCTL="$BUILD/native/opsctl" sh "$PACKAGE/operator/selftest.sh" > "$CHECK/selftest.jsonl"
  OPSCTL="$BUILD/native/opsctl" sh "$PACKAGE/operator/replay.sh" "$CHECK/replay"
  printf '%s\n' "本机构建、自检和三应用合成回放通过：$CHECK" '这些结果不代表鸿蒙服务注册、权限或真实操作已通过。' > "$BUILD/READY_REPORT.txt"
fi
printf '%s\n' '现有服务只链接 harmony_operation_host，启动/触发/退出各一处调用。' >> "$BUILD/READY_REPORT.txt"
cat "$BUILD/READY_REPORT.txt"
