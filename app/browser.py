"""Use an authenticated native Chromium tab as the Pippit Web API transport."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import websocket


BASE = "https://www.pippit.ai"
_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}


class BrowserError(Exception):
    pass


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"Pippit CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int) -> dict[str, Any]:
    pages = [target for target in _targets(port) if target.get("type") == "page" and
             (urllib.parse.urlsplit(target.get("url") or "").hostname or "").endswith("pippit.ai")]
    pages.sort(key=lambda target: int("projectId=" in str(target.get("url") or "")), reverse=True)
    if pages:
        return pages[0]
    raise BrowserError("open and log in to www.pippit.ai in the account browser")


def browser_alive(port: int) -> bool:
    try:
        _page(port)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def start_managed_browser(account: dict[str, Any], *, chrome: str, profiles: Path) -> int:
    account_id = int(account["id"])
    port = 19300 + account_id
    profile = profiles / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960"]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = account_proxy_url(account)
    args.append("--proxy-server=" + proxy_url)
    args.append(BASE + "/home")
    with _PROCESS_LOCK:
        if browser_alive(port):
            return port
        existing = _PROCESSES.get(account_id)
        if existing and existing.poll() is None:
            existing.terminate()
            try:
                existing.wait(timeout=4)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=4)
        _remove_stale_profile_locks(profile)
        _PROCESSES[account_id] = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if browser_alive(port):
                return port
            time.sleep(.1)
        try:
            pages = [item for item in _targets(port) if item.get("type") == "page"]
            hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
            redirected = sorted(host for host in hosts if host and not host.endswith("pippit.ai"))
            if redirected:
                raise BrowserError(f"Pippit browser reached {redirected[0]}; configure an account proxy")
        except BrowserError as exc:
            if "configure an account proxy" in str(exc):
                raise
    raise BrowserError("managed Pippit Chrome did not start")


def stop_managed_browser(account_id: int) -> None:
    with _PROCESS_LOCK:
        process = _PROCESSES.pop(account_id, None)
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()


def clear_managed_profile(account: dict[str, Any], profiles: Path) -> None:
    """Delete only the service-owned profile for this numeric account ID."""
    account_id = int(account["id"])
    port = int(account.get("cdp_port") or 0)
    if port == 19300 + account_id and browser_alive(port):
        try:
            CDPBrowser(port).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port):
                break
            time.sleep(.1)
        if browser_alive(port):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90) -> None:
        self.port = int(port)
        self.timeout = timeout

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                         suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"CDP {method} failed: {type(exc).__name__}") from exc
        finally:
            ws.close()

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression,
                                                   "awaitPromise": await_promise,
                                                   "returnByValue": True})
        if "exceptionDetails" in result:
            raise BrowserError("Pippit browser JavaScript request failed")
        value = result.get("result") or {}
        if value.get("subtype") == "error":
            raise BrowserError("Pippit browser JavaScript request returned an error")
        return value.get("value")

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Pippit API path must be same-origin")
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        expression = (
            "(async()=>{const r=await fetch(" + json.dumps(path) + ","
            "{method:'POST',credentials:'include',headers:{'content-type':'application/json'},"
            "body:JSON.stringify(" + payload + ")});"
            "return JSON.stringify({http:r.status,body:await r.json()})})()"
        )
        value = self.evaluate(expression, await_promise=True)
        try:
            response = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrowserError("Pippit returned an unreadable JSON response") from exc
        if int(response.get("http") or 0) >= 400:
            raise BrowserRejected(f"Pippit HTTP {response['http']}")
        data = response.get("body")
        if not isinstance(data, dict):
            raise BrowserError("Pippit returned an invalid response envelope")
        if str(data.get("ret")) != "0":
            raise BrowserRejected(f"Pippit ret={data.get('ret')}: {str(data.get('errmsg') or 'upstream error')[:300]}")
        return data.get("data") or {}

    def screenshot(self) -> str:
        result = self.command("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        if not result.get("data"):
            raise BrowserError("Pippit Chrome returned no screenshot")
        return "data:image/png;base64," + result["data"]

    def click(self, x: float, y: float) -> None:
        for count, kind in enumerate(("mousePressed", "mouseReleased"), 1):
            self.command("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                      "button": "left", "clickCount": 1})

    def scroll(self, delta_y: float) -> None:
        self.command("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 700, "y": 500,
                                                  "deltaX": 0, "deltaY": delta_y})

    def cookies(self) -> list[dict[str, Any]]:
        return self.command("Network.getCookies", {"urls": [BASE + "/"]}).get("cookies") or []

    def project_id(self) -> str:
        value = self.evaluate("new URL(location.href).searchParams.get('projectId')||''")
        return str(value or "")

    def user_info(self) -> dict[str, str]:
        """Read the logged-in web identity injected by Pippit into its page."""
        result = self.evaluate("""(()=>{let d=JSON.parse(window.__businessUserInfoStringify||'{}').data||{};
          return {consumer_uid:String(d.consumer_user?.user_info?.user_id||''),
            workspace_id:String(d.space_info?.workspace_id||''),
            space_id:String(d.space_info?.space_id||''), app_id:'573081'}})()""")
        if not isinstance(result, dict) or not all(result.values()):
            raise BrowserError("Pippit login identity is not available in the browser")
        return result

    def upload_file(self, path: Path) -> dict[str, Any]:
        """Upload through the logged-in web composer's real file input."""
        if not path.is_file():
            raise BrowserError("local upload file is missing")
        with _PROCESS_LOCK:
            lock = _UPLOAD_LOCKS.setdefault(self.port, threading.RLock())
        with lock:
            return self._upload_file_locked(path)

    def _upload_file_locked(self, path: Path) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=90,
                                         suppress_origin=True)
        sequence = 0
        pending: deque[dict[str, Any]] = deque()

        def command(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal sequence
            sequence += 1
            current = sequence
            ws.send(json.dumps({"id": current, "method": method, "params": params or {}}))
            while True:
                event = json.loads(ws.recv())
                if event.get("id") == current:
                    if event.get("error"):
                        raise BrowserError(f"CDP {method}: {event['error'].get('message', 'failed')}")
                    return event.get("result") or {}
                pending.append(event)

        def evaluate(expression: str) -> Any:
            return command("Runtime.evaluate", {"expression": expression,
                "returnByValue": True}).get("result", {}).get("value")

        def click(point: dict[str, float]) -> None:
            for kind in ("mousePressed", "mouseReleased"):
                command("Input.dispatchMouseEvent", {"type": kind, "x": point["x"],
                    "y": point["y"], "button": "left", "clickCount": 1})

        def clear_composer_attachments() -> None:
            count_js = "document.querySelectorAll('[data-slot=\"prompt-composer\"] [data-slot=\"attachment-preview-media-shell\"]').length"
            click_js = "(()=>{const x=document.querySelector('[data-slot=\"prompt-composer\"] [data-slot=\"attachment-preview-remove-button\"]');x?.click();return !!x})()"
            for _ in range(50):
                count = int(evaluate(count_js) or 0)
                if count == 0:
                    return
                if not evaluate(click_js):
                    raise BrowserError("Pippit composer attachment cannot be removed")
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if int(evaluate(count_js) or 0) < count:
                        break
                    time.sleep(.1)
                else:
                    raise BrowserError("Pippit composer attachment did not clear")
            raise BrowserError("Pippit composer has too many stale attachments")

        create_body: dict[str, Any] | None = None
        create_result: dict[str, Any] | None = None
        intercept_chooser = False
        try:
            command("Page.enable")
            command("Network.enable")
            clear_composer_attachments()
            # Current Pippit keeps its composer input mounted even when the upload
            # dropdown does not open. Setting this input dispatches the same change
            # event as the browser file picker and preserves the account's proxy.
            root = command("DOM.getDocument").get("root", {}).get("nodeId")
            node = command("DOM.querySelector", {"nodeId": root,
                "selector": '[data-slot="prompt-composer-resource-runtime"] input[type="file"][multiple]'})
            input_node_id = node.get("nodeId")
            if input_node_id:
                command("DOM.setFileInputFiles", {"files": [str(path.resolve())],
                        "nodeId": input_node_id})
            else:
                # Older composer versions expose the input only after opening a menu.
                command("Page.setInterceptFileChooserDialog", {"enabled": True})
                intercept_chooser = True
                menu_point = evaluate("""(()=>{let x=document.querySelector('[role=menuitem]');
                  let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                if not menu_point:
                    button_point = evaluate(r"""(()=>{let x=[...document.querySelectorAll('[aria-label]')]
                      .find(x=>x.getAttribute('aria-label')==='\u4e0a\u4f20\u53c2\u8003\u7d20\u6750')
                      || document.querySelector('button:has([data-slot="home-input-add-icon"])');
                      let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                    if not button_point:
                        raise BrowserError("Pippit reference upload button is unavailable")
                    click(button_point)
                    time.sleep(.2)
                    menu_point = evaluate("""(()=>{let x=document.querySelector('[role=menuitem]');
                      let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                chooser = next((item for item in pending
                                if item.get("method") == "Page.fileChooserOpened"), None)
                if chooser is None and menu_point:
                    click(menu_point)
                if chooser is None and not menu_point:
                    raise BrowserError("Pippit upload input and menu are unavailable")
                until = time.monotonic() + 8
                while chooser is None and time.monotonic() < until:
                    item = json.loads(ws.recv())
                    pending.append(item)
                    if item.get("method") == "Page.fileChooserOpened":
                        chooser = item
                if not chooser or not chooser.get("params", {}).get("backendNodeId"):
                    raise BrowserError("Pippit did not open the expected file chooser")
                command("DOM.setFileInputFiles", {"files": [str(path.resolve())],
                        "backendNodeId": chooser["params"]["backendNodeId"]})
            tracked: dict[str, str] = {}
            responses: dict[str, str] = {}
            until = time.monotonic() + 90
            while time.monotonic() < until and create_result is None:
                item = pending.popleft() if pending else json.loads(ws.recv())
                method = item.get("method")
                params = item.get("params") or {}
                request_id = str(params.get("requestId") or "")
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    route = urllib.parse.urlsplit(str(request.get("url") or "")).path
                    if route.endswith("/asset/create_v2"):
                        raw = request.get("postData")
                        if raw:
                            try:
                                create_body = json.loads(raw)
                            except ValueError:
                                pass
                elif method == "Network.responseReceived":
                    route = urllib.parse.urlsplit(str((params.get("response") or {}).get("url") or "")).path
                    if route.endswith("/asset/create_v2"):
                        tracked[request_id] = route
                elif method == "Network.loadingFinished" and request_id in tracked:
                    result = command("Network.getResponseBody", {"requestId": request_id})
                    raw = result.get("body") or "{}"
                    if result.get("base64Encoded"):
                        raw = base64.b64decode(raw).decode("utf-8", "replace")
                    try:
                        create_result = json.loads(raw)
                    except ValueError as exc:
                        raise BrowserError("Pippit asset creation returned invalid JSON") from exc
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"Pippit browser upload failed: {type(exc).__name__}") from exc
        finally:
            try:
                clear_composer_attachments()
            except Exception:
                # A failed upload must retain its original error. The next upload
                # will try to clear any remaining composer attachments first.
                pass
            if intercept_chooser:
                try:
                    command("Page.setInterceptFileChooserDialog", {"enabled": False})
                except Exception:
                    pass
            ws.close()
        if create_result is None:
            raise BrowserError("Pippit asset creation did not complete")
        if str(create_result.get("ret")) != "0" and create_body:
            # This registration RPC is idempotent by source asset ID.
            return self.post("/api/biz/v3/asset/create_v2", create_body)
        if str(create_result.get("ret")) != "0":
            raise BrowserRejected(f"Pippit asset registration: {str(create_result.get('errmsg') or 'failed')[:200]}")
        return create_result.get("data") or {}
