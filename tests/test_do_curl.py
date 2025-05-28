from subprocess import CalledProcessError

import pytest

from wb.cloud_agent.main import do_curl


def test_called_process_error_58_message(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=58, cmd=['curl'], output=b'', stderr=b'some low-level curl error'
    )

    with pytest.raises(RuntimeError) as exc_info:
        do_curl(settings=settings)

    assert str(exc_info.value) == (
        f'Cert {settings.CLIENT_CERT_FILE} and key {settings.CLIENT_CERT_ENGINE_KEY} '
        'seem to be inconsistent (possibly because of CPU board missmatch)!'
    )


def test_called_process_error_non_58_message(mock_subprocess_run, settings):
    mock_subprocess_run.side_effect = CalledProcessError(
        returncode=57, cmd=['curl'], output=b'', stderr=b'some low-level curl error'
    )

    with pytest.raises(CalledProcessError) as exc_info:
        do_curl(settings)

    assert str(exc_info.value) == "Command '['curl']' returned non-zero exit status 57."
