#!/usr/bin/env python3
"""使用真实 Chromium 验证 noVNC 画面、桌面输入、程序保持和连接恢复。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from PIL import Image
from playwright.sync_api import TimeoutError as BrowserTimeoutError, sync_playwright

from install_local import CONFIG_DIR, STATE_DIR, private_json
from run_desktop import session_environment


def connect(page, url: str, password: str) -> None:
    """通过 noVNC 标准界面连接共享桌面。

    Args:
        page: Playwright 页面。
        url: 网页入口。
        password: 本机生成的 VNC 密码，不写入 URL 或日志。
    """
    page.goto(url, wait_until="networkidle")
    password_input = page.locator("#noVNC_password_input")
    try:
        password_input.wait_for(state="visible", timeout=20000)
    except BrowserTimeoutError as error:
        page.screenshot(path=str(STATE_DIR / "artifacts/connect-failure.png"))
        state = page.evaluate("""() => ({
            classes: document.documentElement.className,
            status: document.querySelector('#noVNC_status')?.textContent
        })""")
        raise RuntimeError(f"连接未进入认证阶段：{state}") from error
    password_input.fill(password)
    page.locator("#noVNC_credentials_button").click()
    page.wait_for_function(
        "document.documentElement.classList.contains('noVNC_connected')",
        timeout=20000,
    )
    page.wait_for_function(
        "document.querySelector('canvas')?.width >= 1000", timeout=15000,
    )
    page.wait_for_timeout(500)
    page.locator("canvas").first.focus()


def active_dialog(environment: dict[str, str]) -> bool:
    """检查测试对话框是否拥有桌面焦点，避免把输入发到用户的其他窗口。

    Args:
        environment: 目标 X11 会话环境。

    Returns:
        当前活动窗口是否为本次验收对话框。
    """
    window = subprocess.check_output(
        ["xprop", "-root", "_NET_ACTIVE_WINDOW"], env=environment, text=True
    ).split()[-1]
    if window == "0x0":
        return False
    title = subprocess.run(
        ["xprop", "-id", window, "_NET_WM_NAME"], env=environment,
        text=True, capture_output=True, check=False,
    )
    return title.returncode == 0 and "WebDesktop" in title.stdout


def type_keys(page, value: str) -> None:
    """以真实 Shift 按键组合输入 ASCII，避免浏览器自动化生成不一致的物理键码。

    Args:
        page: Playwright 页面。
        value: 需要输入的 ASCII 文本。
    """
    for character in value:
        if character.isupper():
            page.keyboard.press(f"Shift+{character}")
        else:
            page.keyboard.type(character)


def main() -> None:
    """执行浏览器验收并将截图和无密码报告保存在私有状态目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=(
        "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale"
        "&reconnect=1&reconnect_delay=1500&path=websockify"
    ))
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--browser", type=Path, default=Path.home() / (
        ".cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
    ))
    args = parser.parse_args()
    os.umask(0o077)
    artifacts = STATE_DIR / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = json.loads((CONFIG_DIR / "credentials.json").read_text())
    report = {"url": args.url, "page_errors": [], "input": {}, "mobile": {}}
    environment = session_environment() | {
        "GTK_IM_MODULE": "gtk-im-context-simple", "XMODIFIERS": "@im=none"
    }
    dialog = None
    dialog_output = tempfile.TemporaryFile(mode="w+")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(args.browser), headless=True,
            args=["--no-sandbox"],
        )
        options = {"viewport": {"width": 2048, "height": 1200}}
        if args.public:
            options["http_credentials"] = {
                "username": credentials["web_username"],
                "password": credentials["web_password"],
            }
        context = browser.new_context(**options)
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        try:
            connect(page, args.url, credentials["vnc_password"])
            canvas = page.locator("canvas").first
            report["canvas"] = canvas.evaluate(
                "canvas => ({width: canvas.width, height: canvas.height})"
            )
            screenshot = artifacts / "desktop.png"
            canvas.screenshot(path=str(screenshot))
            with Image.open(screenshot) as bitmap:
                colors = bitmap.convert("RGB").resize((64, 36)).getcolors(2304)
                report["canvas"]["sample_colors"] = len(colors or [])
                assert report["canvas"]["sample_colors"] > 10, "桌面画面为空白"

            dialog = subprocess.Popen(
                ["zenity", "--entry", "--title=WebDesktop 验收",
                 "--text=WebDesktop browser input verification", "--width=520"],
                env=environment, stdout=dialog_output, stderr=subprocess.DEVNULL,
                text=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not active_dialog(environment):
                time.sleep(0.1)
            assert active_dialog(environment), "测试窗口未获得焦点，已停止输入"
            page.wait_for_timeout(1200)
            page.screenshot(path=str(artifacts / "input-before.png"))
            canvas.focus()
            type_keys(page, "WebDesktop-20261105")
            page.screenshot(path=str(artifacts / "input-typed.png"))
            assert active_dialog(environment), "输入过程中测试窗口失去焦点"
            page.keyboard.press("Control+A")
            type_keys(page, "WebDesktop-keyboard-ok")
            page.screenshot(path=str(artifacts / "input-replaced.png"))
            assert active_dialog(environment), "组合键操作过程中测试窗口失去焦点"
            page.keyboard.press("Enter")
            try:
                dialog.wait(timeout=15)
                dialog_output.seek(0)
                output = dialog_output.read()
            except subprocess.TimeoutExpired:
                page.screenshot(path=str(artifacts / "input-failure.png"))
                raise
            assert dialog.returncode == 0, "测试对话框未正常提交"
            report["input"]["keyboard_and_ctrl_a"] = output.strip()
            assert output.strip() == "WebDesktop-keyboard-ok", repr(output)
            dialog = None

            # 浏览器退出不应终止远端程序，使用独立对话框验证会话保持。
            dialog = subprocess.Popen(
                ["zenity", "--info", "--title=WebDesktop 会话保持验收",
                 "--text=WebDesktop session persistence verification"],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            page.close()
            report["program_survives_browser_close"] = dialog.poll() is None
            assert report["program_survives_browser_close"]
            page = context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            connect(page, args.url, credentials["vnc_password"])
            report["reopen_connected"] = True

            if not args.public:
                subprocess.run(
                    ["systemctl", "--user", "restart", "web-desktop-web"],
                    env=environment, check=True,
                )
                page.wait_for_function(
                    "!document.documentElement.classList.contains('noVNC_connected')",
                    timeout=10000,
                )
                page.wait_for_function(
                    "document.documentElement.classList.contains('noVNC_connected')",
                    timeout=25000,
                )
                report["automatic_reconnect"] = True

            if args.duration:
                started = time.monotonic()
                while time.monotonic() - started < args.duration:
                    page.wait_for_timeout(1000)
                    assert page.evaluate(
                        "document.documentElement.classList.contains('noVNC_connected')"
                    ), "持续使用期间连接断开"
                report["connected_seconds"] = int(time.monotonic() - started)

            mobile_options = options | {
                "viewport": {"width": 980, "height": 640}, "has_touch": True
            }
            mobile = browser.new_context(**mobile_options)
            mobile_page = mobile.new_page()
            connect(mobile_page, args.url, credentials["vnc_password"])
            if dialog is not None and dialog.poll() is None:
                dialog.terminate()
                dialog.wait(timeout=5)
            dialog_output.seek(0)
            dialog_output.truncate()
            dialog = subprocess.Popen(
                ["zenity", "--entry", "--title=WebDesktop 中文输入验收",
                 "--text=WebDesktop soft keyboard verification", "--width=520"],
                env=environment, stdout=dialog_output, stderr=subprocess.DEVNULL,
                text=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not active_dialog(environment):
                mobile_page.wait_for_timeout(100)
            assert active_dialog(environment), "中文测试窗口未获得焦点"
            mobile_page.locator("#noVNC_keyboardinput").focus()
            mobile_page.keyboard.insert_text("中文输入验证")
            mobile_page.keyboard.press("Enter")
            dialog.wait(timeout=10)
            dialog_output.seek(0)
            report["input"]["soft_keyboard_chinese"] = dialog_output.read().strip()
            assert report["input"]["soft_keyboard_chinese"] == "中文输入验证"
            dialog = None
            mobile_page.screenshot(path=str(artifacts / "touch-viewport.png"))
            report["mobile"] = mobile_page.evaluate("""() => ({
                touch: navigator.maxTouchPoints > 0,
                viewportWidth: innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                connected: document.documentElement.classList.contains('noVNC_connected')
            })""")
            assert report["mobile"]["touch"] and report["mobile"]["connected"]
            assert report["mobile"]["documentWidth"] <= report["mobile"]["viewportWidth"]
            mobile.close()
            assert not report["page_errors"], report["page_errors"]
            report["passed"] = True
        finally:
            if dialog is not None and dialog.poll() is None:
                dialog.terminate()
                dialog.wait(timeout=5)
            dialog_output.close()
            private_json(STATE_DIR / "browser-verification.json", report)
            browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
