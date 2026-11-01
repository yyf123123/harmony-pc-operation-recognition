"""Create a private Linux x86_64 operator bundle; no documents, SDK or raw data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
    return h.hexdigest()


def package(build,output,source_sha,report=None):
    build=Path(build).resolve();output=Path(output).resolve()
    if platform.system()!="Linux" or platform.machine()!="x86_64":raise ValueError("package on validated Linux x86_64 host")
    if output.exists() or output.with_suffix(".zip").exists():raise FileExistsError("new package paths required")
    output.mkdir(parents=True)
    (output/"bin").mkdir();(output/"lib").mkdir()
    for executable in ("opsctl","operation_predict","operation_feature","operation_benchmark"):
        path=build/"native"/executable
        header=path.read_bytes()[:20]
        if header[:5]!=b"\x7fELF\x02" or header[5]!=1 or struct.unpack("<H",header[18:20])[0]!=62:
            raise ValueError("prebuilt executable must be ELF64 x86_64")
        shutil.copy2(path,output/"bin"/executable)
    shutil.copy2(build/"collector/linux/linux_vma_collect",output/"bin/linux_vma_collect")
    for source,name in ((build/"native/libharmony_operation_recognition.a","libharmony_operation_recognition.a"),
                        (build/"collector/libharmony_vma_collector.a","libharmony_vma_collector.a")):
        shutil.copy2(source,output/"lib"/name)
    shutil.copytree(ROOT/"native/include",output/"include")
    shutil.copytree(ROOT/"collector/include",output/"include",dirs_exist_ok=True)
    shutil.copytree(ROOT/"models",output/"models")
    shutil.copytree(ROOT/"native/fixtures/raw-window",output/"fixtures/raw-window")
    # Source allowlist excludes build, .git, outputs, payloads, SDK and credentials.
    source=output/"source";source.mkdir()
    for name in ("native","collector","apps","features","classification","scripts","tests","docs","models","packaging",".github"):
        base=ROOT/name
        if any(p.is_symlink() for p in base.rglob("*")):raise ValueError("source symlink outside the release contract")
        shutil.copytree(base,source/name,ignore=shutil.ignore_patterns("__pycache__","*.pyc",".env",".env.*","*.log","*.zip","build","outputs"))
    for name in ("CMakeLists.txt","README.md","AGENTS.md","PROJECT_STATUS.md","requirements.txt","SOURCE_MAP.md",".gitignore",".gitattributes","RUN_STABLE.md","SOURCE_SNAPSHOT.json","SHA256SUMS"):
        shutil.copy2(ROOT/name,source/name)
    scripts={
        "selftest.sh":'''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
sha256sum -c SHA256SUMS >/dev/null
NATIVE="$ROOT/bin/opsctl"
if [ -x "$ROOT/local-build/native/opsctl" ]; then NATIVE="$ROOT/local-build/native/opsctl"; fi
"$NATIVE" selftest --models "$ROOT/models" --fixtures "$ROOT/fixtures/raw-window"
"$NATIVE" doctor --models "$ROOT/models" --app wps --model-profile linux-wps-writer-pilot-20261023-v1
printf '%s\\n' 'SELFTEST PASSED (offline only; desktop remains unverified)'
''',
        "run_check.sh":'''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NATIVE="$ROOT/bin/opsctl"
if [ -x "$ROOT/local-build/native/opsctl" ]; then NATIVE="$ROOT/local-build/native/opsctl"; fi
cd "$ROOT/source"
exec python3 -m scripts.linux_desktop_check --native "$NATIVE" --models "$ROOT/models" --output-root "$ROOT/runs" "$@"
''',
        "build_local.sh":'''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cmake -S "$ROOT/source" -B "$ROOT/local-build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/local-build" --parallel 2
ctest --test-dir "$ROOT/local-build" --output-on-failure
"$ROOT/selftest.sh"
'''}
    for name,text in scripts.items():
        (output/name).write_text(text,encoding="utf-8");(output/name).chmod(0o755)
    (output/"操作卡.md").write_text("""# 慧京：Linux WPS Native 模型验证

这是私下传输的 Linux x86_64 包，不包含文档、采集原始数据、SDK 或凭据。先做有限验证，不启动 1500 批采；现有小试模型不能当作最终部署准确率。解压到新目录，保留原自动化和测试模板。

## 1. 自检

在你的 Linux 桌面普通用户终端进入包目录，运行：

```sh
./selftest.sh
```

若提示 GLIBC/GLIBCXX/可执行格式不兼容，使用包内源码在已有 CMake/G++ 环境执行 `./build_local.sh`，随后仍用相同入口；不 sudo、不自动安装依赖。自检不证明桌面已通过。

## 2. 复用你已有的配置，先测输入、保存、加粗

先启动原来采集时那套 WPS/RPC 环境，确认原有自动化配置能工作。使用现有 `linux.wps.batch.v1` JSON 配置的绝对路径，里面的脚本/模板仍使用你电脑上原来的路径，不用重写 18 类动作或 C++ 接口。

```sh
./run_check.sh --batch-config /你现有配置的绝对路径/config.json
```

如果 WPS 重启后 PID 变了，在已有配置里更新 root_pid/pids，或显式追加 `--root-pid 实际主PID --covered-pids 实际PID列表`；有多个 PID 用逗号分隔，不能照抄示意值，不会自动增补新子进程。不要在其他用户桌面运行。Python 3.10+ 标准库即可编排，不要求 NumPy；真实焦点继续用已有 X11/xdotool。

默认只跑三个独立操作，至少约 1.5 分钟加准备开销。运行时保持 WPS 聚焦，不切换到其他应用。每次在新的测试副本上恢复前置状态，自动执行动作和语义检查；失败即停、保留证据，不用预测是否正确筛样本。外部真值与 Native 预测分文件，核心只接收无标签 ID。准备恢复包含未观测操作，因此每个独立检查重开 Native 会话，不拼成连续用户历史。

三个流程检查通过后（报告 status=passed 表示窗口/语义/输出有效，不要求预测全对），再做全 18 类各一次：

```sh
./run_check.sh --batch-config /你现有配置的绝对路径/config.json --operations all
```

仍是有限检查，至少 9 分钟加准备开销，不是正式批采，也不替代跨重启/连续会话验收。无需重新训练模型。若原配置脚本路径/RPC 依赖失效，保留报告，把配置文件和已有自动化代码路径反馈师兄，修复由 Codex 处理；不要改成无条件成功验证器。

## 3. 回传一个文件

结束时会打印 `feedback_zip` 的绝对路径，把该 `feedback.zip` 发回即可。它只打包报告、真值标签、Native 结果、health 和 Native 错误；不递归带走工作文档/语义正文。实际运行阶段失败也会生成该文件；若配置路径或 JSON 语法错误导致程序未开始，按终端提示修复。请勿直接打包整个 runs/operation 目录，也不要将数据上传公开仓库。

报告包含准确率、两个基线/动作/post 时序、特征、分类、模型加载、输出、历史更新以及准备/动作/验证耗时。动作和验证在 15 秒窗口内，不能重复相加。模型读取和首次启动不应与常驻窗口重复计数。前置权限、焦点、进程变化、动作/验证失败或超时都不计有效窗口。

只有自检和合成编排测试在交付前完成；真正 WPS 在线效果以你这次回传报告为准。
""",encoding="utf-8")
    if report:shutil.copy2(report,output/"PROGRESS_REPORT.md")
    dependencies=subprocess.run(["ldd",str(output/"bin/opsctl")],capture_output=True,text=True,check=True).stdout
    (output/"runtime-dependencies.txt").write_text(dependencies,encoding="utf-8")
    (output/"BUILD.json").write_text(json.dumps({"source_snapshot":source_sha,"source_recipe":json.loads((ROOT/"SOURCE_SNAPSHOT.json").read_text()),"platform":platform.platform(),"architecture":platform.machine(),
            "model_profile":"linux-wps-writer-pilot-20261023-v1","scope":"private finite desktop validation; no real documents or datasets"},indent=2)+"\n")
    files=sorted(p for p in output.rglob("*") if p.is_file())
    (output/"SHA256SUMS").write_text("".join(sha(p)+"  "+str(p.relative_to(output))+"\n" for p in files),encoding="utf-8")
    # Test the exact relocatable artifact, from a different working directory.
    test=subprocess.run([str(output/"selftest.sh")],cwd="/tmp",capture_output=True,text=True,check=True)
    archive=output.with_suffix(".zip")
    with zipfile.ZipFile(archive,"x",compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob("*")):
            if p.is_file():z.write(p,str(Path(output.name)/p.relative_to(output)))
    archive.with_suffix(".zip.sha256").write_text(sha(archive)+"  "+archive.name+"\n")
    return {"archive":str(archive),"sha256":sha(archive),"bytes":archive.stat().st_size,"selftest":test.stdout.strip()}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--build",type=Path,required=True);p.add_argument("--output",type=Path,required=True);p.add_argument("--source-sha",required=True);p.add_argument("--report",type=Path)
    a=p.parse_args();print(json.dumps(package(a.build,a.output,a.source_sha,a.report),ensure_ascii=False))


if __name__=="__main__":main()
