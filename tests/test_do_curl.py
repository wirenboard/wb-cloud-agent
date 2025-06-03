from subprocess import CalledProcessError

import pytest

from wb.cloud_agent.main import CLIENT_CERT_ERROR_MSG, do_curl


def test_do_curl_certs_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=58, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(RuntimeError) as exc_info:
        do_curl(settings=settings)

    assert str(exc_info.value) == (
        CLIENT_CERT_ERROR_MSG.format(
            cert_file=settings.CLIENT_CERT_FILE, cert_engine_key=settings.CLIENT_CERT_ENGINE_KEY
        )
    )


def test_do_curl_generic_error(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=57, cmd=["curl"], output=b"", stderr=b"some low-level curl error"
    )

    with pytest.raises(CalledProcessError) as exc_info:
        do_curl(settings)

    assert str(exc_info.value) == "Command '['curl']' returned non-zero exit status 57."
