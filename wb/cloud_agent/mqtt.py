import logging
import threading
from urllib.parse import urlparse

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings, get_provider_names

# CONNACK codes for a rejected login: bad user name or password, not authorized
MQTT_AUTH_ERRORS = (4, 5)
ACK_TIMEOUT_S = 5  # the broker is local: its acknowledgements take milliseconds


def check_broker_url(broker_url: str) -> None:
    """
    Raise ValueError for a URL MQTTClient cannot connect to.

    The message does not repeat the URL: it may carry a password, and the message is logged
    and shown to the user.
    """
    try:
        url = urlparse(broker_url)
        if url.scheme == "unix" and url.path:
            return
        if url.scheme in ("tcp", "mqtt-tcp", "ws") and url.hostname and url.port:
            return
    except ValueError:
        pass
    raise ValueError("MQTT broker URL must be unix:///path or tcp://host:port (also mqtt-tcp://, ws://)")


class MQTTCloudAgent:  # pylint: disable=too-many-instance-attributes  # connection state is tracked here
    def __init__(self, settings: AppSettings, on_message=None, stop_requested: threading.Event = None):
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

        # The state is published only while the broker is connected; every connection republishes
        # it in full. The lock keeps a value set on the daemon's thread and the republishing on
        # paho's thread in order, so the newest value is the one the broker keeps.
        self._state_lock = threading.Lock()
        self._daemon = False  # set by start(): only the daemon publishes the device on connect
        self.authentication_failed = False
        # The daemon's stop event: a rejected login ends the daemon through it, at any time.
        self._stop_requested = stop_requested or threading.Event()
        self._message_handler = None

    def start(self, daemon=False):
        """
        Connect to the broker. A daemon gets a Last Will, and an unavailable broker does not
        hold it up: paho's thread keeps trying, and the state set meanwhile is published once
        the broker answers. Nothing is queued for it: paho would keep every QoS 2 publish made
        without a connection, some 8600 status updates a day, until its 65535 message ids run
        out, and replay them all on connect.
        """
        self._daemon = daemon
        if daemon:
            self.client.will_set(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)

        self.client.start(retry_first_connection=daemon)

    def stop(self):
        self.client.stop()

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_):
        # 0: Connection successful
        if reason_code in MQTT_AUTH_ERRORS:
            # A rejected login is a configuration problem, retrying will not help: exit with code 2.
            logging.error("MQTT broker rejected the login (CONNACK %d), stopping", reason_code)
            self.authentication_failed = True
            self._stop_requested.set()
            return
        if reason_code != 0:
            logging.error("MQTT connection failed (CONNACK %d), retrying", reason_code)
            return

        # The broker starts every session from what it retained: the daemon republishes its state in
        # full. The provider commands connect through this class too and must not create the device.
        if self._daemon:
            with self._state_lock:
                self._publish_vdev()
                for control, value in self.controls.items():
                    self._publish_ctrl(control, value)
                if self.providers is not None:
                    self._publish_providers()

        self.client.subscribe("/devices/system/controls/HW Revision", qos=2)

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

    def publish_vdev(self):
        with self._state_lock:
            if self.client.is_connected():
                self._publish_vdev()

    def _publish_vdev(self):
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
            info = self.client.publish(topic, "", retain=True, qos=2)
        # the broker applies QoS 2 only after the full handshake: wait for the last one before stop()
        info.wait_for_publish(timeout=ACK_TIMEOUT_S)

    def publish_stopped(self):
        """
        Publish status "stopped" and wait for the broker to take it. Must run before stop().

        stop() disconnects cleanly, so the broker never sends the Last Will: a daemon that exits
        with an error has to report the stop itself.
        """
        if not self.client.is_connected():
            return
        info = self.client.publish(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)
        info.wait_for_publish(timeout=ACK_TIMEOUT_S)

    def publish_ctrl(self, ctrl, value):
        with self._state_lock:
            self.controls[ctrl] = value
            if self.client.is_connected():
                self._publish_ctrl(ctrl, value)

    def _publish_ctrl(self, ctrl, value):
        self.client.publish(f"{self.mqtt_prefix}/controls/{ctrl}", value, retain=True, qos=2)

    def publish_providers(self, providers):
        with self._state_lock:
            self.providers = providers
            if self.client.is_connected():
                self._publish_providers()

    def _publish_providers(self):
        self.client.publish("/wb-cloud-agent/providers", self.providers, retain=True, qos=2)

    def update_providers_list(self) -> None:
        #  Find a better way to update providers list (services enabled? services running?).
        self.publish_providers(",".join(get_provider_names()))
