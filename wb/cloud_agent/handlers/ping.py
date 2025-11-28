import logging
import time

import requests


def wait_for_cloud_reachable(url: str, period: int = 5) -> None:
    while True:
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud '%s' is reachable (status %s)", url, response.status_code)
                return
            logging.error("Cloud '%s' is unreachable (status %s)", url, response.status_code)
        except requests.RequestException as exc:
            logging.exception("Cloud '%s' is unreachable due to exception: %s", url, exc)

        logging.info("Retrying in %s seconds...", period)
        time.sleep(period)
