#!/usr/bin/env python3
"""Direct network control for a GL.iNet Comet KVM.

Uses only the Comet HTTPS API: JPEG snapshots for sight and USB HID for input.
No browser, phone, screen-sharing service, or agent on the controlled Mac is
required.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_HOST = "192.168.1.67"
KEYCHAIN_ACCOUNT = "admin"
KEYCHAIN_SERVICE = "glkvm-comet"
TARGET_KEYCHAIN_ACCOUNT = "husky"
TARGET_KEYCHAIN_SERVICE = "glkvm-target-login"


class GlkvmError(RuntimeError):
    """Expected operator-facing failure."""


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def find_keychain_password(service: str, account: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.rstrip("\n")


def keychain_password() -> Optional[str]:
    return find_keychain_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def save_keychain_password(password: str, service: str, account: str) -> None:
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-a",
                account,
                "-s",
                service,
                "-w",
                password,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise GlkvmError("could not save credential in macOS Keychain") from exc


class Client:
    def __init__(self, host: str, password: str, timeout: float = 15.0) -> None:
        self.host = host
        self.password = password
        self.timeout = timeout
        self.context = tls_context()
        self.token = ""

    def login(self) -> None:
        body = urllib.parse.urlencode({"user": "admin", "passwd": self.password}).encode()
        request = urllib.request.Request(
            "https://{}/api/auth/login".format(self.host),
            data=body,
            method="POST",
        )
        payload = self._open_json(request)
        try:
            self.token = str(payload["result"]["token"])
        except (KeyError, TypeError) as exc:
            raise GlkvmError("Comet login did not return an auth token") from exc

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        raw: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        if not self.token:
            self.login()
        url = "https://{}{}".format(self.host, path)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Cookie": "auth_token={}".format(self.token)},
        )
        try:
            with urllib.request.urlopen(
                request,
                context=self.context,
                timeout=(timeout or self.timeout),
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise GlkvmError("{} {} returned HTTP {}: {}".format(
                method, path, exc.code, message[:300]
            )) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GlkvmError("{} {} failed: {}".format(method, path, exc)) from exc
        if raw:
            return body
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GlkvmError("{} {} returned invalid JSON".format(method, path)) from exc

    def _open_json(self, request: urllib.request.Request) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self.timeout
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise GlkvmError("Comet login returned HTTP {}: {}".format(
                exc.code, message[:300]
            )) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GlkvmError("could not reach Comet at {}: {}".format(self.host, exc)) from exc

    def get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, query=query)

    def post(
        self,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
    ) -> Any:
        return self.request("POST", path, query=query, data=data)

    def hid(self) -> Dict[str, Any]:
        return self.get("/api/hid")["result"]

    def streamer(self) -> Any:
        return self.get("/api/streamer")["result"].get("streamer")

    def snapshot(self) -> bytes:
        return self.request(
            "GET",
            "/api/streamer/snapshot",
            query={"allow_offline": "true"},
            raw=True,
            timeout=25,
        )

    def stream_guard(self) -> "StreamGuard":
        if not self.token:
            self.login()
        return StreamGuard(self)

    def wake(self) -> None:
        state = self.hid()
        active = state.get("mouse", {}).get("outputs", {}).get("active", "")
        available = state.get("mouse", {}).get("outputs", {}).get("available", [])
        if "usb_rel" in available:
            if active != "usb_rel":
                self.post("/api/hid/set_params", {"mouse_output": "usb_rel"})
                time.sleep(0.5)
            for delta_x, delta_y in ((80, 40), (-40, -20), (20, 10)):
                self.post(
                    "/api/hid/events/send_mouse_relative",
                    {"delta_x": delta_x, "delta_y": delta_y},
                )
                time.sleep(0.2)
            if active and active != "usb_rel":
                time.sleep(0.5)
                self.post("/api/hid/set_params", {"mouse_output": active})
        # ArrowLeft cannot enter a character in a password field but reliably
        # counts as a real key press for macOS display wake.
        self.post("/api/hid/events/send_key", {"key": "ArrowLeft"})
        self.post("/api/hid/events/send_key", {"key": "ShiftLeft"})

    def ensure_absolute(self) -> None:
        state = self.hid()
        if not state.get("mouse", {}).get("online"):
            raise GlkvmError("Comet mouse HID is offline; run recover")
        if not state.get("mouse", {}).get("absolute"):
            available = state.get("mouse", {}).get("outputs", {}).get("available", [])
            if "usb" not in available:
                raise GlkvmError("Comet absolute mouse output is unavailable")
            self.post("/api/hid/set_params", {"mouse_output": "usb"})
            time.sleep(0.35)

    def move(self, x: int, y: int, width: int, height: int) -> Tuple[int, int]:
        if width < 2 or height < 2:
            raise GlkvmError("width and height must both be at least 2")
        if not (0 <= x < width and 0 <= y < height):
            raise GlkvmError("point ({},{}) is outside {}x{}".format(x, y, width, height))
        self.ensure_absolute()
        to_x = round((x / float(width - 1)) * 65535 - 32768)
        to_y = round((y / float(height - 1)) * 65535 - 32768)
        self.post(
            "/api/hid/events/send_mouse_move",
            {"to_x": to_x, "to_y": to_y},
        )
        return (to_x, to_y)


class StreamGuard:
    """Keep KVMD's capture pipeline running through its normal WebSocket."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.socket: Optional[ssl.SSLSocket] = None
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def __enter__(self) -> "StreamGuard":
        raw = socket.create_connection((self.client.host, 443), timeout=10)
        wrapped = self.client.context.wrap_socket(raw, server_hostname=self.client.host)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        query_token = urllib.parse.quote(self.client.token, safe="")
        request = (
            "GET /api/ws?auth_token={}&stream=true HTTP/1.1\r\n"
            "Host: {}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://{}\r\n"
            "Cookie: auth_token={}\r\n\r\n"
        ).format(
            query_token,
            self.client.host,
            key,
            self.client.host,
            self.client.token,
        )
        wrapped.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 65536:
            chunk = wrapped.recv(4096)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            wrapped.close()
            raise GlkvmError("Comet stream WebSocket handshake failed: {}".format(
                status_line.decode("ascii", errors="replace")
            ))
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        if expected.lower().encode("ascii") not in response.lower():
            wrapped.close()
            raise GlkvmError("Comet stream WebSocket returned an invalid accept key")
        wrapped.settimeout(0.5)
        self.socket = wrapped
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        return self

    def _send_binary(self, payload: bytes) -> None:
        assert self.socket is not None
        if len(payload) > 125:
            raise GlkvmError("internal WebSocket payload is too large")
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        frame = bytes([0x82, 0x80 | len(payload)]) + mask + masked
        self.socket.sendall(frame)

    def _pump(self) -> None:
        assert self.socket is not None
        last_ping = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                if now - last_ping >= 1:
                    self._send_binary(b"\x00")
                    last_ping = now
                try:
                    self.socket.recv(65536)
                except socket.timeout:
                    pass
            except OSError:
                return

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop.set()
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def jpeg_size(data: bytes) -> Tuple[int, int]:
    if not data.startswith(b"\xff\xd8"):
        raise GlkvmError("snapshot was not a JPEG image")
    index = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 8 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 1 >= len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if marker in sof_markers and index + 7 < len(data):
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return (width, height)
        index += length
    raise GlkvmError("could not determine JPEG dimensions")


def healthy_hid(state: Dict[str, Any]) -> bool:
    return bool(
        state.get("connected")
        and state.get("enabled")
        and state.get("online")
        and state.get("keyboard", {}).get("online")
        and state.get("mouse", {}).get("online")
    )


def compact_status(client: Client) -> Dict[str, Any]:
    hid = client.hid()
    streamer = client.streamer()
    video = None
    if streamer:
        video = {
            "signal": streamer.get("hdmi", {}).get("signal"),
            "online": streamer.get("h264", {}).get("online"),
            "fps": streamer.get("h264", {}).get("fps"),
            "resolution": streamer.get("source", {}).get("real_resolution"),
        }
    return {
        "host": client.host,
        "healthy": healthy_hid(hid),
        "hid": {
            "connected": hid.get("connected"),
            "enabled": hid.get("enabled"),
            "online": hid.get("online"),
            "keyboard_online": hid.get("keyboard", {}).get("online"),
            "mouse_online": hid.get("mouse", {}).get("online"),
            "mouse_absolute": hid.get("mouse", {}).get("absolute"),
            "mouse_output": hid.get("mouse", {}).get("outputs", {}).get("active"),
        },
        "video": video,
    }


def capture_with_wake(client: Client, output: Path, wait_seconds: float) -> Dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    woke = False
    last_wake = 0.0
    last_error = ""
    with client.stream_guard():
        time.sleep(2)
        while True:
            try:
                data = client.snapshot()
                width, height = jpeg_size(data)
                output = output.expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return {
                    "ok": True,
                    "path": str(output),
                    "width": width,
                    "height": height,
                    "bytes": len(data),
                    "woke_display": woke,
                }
            except GlkvmError as exc:
                last_error = str(exc)
                if time.monotonic() >= deadline:
                    break
                if not woke or time.monotonic() - last_wake >= 6:
                    client.wake()
                    woke = True
                    last_wake = time.monotonic()
                time.sleep(2)
    raise GlkvmError("could not capture video after wake attempt: {}".format(last_error))


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=os.environ.get("GLKVM_HOST", DEFAULT_HOST))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("configure", help="securely save the Comet password in macOS Keychain")
    sub.add_parser(
        "configure-target-login",
        help="securely save the controlled Mac login password in macOS Keychain",
    )
    sub.add_parser("status", help="report direct HID and current video health")
    sub.add_parser("wake", help="wake the controlled computer through USB HID")

    shot = sub.add_parser("screenshot", help="save a current JPEG from the Comet capture input")
    shot.add_argument("--output", "-o", type=Path, required=True)
    shot.add_argument("--wait", type=float, default=20.0)

    move = sub.add_parser("move", help="move the absolute mouse using screenshot pixel coordinates")
    move.add_argument("--x", type=int, required=True)
    move.add_argument("--y", type=int, required=True)
    move.add_argument("--width", type=int, default=1920)
    move.add_argument("--height", type=int, default=1080)

    click = sub.add_parser("click", help="optionally move, then click a mouse button")
    click.add_argument("--x", type=int)
    click.add_argument("--y", type=int)
    click.add_argument("--width", type=int, default=1920)
    click.add_argument("--height", type=int, default=1080)
    click.add_argument("--button", choices=["left", "right", "middle", "up", "down"], default="left")

    scroll = sub.add_parser("scroll", help="scroll; positive dy means down, negative means up")
    scroll.add_argument("--dy", type=int, required=True)
    scroll.add_argument("--dx", type=int, default=0)

    key = sub.add_parser("key", help="press and release one web KeyboardEvent code")
    key.add_argument("key")

    shortcut = sub.add_parser("shortcut", help="press a comma-separated key chord")
    shortcut.add_argument("keys", help="example: MetaLeft,KeyL")

    type_parser = sub.add_parser("type", help="type text through the Comet keymap")
    type_parser.add_argument("text")
    type_parser.add_argument("--slow", action="store_true")

    sub.add_parser(
        "login-target",
        help="type the separately stored target-Mac password and press Enter",
    )

    open_url = sub.add_parser("open-url", help="open a URL in the foreground browser using HID only")
    open_url.add_argument("url")

    recover = sub.add_parser("recover", help="reboot the Comet only when its USB HID is unhealthy")
    recover.add_argument("--wait", type=float, default=180.0)

    video_recover = sub.add_parser(
        "recover-video",
        help="recover a missing capture image without rebooting the target computer",
    )
    video_recover.add_argument("--output", "-o", type=Path, required=True)
    video_recover.add_argument("--wait", type=float, default=25.0)

    sub.add_parser("reboot-comet", help="explicitly reboot the Comet and wait for healthy HID")
    return parser


def password_or_error() -> str:
    password = os.environ.get("GLKVM_PASSWORD") or keychain_password()
    if not password:
        raise GlkvmError(
            "no Comet credential; run 'glkvm_control.py configure' once on this controller"
        )
    return password


def wait_for_healthy(host: str, password: str, wait_seconds: float) -> Dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            client = Client(host, password, timeout=8)
            state = compact_status(client)
            if state["healthy"]:
                return state
            last_error = json.dumps(state["hid"], sort_keys=True)
        except GlkvmError as exc:
            last_error = str(exc)
        time.sleep(3)
    raise GlkvmError("Comet HID did not recover before timeout: {}".format(last_error))


def reboot_comet(client: Client, wait_seconds: float) -> Dict[str, Any]:
    try:
        client.post("/api/upgrade/reboot")
    except GlkvmError:
        # A successful reboot can close the socket before the HTTP response.
        pass
    time.sleep(5)
    return wait_for_healthy(client.host, client.password, wait_seconds)


def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "configure":
        password = getpass.getpass("Comet admin password: ")
        if not password:
            raise GlkvmError("password cannot be empty")
        save_keychain_password(password, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        client = Client(args.host, password)
        state = compact_status(client)
        return {"configured": True, "keychain_service": KEYCHAIN_SERVICE, "status": state}

    if args.command == "configure-target-login":
        password = getpass.getpass("Controlled Mac login password: ")
        if not password:
            raise GlkvmError("password cannot be empty")
        save_keychain_password(password, TARGET_KEYCHAIN_SERVICE, TARGET_KEYCHAIN_ACCOUNT)
        return {
            "configured": True,
            "keychain_service": TARGET_KEYCHAIN_SERVICE,
            "account": TARGET_KEYCHAIN_ACCOUNT,
        }

    password = password_or_error()
    client = Client(args.host, password)

    if args.command == "status":
        return compact_status(client)
    if args.command == "wake":
        client.wake()
        return {"ok": True, "action": "wake", "status": compact_status(client)}
    if args.command == "screenshot":
        return capture_with_wake(client, args.output, args.wait)
    if args.command == "move":
        absolute = client.move(args.x, args.y, args.width, args.height)
        return {"ok": True, "pixel": [args.x, args.y], "absolute": list(absolute)}
    if args.command == "click":
        if (args.x is None) != (args.y is None):
            raise GlkvmError("provide both --x and --y, or neither")
        absolute = None
        if args.x is not None:
            absolute = client.move(args.x, args.y, args.width, args.height)
            time.sleep(0.2)
        client.post("/api/hid/events/send_mouse_button", {"button": args.button})
        return {
            "ok": True,
            "button": args.button,
            "pixel": ([args.x, args.y] if args.x is not None else None),
            "absolute": (list(absolute) if absolute else None),
        }
    if args.command == "scroll":
        if not (-127 <= args.dx <= 127 and -127 <= args.dy <= 127):
            raise GlkvmError("dx and dy must be between -127 and 127")
        # Browser wheel delta is opposite to the USB HID wheel sign.
        client.post(
            "/api/hid/events/send_mouse_wheel",
            {"delta_x": -args.dx, "delta_y": -args.dy},
        )
        return {"ok": True, "dx": args.dx, "dy": args.dy}
    if args.command == "key":
        client.post("/api/hid/events/send_key", {"key": args.key})
        return {"ok": True, "key": args.key}
    if args.command == "shortcut":
        client.post("/api/hid/events/send_shortcut", {"keys": args.keys})
        return {"ok": True, "keys": args.keys}
    if args.command == "type":
        client.post(
            "/api/hid/print",
            {"limit": 0, "keymap": "en-us", "slow": str(args.slow).lower()},
            data=args.text.encode("utf-8"),
        )
        return {"ok": True, "characters": len(args.text), "slow": args.slow}
    if args.command == "login-target":
        target_password = find_keychain_password(
            TARGET_KEYCHAIN_SERVICE, TARGET_KEYCHAIN_ACCOUNT
        )
        if not target_password:
            raise GlkvmError(
                "target login credential is not configured; run configure-target-login locally"
            )
        client.post("/api/hid/events/send_shortcut", {"keys": "MetaLeft,KeyA"})
        client.post("/api/hid/events/send_key", {"key": "Backspace"})
        time.sleep(0.25)
        client.post(
            "/api/hid/print",
            {"limit": 0, "keymap": "en-us", "slow": "true"},
            data=target_password.encode("utf-8"),
        )
        client.post("/api/hid/events/send_key", {"key": "Enter"})
        return {"ok": True, "action": "login-target", "credential_exposed": False}
    if args.command == "open-url":
        parsed = urllib.parse.urlparse(args.url)
        if parsed.scheme not in ("http", "https"):
            raise GlkvmError("URL must begin with http:// or https://")
        client.post("/api/hid/events/send_shortcut", {"keys": "MetaLeft,KeyL"})
        time.sleep(0.35)
        client.post(
            "/api/hid/print",
            {"limit": 0, "keymap": "en-us", "slow": "false"},
            data=args.url.encode("utf-8"),
        )
        client.post("/api/hid/events/send_key", {"key": "Enter"})
        return {"ok": True, "url": args.url, "method": "usb_hid"}
    if args.command == "recover":
        state = compact_status(client)
        if state["healthy"]:
            return {"rebooted": False, "reason": "HID already healthy", "status": state}
        recovered = reboot_comet(client, args.wait)
        return {"rebooted": True, "reason": "unhealthy HID", "status": recovered}
    if args.command == "recover-video":
        errors = []
        try:
            shot = capture_with_wake(client, args.output, args.wait)
            return {"comet_rebooted": False, "streamer_reset": False, "screenshot": shot}
        except GlkvmError as exc:
            errors.append(str(exc))
        client.post("/api/streamer/reset")
        time.sleep(2)
        try:
            shot = capture_with_wake(client, args.output, max(args.wait, 15))
            return {"comet_rebooted": False, "streamer_reset": True, "screenshot": shot}
        except GlkvmError as exc:
            errors.append(str(exc))
        reboot_comet(client, 180)
        shot = capture_with_wake(client, args.output, max(args.wait, 30))
        return {
            "comet_rebooted": True,
            "streamer_reset": True,
            "prior_errors": errors,
            "screenshot": shot,
        }
    if args.command == "reboot-comet":
        recovered = reboot_comet(client, 180)
        return {"rebooted": True, "status": recovered}
    raise GlkvmError("unknown command")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except GlkvmError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
