import logging
import subprocess
import threading

from wb.cloud_agent.handlers.diagnostics import upload_diagnostic
from wb.cloud_agent.settings import AppSettings


def fetch_diagnostics(settings: AppSettings, _payload, _mqtt):
    # remove old diagnostics
    try:
        for fname in settings.diag_archive.glob("diag_*.zip"):
            fname.unlink()
    except OSError as e:
        logging.warning("Erase diagnostic files failed: %s", e.strerror)

    def process_waiter():
        with subprocess.Popen(
            "wb-diag-collect diag",
            cwd=settings.diag_archive,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            process.wait()

        upload_diagnostic(settings)

    thread = threading.Thread(target=process_waiter)
    thread.start()
