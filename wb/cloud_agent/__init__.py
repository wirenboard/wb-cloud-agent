from importlib.metadata import PackageNotFoundError, version

from wb.cloud_agent.utils import get_apt_package_version

try:
    __version__ = version("wb-cloud-agent")
except PackageNotFoundError:
    __version__ = "unknown"

telegraf_package_version = get_apt_package_version("telegraf-wb-cloud-agent")
