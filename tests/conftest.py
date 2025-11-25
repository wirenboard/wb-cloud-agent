import sys
from http import HTTPStatus as status
from unittest.mock import patch

import pytest

from wb.cloud_agent.settings import AppSettings


@pytest.fixture
def settings():
    return AppSettings(provider_name="default")


@pytest.fixture
def mock_serial_number():
    serial_number = "ART6DDNT"
    with patch("wb.cloud_agent.utils.get_ctrl_serial_number", return_value=serial_number):
        yield serial_number


@pytest.fixture
def set_argv(monkeypatch):
    def _inner(argv):
        monkeypatch.setattr(sys, "argv", argv)

    return _inner


@pytest.fixture
def mock_print():
    with patch("builtins.print") as _mock_print:
        yield _mock_print


@pytest.fixture
def mock_subprocess_run():
    with patch("subprocess.run") as p:
        yield p


@pytest.fixture
def mock_subprocess_bad_request(mock_subprocess_run):  # pylint: disable=redefined-outer-name
    headers = f"HTTP/1.1 {status.BAD_REQUEST} Bad Request\r\n\r\n"
    body = '{"error": "bad request"}'
    meta = '{"code": "400"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout
