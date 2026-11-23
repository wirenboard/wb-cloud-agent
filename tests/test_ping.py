import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, call

import pytest

from wb.cloud_agent.handlers.ping import USER_AGENT, wait_for_cloud_reachable

# a scripted answer that drops the connection without a response
DROP = None


@pytest.fixture(autouse=True)
def bypass_proxy(monkeypatch):
    """
    Reach the local test server directly: urllib honours http_proxy, which a build environment
    (sbuild) may set. no_proxy is checked on every request, even by an already built opener.
    """
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


def make_stop_event(*is_set):
    """
    A stop event whose is_set() answers are scripted; wait() returns at once.
    """
    stop = MagicMock()
    stop.is_set.side_effect = list(is_set)
    stop.wait.return_value = False
    return stop


class FakeCloud:  # pylint: disable=too-few-public-methods
    """
    A local HTTP server answering the scripted statuses in turn; a 30x redirects to /final (200).
    """

    def __init__(self):
        self.script = []
        self.requests = []
        self.user_agents = []
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):  # pylint: disable=invalid-name
                cloud.requests.append((self.command, self.path))
                cloud.user_agents.append(self.headers.get("User-Agent"))
                if self.path == "/final":
                    self.send_response(200)
                    self.end_headers()
                    return
                status = cloud.script.pop(0)
                if status is DROP:  # HTTP/1.0: the connection is closed after the handler
                    return
                self.send_response(status)
                if status in (301, 302, 303, 307, 308):
                    self.send_header("Location", "/final")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ping"


@pytest.fixture(name="cloud")
def fixture_cloud():
    cloud = FakeCloud()
    thread = threading.Thread(target=cloud.server.serve_forever, daemon=True)
    thread.start()
    yield cloud
    cloud.server.shutdown()
    cloud.server.server_close()


def test_wait_for_cloud_success_first_try(cloud):
    cloud.script = [200]
    stop = make_stop_event(False)

    assert wait_for_cloud_reachable(cloud.url, 5, stop) is True

    assert cloud.requests == [("HEAD", "/ping")]
    assert cloud.user_agents == [USER_AGENT]
    stop.wait.assert_not_called()


def test_wait_for_cloud_success_after_retries(cloud):
    cloud.script = [500, 404, 200]
    stop = make_stop_event(False, False, False)

    assert wait_for_cloud_reachable(cloud.url, 2, stop) is True

    assert len(cloud.requests) == 3
    assert stop.wait.call_args_list == [call(2), call(2)]


@pytest.mark.parametrize("status", [302, 307, 304])
def test_wait_for_cloud_success_on_redirect(cloud, status):
    """
    A redirect is followed; a 3xx answer that is not a redirect (304) counts as reachable too.
    """
    cloud.script = [status]
    stop = make_stop_event(False)

    assert wait_for_cloud_reachable(cloud.url, 5, stop) is True

    expected = [("HEAD", "/ping")] + ([("HEAD", "/final")] if status != 304 else [])
    assert cloud.requests == expected
    stop.wait.assert_not_called()


def test_wait_for_cloud_dropped_connection_then_success(cloud):
    cloud.script = [DROP, 200]
    stop = make_stop_event(False, False)

    assert wait_for_cloud_reachable(cloud.url, 3, stop) is True

    assert len(cloud.requests) == 2
    stop.wait.assert_called_once_with(3)


def test_wait_for_cloud_connection_refused_until_stopped():
    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stop = make_stop_event(False, True)

    assert wait_for_cloud_reachable(f"http://127.0.0.1:{port}", 4, stop) is False

    stop.wait.assert_called_once_with(4)


def test_wait_for_cloud_stops_on_request(cloud):
    """
    A stop requested while the cloud is down ends the wait with False instead of another attempt.
    """
    cloud.script = [503]
    stop = make_stop_event(False, True)

    assert wait_for_cloud_reachable(cloud.url, 10, stop) is False

    assert len(cloud.requests) == 1
    stop.wait.assert_called_once_with(10)
