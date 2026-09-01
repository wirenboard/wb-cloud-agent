import logging
import threading
from typing import Optional

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings, get_provider_names

MQTT_AUTH_ERROR_CODES = (4, 5, 134, 135)
EXIT_FAILURE = 1
EXIT_INVALID_ARGUMENT = 2
EXIT_STOPPED = 7


class MQTTCloudAgent:
    def __init__(self, settings: AppSettings, on_message=None):
        self.mqtt_prefix = settings.mqtt_prefix
        self.on_message = on_message
        self.controls = {}
        self.provider_name = settings.provider_name
        self.providers = None
        self._connected = threading.Event()
        self._fatal_error = threading.Event()
        self._fatal_exit_code: Optional[int] = None
        self._connect_error_reported = False

        self.client = MQTTClient(
            f"wb-cloud-agent@{self.provider_name}", settings.broker_url, userdata={"settings": settings}
        )
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.was_disconnected = False

    def start(self, update_status=False):
        if update_status:
            self.client.will_set(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)

        self.client.start()

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_):
        code = getattr(reason_code, "value", reason_code)
        if code != 0:
            self._connected.clear()
            self._fatal_exit_code = EXIT_INVALID_ARGUMENT if code in MQTT_AUTH_ERROR_CODES else EXIT_FAILURE
            self._fatal_error.set()
            if self._fatal_exit_code == EXIT_INVALID_ARGUMENT:
                logging.error("MQTT authentication failed (reason code %s)", code)
            else:
                logging.error("MQTT connection rejected (reason code %s)", code)
            self.client.disconnect()
            return

        if self._connect_error_reported:
            logging.info("MQTT broker is available again")
            self._connect_error_reported = False

        self._connected.set()
        if self.was_disconnected:
            self.was_disconnected = False
            self.publish_vdev()

            for control, value in self.controls.items():
                self.publish_ctrl(control, value)

            if self.providers is not None:
                self.publish_providers(self.providers)

        self.client.subscribe("/devices/system/controls/HW Revision", qos=2)

    def _on_connect_fail(self, _client, _userdata):
        if not self._connect_error_reported and not self._fatal_error.is_set():
            logging.warning("MQTT broker is unreachable, waiting for it")
            self._connect_error_reported = True

    def _on_message(self, _client, userdata, message):
        assert "settings" in userdata, "No settings in userdata"
        self.client.unsubscribe("/devices/system/controls/HW Revision")

        if self.on_message:
            self.on_message(userdata, message)

    def _on_disconnect(self, *_args):
        self._connected.clear()
        self.was_disconnected = True

    def wait_for_connection(self, stop_requested: threading.Event) -> Optional[int]:
        while not self._connected.is_set():
            if self._fatal_error.is_set():
                return self._fatal_exit_code or EXIT_FAILURE
            if stop_requested.wait(0.1):
                return EXIT_STOPPED
        return None

    @property
    def fatal_exit_code(self) -> Optional[int]:
        return self._fatal_exit_code

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
        if not self.client.is_connected():
            logging.error("Unable to remove Cloud Agent MQTT topics: broker is unavailable")
            return

        topics = (
            f"{self.mqtt_prefix}/meta/name",
            f"{self.mqtt_prefix}/meta/driver",
            f"{self.mqtt_prefix}/controls/status/meta",
            f"{self.mqtt_prefix}/controls/activation_link/meta",
            f"{self.mqtt_prefix}/controls/cloud_base_url/meta",
            f"{self.mqtt_prefix}/controls/status",
            f"{self.mqtt_prefix}/controls/activation_link",
            f"{self.mqtt_prefix}/controls/cloud_base_url",
        )
        messages = [self.client.publish(topic, "", retain=True, qos=2) for topic in topics]
        for message in messages:
            try:
                message.wait_for_publish(timeout=0.5)
                if not message.is_published():
                    logging.error("Cloud Agent MQTT topic was not removed before shutdown")
            except (RuntimeError, ValueError) as exc:
                logging.error("Unable to remove Cloud Agent MQTT topic: %s", exc)

    def publish_ctrl(self, ctrl, value):
        self.client.publish(f"{self.mqtt_prefix}/controls/{ctrl}", value, retain=True, qos=2)
        self.controls.update({ctrl: value})

    def publish_providers(self, providers):
        self.providers = providers
        self.client.publish("/wb-cloud-agent/providers", providers, retain=True, qos=2)

    def update_providers_list(self) -> None:
        #  Find a better way to update providers list (services enabled? services running?).
        self.publish_providers(",".join(get_provider_names()))

    def stop(self) -> None:
        self.client.stop()
