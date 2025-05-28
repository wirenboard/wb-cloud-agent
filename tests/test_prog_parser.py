import sys

import pytest

from wb.cloud_agent.main import parse_args


def test_unrecognized_arg(capsys, set_argv):
    invalid_arg = '--flag'
    set_argv(['wb-cloud-agent', invalid_arg])

    with pytest.raises(SystemExit) as exc_info:
        parse_args()

    _, err = capsys.readouterr()
    assert exc_info.value.code == 2
    assert f'unrecognized arguments: {invalid_arg}' in err


def test_base_url_validator_with_valid_url(monkeypatch):
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'wb-cloud-agent',
            'change-provider',
            'staging',
            'https://cloud-staging.wirenboard.com/',
        ],
    )
    parse_args()


@pytest.mark.parametrize(
    'invalid_url',
    [
        'httXps://cloud-staging.wirenboard.com/',
        'https:://cloud-staging.wirenboard.com/',
        'https:/cloud-staging.wirenboard.com/',
        'bad-url',
    ],
)
def test_base_url_validator_with_invalid_urls(monkeypatch, capsys, invalid_url):
    monkeypatch.setattr(sys, 'argv', ['wb-cloud-agent', 'change-provider', 'staging', invalid_url])

    with pytest.raises(SystemExit) as exc_info:
        parse_args()

    _, err = capsys.readouterr()
    assert exc_info.value.code == 2
    assert f'Invalid URL: {invalid_url}' in err
