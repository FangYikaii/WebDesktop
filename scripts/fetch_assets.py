#!/usr/bin/env python3
"""下载并校验固定版本的官方桌面共享资源，安装到当前用户目录。"""

import hashlib
import os
from pathlib import Path
import platform
import tarfile
import tempfile

import requests

from install_local import DATA_DIR, STATE_DIR, private_json


ASSETS = (
    {
        "name": "TigerVNC",
        "version": "1.16.2",
        "url": "https://downloads.sourceforge.net/project/tigervnc/stable/1.16.2/"
               "tigervnc-1.16.2.x86_64.tar.gz",
        "archive": "tigervnc-1.16.2.x86_64.tar.gz",
        "sha256": "5b70c84baefc09a030cfc78315c34ccb55b2a0dde4092b7da67a1962c5f0dea6",
        "root": "tigervnc-1.16.2.x86_64",
        "destination": "tigervnc-1.16.2.x86_64",
        "check": "usr/bin/x0vncserver",
    },
    {
        "name": "noVNC",
        "version": "1.7.0",
        "url": "https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v1.7.0",
        "archive": "novnc-v1.7.0.tar.gz",
        "sha256": "b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6",
        "root": "noVNC-1.7.0",
        "destination": "novnc",
        "check": "vnc.html",
    },
)


def checksum(path: Path) -> str:
    """流式计算资源校验值。

    Args:
        path: 下载的文件路径。

    Returns:
        文件的 SHA-256 十六进制校验值。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_asset(asset: dict[str, str], downloads: Path) -> None:
    """校验官方归档后安全解压，保留已经安装的资源。

    Args:
        asset: 固定版本、来源与校验值。
        downloads: 私有下载缓存目录。
    """
    archive = downloads / asset["archive"]
    if not archive.is_file():
        partial = archive.with_suffix(".partial")
        try:
            with requests.get(asset["url"], stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for block in response.iter_content(1024 * 1024):
                        handle.write(block)
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if checksum(archive) != asset["sha256"]:
        raise RuntimeError(f"资源校验失败，停止安装：{archive}")
    destination = DATA_DIR / asset["destination"]
    if destination.exists():
        if not (destination / asset["check"]).is_file():
            raise RuntimeError(f"已有资源不完整，请人工检查：{destination}")
        return
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as temporary:
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        source = Path(temporary) / asset["root"]
        if not (source / asset["check"]).is_file():
            raise RuntimeError(f"归档目录不符合预期：{archive}")
        source.rename(destination)


def main() -> None:
    """安装 x86_64 Linux 资源并记录来源与校验值。"""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("本安装脚本仅支持当前方案的 x86_64 Linux 主机")
    os.umask(0o077)
    downloads = DATA_DIR / "downloads"
    downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for asset in ASSETS:
        install_asset(asset, downloads)
        print(f"{asset['name']} {asset['version']}：归档校验通过，资源已就绪")
    private_json(STATE_DIR / "assets.json", {"assets": ASSETS})


if __name__ == "__main__":
    main()
