"""Export committed source plus validated Linux binaries, never runtime evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def package(native, focus_tool, output, native_source_commit):
    if subprocess.check_output(["git","status","--porcelain"],cwd=ROOT).strip():
        raise ValueError("commit the reviewed source before packaging")
    commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    native_source_commit=subprocess.check_output(["git","rev-parse",native_source_commit+"^{commit}"],cwd=ROOT,text=True).strip()
    subprocess.run(["git","diff","--exit-code",native_source_commit,commit,"--","CMakeLists.txt","native",
        "collector/CMakeLists.txt","collector/device","collector/include","collector/tests",
        "collector/linux/CMakeLists.txt","collector/linux/x11_control.cpp"],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
    output=output.resolve()
    if output.exists() or output.with_suffix(".zip").exists():raise FileExistsError("new paths required")
    for path in (native, focus_tool):
        header=path.read_bytes()[:20]
        if header[:6]!=b"\x7fELF\x02\x01" or header[18:20]!=b"\x3e\x00":
            raise ValueError("validated Linux x86_64 ELF binaries required")
    output.mkdir(parents=True);source=output/"source";source.mkdir();(output/"bin").mkdir()
    with tempfile.TemporaryDirectory() as directory:
        archive=Path(directory)/"source.zip"
        subprocess.run(["git","archive","--format=zip","--output",str(archive),commit],cwd=ROOT,check=True)
        with zipfile.ZipFile(archive) as z:
            if any(Path(n).is_absolute() or ".." in Path(n).parts for n in z.namelist()):raise ValueError("unsafe archive")
            z.extractall(source)
    shutil.copytree(source/"models",output/"models")
    for path,name in ((native,"opsctl"),(focus_tool,"xdotool")):
        shutil.copy2(path,output/"bin"/name);(output/"bin"/name).chmod(0o755)
    for name in ("start_manual.sh","build_local.sh"):
        shutil.copy2(source/"packaging/manual"/name,output/name);(output/name).chmod(0o755)
    shutil.copy2(source/"docs/linux/manual_demo.md",output/"给慧京的操作说明.md")
    (output/"BUILD.json").write_text(json.dumps({"schema":"linux.wps.manual-package.v1", "source_commit":commit,
        "target":"Linux x86_64", "native_sha256":sha(native),"focus_tool_sha256":sha(focus_tool),
        "native_source_commit":native_source_commit,
        "native_validation":"Native and X11 sources unchanged from validated build commit; package replay validated separately",
        "default_model_profile":"linux-wps-writer-pilot-20261023-v1", "capture_protocol":"5+5+15+5",
        "human_desktop_accepted":False,"raw_data_included":False,"sdk_included":False},indent=2)+"\n")
    paths=sorted(p for p in output.rglob("*") if p.is_file())
    (output/"SHA256SUMS").write_text("".join(sha(p)+"  "+p.relative_to(output).as_posix()+"\n" for p in paths))
    archive=output.with_suffix(".zip")
    with zipfile.ZipFile(archive,"x",compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob("*")):
            if p.is_file():z.write(p,Path(output.name)/p.relative_to(output))
    archive.with_suffix(".zip.sha256").write_text(sha(archive)+"  "+archive.name+"\n")
    return {"archive":str(archive),"sha256":sha(archive),"bytes":archive.stat().st_size,"source_commit":commit}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--native",type=Path,required=True)
    p.add_argument("--focus-tool",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--native-source-commit",required=True)
    a=p.parse_args();print(json.dumps(package(a.native,a.focus_tool,a.output,a.native_source_commit),ensure_ascii=False))


if __name__=="__main__":main()
