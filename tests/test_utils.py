import json
from unittest.mock import MagicMock

import pytest

from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import (
    ConfigError,
    get_controller_url,
    normalize_base_url,
    parse_headers,
    quarantine_broken_file,
    read_json_config,
    read_plaintext_config,
    show_providers_table,
    start_and_enable_service,
    stop_and_disable_service,
    write_to_file,
)


def test_base_url_to_agent_url(settings: AppSettings):
    assert (
        settings.base_url_to_agent_url("https://cloud-staging.wirenboard.com/")
        == "https://agent.cloud-staging.wirenboard.com/api-agent/v1/"
    )
    assert (
        settings.base_url_to_agent_url("https://wirenboard.cloud/")
        == "https://agent.wirenboard.cloud/api-agent/v1/"
    )


def test_get_controller_url(mock_serial_number, settings: AppSettings):
    assert (
        get_controller_url(settings.cloud_base_url)
        == f"{settings.cloud_base_url}/controllers/{mock_serial_number}"
    )


def test_get_controller_url_with_trailing_slash(mock_serial_number):
    assert (
        get_controller_url("https://wirenboard.cloud/")
        == f"https://wirenboard.cloud/controllers/{mock_serial_number}"
    )


def test_normalize_base_url():
    assert normalize_base_url("https://wirenboard.cloud/") == "https://wirenboard.cloud"
    assert normalize_base_url("https://wirenboard.cloud") == "https://wirenboard.cloud"


def test_parse_headers_basic():
    raw_headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "X-Poll-Interval: 30\r\n"
        "Cache-Control: no-cache\r\n"
    )
    headers = parse_headers(raw_headers)
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Poll-Interval"] == "30"
    assert headers["Cache-Control"] == "no-cache"


def test_parse_headers_empty():
    raw_headers = "HTTP/1.1 200 OK\r\n"
    headers = parse_headers(raw_headers)
    assert not headers


def test_parse_headers_no_colon():
    raw_headers = "HTTP/1.1 200 OK\r\nInvalid-Header-Line"
    headers = parse_headers(raw_headers)
    assert not headers


def test_read_json_config(tmp_path):
    config_file = tmp_path / "config.json"
    config_data = {"key1": "value1", "key2": "value2"}
    config_file.write_text(json.dumps(config_data))

    result = read_json_config(config_file)
    assert result == config_data


@pytest.mark.parametrize(
    "contents, reason",
    [
        ("{invalid json", "is not valid JSON"),
        ("", "is empty"),
        ("   \n", "is empty"),
        ("[1, 2]", "is not a JSON object"),
    ],
)
def test_read_json_config_broken(tmp_path, contents, reason):
    config_file = tmp_path / "config.json"
    config_file.write_text(contents)

    with pytest.raises(ConfigError) as exc_info:
        read_json_config(config_file)

    assert reason in str(exc_info.value)


def test_read_json_config_missing(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        read_json_config(tmp_path / "absent.json")

    assert "is missing" in str(exc_info.value)


def test_read_json_config_delegates_to_rebuild(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("")
    reasons = []

    def rebuild(reason):
        reasons.append(reason)
        return {"key": "rebuilt"}

    assert read_json_config(config_file, rebuild=rebuild) == {"key": "rebuilt"}
    assert reasons == ["is empty"]


def test_read_plaintext_config(tmp_path):
    config_file = tmp_path / "config.txt"
    config_file.write_text("some-config-value\n")

    result = read_plaintext_config(config_file)
    assert result == "some-config-value"


def test_read_plaintext_config_strips_whitespace(tmp_path):
    config_file = tmp_path / "config.txt"
    config_file.write_text("  config-with-spaces  \n")

    result = read_plaintext_config(config_file)
    assert result == "config-with-spaces"


def test_read_plaintext_config_missing(tmp_path):
    assert read_plaintext_config(tmp_path / "absent.txt") == ""


def test_read_plaintext_config_undecodable(tmp_path):
    config_file = tmp_path / "config.txt"
    config_file.write_bytes(b"\xff\xfe binary junk")

    assert read_plaintext_config(config_file) == ""


def test_write_to_file(tmp_path):
    file_path = tmp_path / "subdir" / "file.txt"
    content = "test content"

    write_to_file(file_path, content)

    assert file_path.exists()
    assert file_path.read_text() == content


def test_write_to_file_creates_parent_dirs(tmp_path):
    file_path = tmp_path / "dir1" / "dir2" / "dir3" / "file.txt"

    write_to_file(file_path, "content")

    assert file_path.parent.exists()
    assert file_path.exists()


def test_write_to_file_replaces_without_leaving_temporaries(tmp_path):
    file_path = tmp_path / "file.txt"
    write_to_file(file_path, "old")

    write_to_file(file_path, "new")

    assert file_path.read_text() == "new"
    assert [entry.name for entry in tmp_path.iterdir()] == ["file.txt"]


def test_quarantine_broken_file(tmp_path):
    file_path = tmp_path / "config.json"
    file_path.write_text("{broken")

    quarantined = quarantine_broken_file(file_path)

    assert not file_path.exists()
    assert quarantined.read_text() == "{broken"
    assert quarantined.name.startswith("config.json.broken-")


def test_quarantine_broken_file_skips_empty_and_missing(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("")

    assert quarantine_broken_file(empty) is None
    assert quarantine_broken_file(tmp_path / "absent.json") is None
    assert empty.exists()


def test_start_and_enable_service(mock_subprocess_run):
    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = "Enabled service"
    mock_subprocess_run.return_value.stderr = ""

    start_and_enable_service("test.service")

    assert mock_subprocess_run.call_count == 2
    # First call: enable
    enable_call = mock_subprocess_run.call_args_list[0]
    assert enable_call[0][0] == ["systemctl", "enable", "test.service"]
    # Second call: start
    start_call = mock_subprocess_run.call_args_list[1]
    assert start_call[0][0] == ["systemctl", "start", "test.service"]


def test_start_and_enable_service_with_restart(mock_subprocess_run):
    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = ""
    mock_subprocess_run.return_value.stderr = ""

    start_and_enable_service("test.service", restart=True)

    assert mock_subprocess_run.call_count == 2
    # Second call should be restart
    restart_call = mock_subprocess_run.call_args_list[1]
    assert restart_call[0][0] == ["systemctl", "restart", "test.service"]


def test_stop_and_disable_service(mock_subprocess_run):
    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = "Disabled service"
    mock_subprocess_run.return_value.stderr = ""

    stop_and_disable_service("test.service")

    assert mock_subprocess_run.call_count == 2
    # First call: disable (changed order to prevent process killing itself)
    disable_call = mock_subprocess_run.call_args_list[0]
    assert disable_call[0][0] == ["systemctl", "disable", "test.service"]
    # Second call: stop
    stop_call = mock_subprocess_run.call_args_list[1]
    assert stop_call[0][0] == ["systemctl", "stop", "test.service"]


def test_show_providers_table_empty(mock_print):
    show_providers_table([])

    mock_print.assert_called_once_with("No one provider was found")


def test_show_providers_table_with_providers(mock_print):
    provider1 = MagicMock()
    provider1.name = "provider1"
    provider1.display_url = "https://example.com"

    provider2 = MagicMock()
    provider2.name = "provider2"
    provider2.display_url = "https://example2.com"

    show_providers_table([provider1, provider2])

    mock_print.assert_called_once()
    output = mock_print.call_args[0][0]
    assert "provider1" in output
    assert "provider2" in output
    assert "https://example.com" in output
    assert "https://example2.com" in output
