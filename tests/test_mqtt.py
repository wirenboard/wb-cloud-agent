# pylint: disable=redefined-outer-name, protected-access

import logging
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from wb.cloud_agent.mqtt import MQTTCloudAgent, check_broker_url


@pytest.fixture
def mock_mqtt_client():
    with patch("wb.cloud_agent.mqtt.MQTTClient") as mock:
        yield mock


@pytest.fixture
def mqtt_cloud_agent(settings, mock_mqtt_client):
    agent = MQTTCloudAgent(settings)
    agent.client = mock_mqtt_client.return_value
    return agent


@pytest.mark.parametrize(
    "broker_url",
    [
        "tcp://user:s3cr3t@broker.example.com",
        "tcp://user:s3cr3t#x@broker.example.com:1883",
        "tcp://user:s3cr3t/x@broker.example.com:1883",
        "tcp://user:s3cr3t?x@broker.example.com:1883",
    ],
    ids=["no-port", "hash-in-password", "slash-in-password", "question-mark-in-password"],
)
def test_check_broker_url_keeps_the_credentials_out_of_the_error(broker_url):
    """
    The message goes to the journal and to stderr, so a password from the provider config must not be in
    it. An unescaped "#", "/" or "?" in the password makes urlparse take a piece of the password for the
    port and quote it in its own ValueError: that one must not leak through the exception chain either.
    """
    with pytest.raises(ValueError) as error:
        check_broker_url(broker_url)

    message = str(error.value)
    assert "s3cr3t" not in message
    assert "user" not in message
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ or error.value.__context__ is None


def test_mqtt_cloud_agent_init(settings, mock_mqtt_client):
    agent = MQTTCloudAgent(settings)

    assert agent.mqtt_prefix == settings.mqtt_prefix
    assert agent.provider_name == settings.provider_name
    assert not agent.controls
    assert agent.authentication_failed is False

    mock_mqtt_client.assert_called_once()


@pytest.mark.usefixtures("mock_mqtt_client")
def test_mqtt_cloud_agent_init_with_on_message(settings):
    on_message_handler = MagicMock()
    agent = MQTTCloudAgent(settings, on_message=on_message_handler)

    assert agent.on_message == on_message_handler


def test_start_as_tool_connects_once(mqtt_cloud_agent):
    mqtt_cloud_agent.start()

    mqtt_cloud_agent.client.start.assert_called_once_with(retry_first_connection=False)
    mqtt_cloud_agent.client.will_set.assert_not_called()


def test_start_as_daemon_sets_will_and_waits_for_broker(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.start(daemon=True)

    mqtt_cloud_agent.client.will_set.assert_called_once_with(
        f"{settings.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2
    )
    mqtt_cloud_agent.client.start.assert_called_once_with(retry_first_connection=True)
    mqtt_cloud_agent.client.publish.assert_not_called()


def test_on_connect_successful(mqtt_cloud_agent):
    mqtt_cloud_agent._on_connect(None, None, None, 0)

    mqtt_cloud_agent.client.subscribe.assert_called_once_with("/devices/system/controls/HW Revision", qos=2)


@pytest.mark.usefixtures("mock_mqtt_client")
def test_on_connect_failure_does_not_stop_the_daemon(settings, caplog):
    """
    A broker that is not ready yet is retried by paho; only a rejected login is final.
    """
    stop_requested = threading.Event()
    agent = MQTTCloudAgent(settings, stop_requested=stop_requested)

    with caplog.at_level(logging.ERROR):
        agent._on_connect(None, None, None, 1)

    agent.client.subscribe.assert_not_called()
    assert agent.authentication_failed is False
    assert not stop_requested.is_set()
    assert "MQTT connection failed (CONNACK 1), retrying" in caplog.text


@pytest.mark.parametrize("reason_code", [4, 5])
@pytest.mark.usefixtures("mock_mqtt_client")
def test_on_connect_rejected_login_stops_the_daemon(settings, reason_code, caplog):
    """
    At startup and after a reconnect alike: the login is a configuration problem, code 2.
    The journal says so; a "retrying" line would contradict the exit that follows.
    """
    stop_requested = threading.Event()
    agent = MQTTCloudAgent(settings, stop_requested=stop_requested)
    agent._on_connect(None, None, None, 0)

    with caplog.at_level(logging.ERROR):
        agent._on_connect(None, None, None, reason_code)

    assert agent.authentication_failed is True
    assert stop_requested.is_set()
    assert f"MQTT broker rejected the login (CONNACK {reason_code}), stopping" in caplog.text
    assert "retry" not in caplog.text


def test_stop(mqtt_cloud_agent):
    mqtt_cloud_agent.stop()

    mqtt_cloud_agent.client.stop.assert_called_once_with()


def test_on_connect_publishes_the_current_state(mqtt_cloud_agent, settings):
    """
    The first connection and a reconnect alike: the device meta, every control and the providers
    list go out, so a state set while the broker was down reaches it now.
    """
    mqtt_cloud_agent.start(daemon=True)
    mqtt_cloud_agent.controls = {"status": "running", "activation_link": "http://test"}
    mqtt_cloud_agent.providers = "provider1,provider2"

    mqtt_cloud_agent._on_connect(None, None, None, 0)

    published = mqtt_cloud_agent.client.publish.call_args_list
    assert call(f"{settings.mqtt_prefix}/meta/driver", "wb-cloud-agent", retain=True, qos=2) in published
    assert call(f"{settings.mqtt_prefix}/controls/status", "running", retain=True, qos=2) in published
    assert (
        call(f"{settings.mqtt_prefix}/controls/activation_link", "http://test", retain=True, qos=2)
        in published
    )
    assert call("/wb-cloud-agent/providers", "provider1,provider2", retain=True, qos=2) in published


def test_on_connect_as_a_tool_publishes_nothing(mqtt_cloud_agent):
    """del-provider with the provider's daemon down must not leave the device's retained topics behind."""
    mqtt_cloud_agent.start()
    mqtt_cloud_agent.controls = {"status": "running"}
    mqtt_cloud_agent.providers = "provider1"

    mqtt_cloud_agent._on_connect(None, None, None, 0)

    mqtt_cloud_agent.client.publish.assert_not_called()
    mqtt_cloud_agent.client.subscribe.assert_called_once_with("/devices/system/controls/HW Revision", qos=2)


def test_on_connect_without_a_providers_list_leaves_its_topic_alone(mqtt_cloud_agent):
    """Publishing None would clear the retained list of every provider on the controller."""
    mqtt_cloud_agent.start(daemon=True)
    mqtt_cloud_agent._on_connect(None, None, None, 0)

    topics = [c.args[0] for c in mqtt_cloud_agent.client.publish.call_args_list]
    assert "/wb-cloud-agent/providers" not in topics


def test_state_set_without_a_connection_is_kept_for_the_next_connect(mqtt_cloud_agent, settings):
    """
    Nothing is handed to paho while the broker is away: it would queue every status update, some
    8600 a day, until its message ids run out, and replay them all on connect. The values are
    remembered instead and published by _on_connect.
    """
    mqtt_cloud_agent.start(daemon=True)
    mqtt_cloud_agent.client.is_connected.return_value = False

    mqtt_cloud_agent.publish_vdev()
    mqtt_cloud_agent.publish_ctrl("status", "Network or Cloud is unreachable! Retrying...")
    mqtt_cloud_agent.publish_ctrl("status", "connecting")
    mqtt_cloud_agent.publish_providers("provider1")

    mqtt_cloud_agent.client.publish.assert_not_called()
    assert mqtt_cloud_agent.controls == {"status": "connecting"}
    assert mqtt_cloud_agent.providers == "provider1"

    mqtt_cloud_agent.client.is_connected.return_value = True
    mqtt_cloud_agent._on_connect(None, None, None, 0)

    statuses = [
        c.args[1]
        for c in mqtt_cloud_agent.client.publish.call_args_list
        if c.args[0] == f"{settings.mqtt_prefix}/controls/status"
    ]
    assert statuses == ["connecting"]


def test_on_message(mqtt_cloud_agent):
    userdata = {"settings": MagicMock()}
    message = MagicMock()
    on_message_handler = MagicMock()
    mqtt_cloud_agent.on_message = on_message_handler

    mqtt_cloud_agent._on_message(None, userdata, message)
    mqtt_cloud_agent._message_handler.join(1)

    mqtt_cloud_agent.client.unsubscribe.assert_called_once_with("/devices/system/controls/HW Revision")
    on_message_handler.assert_called_once_with(userdata, message)


def test_on_message_handler_error_is_logged(mqtt_cloud_agent, caplog):
    """
    A failing handler must not take paho's network thread down with it.
    """
    message = MagicMock(topic="/devices/system/controls/HW Revision")
    mqtt_cloud_agent.on_message = MagicMock(side_effect=ConnectionError("cloud is down"))

    with caplog.at_level(logging.ERROR):
        mqtt_cloud_agent._on_message(None, {"settings": MagicMock()}, message)
        mqtt_cloud_agent._message_handler.join(1)

    assert "Cannot handle MQTT message /devices/system/controls/HW Revision: cloud is down" in caplog.text


def test_on_message_without_handler(mqtt_cloud_agent):
    userdata = {"settings": MagicMock()}
    message = MagicMock()
    mqtt_cloud_agent.on_message = None

    mqtt_cloud_agent._on_message(None, userdata, message)

    mqtt_cloud_agent.client.unsubscribe.assert_called_once()


def test_publish_vdev(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.publish_vdev()

    expected_calls = [
        call(
            f"{settings.mqtt_prefix}/meta/name",
            f"Cloud status {settings.provider_name}",
            retain=True,
            qos=2,
        ),
        call(f"{settings.mqtt_prefix}/meta/driver", "wb-cloud-agent", retain=True, qos=2),
        call(
            f"{settings.mqtt_prefix}/controls/status/meta",
            '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}',
            retain=True,
            qos=2,
        ),
        call(
            f"{settings.mqtt_prefix}/controls/activation_link/meta",
            '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}',
            retain=True,
            qos=2,
        ),
        call(
            f"{settings.mqtt_prefix}/controls/cloud_base_url/meta",
            '{"type": "text", "readonly": true, "order": 3, "title": {"en": "URL"}}',
            retain=True,
            qos=2,
        ),
    ]

    for expected_call in expected_calls:
        assert expected_call in mqtt_cloud_agent.client.publish.call_args_list


def test_remove_vdev(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.remove_vdev()

    expected_calls = [
        call(f"{settings.mqtt_prefix}/meta/name", "", retain=True, qos=2),
        call(f"{settings.mqtt_prefix}/meta/driver", "", retain=True, qos=2),
        call(f"{settings.mqtt_prefix}/controls/status/meta", "", retain=True, qos=2),
        call(
            f"{settings.mqtt_prefix}/controls/activation_link/meta",
            "",
            retain=True,
            qos=2,
        ),
        call(
            f"{settings.mqtt_prefix}/controls/cloud_base_url/meta",
            "",
            retain=True,
            qos=2,
        ),
        call(f"{settings.mqtt_prefix}/controls/status", "", retain=True, qos=2),
        call(f"{settings.mqtt_prefix}/controls/activation_link", "", retain=True, qos=2),
        call(f"{settings.mqtt_prefix}/controls/cloud_base_url", "", retain=True, qos=2),
    ]

    for expected_call in expected_calls:
        assert expected_call in mqtt_cloud_agent.client.publish.call_args_list


def test_remove_vdev_without_connection_logs_error(mqtt_cloud_agent, caplog):
    mqtt_cloud_agent.client.is_connected.return_value = False

    with caplog.at_level(logging.ERROR):
        mqtt_cloud_agent.remove_vdev()

    mqtt_cloud_agent.client.publish.assert_not_called()
    assert "Cannot remove MQTT topics: not connected to the broker" in caplog.text


def test_publish_stopped_waits_for_the_broker(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.publish_stopped()

    mqtt_cloud_agent.client.publish.assert_called_once_with(
        f"{settings.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2
    )
    mqtt_cloud_agent.client.publish.return_value.wait_for_publish.assert_called_once_with(timeout=5)


def test_publish_stopped_without_connection_publishes_nothing(mqtt_cloud_agent):
    mqtt_cloud_agent.client.is_connected.return_value = False

    mqtt_cloud_agent.publish_stopped()

    mqtt_cloud_agent.client.publish.assert_not_called()


def test_publish_ctrl(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.publish_ctrl("status", "running")

    mqtt_cloud_agent.client.publish.assert_called_once_with(
        f"{settings.mqtt_prefix}/controls/status", "running", retain=True, qos=2
    )
    assert mqtt_cloud_agent.controls == {"status": "running"}


def test_publish_providers(mqtt_cloud_agent):
    providers = "provider1,provider2"
    mqtt_cloud_agent.publish_providers(providers)

    mqtt_cloud_agent.client.publish.assert_called_once_with(
        "/wb-cloud-agent/providers", providers, retain=True, qos=2
    )
    assert mqtt_cloud_agent.providers == providers


def test_update_providers_list(mqtt_cloud_agent):
    with patch(
        "wb.cloud_agent.mqtt.get_provider_names",
        return_value=["provider1", "provider2"],
    ):
        mqtt_cloud_agent.update_providers_list()

    mqtt_cloud_agent.client.publish.assert_called_once_with(
        "/wb-cloud-agent/providers", "provider1,provider2", retain=True, qos=2
    )
