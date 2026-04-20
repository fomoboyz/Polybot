"""Tiny HTTP health endpoint + local read-only dashboard.

Serves:
  GET /health      → 200 if operational, else 503
  GET /ready       → 200 once the first tick has completed
  GET /status      → 200 with JSON snapshot of runtime state
  GET /            → dashboard HTML (if attached)
  GET /api/<name>  → dashboard JSON resources (if attached)

Runs in a dedicated daemon thread; never blocks the main loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .dashboard import Dashboard

log = logging.getLogger(__name__)


class HealthState:
    def __init__(self, max_tick_age_sec: float = 30.0):
        self._max_tick_age = max_tick_age_sec
        self.ready = False
        self.last_tick_ts: float = 0.0
        self._provider: Optional[Callable[[], dict]] = None
        self.dashboard: Optional["Dashboard"] = None

    def mark_tick(self) -> None:
        self.last_tick_ts = time.time()
        self.ready = True

    def register_provider(self, provider: Callable[[], dict]) -> None:
        self._provider = provider

    def attach_dashboard(self, dashboard: "Dashboard") -> None:
        self.dashboard = dashboard

    def is_healthy(self) -> bool:
        if not self.ready:
            return False
        if self.last_tick_ts == 0:
            return False
        age = time.time() - self.last_tick_ts
        if age > self._max_tick_age:
            return False
        if self._provider is not None:
            try:
                status = self._provider()
                if status.get("kill_switch"):
                    return False
                if status.get("breaker_tripped"):
                    return False
            except Exception:
                return False
        return True

    def snapshot(self) -> dict:
        payload = {
            "ready": self.ready,
            "last_tick_age_sec": (
                time.time() - self.last_tick_ts if self.last_tick_ts else None
            ),
            "healthy": self.is_healthy(),
        }
        if self._provider is not None:
            try:
                payload.update(self._provider())
            except Exception as e:
                payload["provider_error"] = str(e)
        return payload


class _Handler(BaseHTTPRequestHandler):
    state: HealthState  # set by server

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path == "/health":
            ok = self.state.is_healthy()
            self._write(200 if ok else 503, {"ok": ok})
        elif path == "/ready":
            self._write(200 if self.state.ready else 503, {"ready": self.state.ready})
        elif path == "/status":
            self._write(200, self.state.snapshot())
        elif path == "/" and self.state.dashboard is not None:
            from .dashboard import HTML
            self._write_html(200, HTML)
        elif path.startswith("/api/") and self.state.dashboard is not None:
            name = path[len("/api/"):].strip("/")
            payload = self.state.dashboard.resource(name)
            if payload is None:
                self._write(404, {"error": f"unknown resource: {name}"})
            else:
                self._write(200, payload)
        else:
            self._write(404, {"error": "not found"})

    def _write(self, code: int, body: dict) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_html(self, code: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # silence
        return


class HealthServer:
    def __init__(self, state: HealthState, port: int = 8080, host: str = "0.0.0.0"):
        self._state = state
        self._host = host
        self._port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server is not None:
            return
        handler_cls = type("Handler", (_Handler,), {"state": self._state})
        try:
            self._server = ThreadingHTTPServer((self._host, self._port), handler_cls)
        except OSError as e:
            log.warning("health server start failed: %s", e)
            self._server = None
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="polybot-health", daemon=True,
        )
        self._thread.start()
        log.info("health endpoint on :%d", self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
