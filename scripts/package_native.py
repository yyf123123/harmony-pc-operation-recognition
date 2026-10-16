"""Build an offline, allowlisted Native handoff directory; never bundle an SDK/data."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", action="append", default=[], metavar="TARGET=BUILD_DIR")
    parser.add_argument("--elf-reader", type=Path, help="optional host readelf / llvm-readelf for ELF dependency audit")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists():
        parser.error("output must be a new directory")
    # The allowlist is intentional: no Python environment, documents, SDK or capture logs.
    output.mkdir(parents=True)
    source = output / "source"
    source.mkdir()
    for name in ("CMakeLists.txt", "AGENTS.md", "README.md"):
        shutil.copy2(repo / name, source / name)
    for folder in ("native", "collector/include", "collector/device", "collector/linux", "collector/tests"):
        for path in sorted((repo / folder).rglob("*")):
            if not path.is_file() or path.suffix not in {".c", ".h", ".cpp", ".hpp", ".cmake", ".smaps", ".csv", ".md"} and path.name != "CMakeLists.txt":
                continue
            target = source / path.relative_to(repo)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    shutil.copy2(repo / "collector/CMakeLists.txt", source / "collector/CMakeLists.txt")
    shutil.copytree(repo / "models", output / "models")
    shutil.copytree(repo / "native/fixtures", output / "fixtures")
    shutil.copytree(repo / "packaging/native", output / "operator")
    shutil.copytree(repo / "docs/handoff", output / "docs")
    shutil.copy2(repo / "native/examples/platform_bridge.cpp", output / "operator/platform_bridge.cpp")
    shutil.copytree(repo / "native/include", output / "include")
    shutil.copytree(repo / "collector/include", output / "include", dirs_exist_ok=True)
    builds = []
    for item in args.binary:
        target_name, sep, directory = item.partition("=")
        if not sep or not target_name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in target_name):
            parser.error("--binary requires safe target-name=build-directory")
        build = Path(directory).resolve()
        destination = output / "targets" / target_name
        copied = []
        candidates = [build / "native/opsctl", build / "native/opsctl.exe",
                      build / "native/libharmony_operation_recognition.a", build / "native/harmony_operation_recognition.lib",
                      build / "collector/libharmony_vma_collector.a", build / "collector/harmony_vma_collector.lib"]
        candidates += [build / "native/Release/opsctl.exe", build / "native/Release/harmony_operation_recognition.lib",
                       build / "collector/Release/harmony_vma_collector.lib"]
        for path in candidates:
            if path.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination / path.name)
                copied.append(path.name)
        if len(copied) < 3:
            raise RuntimeError(f"missing executable/static libraries in {build}")
        cache = build / "CMakeCache.txt"
        facts = [line for line in cache.read_text().splitlines() if any(line.startswith(key) for key in
                 ("CMAKE_CXX_COMPILER:", "CMAKE_C_COMPILER:", "CMAKE_BUILD_TYPE:", "CMAKE_TOOLCHAIN_FILE:", "OHOS_ARCH:", "CMAKE_OSX_ARCHITECTURES:"))]
        # Toolchain path can be private host metadata; record basename, never bundle it.
        facts = [line.split("=", 1)[0] + "=" + Path(line.split("=", 1)[1]).name if "COMPILER:" in line or "TOOLCHAIN_FILE:" in line else line for line in facts]
        compiler = []
        for info in sorted((build / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")):
            compiler += re.findall(r'set\(CMAKE_CXX_(?:COMPILER_ID|COMPILER_VERSION|COMPILER_TARGET|SIZEOF_DATA_PTR) [^\n]+', info.read_text())
        binaries = []
        for path in sorted(destination.iterdir()):
            info = subprocess.run(["file", str(path)], text=True, capture_output=True, check=True).stdout.strip()
            dep_command = (["otool", "-L", str(path)] if target_name.startswith("macos") else [str(args.elf_reader or "readelf"), "-d", str(path)])
            dependencies = "unavailable on packaging host; inspect on target with readelf -d / otool -L"
            if shutil.which(dep_command[0]) and path.name.startswith("opsctl"):
                proc = subprocess.run(dep_command, text=True, capture_output=True)
                dependencies = proc.stdout if proc.returncode == 0 else dependencies
            binaries.append({"file": path.name, "format": info.replace(str(destination), "targets/" + target_name), "dependencies": dependencies.replace(str(destination), "targets/" + target_name)})
        builds.append({"target": target_name, "cmake": facts, "compiler": compiler, "binaries": binaries})
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, text=True, capture_output=True).stdout)
    (output / "BUILD_INFO.json").write_text(json.dumps({"schema": "harmony.ops.handoff.v1", "source_commit": commit, "source_dirty": dirty,
        "packaging_host": platform.system() + "-" + platform.machine(), "targets": builds,
        "runtime": "C++17 standard library, C/POSIX runtime, threads; exact ABI must be checked on target",
        "fixture_provenance": "synthetic only; no accuracy claim", "sdk_included": False}, ensure_ascii=False, indent=2) + "\n")
    for path in (output / "operator").glob("*.sh"):
        path.chmod(0o755)
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(output).as_posix()
             for path in sorted(output.rglob("*")) if path.is_file()]
    (output / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
