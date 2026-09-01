import logging
import socket
import threading
import time
from typing import Optional

import requests

NETWORK_ERRORS = (
    requests.RequestException,
    ConnectionError,
    socket.timeout,
    socket.gaierror,
    OSError,
)


class CloudUnreachableError(Exception):
    """Cloud is unreachable after multiple attempts."""


def wait_for_cloud_reachable(
    url: str,
    interval: int = 5,
    max_retries: Optional[int] = 100,
    stop_requested: Optional[threading.Event] = None,
) -> bool:
    logging.info("Start checking cloud reachability (interval: %ss, max_attempts: %s)", interval, max_retries)

    attempt = 0
    while max_retries is None or attempt < max_retries:
        if stop_requested is not None and stop_requested.is_set():
            return False
        attempt += 1
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud reachability - OK")
                return True

            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable (status %s)",
                attempt,
                max_retries or "unlimited",
                url,
                response.status_code,
            )
        except NETWORK_ERRORS as exc:
            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable due to network issue: %s",
                attempt,
                max_retries or "unlimited",
                url,
                exc,
            )
        except Exception as exc:  # pylint:disable=broad-exception-caught
            raise CloudUnreachableError("Unexpected error during cloud reachability check") from exc

        if max_retries is None or attempt < max_retries:
            logging.debug("Retrying in %s seconds...", interval)
            if stop_requested is None:
                time.sleep(interval)
            elif stop_requested.wait(interval):
                return False

    raise CloudUnreachableError(f"Cloud '{url}' is unreachable after {max_retries} attempts")
