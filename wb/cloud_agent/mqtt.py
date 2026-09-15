import logging
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlparse

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings, get_provider_names


def connect_mqtt(client: MQTTClient, stop_requested: threading.Event, connected: Future) -> bool:
    logging.info("Waiting for MQTT connectivity")
    while not stop_requested.is_set():
        try:
            client.start()
            break
        except OSError as error:
            logging.debug("MQTT is unavailable: %s", error)
            stop_requested.wait(1)
    while not stop_requested.is_set():
        try:
            connected.result(timeout=0.1)
            logging.info("MQTT connected")
            return True
        except FutureTimeoutError:
            pass
    return False


class MQTTCloudAgent:
    def __init__(self, settings: AppSettings):
        broker = urlparse(settings.broker_url)
        if broker.scheme == "unix":
            if not broker.path:
                raise ValueError("MQTT socket path is missing")
        elif broker.scheme not in ("tcp", "mqtt-tcp", "ws") or not broker.hostname or not broker.port:
            raise ValueError("MQTT broker URL must specify a supported transport, host and port")
        self.settings = settings
        self.client = MQTTClient(f"wb-cloud-agent@{settings.provider_name}", settings.broker_url)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.hardware_revision = None
        self._startup = Future()
        self._publications = {}
        self._lock = threading.Lock()
        self._closing = False

    def start(self) -> None:
        self.client.start()

    def connect(self, stop_requested: threading.Event) -> bool:
        self.client.will_set(f"{self.settings.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)
        return connect_mqtt(self.client, stop_requested, self._startup)

    def stop(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_):
        with self._lock:
            if self._closing:
                return
            if reason_code != 0:
                logging.error("MQTT connection refused: %s", reason_code)
                if reason_code in (4, 5) and not self._startup.done():
                    self._startup.set_exception(PermissionError("MQTT authentication failed"))
                return
            for topic, value in self._publications.items():
                self.client.publish(topic, value, retain=True, qos=1)
            self.client.subscribe("/devices/system/controls/HW Revision", qos=2)
            if not self._startup.done():
                self._startup.set_result(None)

    def _on_message(self, _client, _userdata, message):
        self.hardware_revision = message.payload.decode("utf-8", errors="replace")
        self.client.unsubscribe("/devices/system/controls/HW Revision")

    def _publish(self, topic: str, value) -> None:
        with self._lock:
            self._publications[topic] = value
            # Retained state is idempotent. Keep the same QoS as deletion so a late
            # QoS 2 PUBREL cannot restore a topic after cleanup.
            if self.client.is_connected():
                self.client.publish(topic, value, retain=True, qos=1)

    def publish_vdev(self) -> None:
        prefix = self.settings.mqtt_prefix
        self._publish(f"{prefix}/meta/name", f"Cloud status {self.settings.provider_name}")
        self._publish(f"{prefix}/meta/driver", "wb-cloud-agent")
        self._publish(
            f"{prefix}/controls/status/meta",
            '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}',
        )
        self._publish(
            f"{prefix}/controls/activation_link/meta",
            '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}',
        )
        self._publish(
            f"{prefix}/controls/cloud_base_url/meta",
            '{"type": "text", "readonly": true, "order": 3, "title": {"en": "URL"}}',
        )

    def publish_ctrl(self, ctrl, value) -> None:
        self._publish(f"{self.settings.mqtt_prefix}/controls/{ctrl}", value)

    def publish_providers(self, providers) -> None:
        self._publish("/wb-cloud-agent/providers", providers)

    def update_providers_list(self) -> None:
        self.publish_providers(",".join(get_provider_names()))

    def remove_vdev(self) -> None:
        try:
            with self._lock:
                self._closing = True
                if not self.client.is_connected():
                    raise ConnectionError("broker is unavailable")
                prefix = self.settings.mqtt_prefix
                topics = {f"{prefix}/meta/name", f"{prefix}/meta/driver"}
                for control in ("status", "activation_link", "cloud_base_url"):
                    topics.update((f"{prefix}/controls/{control}", f"{prefix}/controls/{control}/meta"))
                topics.update(topic for topic in self._publications if topic.startswith(prefix + "/"))
                messages = [self.client.publish(topic, "", retain=True, qos=1) for topic in topics]
            deadline = time.monotonic() + 5
            for message in messages:
                message.wait_for_publish(max(0, deadline - time.monotonic()))
                if not message.is_published():
                    raise TimeoutError("broker did not acknowledge deletion")
        except (OSError, RuntimeError, ValueError) as error:
            logging.error("Cannot remove cloud agent MQTT topics: %s", error)
