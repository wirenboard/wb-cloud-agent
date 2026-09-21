#!/usr/bin/env python3

import os

from setuptools import setup


def get_version():
    return os.environ.get("DEB_VERSION", "0.0.0").split("~")[0].replace("-", "+")


setup(
    name="wb-cloud-agent",
    version=get_version(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wirenboard Cloud agent",
    license="MIT",
    url="https://github.com/wirenboard/wb-cloud-agent",
    packages=[
        "wb.cloud_agent",
        "wb.cloud_agent.handlers",
        "wb.cloud_agent.services",
    ],
    scripts=["wb-cloud-agent"],
)
