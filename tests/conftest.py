import sys
from unittest.mock import patch

import pytest

from wb.cloud_agent.settings import AppSettings


@pytest.fixture
def settings():
    return AppSettings("default")


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
