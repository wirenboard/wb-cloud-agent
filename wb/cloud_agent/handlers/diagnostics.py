import logging
from http import HTTPStatus as status

from wb.cloud_agent.handlers.curl import do_curl
from wb.cloud_agent.settings import AppSettings


def upload_diagnostic(settings: AppSettings) -> None:
    files = sorted(settings.diag_archive.glob("diag_*.zip"), key=lambda p: p.stat().st_mtime)
    if not files:
        logging.error("No diagnostics collected")

        _, http_status = do_curl(
            settings=settings, method="put", endpoint="diagnostic-status/", params={"status": "error"}
        )
        if http_status != status.OK:
            logging.error("Not a %s status while updating diagnostic status: %s", status.OK, http_status)
        return

    last_diagnostic = files[-1]
    logging.info("Diagnostics collected: %s", last_diagnostic)

    _status_data, http_status = do_curl(
        settings=settings, method="multipart-post", endpoint="upload-diagnostic/", params=last_diagnostic
    )
    if http_status != status.OK:
        logging.error("Not a %s status while making upload_diagnostic request: %s", status.OK, http_status)

    last_diagnostic.unlink()
