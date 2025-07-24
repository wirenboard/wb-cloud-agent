import json
from http import HTTPStatus as status
from subprocess import CalledProcessError

import pytest

from wb.cloud_agent.constants import CLIENT_CERT_ERROR_MSG
from wb.cloud_agent.handlers.curl import do_curl, handle_curl_output


def test_do_curl_certs_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=58, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(RuntimeError) as exc_info:
        do_curl(settings=settings)

    assert str(exc_info.value) == (
        CLIENT_CERT_ERROR_MSG.format(
            cert_file=settings.client_cert_file, cert_engine_key=settings.client_cert_engine_key
        )
    )


def test_do_curl_generic_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=57, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(CalledProcessError) as exc_info:
        do_curl(settings)

    assert str(exc_info.value) == "Command '['curl']' returned non-zero exit status 57."


def test_handle_curl_output_with_poll_interval(settings):
    settings.request_period_seconds = 10

    x_poll_interval = 42
    headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"x-poll-interval: {x_poll_interval}\r\n"
        "\r\n"
    )
    input_data = {"result": "ok"}
    body = json.dumps(input_data)
    meta = json.dumps({"code": "200"})
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    output_data, status_code = handle_curl_output(settings, stdout)

    assert status_code == status.OK
    assert output_data == input_data
    assert settings.request_period_seconds == x_poll_interval


def test_handle_curl_output_without_poll_interval(settings):
    request_period_seconds = settings.request_period_seconds

    headers = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
    input_data = {"msg": "no poll header"}
    body = json.dumps(input_data)
    meta = json.dumps({"code": "200"})
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    output_data, status_code = handle_curl_output(settings, stdout)

    assert status_code == status.OK
    assert output_data == input_data
    assert settings.request_period_seconds == request_period_seconds


def test_handle_curl_output_invalid_json(settings):
    headers = "HTTP/1.1 200 OK\r\n\r\n"
    body = "not-json"
    meta = json.dumps({"code": "200"})
    stdout = (headers + body + "|||" + meta).encode("utf-8")

    output_data, status_code = handle_curl_output(settings, stdout)

    assert output_data == {}  # fallback
    assert status_code == 200


def test_handle_curl_output_malformed_split_raises(settings):
    bad_output = (
        "HTTP/1.1 200 OK\r\n"
        "\r\n"
        '{"data": true}'  # no DATA_DELIMITER present
    ).encode(
        "utf-8"
    )

    with pytest.raises(ValueError, match="Invalid data in response"):
        handle_curl_output(settings, bad_output)
