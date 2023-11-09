FROM debian:bullseye

WORKDIR /src

RUN apt update

RUN apt install -y fakeroot build-essential

RUN apt install -y devscripts

RUN apt install -y python3

RUN apt-get install -y python3-venv python3-pip

RUN pip install setuptools stdeb

RUN apt install -y python3-stdeb fakeroot python-all
RUN apt install -y debhelper dh-python

COPY ./src /src

RUN python3 setup.py --command-packages=stdeb.command bdist_deb
