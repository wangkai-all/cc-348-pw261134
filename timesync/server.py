"""HTTP 服务：REST JSON API + 静态网页（仅用 Python 标准库）。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .app import App, SCENARIOS

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TimeSyncDemo/1.0"

    # ---------- 基础工具 ----------

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args) -> None:  # 静音默认访问日志
        return

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._send_json(self.app.state())
        if path == "/api/scenarios":
            return self._send_json({"scenarios": [
                {"name": k, "label": v["label"]} for k, v in SCENARIOS.items()]})
        if path in _STATIC:
            return self._serve_static(path)
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/devices/rate":
                self.app.set_device_rate(str(data["device_id"]), float(data["rate"]))
            elif path == "/api/devices/pause":
                self.app.pause_device(str(data["device_id"]))
            elif path == "/api/devices/resume":
                self.app.resume_device(str(data["device_id"]))
            elif path == "/api/devices/restart":
                self.app.restart_device(str(data["device_id"]))
            elif path == "/api/calibrations":
                result = self.app.add_calibration(
                    device_id=str(data["device_id"]),
                    boot_epoch=data.get("boot_epoch"),
                    device_time_ms=_opt_float(data.get("device_time_ms")),
                    ref_time_ms=_opt_float(data.get("ref_time_ms")),
                    note=str(data.get("note", "")),
                )
                return self._send_json({"ok": True, "calibration": result})
            elif path == "/api/calculator/pause":
                self.app.calculator.pause()
            elif path == "/api/calculator/resume":
                self.app.calculator.resume()
            elif path == "/api/publisher/auto":
                self.app.publisher.set_auto_confirm(bool(data["enabled"]))
            elif path == "/api/publisher/confirm":
                return self._send_json(self.app.confirm_current_candidate())
            elif path == "/api/reset":
                scenario = data.get("scenario")
                self.app.reset(scenario=str(scenario) if scenario else None)
            else:
                return self._send_json({"error": "unknown endpoint"}, status=404)
            return self._send_json({"ok": True})
        except KeyError as exc:
            self._send_json({"error": f"缺少参数: {exc}"}, status=400)
        except ValueError as exc:
            self._send_json({"error": f"参数非法: {exc}"}, status=400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def _serve_static(self, path: str) -> None:
        filename, ctype = _STATIC[path]
        target = WEB_DIR / filename
        if not target.is_file():
            self._send_json({"error": "static file missing"}, status=404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def build_server(host: str = "127.0.0.1", port: int = 8000,
                 scenario: str = "default") -> ThreadingHTTPServer:
    app = App(scenario=scenario)
    app.start_drivers()
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8000, scenario: str = "default") -> None:
    server = build_server(host, port, scenario)
    print(f"漂移时钟校时演示系统已启动: http://{host}:{port}/")
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        server.app.shutdown()
        server.shutdown()
