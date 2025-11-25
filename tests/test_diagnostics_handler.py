import time
from http import HTTPStatus as status
from unittest.mock import patch

import pytest

from wb.cloud_agent.handlers.diagnostics import upload_diagnostic


def test_upload_diagnostic_success(settings, mock_subprocess_run, tmp_path):
    settings.diag_archive = tmp_path
    diag_file = tmp_path / "diag_20231201.zip"
    diag_file.write_text("fake diagnostic data")

    headers = f"HTTP/1.1 {status.OK} OK\r\n\r\n"
    body = '{"result": "ok"}'
    meta = '{"code": "200"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    upload_diagnostic(settings)

    assert not diag_file.exists()


def test_upload_diagnostic_no_files(settings, tmp_path):
    settings.diag_archive = tmp_path

    with patch("wb.cloud_agent.handlers.diagnostics.do_curl") as mock_curl:
        mock_curl.return_value = ({}, status.OK)

        upload_diagnostic(settings)

        mock_curl.assert_called_once()
        args = mock_curl.call_args
        assert args[1]["endpoint"] == "diagnostic-status/"
        assert args[1]["params"] == {"status": "error"}


@pytest.mark.usefixtures("mock_subprocess_bad_request")
def test_upload_diagnostic_no_files_status_update_fails(settings, tmp_path):
    settings.diag_archive = tmp_path
    upload_diagnostic(settings)


@pytest.mark.usefixtures("mock_subprocess_bad_request")
def test_upload_diagnostic_upload_fails(settings, tmp_path):
    settings.diag_archive = tmp_path
    diag_file = tmp_path / "diag_20231201.zip"
    diag_file.write_text("fake diagnostic data")

    upload_diagnostic(settings)

    assert not diag_file.exists()


def test_upload_diagnostic_selects_latest_file(settings, mock_subprocess_run, tmp_path):
    settings.diag_archive = tmp_path

    diag_file1 = tmp_path / "diag_20231201.zip"
    diag_file1.write_text("old diagnostic")
    time.sleep(0.01)

    diag_file2 = tmp_path / "diag_20231202.zip"
    diag_file2.write_text("new diagnostic")

    headers = f"HTTP/1.1 {status.OK} OK\r\n\r\n"
    body = '{"result": "ok"}'
    meta = '{"code": "200"}'
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    mock_subprocess_run.return_value.returncode = 0
    mock_subprocess_run.return_value.stdout = stdout

    upload_diagnostic(settings)

    assert diag_file1.exists()
    assert not diag_file2.exists()
