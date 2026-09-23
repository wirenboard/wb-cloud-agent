import logging
import threading

import requests


def wait_for_cloud_reachable(url: str, interval: int, stop_requested: threading.Event) -> bool:
    """
    Poll the cloud with HEAD requests until it answers; False if a stop was requested first.
    """
    logging.info("Start checking cloud reachability (interval: %ss)", interval)

    while not stop_requested.is_set():
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud reachability - OK")
                return True

            logging.debug("Cloud '%s' unreachable (status %s)", url, response.status_code)
        except (requests.RequestException, OSError) as exc:
            logging.debug("Cloud '%s' unreachable due to network issue: %s", url, exc)

        logging.debug("Retrying in %s seconds...", interval)
        stop_requested.wait(interval)

    return False
