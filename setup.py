#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1]


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
    ],
)
