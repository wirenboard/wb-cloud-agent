import sys
from unittest.mock import patch

import pytest

from wb.cloud_agent.settings import AppSettings


@pytest.fixture
def settings():
    return AppSettings()


@pytest.fixture
def mock_hostname():
    with patch('wb.cloud_agent.main.socket.gethostname', return_value='wirenboard-ART6DDNT'):
        yield


@pytest.fixture
def set_argv(monkeypatch):
    def _inner(argv):
        monkeypatch.setattr(sys, 'argv', argv)

    return _inner


@pytest.fixture
def mock_print():
    with patch('builtins.print') as _mock_print:
        yield _mock_print


@pytest.fixture
def mock_subprocess_run():
    with patch('subprocess.run') as p:
        yield p
