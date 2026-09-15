import logging
import threading

import requests


def wait_for_cloud_reachable(url: str, interval: int, stop_requested: threading.Event) -> bool:
    logging.info("Waiting for cloud connectivity")
    while not stop_requested.is_set():
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud reachability - OK")
                return True
            logging.debug("Cloud HEAD returned HTTP %s", response.status_code)
        except (requests.RequestException, OSError) as error:
            logging.debug("Cloud is unavailable: %s", error)
        stop_requested.wait(interval)
    return False
