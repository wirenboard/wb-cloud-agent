from argparse import ArgumentTypeError
from unittest.mock import patch

import pytest

from wb.cloud_agent.main import main, validate_url


def test_validate_url_valid_https():
    result = validate_url("https://wirenboard.cloud/")
    assert result == "https://wirenboard.cloud/"


def test_validate_url_valid_http():
    result = validate_url("http://example.com")
    assert result == "http://example.com"


def test_validate_url_invalid_scheme():
    with pytest.raises(ArgumentTypeError, match="Invalid URL"):
        validate_url("ftp://example.com")


def test_validate_url_no_netloc():
    with pytest.raises(ArgumentTypeError, match="Invalid URL"):
        validate_url("https://")


def test_validate_url_with_path():
    with pytest.raises(ArgumentTypeError, match="Invalid URL"):
        validate_url("https://example.com/some/path")


def test_main_with_show_providers(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent"])

    with patch("wb.cloud_agent.main.show_providers", return_value=0) as mock_show:
        result = main()

        assert result == 0
        mock_show.assert_called_once()


def test_main_with_add_provider(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "add-provider", "https://example.com"])

    with patch("wb.cloud_agent.main.add_provider", return_value=0) as mock_add:
        result = main()

        assert result == 0
        mock_add.assert_called_once()


def test_main_with_del_provider(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "del-provider", "test_provider"])

    with patch("wb.cloud_agent.main.del_provider", return_value=0) as mock_del:
        result = main()

        assert result == 0
        mock_del.assert_called_once()


def test_main_with_run_daemon(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "run-daemon", "test_provider"])

    with patch("wb.cloud_agent.main.run_daemon", return_value=0) as mock_run:
        result = main()

        assert result == 0
        mock_run.assert_called_once()
        options = mock_run.call_args.args[0]
        assert (options.provider_name, options.config, options.broker) == ("test_provider", None, None)


def test_main_with_run_daemon_config_and_broker(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["wb-cloud-agent", "run-daemon", "-c", "/tmp/custom.conf", "--broker", "tcp://host:1883", "test"],
    )

    with patch("wb.cloud_agent.main.run_daemon", return_value=0) as mock_run:
        main()

    options = mock_run.call_args.args[0]
    assert (options.config, options.broker) == ("/tmp/custom.conf", "tcp://host:1883")


@pytest.mark.parametrize("broker", ["mqtt://host:1883", "tcp://host", "unix://", "localhost:1883"])
def test_main_rejects_invalid_broker_url(monkeypatch, broker):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "run-daemon", "--broker", broker, "test"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2


def test_main_with_cloud_unbind(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wb-cloud-agent", "cloud-unbind", "https://example.com"])

    with patch("wb.cloud_agent.main.del_controller_from_cloud", return_value=0) as mock_unbind:
        result = main()

        assert result == 0
        mock_unbind.assert_called_once()
