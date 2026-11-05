#!/usr/bin/env python3
"""在回环地址临时运行 Nginx，验证部署模板的页面认证和 WebSocket 边界。"""

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time

import requests

from install_local import CONFIG_DIR, STATE_DIR, private_json
from render_cloud import resolve_public_ip, write_private


def websocket_status(
    port: int, certificate: Path, origin: str, authorization: str | None,
) -> tuple[int, bool]:
    """通过受校验的本地 TLS 连接测试升级与实际 RFB 握手。

    Args:
        port: 本机临时 Nginx 端口。
        certificate: 仅用于此次本地测试的证书。
        origin: 浏览器请求来源。
        authorization: 可选的测试 Basic 认证头，不写入日志。

    Returns:
        HTTP 状态码，以及成功升级后是否收到 VNC 协议标识。
    """
    context = ssl.create_default_context(cafile=str(certificate))
    headers = [
        "GET /websockify HTTP/1.1", f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket", "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", f"Origin: {origin}",
    ]
    if authorization:
        headers.append(f"Authorization: {authorization}")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname="127.0.0.1") as secure:
            secure.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            with secure.makefile("rb") as stream:
                status = int(stream.readline(8192).split()[1])
                for _ in range(100):
                    if stream.readline(8192) == b"\r\n":
                        break
                else:
                    raise RuntimeError("WebSocket 响应头超出预期")
                if status != 101:
                    return status, False
                frame = stream.read(2)
                assert len(frame) == 2 and frame[0] & 0x0F == 2
                assert not frame[1] & 0x80
                length = frame[1] & 0x7F
                assert 0 < length < 126
                return status, stream.read(length).startswith(b"RFB 003.")


def main() -> None:
    """用真实 Nginx 和真实回源服务验证模板，退出时清理临时监听。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    credentials = json.loads((CONFIG_DIR / "credentials.json").read_text())
    bundle = STATE_DIR / "cloud-bundle"
    deployment = json.loads((bundle / "deployment.json").read_text())
    public_ip = resolve_public_ip(deployment["public_ip"], {})
    report = {"scope": "loopback-template-test", "public_deployed": False}
    with tempfile.TemporaryDirectory(prefix="nginx-check-", dir=STATE_DIR) as temporary:
        directory = Path(temporary)
        certificate = directory / "certificate.pem"
        key = directory / "key.pem"
        subprocess.run(
            ["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(certificate), "-days", "1",
             "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"https://127.0.0.1:{port}"
        template = (bundle / "nginx-web-desktop.conf").read_text()
        substitutions = {
            "listen 18443 ssl;": f"listen 127.0.0.1:{port} ssl;",
            "listen [::]:18443 ssl;": "",
            f"https://{public_ip}:18443": origin,
            "127.0.0.1:18097": "127.0.0.1:6080",
            "/etc/letsencrypt/live/web-desktop-ip/fullchain.pem": str(certificate),
            "/etc/letsencrypt/live/web-desktop-ip/privkey.pem": str(key),
            "/etc/nginx/web-desktop.htpasswd": str(bundle / "web-desktop.htpasswd"),
            "/var/log/nginx/web-desktop.access.log": str(directory / "access.log"),
            "/var/log/nginx/web-desktop.error.log": str(directory / "error.log"),
        }
        for source, target in substitutions.items():
            template = template.replace(source, target)
        config = directory / "nginx.conf"
        write_private(config, (
            "daemon off;\nmaster_process off;\n"
            f"pid {directory / 'nginx.pid'};\nerror_log stderr;\n"
            "events {}\nhttp {\n"
            f"client_body_temp_path {directory / 'body'};\n"
            f"proxy_temp_path {directory / 'proxy'};\n"
            f"fastcgi_temp_path {directory / 'fastcgi'};\n"
            f"uwsgi_temp_path {directory / 'uwsgi'};\n"
            f"scgi_temp_path {directory / 'scgi'};\n"
            + template + "\n}\n"
        ))
        command = [str(args.nginx.resolve()), "-p", str(directory), "-c", str(config)]
        subprocess.run(command + ["-t"], check=True, capture_output=True, text=True)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with requests.Session() as client:
                client.trust_env = False
                client.verify = str(certificate)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        client.get(origin, timeout=2)
                        break
                    except requests.ConnectionError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise RuntimeError("临时 Nginx 未能就绪")
                        time.sleep(0.1)
                statuses = {}
                for path in ("/", "/vnc.html", "/app/ui.js"):
                    statuses[path] = client.get(origin + path, timeout=10).status_code
                    assert statuses[path] == 401, statuses
                report["unauthenticated_http"] = statuses
                auth = (credentials["web_username"], credentials["web_password"])
                assert client.get(origin, auth=(auth[0], "invalid"), timeout=10).status_code == 401
                page = client.get(origin, auth=auth, timeout=10)
                assert page.status_code == 200
                assert 'id="noVNC_container"' in page.text
                assert client.get(origin + "/app/ui.js", auth=auth, timeout=10).status_code == 200
                report["authenticated_http"] = 200
            encoded = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            authorization = f"Basic {encoded}"
            assert websocket_status(port, certificate, origin, None)[0] == 401
            assert websocket_status(port, certificate, origin, "Basic aW52YWxpZDppbnZhbGlk")[0] == 401
            assert websocket_status(port, certificate, "https://invalid.example", authorization)[0] == 403
            status, rfb = websocket_status(port, certificate, origin, authorization)
            assert status == 101 and rfb
            report["websocket"] = {
                "no_auth": 401, "wrong_auth": 401, "wrong_origin": 403,
                "authorized_upgrade": status, "received_rfb": rfb,
            }
            report["passed"] = True
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stderr:
                process.stderr.close()
            private_json(STATE_DIR / "gateway-verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
