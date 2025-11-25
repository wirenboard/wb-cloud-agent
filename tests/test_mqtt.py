# pylint: disable=redefined-outer-name, protected-access

from unittest.mock import MagicMock, call, patch

import pytest

from wb.cloud_agent.mqtt import MQTTCloudAgent


@pytest.fixture
def mock_mqtt_client():
    with patch("wb.cloud_agent.mqtt.MQTTClient") as mock:
        yield mock


@pytest.fixture
def mqtt_cloud_agent(settings, mock_mqtt_client):
    agent = MQTTCloudAgent(settings)
    agent.client = mock_mqtt_client.return_value
    return agent


def test_mqtt_cloud_agent_init(settings, mock_mqtt_client):
    agent = MQTTCloudAgent(settings)

    assert agent.mqtt_prefix == settings.mqtt_prefix
    assert agent.provider_name == settings.provider_name
    assert not agent.controls
    assert agent.was_disconnected is False

    mock_mqtt_client.assert_called_once()


@pytest.mark.usefixtures("mock_mqtt_client")
def test_mqtt_cloud_agent_init_with_on_message(settings):
    on_message_handler = MagicMock()
    agent = MQTTCloudAgent(settings, on_message=on_message_handler)

    assert agent.on_message == on_message_handler


def test_start_without_update_status(mqtt_cloud_agent):
    mqtt_cloud_agent.start(update_status=False)

    mqtt_cloud_agent.client.start.assert_called_once()
    mqtt_cloud_agent.client.will_set.assert_not_called()


def test_start_with_update_status(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.start(update_status=True)

    mqtt_cloud_agent.client.will_set.assert_called_once_with(
        f"{settings.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2
    )
    mqtt_cloud_agent.client.start.assert_called_once()
    mqtt_cloud_agent.client.publish.assert_called_with(
        f"{settings.mqtt_prefix}/controls/status", "starting", retain=True, qos=2
    )


def test_on_connect_successful(mqtt_cloud_agent):
    mqtt_cloud_agent._on_connect(None, None, None, 0)

    mqtt_cloud_agent.client.subscribe.assert_called_once_with("/devices/system/controls/HW Revision", qos=2)


def test_on_connect_failure(mqtt_cloud_agent):
    mqtt_cloud_agent._on_connect(None, None, None, 1)

    mqtt_cloud_agent.client.subscribe.assert_not_called()


def test_on_connect_after_disconnect(mqtt_cloud_agent, settings):
    mqtt_cloud_agent.was_disconnected = True
    mqtt_cloud_agent.controls = {"status": "running", "activation_link": "http://test"}
    mqtt_cloud_agent.providers = "provider1,provider2"

    with (
        patch.object(mqtt_cloud_agent, "publish_vdev") as mock_publish_vdev,
        patch.object(mqtt_cloud_agent, "publish_providers") as mock_publish_providers,
    ):
        mqtt_cloud_agent._on_connect(None, None, None, 0)

    assert mqtt_cloud_agent.was_disconnected is False
    mock_publish_vdev.assert_called_once()
    mock_publish_providers.assert_called_once_with("provider1,provider2")

    # Check that controls were republished
    expected_calls = [
        call(f"{settings.mqtt_prefix}/controls/status", "running", retain=True, qos=2),
        call(
            f"{settings.mqtt_prefix}/controls/activation_link",
            "http://test",
            retain=True,
            qos=2,
        ),
    ]
    for expected_call in expected_calls:
        assert expected_call in mqtt_cloud_agent.client.publish.call_args_list


def test_on_message(mqtt_cloud_agent):
    userdata = {"settings": MagicMock()}
    message = MagicMock()
    on_message_handler = MagicMock()
    mqtt_cloud_agent.on_message = on_message_handler

    mqtt_cloud_agent._on_message(None, userdata, message)

    mqtt_cloud_agent.client.unsubscribe.assert_called_once_with("/devices/system/controls/HW Revision")
    on_message_handler.assert_called_once_with(userdata, message)


def test_on_message_without_handler(mqtt_cloud_agent):
    userdata = {"settings": MagicMock()}
    message = MagicMock()
    mqtt_cloud_agent.on_message = None

    mqtt_cloud_agent._on_message(None, userdata, message)

    mqtt_cloud_agent.client.unsubscribe.assert_called_once()


def test_on_disconnect(mqtt_cloud_agent):
    mqtt_cloud_agent.was_disconnected = False

    mqtt_cloud_agent._on_disconnect(None, None, None)

    assert mqtt_cloud_agent.was_disconnected is True


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
