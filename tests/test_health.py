import json
import time
import urllib.request

from polybot.health import HealthServer, HealthState


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str):
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.status, json.loads(r.read())


def _get_expect_error(url: str) -> int:
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_health_reports_healthy_after_tick():
    state = HealthState(max_tick_age_sec=5)
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        code = _get_expect_error(f"http://127.0.0.1:{port}/health")
        assert code == 503  # not ready yet
        state.mark_tick()
        status, body = _get(f"http://127.0.0.1:{port}/health")
        assert status == 200 and body["ok"] is True
        status, body = _get(f"http://127.0.0.1:{port}/status")
        assert status == 200 and body["ready"] is True
    finally:
        srv.stop()


def test_health_drops_when_stale():
    state = HealthState(max_tick_age_sec=0.1)
    state.mark_tick()
    time.sleep(0.2)
    assert state.is_healthy() is False


def test_health_reflects_provider():
    state = HealthState(max_tick_age_sec=5)
    state.register_provider(lambda: {"kill_switch": True})
    state.mark_tick()
    assert state.is_healthy() is False


def test_unknown_path_returns_404():
    state = HealthState()
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        code = _get_expect_error(f"http://127.0.0.1:{port}/garbage")
        assert code == 404
    finally:
        srv.stop()
