"""Independent X11/Xwayland focus evidence; native Wayland is unsupported."""

import json
import subprocess


def main() -> None:
    try:
        window = subprocess.check_output(["xdotool", "getactivewindow"], text=True, timeout=5).strip()
        pid = subprocess.check_output(["xdotool", "getwindowpid", window], text=True, timeout=5).strip()
        if not window.isdigit() or not pid.isdigit() or int(pid) <= 0:
            raise ValueError("no active client PID")
        # Window titles may contain document content; never emit them here.
        print(json.dumps({"pid": int(pid), "window_id": window, "source": "xdotool-active-window"}))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("independent focus evidence unavailable; verify X11/Xwayland and xdotool")


if __name__ == "__main__":
    main()
