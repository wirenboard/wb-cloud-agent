import json
from http import HTTPStatus as status
from subprocess import CalledProcessError

import pytest

from wb.cloud_agent.constants import CLIENT_CERT_ERROR_MSG
from wb.cloud_agent.handlers.curl import do_curl, handle_curl_output


def test_do_curl_success_response(settings, mock_subprocess):
    mock_subprocess(status.OK, '{"result": "success"}')
    data, code = do_curl(settings)

    assert data == {"result": "success"}
    assert code == 200


def test_do_curl_invalid_method(settings):
    with pytest.raises(ValueError, match="Invalid method"):
        do_curl(settings, method="invalid")


def test_do_curl_certs_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=58, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(RuntimeError) as exc_info:
        do_curl(settings=settings)

    assert str(exc_info.value) == (
        CLIENT_CERT_ERROR_MSG.format(
            cert_file=settings.client_cert_file,
            cert_engine_key=settings.client_cert_engine_key,
        )
    )


def test_do_curl_generic_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=57, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(CalledProcessError) as exc_info:
        do_curl(settings)

    assert str(exc_info.value) == "Command '['curl']' returned non-zero exit status 57."


def test_handle_curl_output_with_poll_interval(settings, mock_subprocess):
    settings.request_period_seconds = 10
    x_poll_interval = 42
    input_data = {"result": "ok"}
    headers = (
        f"HTTP/1.1 {status.OK} "
        "OK\r\nContent-Type: application/json\r\n"
        f"x-poll-interval: {x_poll_interval}\r\n\r\n"
    )
    stdout = mock_subprocess(status.OK, json.dumps(input_data), headers=headers)

    output_data, status_code = handle_curl_output(settings, stdout)

    assert status_code == status.OK
    assert output_data == input_data
    assert settings.request_period_seconds == x_poll_interval


def test_handle_curl_output_without_poll_interval(settings, mock_subprocess):
    request_period_seconds = settings.request_period_seconds
    input_data = {"msg": "no poll header"}
    stdout = mock_subprocess(status.OK, json.dumps(input_data))

    output_data, status_code = handle_curl_output(settings, stdout)

    assert status_code == status.OK
    assert output_data == input_data
    assert settings.request_period_seconds == request_period_seconds


def test_handle_curl_output_invalid_json(settings, mock_subprocess):
    stdout = mock_subprocess(status.OK, "invalid json")

    output_data, status_code = handle_curl_output(settings, stdout)

    assert output_data == {}  # fallback
    assert status_code == status.OK


def test_handle_curl_output_malformed_split_raises(settings):
    bad_output = f'HTTP/1.1 {status.OK} OK\r\n\r\n{{"data": true}}'.encode("utf-8")  # Missing delimiter

    with pytest.raises(ValueError, match="Invalid data in response"):
        handle_curl_output(settings, bad_output)


def test_do_curl_dns_resolution_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=6, cmd=["curl"], output=b"", stderr=b"Could not resolve host"
    )

    with pytest.raises(RuntimeError) as exc_info:
        do_curl(settings=settings, endpoint="test/")

    assert "Curl couldnt find the IP address" in str(exc_info.value)
    assert settings.cloud_base_url in str(exc_info.value)


def test_do_curl_multipart_post_method(mock_subprocess_run, settings, mock_subprocess, tmp_path):
    mock_subprocess(status.OK, '{"result": "success"}')

    test_file = tmp_path / "test.zip"
    test_file.write_text("test data")
    data, code = do_curl(settings, method="multipart-post", params=test_file)

    assert data == {"result": "success"}
    assert code == 200

    # Check that curl was called with correct multipart form parameters
    args = mock_subprocess_run.call_args[0][0]
    assert "-F" in args
    assert f"file=@{test_file}" in args


def test_do_curl_post_method_with_params(mock_subprocess_run, settings, mock_subprocess):
    mock_subprocess(status.OK, '{"result": "success"}')
    params = {"key": "value", "number": 123}
    data, code = do_curl(settings, method="post", params=params)

    assert data == {"result": "success"}
    assert code == 200

    # Check that curl was called with JSON data
    args = mock_subprocess_run.call_args[0][0]
    assert "-X" in args
    assert "POST" in args
    assert "-d" in args


def test_do_curl_put_method(mock_subprocess_run, settings, mock_subprocess):
    mock_subprocess(status.OK, '{"result": "success"}')
    data, code = do_curl(settings, method="put", params={"data": "test"})

    assert data == {"result": "success"}
    assert code == 200

    args = mock_subprocess_run.call_args[0][0]
    assert "PUT" in args


def test_do_curl_delete_method(mock_subprocess_run, settings, mock_subprocess):
    mock_subprocess(status.OK, '{"result": "success"}')
    data, code = do_curl(settings, method="delete")

    assert data == {"result": "success"}
    assert code == 200

    args = mock_subprocess_run.call_args[0][0]
    assert "DELETE" in args


def test_do_curl_custom_retry_opts(mock_subprocess_run, settings, mock_subprocess):
    mock_subprocess(status.OK, '{"result": "success"}')
    retry_opts = ["--retry", "3", "--connect-timeout", "10"]
    data, code = do_curl(settings, retry_opts=retry_opts)

    assert data == {"result": "success"}
    assert code == 200

    args = mock_subprocess_run.call_args[0][0]
    assert "--retry" in args
    assert "3" in args
    assert "--connect-timeout" in args
    assert "10" in args


def test_handle_curl_output_invalid_status_code_format(settings, mock_subprocess):
    stdout = mock_subprocess(status.OK, '{"data": "test"}', '{"code": "invalid"}')

    with pytest.raises(ValueError, match="Invalid data in response"):
        handle_curl_output(settings, stdout)
