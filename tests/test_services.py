import json
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests import CLOUD_VARS
from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.services.activation import (
    read_activation_link,
    update_activation_link,
    write_activation_link,
)
from wb.cloud_agent.services.diagnostics import fetch_diagnostics
from wb.cloud_agent.services.metrics import (
    _collect_service_journal,
    _monitor_metrics_service,
    _report_metrics_health,
    reconcile_metrics_script,
    render_metrics_script,
    update_metrics_config,
)
from wb.cloud_agent.services.tunnel import update_tunnel_config


def test_read_activation_link_exists(settings, tmp_path):
    settings.activation_link_config = tmp_path / "activation_link.txt"
    settings.activation_link_config.write_text("http://example.com/activate\n")

    link = read_activation_link(settings)

    assert link == "http://example.com/activate"


def test_read_activation_link_not_exists(settings, tmp_path):
    settings.activation_link_config = tmp_path / "nonexistent.txt"

    link = read_activation_link(settings)

    assert link == UNKNOWN_LINK


def test_update_activation_link(settings):
    mock_mqtt = MagicMock()
    payload = {"activationLink": "http://example.com/new-activate"}

    with patch("wb.cloud_agent.services.activation.write_activation_link") as mock_write:
        update_activation_link(settings, payload, mock_mqtt)

        mock_write.assert_called_once_with(settings, "http://example.com/new-activate", mock_mqtt)


def test_write_activation_link(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.activation_link_config = tmp_path / "activation_link.txt"

    write_activation_link(settings, "http://example.com/activate", mock_mqtt)

    assert settings.activation_link_config.read_text() == "http://example.com/activate"
    mock_mqtt.publish_ctrl.assert_called_once_with("activation_link", "http://example.com/activate")


def test_update_tunnel_config(settings, tmp_path):
    mock_mqtt = MagicMock()
    settings.frp_config = tmp_path / "frpc.ini"
    settings.frp_service = "wb-cloud-agent-frpc@default.service"

    payload = {"config": "[common]\nserver_addr = 1.2.3.4"}

    with (
        patch("wb.cloud_agent.services.tunnel.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.tunnel.write_activation_link") as mock_write,
    ):
        update_tunnel_config(settings, payload, mock_mqtt)

        assert settings.frp_config.read_text() == "[common]\nserver_addr = 1.2.3.4"
        mock_service.assert_called_once_with(settings.frp_service, restart=True)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config_disabled(settings):
    mock_mqtt = MagicMock()

    with (
        patch("wb.cloud_agent.services.metrics._safe_stop_and_disable_service") as mock_stop,
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_write,
    ):
        update_metrics_config(settings, {"enabled": False}, mock_mqtt)

        mock_stop.assert_called_once_with(settings.metrics_service)
        mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


def test_update_metrics_config_without_vars_fails_before_confirm(settings):
    with pytest.raises(ValueError, match="no variables"):
        update_metrics_config(settings, {"enabled": True}, MagicMock())


def test_update_metrics_config_ignores_cloud_delivered_script(cloud_vars_settings):
    """Security: a script in the payload must never be written or executed."""
    settings = cloud_vars_settings
    payload = {"enabled": True, "script": 'import os; os.system("evil")'}

    with pytest.raises(ValueError, match="no variables"):
        update_metrics_config(settings, payload, MagicMock())

    assert not settings.metrics_script.exists()


@pytest.mark.usefixtures("metrics_template")
def test_update_metrics_config_cloud_vars(cloud_vars_settings):
    settings = cloud_vars_settings
    mock_mqtt = MagicMock()
    payload = {"enabled": True, "vars": dict(CLOUD_VARS)}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link") as mock_write,
        patch("wb.cloud_agent.services.metrics.threading.Thread"),
    ):
        update_metrics_config(settings, payload, mock_mqtt)

    content = settings.metrics_script.read_text()
    assert 'METRICS_URL = "https://metrics.example.com/write"' in content
    assert 'BROKER_URL = "tcp://localhost:1883"' in content
    assert 'CRYPTO_ENGINE_KEY = "ATECCx08:00:02:C0:00"' in content
    assert 'MAX_REQUEST_BYTES = int("500000")' in content
    assert "$" not in content
    conf = json.loads(settings.metrics_vars_config.read_text())
    assert conf["vars"] == CLOUD_VARS
    assert conf["mqtt_client_id"].startswith("wb-cloud-agent-metrics-")
    mock_service.assert_called_once_with(settings.metrics_service, restart=True)
    mock_write.assert_called_once_with(settings, UNKNOWN_LINK, mock_mqtt)


@pytest.mark.usefixtures("metrics_template")
def test_cloud_vars_injection_is_escaped(cloud_vars_settings):
    settings = cloud_vars_settings
    malicious = 'x"; __import__("os").system("evil") #'
    payload = {"enabled": True, "vars": dict(CLOUD_VARS, metrics_url=malicious)}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service"),
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link"),
        patch("wb.cloud_agent.services.metrics.threading.Thread"),
    ):
        update_metrics_config(settings, payload, MagicMock())

    content = settings.metrics_script.read_text()
    metrics_line = next(line for line in content.splitlines() if line.startswith("METRICS_URL"))
    # The malicious value stays a quoted string literal: quotes escaped, no breakout.
    assert metrics_line == 'METRICS_URL = "x\\"; __import__(\\"os\\").system(\\"evil\\") #"'


@pytest.mark.usefixtures("metrics_template")
def test_cloud_vars_cannot_override_agent_keys(cloud_vars_settings):
    settings = cloud_vars_settings
    payload = {"enabled": True, "vars": dict(CLOUD_VARS, crypto_engine_key="EVIL")}

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service"),
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link"),
        patch("wb.cloud_agent.services.metrics.threading.Thread"),
    ):
        update_metrics_config(settings, payload, MagicMock())

    content = settings.metrics_script.read_text()
    assert 'CRYPTO_ENGINE_KEY = "ATECCx08:00:02:C0:00"' in content
    assert "EVIL" not in content
    conf = json.loads(settings.metrics_vars_config.read_text())
    assert "crypto_engine_key" not in conf["vars"]


@pytest.mark.usefixtures("metrics_template")
def test_update_metrics_config_restarts_existing_monitor(cloud_vars_settings):
    settings = cloud_vars_settings
    old_stop_event = threading.Event()
    old_thread = MagicMock()
    old_thread.is_alive.return_value = True

    payload = {"enabled": True, "vars": dict(CLOUD_VARS)}

    with (
        patch.dict(
            "wb.cloud_agent.services.metrics._monitor_threads",
            {settings.provider_name: old_thread},
            clear=True,
        ),
        patch.dict(
            "wb.cloud_agent.services.metrics._monitor_stop_events",
            {settings.provider_name: old_stop_event},
            clear=True,
        ),
        patch("wb.cloud_agent.services.metrics.start_and_enable_service"),
        patch("wb.cloud_agent.services.metrics._ensure_service_is_active"),
        patch("wb.cloud_agent.services.metrics.os.chmod"),
        patch("wb.cloud_agent.services.metrics.write_activation_link"),
        patch("wb.cloud_agent.services.metrics.threading.Thread") as mock_thread,
    ):
        new_thread = MagicMock()
        mock_thread.return_value = new_thread

        update_metrics_config(settings, payload, MagicMock())

        assert old_stop_event.is_set()
        mock_thread.assert_called_once()
        new_thread.start.assert_called_once()


def test_reconcile_no_conf_is_noop(cloud_vars_settings):
    settings = cloud_vars_settings

    with patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service:
        reconcile_metrics_script(settings)

    assert not settings.metrics_script.exists()
    mock_service.assert_not_called()


@pytest.mark.usefixtures("metrics_template")
def test_reconcile_rerenders_when_script_outdated(cloud_vars_settings):
    settings = cloud_vars_settings
    settings.metrics_vars_config.write_text(
        json.dumps(
            {
                "vars": dict(CLOUD_VARS),
                "mqtt_client_id": "wb-cloud-agent-metrics-fixedaaa",
                "created_at": "2026-01-01 00:00:00+00:00",
            }
        )
    )
    settings.metrics_script.write_text("OUTDATED")

    with (
        patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service,
        patch("wb.cloud_agent.services.metrics.os.chmod"),
    ):
        reconcile_metrics_script(settings)

    content = settings.metrics_script.read_text()
    assert "OUTDATED" not in content
    assert 'METRICS_URL = "https://metrics.example.com/write"' in content
    mock_service.assert_called_once_with(settings.metrics_service, restart=True)


@pytest.mark.usefixtures("metrics_template")
def test_reconcile_noop_when_already_current(cloud_vars_settings):
    settings = cloud_vars_settings
    conf = {
        "vars": dict(CLOUD_VARS),
        "mqtt_client_id": "wb-cloud-agent-metrics-fixedaaa",
        "created_at": "2026-01-01 00:00:00+00:00",
    }
    settings.metrics_vars_config.write_text(json.dumps(conf))
    settings.metrics_script.write_text(render_metrics_script(settings, conf))

    with patch("wb.cloud_agent.services.metrics.start_and_enable_service") as mock_service:
        reconcile_metrics_script(settings)

    mock_service.assert_not_called()


def test_bundled_template_renders_with_all_substitutions(cloud_vars_settings):
    """The shipped template must have every placeholder filled by the agent."""
    settings = cloud_vars_settings
    # During the deb build pytest runs from a copied .pybuild/.../build tree, so the
    # repo-root template is not a fixed number of levels up — walk up until we find it.
    template_path = next(
        (
            parent / "metrics_collector.py.tpl"
            for parent in Path(__file__).resolve().parents
            if (parent / "metrics_collector.py.tpl").is_file()
        ),
        None,
    )
    if template_path is None:
        pytest.skip("bundled metrics_collector.py.tpl not found in the source tree")
    conf = {
        "vars": dict(CLOUD_VARS),
        "mqtt_client_id": "wb-cloud-agent-metrics-fixedaaa",
        "created_at": "2026-01-01 00:00:00+00:00",
    }

    with patch("wb.cloud_agent.services.metrics.METRICS_COLLECTOR_TEMPLATE_PATH", str(template_path)):
        rendered = render_metrics_script(settings, conf)

    assert "$" not in rendered
    compile(rendered, "metrics_collector.py", "exec")


def test_report_metrics_health_logs_success(settings, caplog):
    caplog.set_level(logging.INFO)

    with patch("wb.cloud_agent.services.metrics.do_curl", return_value=({}, 204)) as mock_curl:
        reported = _report_metrics_health(settings, "persistent_errors", "traceback")

    mock_curl.assert_called_once_with(
        settings,
        method="post",
        endpoint="metrics-collector-log/",
        params={"reason": "persistent_errors", "log": "traceback"},
        retry_opts=["--connect-timeout", "15", "--retry", "2", "--retry-delay", "5"],
        compress_request_body=True,
    )
    assert reported is True
    assert "Reported metrics health" in caplog.text


def test_report_metrics_health_warns_on_backend_error(settings, caplog):
    with patch("wb.cloud_agent.services.metrics.do_curl", return_value=({}, 500)):
        reported = _report_metrics_health(settings, "persistent_errors", "traceback")

    assert reported is False
    assert "Failed to report metrics health: metrics health endpoint returned HTTP 500" in caplog.text


def test_report_metrics_health_warns_on_invalid_response(settings, caplog):
    with patch("wb.cloud_agent.services.metrics.do_curl", side_effect=ValueError("Invalid data in response")):
        reported = _report_metrics_health(settings, "persistent_errors", "traceback")

    assert reported is False
    assert "Failed to report metrics health: Invalid data in response" in caplog.text


def test_monitor_metrics_service_logs_stop_after_successful_report(settings, caplog):
    caplog.set_level(logging.INFO)
    stop_event = threading.Event()

    with (
        patch("wb.cloud_agent.services.metrics.METRICS_HEALTH_CHECK_COUNT", 1),
        patch("wb.cloud_agent.services.metrics.METRICS_HEALTH_CHECK_INTERVAL_S", 0),
        patch("wb.cloud_agent.services.metrics._is_service_failed", return_value=True),
        patch("wb.cloud_agent.services.metrics._collect_service_journal", return_value="traceback"),
        patch("wb.cloud_agent.services.metrics._report_metrics_health", return_value=True),
    ):
        _monitor_metrics_service(settings, settings.metrics_service, stop_event)

    assert "Stopping metrics health monitor" in caplog.text
    assert "after reporting metrics health (reason=service_failed)" in caplog.text


def test_collect_service_journal_limits_utf8_bytes():
    with (
        patch("wb.cloud_agent.services.metrics.METRICS_HEALTH_JOURNAL_MAX_BYTES", 5),
        patch("subprocess.run", return_value=MagicMock(stdout="абв".encode("utf-8"))) as mock_run,
    ):
        journal = _collect_service_journal("wb-cloud-agent-metrics@default.service", 600)

    assert journal == "аб"
    assert len(journal.encode("utf-8")) <= 5
    mock_run.assert_called_once()


def test_fetch_diagnostics(settings, tmp_path):
    settings.diag_archive = tmp_path
    mock_mqtt = MagicMock()

    old_file1 = tmp_path / "diag_old1.zip"
    old_file1.write_text("old data")
    old_file2 = tmp_path / "diag_old2.zip"
    old_file2.write_text("old data")

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("wb.cloud_agent.services.diagnostics.upload_diagnostic"),
    ):
        mock_process = MagicMock()
        mock_process.wait.return_value = None
        mock_popen.return_value.__enter__.return_value = mock_process

        fetch_diagnostics(settings, {}, mock_mqtt)

        assert not old_file1.exists()
        assert not old_file2.exists()

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args[0] == "wb-diag-collect diag"
        assert kwargs["cwd"] == settings.diag_archive
        assert kwargs["shell"] is True


def test_fetch_diagnostics_deletion_error(settings, tmp_path):
    settings.diag_archive = tmp_path
    mock_mqtt = MagicMock()

    old_file = tmp_path / "diag_old.zip"
    old_file.write_text("old data")

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("wb.cloud_agent.services.diagnostics.upload_diagnostic"),
        patch.object(Path, "unlink", side_effect=OSError("Permission denied")),
    ):
        mock_process = MagicMock()
        mock_process.wait.return_value = None
        mock_popen.return_value.__enter__.return_value = mock_process

        fetch_diagnostics(settings, {}, mock_mqtt)
