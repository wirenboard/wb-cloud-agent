import logging
import threading
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wb.cloud_agent import __version__ as agent_package_version

USER_AGENT = f"wb-cloud-agent/{agent_package_version}"


def _head_status(url: str, timeout: float) -> int:
    """
    HTTP status of a HEAD request, after redirects; raises OSError or HTTPException on a network error.
    """
    request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status
    except HTTPError as exc:  # any status but 2xx, including a 3xx that is not followed
        with exc:  # it holds the open response; close the connection now, not on GC
            return exc.code


def wait_for_cloud_reachable(url: str, interval: int, stop_requested: threading.Event) -> bool:
    """
    Poll the cloud with HEAD requests until it answers; False if a stop was requested first.
    """
    logging.info("Start checking cloud reachability (interval: %ss)", interval)

    while not stop_requested.is_set():
        try:
            status = _head_status(url, timeout=15)
            if 200 <= status < 400:
                logging.info("Cloud reachability - OK")
                return True

            logging.debug("Cloud '%s' unreachable (status %s)", url, status)
        except (OSError, HTTPException) as exc:
            logging.debug("Cloud '%s' unreachable due to network issue: %s", url, exc)

        logging.debug("Retrying in %s seconds...", interval)
        stop_requested.wait(interval)

    return False
