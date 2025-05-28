import sys
from unittest.mock import patch

from wb.cloud_agent.main import parse_args


def test_change_provider_cmd(monkeypatch):
    def change_provider_mock(options, mqtt):
        pass

    with patch(
        'wb.cloud_agent.main.change_provider',
        side_effect=change_provider_mock,
    ) as _mock:
        provider_name = 'staging'
        base_url = 'https://cloud-staging.wirenboard.com/'
        monkeypatch.setattr(sys, 'argv', ['wb-cloud-agent', 'change-provider', provider_name, base_url])

        options = parse_args()

        assert options.provider_name == provider_name
        assert options.base_url == base_url

        options.func(options, None)

        _mock.assert_called_once_with(options, None)
