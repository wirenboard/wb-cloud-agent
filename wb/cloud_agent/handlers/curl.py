import json
import subprocess
from collections.abc import Iterable
from typing import Optional

from wb.cloud_agent.constants import CLIENT_CERT_ERROR_MSG
from wb.cloud_agent.settings import AppSettings


def do_curl(
    settings: AppSettings,
    method: str = "get",
    endpoint: str = "",
    params: Optional[dict] = None,
    retry_opts: Optional[Iterable[str]] = None,
) -> tuple[dict, int]:
    data_delimiter = "|||"
    output_format = data_delimiter + '{"code":"%{response_code}"}'

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

    url = settings.cloud_agent_url + endpoint

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
        "-w",
        output_format,
        url,
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
        raise e

    decoded_result = result.stdout.decode("utf-8")
    split_result = decoded_result.split(data_delimiter)
    if len(split_result) != 2:
        raise ValueError("Invalid data in response: " + str(split_result))

    try:
        data = json.loads(split_result[0])
    except json.JSONDecodeError:
        data = {}

    try:
        status_code = int(json.loads(split_result[1])["code"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid data in response: {split_result}") from e

    return data, status_code
