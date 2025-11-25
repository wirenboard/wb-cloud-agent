import logging
import subprocess
import time
from typing import Optional


def wait_for_ping(host: str, period: int = 5, max_retries: Optional[int] = None) -> bool:
    retries = 0

    while True:
        if max_retries is not None and retries >= max_retries:
            logging.error("Max retries reached. Host %s is still unreachable.", host)
            return False

        result = subprocess.run(  # pylint: disable=subprocess-run-check
            ["ping", "-c", "1", "-W", "2", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if result.returncode == 0:
            logging.info("Host %s is reachable", host)
            return True

        logging.warning("Host %s is unreachable. Retrying in %s seconds...", host, period)
        retries += 1
        time.sleep(period)
