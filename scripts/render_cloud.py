#!/usr/bin/env python3
"""生成待审核的云端配置及独立认证文件，不连接远端或启用公网入口。"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from install_local import CONFIG_DIR, ROOT, STATE_DIR, private_json


def resolve_public_ip(public_ip: str | None, credentials: dict) -> str:
    """读取显式参数或私有配置中的公网地址并校验 IPv4 格式。

    Args:
        public_ip: 命令行指定的 IPv4 地址，为空时读取私有配置。
        credentials: 本机私有配置，允许通过 public_url 提供地址。

    Returns:
        通过格式校验的 IPv4 地址字符串。
    """
    if public_ip is None:
        public_ip = urlsplit(credentials.get("public_url", "")).hostname
    if not public_ip:
        raise ValueError("请使用 --public-ip 指定公网 IPv4 地址，或配置私有 public_url")
    try:
        return str(ipaddress.IPv4Address(public_ip))
    except ipaddress.AddressValueError as error:
        raise ValueError("公网地址必须为有效的 IPv4 地址") from error


def write_private(path: Path, content: str) -> None:
    """以私有权限保存部署材料，避免凭据进入项目目录。

    Args:
        path: 私有部署目录下的目标文件。
        content: 配置内容。
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
    path.chmod(0o600)


def main() -> None:
    """渲染当前方案的 Nginx 配置和 SHA-512 crypt 认证文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-ip", help="公网 IPv4 地址，默认读取私有配置的 public_url")
    args = parser.parse_args()
    os.umask(0o077)
    credentials = json.loads((CONFIG_DIR / "credentials.json").read_text())
    try:
        public_ip = resolve_public_ip(args.public_ip, credentials)
    except ValueError as error:
        parser.error(str(error))
    output = STATE_DIR / "cloud-bundle"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    username = credentials["web_username"]
    if any(character in username for character in ":\r\n"):
        raise ValueError("网页用户名含有认证文件不允许的字符")
    password_hash = subprocess.check_output(
        ["/usr/bin/openssl", "passwd", "-6", "-stdin"],
        input=credentials["web_password"] + "\n", text=True,
    ).strip()
    write_private(output / "web-desktop.htpasswd", f"{username}:{password_hash}\n")
    template = (ROOT / "deploy/nginx-web-desktop.conf.in").read_text()
    write_private(output / "nginx-web-desktop.conf", template.replace("@PUBLIC_IP@", public_ip))
    private_json(output / "deployment.json", {"public_ip": public_ip})
    print(f"云端配置草案已生成：{output}，未应用到服务器")


if __name__ == "__main__":
    main()
