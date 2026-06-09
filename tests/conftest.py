import sys
from unittest.mock import patch

import pytest

from wb.cloud_agent.services import metrics
from wb.cloud_agent.settings import AppSettings


@pytest.fixture
def settings():
    return AppSettings(provider_name="default")


@pytest.fixture(autouse=True)
def _clear_metrics_monitor_state():
    # pylint: disable=protected-access
    metrics._monitor_threads.clear()
    metrics._monitor_stop_events.clear()
    yield
    metrics._monitor_threads.clear()
    metrics._monitor_stop_events.clear()


METRICS_TEMPLATE = (
    'VERSION = "1.0.9"\n'
    'BROKER_URL = "$mqtt_broker_url"\n'
    'CLIENT_ID = "$mqtt_client_id"\n'
    'METRICS_URL = "$metrics_url"\n'
    'CRYPTO_ENGINE = "$crypto_engine"\n'
    'CRYPTO_ENGINE_KEY = "$crypto_engine_key"\n'
    'STATE_FILE = "$state_file"\n'
    'MEASUREMENT = "$measurement_name"\n'
    'CREATED_AT = "$created_at"\n'
    'CATCH_UP_SLEEP_SECONDS = int("$catch_up_sleep_seconds")\n'
    'SEND_RATE_LIMIT_RETRY_DELAY_SECONDS = int("$send_rate_limit_retry_delay_seconds")\n'
    'MAX_REQUEST_BYTES = int("$max_request_bytes")\n'
)


@pytest.fixture
def metrics_template(tmp_path):
    template_file = tmp_path / "metrics_collector.py.tpl"
    template_file.write_text(METRICS_TEMPLATE)
    with patch("wb.cloud_agent.services.metrics.METRICS_COLLECTOR_TEMPLATE_PATH", str(template_file)):
        yield template_file


@pytest.fixture
def cloud_vars_settings(settings, tmp_path):  # pylint: disable=redefined-outer-name
    settings.metrics_script = tmp_path / "metrics_collector.py"
    settings.metrics_vars_config = tmp_path / "metrics_collector.conf"
    settings.metrics_last_uid = tmp_path / "metrics_last_uid"
    settings.metrics_service = "wb-cloud-agent-metrics@default.service"
    settings.broker_url = "tcp://localhost:1883"
    settings.client_cert_engine_key = "ATECCx08:00:02:C0:00"
    return settings


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
def mock_subprocess(mock_subprocess_run):  # pylint: disable=redefined-outer-name
    def _inner(http_status, body, meta=None, headers=None):
        if headers is None:
            headers = f"HTTP/1.1 {http_status.value} {http_status.phrase}\r\n\r\n"
        if meta is None:
            meta = f'{{"code": "{http_status.value}"}}'
        stdout = (headers + body + "|||" + meta).encode("utf-8")

        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = stdout
        return stdout

    return _inner
