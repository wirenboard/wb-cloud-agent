import logging
import threading
from urllib.parse import urlparse

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings, get_provider_names

# CONNACK codes for a rejected login: bad user name or password, not authorized
MQTT_AUTH_ERRORS = (4, 5)


def check_broker_url(broker_url: str) -> None:
    """
    Raise ValueError for a URL MQTTClient cannot connect to.
    """
    url = urlparse(broker_url)
    if url.scheme == "unix":
        if not url.path:
            raise ValueError(f"MQTT broker URL has no socket path: {broker_url}")
    elif url.scheme in ("tcp", "mqtt-tcp", "ws"):
        if not url.hostname or not url.port:
            raise ValueError(f"MQTT broker URL must have a host and a port: {broker_url}")
    else:
        raise ValueError(f"Unsupported MQTT broker URL scheme: {broker_url}")


class MQTTCloudAgent:  # pylint: disable=too-many-instance-attributes  # connection state is tracked here
    def __init__(self, settings: AppSettings, on_message=None):
        self.mqtt_prefix = settings.mqtt_prefix
        self.on_message = on_message
        self.controls = {}
        self.provider_name = settings.provider_name
        self.providers = None

        self.client = MQTTClient(
            f"wb-cloud-agent@{self.provider_name}", settings.broker_url, userdata={"settings": settings}
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.was_disconnected = False
        self.authentication_failed = False
        # Set by the first CONNACK: either the broker accepted us or it rejected the login.
        self._connack = threading.Event()
        self._message_handler = None

    def start(self, daemon=False):
        """
        Connect to the broker. A daemon gets a Last Will and waits for an unavailable broker.
        """
        if daemon:
            self.client.will_set(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)

        self.client.start(retry_first_connection=daemon)

    def wait_for_connection(self, stop_requested: threading.Event) -> bool:
        """
        Block until the broker accepts the connection; False if the login was rejected or stop was requested.
        """
        while not self._connack.wait(0.1):
            if stop_requested.is_set():
                return False
        return not self.authentication_failed

    def stop(self):
        self.client.stop()

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_):
        # 0: Connection successful
        if reason_code != 0:
            logging.error("Failed to connect: %d. loop_forever() will retry connection", reason_code)
            if reason_code in MQTT_AUTH_ERRORS and not self._connack.is_set():
                # A rejected login at startup is a configuration problem, retrying will not help.
                self.authentication_failed = True
                self._connack.set()
            return

        if self.was_disconnected:
            self.was_disconnected = False
            self.publish_vdev()

            for control, value in self.controls.items():
                self.publish_ctrl(control, value)

            self.publish_providers(self.providers)

        self.client.subscribe("/devices/system/controls/HW Revision", qos=2)
        self._connack.set()

    def _on_message(self, _client, userdata, message):
        assert "settings" in userdata, "No settings in userdata"
        self.client.unsubscribe("/devices/system/controls/HW Revision")

        if self.on_message:
            # The handler sends the value to the cloud with curl. Paho's network thread must not
            # wait for that: a slow cloud would stall keepalives and an error would kill the loop.
            self._message_handler = threading.Thread(
                target=self._run_message_handler, args=(userdata, message), daemon=True
            )
            self._message_handler.start()

    def _run_message_handler(self, userdata, message):
        try:
            self.on_message(userdata, message)
        except Exception as exc:  # pylint:disable=broad-exception-caught
            # Nothing to retry here: the value is sent again on the next connection.
            logging.error("Cannot handle MQTT message %s: %s", message.topic, exc)

    def _on_disconnect(self, _, __, ___):
        self.was_disconnected = True

    def publish_vdev(self):
        self.client.publish(
            f"{self.mqtt_prefix}/meta/name", f"Cloud status {self.provider_name}", retain=True, qos=2
        )
        self.client.publish(f"{self.mqtt_prefix}/meta/driver", "wb-cloud-agent", retain=True, qos=2)
        self.client.publish(
            f"{self.mqtt_prefix}/controls/status/meta",
            '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}',
            retain=True,
            qos=2,
        )
        self.client.publish(
            f"{self.mqtt_prefix}/controls/activation_link/meta",
            '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}',
            retain=True,
            qos=2,
        )
        self.client.publish(
            f"{self.mqtt_prefix}/controls/cloud_base_url/meta",
            '{"type": "text", "readonly": true, "order": 3, "title": {"en": "URL"}}',
            retain=True,
            qos=2,
        )

    def remove_vdev(self):
        """
        Clear the retained topics of the virtual device. Must run before stop().
        """
        if not self.client.is_connected():
            logging.error("Cannot remove MQTT topics: not connected to the broker")
            return

        topics = [f"{self.mqtt_prefix}/meta/name", f"{self.mqtt_prefix}/meta/driver"]
        for control in ("status", "activation_link", "cloud_base_url"):
            topics += [
                f"{self.mqtt_prefix}/controls/{control}/meta",
                f"{self.mqtt_prefix}/controls/{control}",
            ]
        for topic in topics:
            self.client.publish(topic, "", retain=True, qos=2)

    def publish_ctrl(self, ctrl, value):
        self.client.publish(f"{self.mqtt_prefix}/controls/{ctrl}", value, retain=True, qos=2)
        self.controls.update({ctrl: value})

    def publish_providers(self, providers):
        self.providers = providers
        self.client.publish("/wb-cloud-agent/providers", providers, retain=True, qos=2)

    def update_providers_list(self) -> None:
        #  Find a better way to update providers list (services enabled? services running?).
        self.publish_providers(",".join(get_provider_names()))
