FROM debian:bullseye

WORKDIR /src

RUN apt update

RUN apt install -y fakeroot build-essential devscripts python3 python3-venv python3-pip python3-stdeb python-all debhelper dh-python

RUN pip install setuptools stdeb

COPY ./src /src

RUN python3 setup.py --command-packages=stdeb.command bdist_deb
