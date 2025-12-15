import json
import logging
import subprocess
from collections.abc import Iterable
from typing import Optional

from wb.cloud_agent.constants import CLIENT_CERT_ERROR_MSG
from wb.cloud_agent.settings import AppSettings
from wb.cloud_agent.utils import parse_headers

DATA_DELIMITER = "|||"


class CloudNetworkError(OSError):
    """Network-level error while communicating with the cloud."""


def do_curl(
    settings: AppSettings,
    method: str = "get",
    endpoint: str = "",
    params: Optional[dict] = None,
    retry_opts: Optional[Iterable[str]] = None,
) -> tuple[dict, int]:
    output_format = DATA_DELIMITER + '{"code":"%{response_code}"}'

    if method == "get":
        command = ["curl"]
    elif method in ("post", "put", "delete"):
        command = ["curl", "-X", method.upper()]
        if params:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(params)]
    elif method == "multipart-post":
        command = ["curl", "-X", "POST", "-F", f"file=@{params}"]
    else:
        raise ValueError("Invalid method: " + method)

    if not retry_opts:
        retry_opts = [
            "--connect-timeout",
            "45",
            "--retry",
            "8",
            "--retry-delay",
            "1",
            "--retry-all-errors",
        ]

    command += [
        *retry_opts,
        "--cert",
        settings.client_cert_file,
        "--key",
        settings.client_cert_engine_key,
        "--engine",
        "ateccx08",
        "--key-type",
        "ENG",
        "-D",  # Capturing headers in stdout
        "-",
        "-w",
        output_format,
        settings.cloud_agent_url + endpoint,
    ]

    try:
        result = subprocess.run(command, timeout=360, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        if e.returncode == 58:
            raise RuntimeError(
                CLIENT_CERT_ERROR_MSG.format(
                    cert_file=settings.client_cert_file, cert_engine_key=settings.client_cert_engine_key
                )
            ) from e
        if e.returncode in (6, 7, 28):
            logging.debug(e)
            raise CloudNetworkError(
                f"{endpoint} Network error while accessing {settings.cloud_base_url}"
            ) from e
        raise e

    return handle_curl_output(settings, result.stdout)


def handle_curl_output(settings: AppSettings, stdout: bytes) -> tuple[dict, int]:
    decoded_output = stdout.decode("utf-8")

    header_section, decoded_result = decoded_output.split("\r\n\r\n", 1)

    response_headers = parse_headers(header_section)
    poll_interval = int(response_headers.get("x-poll-interval", settings.request_period_seconds))

    if poll_interval != settings.request_period_seconds:
        settings.request_period_seconds = poll_interval
        logging.debug("A new poll interval has been set: %s", settings.request_period_seconds)

    split_result = decoded_result.split(DATA_DELIMITER)
    if len(split_result) != 2:
        raise ValueError(f"Invalid data in response: {split_result}")

    try:
        data = json.loads(split_result[0])
    except json.JSONDecodeError:
        data = {}

    try:
        status_code = int(json.loads(split_result[1])["code"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid data in response: {split_result}") from e

    return data, status_code
