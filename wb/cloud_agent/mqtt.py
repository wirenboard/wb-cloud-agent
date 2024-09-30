import logging

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.settings import AppSettings


class MQTTCloudAgent:
    def __init__(self, settings: AppSettings, on_message=None):
        self.mqtt_prefix = settings.MQTT_PREFIX
        self.on_message = on_message
        self.controls = {}

        self.client = MQTTClient(
            f"wb-cloud-agent@{settings.PROVIDER}", settings.BROKER_URL, userdata={"settings": settings}
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect        

        self.was_disconnected = False

    def start(self, update_status=False):
        if update_status:
            self.client.will_set(f"{self.mqtt_prefix}/controls/status", "stopped", retain=True, qos=2)
        self.client.start()
        if update_status:
            self.publish_ctrl("status", "starting")

    def _on_connect(self, client, _userdata, _flags, reason_code, *_):
        # 0: Connection successful
        if reason_code != 0:
            logging.error("Failed to connect: %d. loop_forever() will retry connection", reason_code)
        else:
            if self.was_disconnected:
                self.was_disconnected = False
                self.publish_vdev()
                for control,value in self.controls.items():
                    self.publish_ctrl(control, value)

            self.client.subscribe("/devices/system/controls/HW Revision", qos=2)


    def _on_message(self, client, userdata, message):
        assert "settings" in userdata, "No settings in userdata"
        self.client.unsubscribe("/devices/system/controls/HW Revision")
        if self.on_message:
            self.on_message(userdata, message)

    def _on_disconnect(self, _, __, ___):
        self.was_disconnected = True

    def publish_vdev(self):
        self.client.publish(f"{self.mqtt_prefix}/meta/name", "cloud status", retain=True, qos=2)
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
        self.controls.update({ctrl:value})

    def publish_providers(self, providers):
        self.client.publish("/wb-cloud-agent/providers", providers, retain=True, qos=2)
