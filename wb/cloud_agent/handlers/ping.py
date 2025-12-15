import logging
import socket
import time

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


def wait_for_cloud_reachable(url: str, interval: int = 5, max_retries: int = 100) -> None:
    logging.info("Start checking cloud reachability (interval: %ss, max_attempts: %s)", interval, max_retries)

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud reachability - OK")
                return

            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable (status %s)",
                attempt,
                max_retries,
                url,
                response.status_code,
            )
        except NETWORK_ERRORS as exc:
            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable due to network issue: %s",
                attempt,
                max_retries,
                url,
                exc,
            )
        except Exception as exc:  # pylint:disable=broad-exception-caught
            raise CloudUnreachableError("Unexpected error during cloud reachability check") from exc

        if attempt < max_retries:
            logging.debug("Retrying in %s seconds...", interval)
            time.sleep(interval)

    raise CloudUnreachableError(f"Cloud '{url}' is unreachable after {max_retries} attempts")
