import logging

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings, get_provider_names

HW_REVISION_TOPIC = "/devices/system/controls/HW Revision"


class MQTTCloudAgent:
    def __init__(self, settings: AppSettings, on_message=None):
        self.settings = settings
        self.on_message = on_message
        self.controls = {}
        self.providers = None

        self.client = MQTTClient(
            f"wb-cloud-agent@{self.provider_name}", settings.broker_url, userdata={"settings": settings}
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.was_disconnected = False

    @property
    def mqtt_prefix(self) -> str:
        return self.settings.mqtt_prefix

    @property
    def provider_name(self) -> str:
        return self.settings.provider_name

    def start(self, update_status=False):
        if update_status:
            self.client.will_set(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)

        self.client.start()

        if update_status:
            self.publish_ctrl("status", "starting")

    def ensure_running(self) -> None:
        """Reconnect when the network loop has stopped: without it publishes and keepalives are lost."""
        if self._network_loop_alive():
            return

        self.was_disconnected = True
        self.client.loop_stop()
        try:
            self.client.start()
        except Exception as exc:  # pylint:disable=broad-exception-caught
            logging.error("Error restarting MQTT client: %s", exc)

    def watch_hw_revision(self) -> None:
        """Held back while the config is unusable: the retained value reaches the cloud through on_message."""
        if self.settings.config_error:
            return

        self.client.subscribe(HW_REVISION_TOPIC, qos=2)

    def _network_loop_alive(self) -> bool:
        # paho keeps the loop thread in client._thread on both 1.5.x (bullseye) and 2.x (trixie).
        thread = self.client._thread  # pylint:disable=protected-access
        return thread is not None and thread.is_alive()

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_):
        # 0: Connection successful
        if reason_code != 0:
            logging.error("Failed to connect: %d. loop_forever() will retry connection", reason_code)
        else:
            if self.was_disconnected:
                self.was_disconnected = False
                self.publish_vdev()

                for control, value in self.controls.items():
                    self.publish_ctrl(control, value)

                if self.providers is not None:
                    self.publish_providers(self.providers)

            self.watch_hw_revision()

    def _on_message(self, _client, userdata, message):
        """An exception escaping a paho callback kills the network loop thread."""
        try:
            assert "settings" in userdata, "No settings in userdata"
            self.client.unsubscribe(HW_REVISION_TOPIC)

            if self.on_message:
                self.on_message(userdata, message)
        except Exception:  # pylint:disable=broad-exception-caught
            # message.topic decodes lazily, so reading it here would raise out of this very guard.
            topic = message._topic.decode("utf-8", "replace")  # pylint:disable=protected-access
            logging.exception("Error handling MQTT message on %s", topic)

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
        self.client.publish(f"{self.mqtt_prefix}/meta/name", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/meta/driver", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/status/meta", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/activation_link/meta", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/cloud_base_url/meta", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/status", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/activation_link", "", retain=True, qos=2)
        self.client.publish(f"{self.mqtt_prefix}/controls/cloud_base_url", "", retain=True, qos=2)

    def publish_ctrl(self, ctrl, value):
        self.client.publish(f"{self.mqtt_prefix}/controls/{ctrl}", value, retain=True, qos=2)
        self.controls.update({ctrl: value})

    def publish_providers(self, providers):
        self.providers = providers
        self.client.publish("/wb-cloud-agent/providers", providers, retain=True, qos=2)

    def update_providers_list(self) -> None:
        #  Find a better way to update providers list (services enabled? services running?).
        self.publish_providers(",".join(get_provider_names()))
