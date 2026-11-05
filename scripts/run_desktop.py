#!/usr/bin/env python3
"""在当前用户已登录的 X11 桌面上启动只监听回环地址的远程桌面服务。"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


DATA_DIR = Path.home() / ".local/share/web-desktop"
CONFIG_DIR = Path.home() / ".config/web-desktop"


def session_environment() -> dict[str, str]:
    """发现本地 X11 会话并验证显示连接，不读取或输出其他环境凭据。

    Returns:
        包含有效 DISPLAY 和 XAUTHORITY 的进程环境。
    """
    environment = os.environ.copy()
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)
    environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    sessions = subprocess.check_output(
        ["loginctl", "list-sessions", "--no-legend"], text=True
    )
    found = False
    for line in sessions.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] != str(os.getuid()):
            continue
        details = subprocess.check_output(
            ["loginctl", "show-session", fields[0]], text=True
        )
        properties = dict(
            row.split("=", 1) for row in details.splitlines() if "=" in row
        )
        if (
            properties.get("Type") == "x11"
            and properties.get("Remote") == "no"
            and properties.get("Seat")
            and properties.get("State") in {"active", "online"}
        ):
            found = True
            break
    if not found:
        raise RuntimeError("尚无已登录的本地 X11 图形会话")

    imported = subprocess.check_output(
        ["systemctl", "--user", "show-environment"],
        env=environment,
        text=True,
    )
    for line in imported.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"DISPLAY", "XAUTHORITY"}:
            environment[key] = value

    authorities = [
        Path(environment.get("XAUTHORITY", "/nonexistent")),
        runtime_dir / "gdm/Xauthority",
        Path.home() / ".Xauthority",
    ]
    displays = list(dict.fromkeys(
        [environment.get("DISPLAY", "")]
        + [f":{p.name[1:]}" for p in sorted(Path("/tmp/.X11-unix").glob("X*"))]
    ))
    for authority in authorities:
        if not authority.is_file():
            continue
        for display in filter(None, displays):
            candidate = environment | {
                "DISPLAY": display, "XAUTHORITY": str(authority)
            }
            result = subprocess.run(
                ["/usr/bin/xrandr", "--display", display, "--query"],
                env=candidate, capture_output=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                return candidate
    raise RuntimeError("无法连接已登录的 X11 桌面，请检查图形会话授权")


def main() -> None:
    """启动桌面共享或浏览器网关，保持 systemd 可直接管理主进程。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=["vnc", "web"])
    args = parser.parse_args()
    if args.service == "vnc":
        environment = session_environment()
        executable = DATA_DIR / "tigervnc-1.16.2.x86_64/usr/bin/x0vncserver"
        arguments = [
            str(executable), "-display", environment["DISPLAY"],
            "-localhost", "-rfbport", "5900", "-AlwaysShared=1",
            "-SecurityTypes=VncAuth",
            f"-PasswordFile={CONFIG_DIR / 'vnc.passwd'}",
            "-FrameRate=20", "-CompareFB=2", "-AcceptSetDesktopSize=0",
            "-RawKeyboard=1",
        ]
        print(
            f"共享已登录的 X11 桌面 {environment['DISPLAY']}，仅监听回环地址",
            flush=True,
        )
    else:
        environment = os.environ.copy()
        arguments = [
            sys.executable, "-m", "websockify",
            f"--web={DATA_DIR / 'novnc'}", "--file-only", "--heartbeat=30",
            "127.0.0.1:6080", "127.0.0.1:5900",
        ]
    os.execvpe(arguments[0], arguments, environment)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
