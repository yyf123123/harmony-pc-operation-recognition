"""Start an authenticated, isolated WPS desktop and run the three-action smoke.

No system installation, existing desktop, real document or global WPS profile
is used. First-run consent is never automated: a separately initialized test
profile with an explicit marker is required. Formal batch collection stays off.
"""
from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
import zipfile

from collector.linux.procfs import descendant_pids, identity
from scripts.linux_server_wps_smoke import run as smoke, ui, verify_saved


def blank_document(path):
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t/></w:r></w:p><w:sectPr/></w:body></w:document>')


def session(args, root):
    children = []
    try:
        document = root / (uuid.uuid4().hex + ".docx"); blank_document(document)
        for name, argv in (("wm", ["xfwm4", "--compositor=off"]), ("wps", [str(args.wps), str(document)])):
            with (root / (name + ".log")).open("x") as log:
                process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
            children.append((process, identity(process.pid)))
            time.sleep(1)
        target = children[-1][0]
        previous = None; stable_since = None; until = time.monotonic() + 40
        while time.monotonic() < until:
            if target.poll() is not None: raise RuntimeError("new WPS process exited")
            try:
                window = ui("getactivewindow")
                if int(ui("getwindowpid", window)) != target.pid: raise ValueError("WPS is not focused")
                pids = tuple(sorted(descendant_pids(target.pid)))
                current = (window, tuple(identity(pid) for pid in pids))
                if current != previous: previous = current; stable_since = time.monotonic()
                if time.monotonic() - stable_since >= 4: break
            except (RuntimeError, ValueError, OSError):
                stable_since = None; previous = None
            time.sleep(.25)
        else: raise RuntimeError("new WPS focus/process tree did not stabilize")
        # The initial explicit set comes only from this launch. The Native
        # runtime rejects any later coverage change; it never auto-adds a PID.
        if shutil.which("import"):
            subprocess.run(["import", "-window", "root", str(root/"startup.png")], timeout=5, check=False)
        # Cancel routine startup notices; Escape cannot accept license terms or
        # install an update. Confirm a real editable document BEFORE sampling.
        for _ in range(3):
            ui("key", "Escape")
            ui("close-system-check", str(target.pid))
            time.sleep(.3)
        try:
            ui("type", "--", "Calibration text"); ui("key", "ctrl+s")
            verify_saved(document, "Calibration text")
            ui("key", "ctrl+z"); ui("key", "ctrl+s"); verify_saved(document, "")
        except Exception:
            if shutil.which("import"):
                subprocess.run(["import", "-window", "root", str(root/"precondition-failure.png")], timeout=5, check=False)
            raise RuntimeError("startup notices or document focus prevented the editable-document precheck") from None
        # Preparation may create helpers. Freeze an explicit initial set only
        # once it has stabilized; never expand a running capture's coverage.
        pids = tuple(sorted(descendant_pids(target.pid)))
        for _ in range(8):
            time.sleep(.5)
            if tuple(sorted(descendant_pids(target.pid))) != pids:
                raise RuntimeError("process coverage changed during precheck stabilization")
        window = ui("getactivewindow")
        if int(ui("getwindowpid", window)) != target.pid:
            raise RuntimeError("prepared document did not retain actual WPS focus")
        result = smoke(SimpleNamespace(document=document, test_copy=True, root_pid=target.pid,
            covered_pids=list(pids), window_id=window, native=args.native, models=args.models,
            output=root/"smoke", app_version=args.app_version))
        if result["status"] != "smoke_complete" and shutil.which("import"):
            subprocess.run(["import", "-window", "root", str(root/"failure.png")], timeout=5, check=False)
        return 0 if result["status"] == "smoke_complete" and result["valid_native_windows"] == 3 else 2
    finally:
        # Only process groups explicitly created here, with matching identities.
        for process, original in reversed(children):
            try:
                if identity(process.pid) == original and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
            except (OSError, RuntimeError): pass
        for process, original in reversed(children):
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    if identity(process.pid) == original and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                except (OSError, RuntimeError): pass
                process.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True); parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--wps", type=Path, default=Path("/opt/kingsoft/wps-office/office6/wps"))
    parser.add_argument("--_session", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "linux": parser.error("Linux required")
    if args._session:
        try:
            return session(args, args._session)
        except (Exception, KeyboardInterrupt) as error:
            (args._session/"desktop-failure.json").write_text(json.dumps({"status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                "error_type": type(error).__name__, "reason": str(error), "formal_collection_enabled": False}) + "\n")
            return 2
    profile = args.profile_root.resolve(strict=True)
    marker = json.loads((profile / "server-wps-profile.json").read_text())
    if (args.profile_root.is_symlink() or profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077 or
            marker.get("schema") != "linux.wps.isolated-test-profile.v1" or marker.get("human_consent_confirmed") is not True):
        parser.error("private initialized test profile with human consent required")
    lock_path = profile / ".desktop.lock"
    if lock_path.is_symlink(): raise ValueError("profile lock must not be a symlink")
    lock = lock_path.open("a")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: parser.error("test profile is already in use; no new desktop started")
    for program in ("c++", "xvfb-run", "xauth", "dbus-run-session", "xfwm4"):
        if not shutil.which(program): parser.error("missing dependency: " + program + "; no automatic installation")
    root = args.output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False, mode=0o700); (root/"bin").mkdir()
    source_root = Path(__file__).resolve().parents[1]
    subprocess.run(["c++", "-std=c++17", str(source_root/"collector/linux/x11_control.cpp"), "-lX11", "-ldl", "-o", str(root/"bin/xdotool")], check=True)
    env = dict(os.environ)
    for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                             ("XDG_DATA_HOME", "data"), ("XDG_RUNTIME_DIR", "runtime")):
        directory = profile/folder; directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise ValueError("private profile subdirectory required")
        env[variable] = str(directory)
    env.update(PATH=str(root/"bin")+os.pathsep+env["PATH"], QT_QPA_PLATFORM="xcb", PYTHONPATH=str(source_root))
    for variable in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
        env.pop(variable, None)
    argv = ["xvfb-run", "-a", "-n", "180", "-s", "-screen 0 1280x960x24 -nolisten tcp -noreset", "dbus-run-session", "--",
            sys.executable, "-m", "scripts.linux_server_desktop", *sys.argv[1:], "--_session", str(root)]
    with (root/"session.log").open("x") as log:
        process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=300)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM); process.wait(timeout=6)
            code = 2
    receipt = {"returncode": code, "status": "complete" if code == 0 else "failed",
               "report": str(root/"smoke/report.json") if (root/"smoke/report.json").exists() else None,
               "desktop_failure": str(root/"desktop-failure.json") if (root/"desktop-failure.json").exists() else None,
               "formal_collection_enabled": False}
    (root/"desktop-report.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return code


if __name__ == "__main__": raise SystemExit(main())
