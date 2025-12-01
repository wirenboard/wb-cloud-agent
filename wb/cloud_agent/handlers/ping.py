import logging
import time

import requests


def wait_for_cloud_reachable(url: str, period: int = 5, max_retries: int = 100) -> None:
    logging.info(
        "Trying to reach cloud at '%s' every %s seconds (max %s attempts)...", url, period, max_retries
    )

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if 200 <= response.status_code < 400:
                logging.info(
                    "Cloud '%s' is reachable (status %s) after %s attempts.",
                    url,
                    response.status_code,
                    attempt,
                )
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
            logging.debug("Retrying in %s seconds...", period)
            time.sleep(period)

    logging.info("Cloud '%s' is still unreachable after %s attempts.", url, max_retries)
