import logging
import time

import requests


def wait_for_cloud_reachable(url: str, interval: int = 5, max_retries: int = 100) -> None:
    logging.info("Waiting for cloud connectivity (interval=%ss, max_attempts=%s)", interval, max_retries)

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info("Cloud reachable")
                return

            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable (status %s)",
                attempt,
                max_retries,
                url,
                response.status_code,
            )
        except requests.RequestException as exc:
            logging.debug(
                "Attempt %s/%s: cloud '%s' unreachable due to exception: %s", attempt, max_retries, url, exc
            )

        if attempt < max_retries:
            logging.debug("Retrying in %s seconds...", interval)
            time.sleep(interval)

    logging.info("Cloud unreachable after %s attempts — exiting", max_retries)
