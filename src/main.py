import sys
import time
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from json import JSONDecodeError
from typing import Optional
from urllib.parse import urljoin

from requests import Session

WIRENBOARD_CLOUD_URL = 'https://app.wirenboard.cloud/api/v1/'

REQUEST_PERIOD_SECONDS = 3


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


def main():

    client = ConvenientHTTPClient()
    while True:
        start = time.perf_counter()

        data, status = client.request(method='get', endpoint='agent-api/ask/')

        request_time = time.perf_counter() - start

        print(datetime.now(), status, int(request_time * 1000), data)

        time.sleep(REQUEST_PERIOD_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
