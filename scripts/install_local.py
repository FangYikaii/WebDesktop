#!/usr/bin/env python3
"""安装用户级服务及随机凭据，使用已准备的第三方资源和 Python 环境。"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path.home() / ".local/share/web-desktop"
CONFIG_DIR = Path.home() / ".config/web-desktop"
STATE_DIR = Path.home() / ".local/state/web-desktop"


def private_json(path: Path, payload: dict) -> None:
    """以仅当前用户可读写的权限原子保存生成的配置。

    Args:
        path: 配置文件路径。
        payload: 需要序列化的配置对象。
    """
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> None:
    """生成独立认证信息并启用随图形会话运行的两个用户服务。"""
    for directory in (DATA_DIR, CONFIG_DIR, STATE_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    credentials_file = CONFIG_DIR / "credentials.json"
    if credentials_file.exists():
        credentials = json.loads(credentials_file.read_text())
        credentials_file.chmod(0o600)
    else:
        credentials = {
            "web_username": "desktop",
            "web_password": secrets.token_urlsafe(24),
            "vnc_password": secrets.token_urlsafe(6),
            "local_url": "http://127.0.0.1:6080/vnc.html?resize=scale&reconnect=1",
            "public_url": "",
        }
        private_json(credentials_file, credentials)
    encoded = subprocess.check_output(
        [str(DATA_DIR / "tigervnc-1.16.2.x86_64/usr/bin/vncpasswd"), "-f"],
        input=(credentials["vnc_password"] + "\n").encode(),
    )
    password_file = CONFIG_DIR / "vnc.passwd"
    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    password_file.chmod(0o600)

    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    for service in ("vnc", "web"):
        unit = f"""[Unit]
Description=Web Desktop {service}
PartOf=graphical-session.target
After=graphical-session-pre.target

[Service]
Type=simple
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONUNBUFFERED=1
ExecStart={sys.executable} {ROOT / 'scripts/run_desktop.py'} {service}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=graphical-session.target
"""
        (units / f"web-desktop-{service}.service").write_text(unit)
    environment = os.environ | {
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=environment, check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "web-desktop-vnc.service",
         "web-desktop-web.service"], env=environment, check=True,
    )
    print(f"服务已启用，凭据保存在 {credentials_file}（未输出密码）")


if __name__ == "__main__":
    main()
