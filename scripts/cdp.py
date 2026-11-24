"""표준 라이브러리만 사용하는 로컬 Chrome DevTools Protocol 연결."""

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


class BrowserError(Exception):
    pass


def find_browser():
    configured = os.environ.get("DECK_BROWSER")
    if configured:
        found = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if not found:
            raise BrowserError(f"DECK_BROWSER의 브라우저를 찾을 수 없습니다: {configured} · 실행 파일 경로를 확인하세요")
        return found
    chrome, edge, chromium = [], [], []
    if os.name == "nt":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if root:
                chrome.append(Path(root) / "Google/Chrome/Application/chrome.exe")
                edge.append(Path(root) / "Microsoft/Edge/Application/msedge.exe")
    elif os.sys.platform == "darwin":
        chrome.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        edge.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    else:
        chrome += [Path("/opt/google/chrome/chrome"), Path("/usr/bin/google-chrome")]
        edge += [Path("/opt/microsoft/msedge/msedge"), Path("/usr/bin/microsoft-edge")]
        chromium += [Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]
    groups = ((chrome, ("google-chrome", "google-chrome-stable", "chrome", "chrome.exe")),
              (edge, ("microsoft-edge", "microsoft-edge-stable", "msedge", "msedge.exe")),
              (chromium, ("chromium", "chromium-browser")))
    for paths, names in groups:
        for path in paths:
            if path.is_file():
                return str(path)
        for name in names:
            path = shutil.which(name)
            if path:
                return path
    raise BrowserError("Chrome 또는 Edge를 찾을 수 없습니다 · DECK_BROWSER에 브라우저 실행 파일의 경로를 지정하세요")


class WebSocket:
    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise BrowserError("브라우저 디버깅 주소가 로컬 주소가 아닙니다 · 브라우저 설정을 확인하세요")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=20)
        self.sock.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (f"GET {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            part = self.sock.recv(1)
            if not part or len(header) > 16384:
                raise BrowserError("브라우저 WebSocket 연결에 실패했습니다 · 브라우저를 다시 실행하세요")
            header += part
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        lines = header.decode("latin1").split("\r\n")
        fields = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line
                  for k, v in [line.split(":", 1)]}
        if " 101 " not in lines[0] or fields.get("sec-websocket-accept") != expected:
            self.sock.close()
            raise BrowserError("브라우저 WebSocket 응답이 올바르지 않습니다 · 브라우저를 다시 실행하세요")

    def _read(self, length):
        data = bytearray()
        while len(data) < length:
            part = self.sock.recv(length - len(data))
            if not part:
                raise BrowserError("브라우저 연결이 끊겼습니다 · 브라우저를 다시 실행하세요")
            data.extend(part)
        return bytes(data)

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytes((0x80 | opcode, 0x80 | n))
        elif n < 65536:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", n)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", n)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, value):
        self._send_frame(1, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def receive(self):
        parts = []
        while True:
            a, b = self._read(2)
            length = b & 127
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length > 128 * 1024 * 1024:
                raise BrowserError("브라우저 응답이 너무 큽니다 · 슬라이드 자산의 크기를 줄이세요")
            mask = self._read(4) if b & 128 else None
            payload = self._read(length)
            if mask:
                payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
            opcode = a & 15
            if opcode == 8:
                raise BrowserError("브라우저가 연결을 닫았습니다 · 브라우저를 다시 실행하세요")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            parts.append(payload)
            if a & 128:
                return json.loads(b"".join(parts).decode("utf-8"))

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.events = []

    def call(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        self.ws.send({"id": ident, "method": method, "params": params or {}})
        while True:
            item = self.ws.receive()
            if item.get("id") == ident:
                if "error" in item:
                    raise BrowserError(f"브라우저 명령 {method} 실패: {item['error'].get('message', '')} · 파일과 브라우저를 확인하세요")
                return item.get("result", {})
            if "method" in item:
                self.events.append(item)

    def evaluate(self, expression, await_promise=False):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                  "awaitPromise": await_promise, "userGesture": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "오류")
            raise BrowserError(f"브라우저에서 실행하지 못했습니다: {text.splitlines()[0]} · 파일의 스크립트를 확인하세요")
        return result.get("result", {}).get("value")

    def wait_event(self, method, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            if any(e.get("method") == method for e in self.events):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"브라우저 이벤트 {method}를 기다리다 시간이 지났습니다 · 파일을 확인하세요")
            self.ws.sock.settimeout(remaining)
            try:
                item = self.ws.receive()
            except socket.timeout:
                raise BrowserError(f"브라우저 이벤트 {method}를 기다리다 시간이 지났습니다 · 파일을 확인하세요")
            finally:
                self.ws.sock.settimeout(30)
            if "method" in item:
                self.events.append(item)


class Browser:
    def __init__(self):
        self.process = None
        self.temp = None
        self.ws = None
        self.cdp = None

    def __enter__(self):
        executable = find_browser()
        self.temp = tempfile.TemporaryDirectory(prefix="deck-profile-")
        flags = [executable, "--headless=new", "--remote-debugging-port=0",
                 "--user-data-dir=" + self.temp.name, "--disable-background-networking",
                 "--disable-component-update", "--disable-default-apps", "--no-first-run",
                 "--no-default-browser-check", "--hide-scrollbars", "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            flags.insert(1, "--no-sandbox")
        try:
            self.process = subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            port_file = Path(self.temp.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 20
            port = None
            while port is None:
                try:
                    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    # 파일이 아직 없거나, 브라우저가 쓰는 중이라 잠겨 있거나 비어 있다. 잠깐 뒤 다시 읽는다.
                    if self.process.poll() is not None or time.monotonic() >= deadline:
                        raise BrowserError("브라우저 디버깅 포트를 열지 못했습니다 · 브라우저 실행 파일을 확인하세요") from None
                    time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
                targets = json.load(response)
            target = next((v for v in targets if v.get("type") == "page"), None)
            if not target:
                raise BrowserError("브라우저의 빈 페이지를 찾지 못했습니다 · 다시 실행하세요")
            self.ws = WebSocket(target["webSocketDebuggerUrl"])
            self.cdp = CDP(self.ws)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb):
        if self.cdp and self.process and self.process.poll() is None:
            with contextlib.suppress(Exception):
                self.ws.send({"id": 999999, "method": "Browser.close", "params": {}})
        if self.ws:
            self.ws.close()
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    self.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=5)
        if self.temp:
            self.temp.cleanup()
