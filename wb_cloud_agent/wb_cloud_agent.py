import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from json import JSONDecodeError
from typing import Optional
from urllib.parse import urljoin

from requests import Session

HTTP_200_OK = 204
HTTP_204_NO_CONTENT = 204

WIRENBOARD_CLOUD_URL = os.getenv('WIRENBOARD_CLOUD_URL', 'http://localhost:7000/api-agent/v1/')
WIRENBOARD_FRP_CONFIG = os.getenv('WIRENBOARD_FRP_CONFIG', '/var/lib/wb-cloud-agent/frpc.conf')
WIRENBOARD_TELEGRAF_CONFIG = os.getenv('WIRENBOARD_TELEGRAF_CONFIG', '/var/lib/wb-cloud-agent/telegraf.conf')
WIRENBOARD_ACTIVATION_LINK_CONFIG = os.getenv('WIRENBOARD_ACTIVATION_LINK_CONFIG', '/var/lib/wb-cloud-agent/activation_link.conf')

WIRENBOARD_REQUEST_PERIOD_SECONDS = int(os.getenv('WIRENBOARD_REQUEST_PERIOD_SECONDS', '3'))


@dataclass
class ConvenientHTTPClient:
    base_url: str = WIRENBOARD_CLOUD_URL

    def __post_init__(self) -> None:
        self.session: Session = Session()

    def request(
            self,
            *,
            method: str,
            endpoint: str,
            data: Optional[dict] = None,
            raise_for_status: bool = False,
    ) -> tuple[dict, int]:
        response = getattr(self.session, method)(
            self._join_url(path=endpoint),
            headers=self._get_headers(),
            json=data,
        )

        if raise_for_status:
            response.raise_for_status()

        try:
            serialized_response = response.json()
        except JSONDecodeError:
            serialized_response = {}

        return serialized_response, response.status_code

    @staticmethod
    def _get_headers() -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _join_url(self, *, path: str) -> str:
        return urljoin(self.cleaned_base_url, f"{path}")

    @cached_property
    def cleaned_base_url(self) -> str:
        if self.base_url.endswith("/"):
            return self.base_url

        return f"{self.base_url}/"


def update_activation_link(payload):
    with open(WIRENBOARD_ACTIVATION_LINK_CONFIG, 'w') as file:
        file.write(payload['activationLink'])


def update_tunnel_config(payload):
    with open(WIRENBOARD_FRP_CONFIG, 'w') as file:
        file.write(payload['config'])

    subprocess.run(['systemctl', 'restart', 'frpc.service'])


def update_metrics_config(payload):
    with open(WIRENBOARD_TELEGRAF_CONFIG, 'w') as file:
        file.write(payload['config'])

    subprocess.run(['systemctl', 'restart', 'telegraf.service'])


HANDLERS = {
    'update_activation_link': update_activation_link,
    'update_tunnel_config': update_tunnel_config,
    'update_metrics_config': update_metrics_config,
}


def make_request(client):
    event_data, event_status = client.request(method='get', endpoint='events/')
    print(datetime.now(), event_status, event_data)

    if event_status == HTTP_204_NO_CONTENT:
        return

    code = event_data.get('code', '')
    handler = HANDLERS.get(code)
    if not handler:
        raise ValueError("Unknown event code: " + str(code))

    event_id = event_data.get('id')
    if not handler:
        raise ValueError("Unknown event id: " + str(event_id))

    payload = event_data.get('payload')
    if not payload:
        raise ValueError("Empty payload")

    handler(payload)
    print(datetime.now(), 'Event handled successfully:', event_id)

    _, confirmation_status = client.request(method='post', endpoint='events/' + event_id + '/confirm/')

    if confirmation_status != HTTP_204_NO_CONTENT:
        raise ValueError("Not a 20X status on event confirmation: " + confirmation_status)


def main():
    client = ConvenientHTTPClient()
    while True:
        start = time.perf_counter()

        try:
            make_request(client)
        except Exception as ex:
            print(datetime.now(), 'Error:', ex)

        request_time = time.perf_counter() - start

        print(datetime.now(), 'Done in:', int(request_time * 1000), 'ms', flush=True)

        time.sleep(WIRENBOARD_REQUEST_PERIOD_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
