import logging
import time

import requests


def wait_for_cloud_reachable(url: str, period: int = 5) -> None:
    logging.info("Trying to reach cloud at '%s' every %s seconds...", url, period)
    while True:
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud '%s' is reachable (status %s)", url, response.status_code)
                return

            logging.debug("Cloud '%s' is unreachable (status %s)", url, response.status_code)
        except requests.RequestException as exc:
            logging.debug("Cloud '%s' is unreachable due to exception: %s", url, exc)

        logging.debug("Retrying in %s seconds...", period)
        time.sleep(period)
