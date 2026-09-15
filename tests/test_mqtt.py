from unittest.mock import patch

import pytest

from wb.cloud_agent.mqtt import MQTTCloudAgent


@pytest.fixture(name="agent")
def fixture_agent(settings):
    with patch("wb.cloud_agent.mqtt.MQTTClient"):
        yield MQTTCloudAgent(settings)


def test_provider_command_does_not_change_status(agent):
    agent.start()
    agent.client.start.assert_called_once_with()
    agent.client.will_set.assert_not_called()
    agent.client.publish.assert_not_called()


def test_publish_vdev(agent, settings):
    agent.publish_vdev()
    published = {args[0]: args[1] for args, _kwargs in agent.client.publish.call_args_list}
    assert published == {
        f"{settings.mqtt_prefix}/meta/name": f"Cloud status {settings.provider_name}",
        f"{settings.mqtt_prefix}/meta/driver": "wb-cloud-agent",
        f"{settings.mqtt_prefix}/controls/status/meta": (
            '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}'
        ),
        f"{settings.mqtt_prefix}/controls/activation_link/meta": (
            '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}'
        ),
        f"{settings.mqtt_prefix}/controls/cloud_base_url/meta": (
            '{"type": "text", "readonly": true, "order": 3, "title": {"en": "URL"}}'
        ),
    }


def test_publish_ctrl(agent, settings):
    agent.publish_ctrl("status", "ok")
    agent.client.publish.assert_called_once_with(
        f"{settings.mqtt_prefix}/controls/status", "ok", retain=True, qos=1
    )


def test_update_providers_list(agent):
    with patch("wb.cloud_agent.mqtt.get_provider_names", return_value=["one", "two"]):
        agent.update_providers_list()
    agent.client.publish.assert_called_once_with("/wb-cloud-agent/providers", "one,two", retain=True, qos=1)


def test_cleanup_removes_owned_topics_but_preserves_provider_listing(agent, settings):
    """
    Clear the eight regular retained topics and an extra control; keep the shared provider list.
    """
    agent.publish_ctrl("extra", "value")
    agent.publish_providers("one")
    agent.client.publish.reset_mock()
    agent.remove_vdev()
    deleted = {
        args[0]
        for args, kwargs in agent.client.publish.call_args_list
        if args[1] == "" and kwargs == {"retain": True, "qos": 1}
    }
    assert len(deleted) == 9
    assert all(topic.startswith(settings.mqtt_prefix + "/") for topic in deleted)
    assert f"{settings.mqtt_prefix}/controls/status" in deleted
    assert f"{settings.mqtt_prefix}/controls/extra" in deleted
    assert agent.client.publish.return_value.wait_for_publish.call_count == 9


@pytest.mark.parametrize("connected,confirmed", [(False, True), (True, False)])
def test_failed_cleanup_is_logged(agent, connected, confirmed, caplog):
    agent.client.is_connected.return_value = connected
    agent.client.publish.return_value.is_published.return_value = confirmed
    agent.remove_vdev()
    assert "Cannot remove cloud agent MQTT topics" in caplog.text


@pytest.mark.parametrize("url", ["bad://localhost:1883", "tcp://localhost", "unix://", "tcp://host:70000"])
def test_invalid_broker_url_is_rejected_before_client_creation(settings, url):
    settings.broker_url = url
    with patch("wb.cloud_agent.mqtt.MQTTClient") as factory:
        with pytest.raises(ValueError):
            MQTTCloudAgent(settings)
        factory.assert_not_called()
