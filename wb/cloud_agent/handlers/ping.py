import logging
import subprocess
import time


def wait_for_ping(host: str, period: int = 5) -> None:
    while True:
        result = subprocess.run(  # pylint: disable=subprocess-run-check
            ["ping", "-c", "1", "-W", "2", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logging.info("Host %s is reachable", host)
            return

        logging.warning("Host %s is unreachable. Retrying in %s seconds...", host, period)
        time.sleep(period)
