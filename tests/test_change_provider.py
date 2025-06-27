# pylint: disable=unused-argument

import sys
from unittest.mock import patch

from wb.cloud_agent.main import parse_args


def test_add_provider_cmd(monkeypatch):
    with patch(
        "wb.cloud_agent.main.add_provider",
        side_effect=lambda options, mqtt: None,
    ) as _mock:
        base_url = "https://cloud-staging.wirenboard.com/"
        monkeypatch.setattr(sys, "argv", ["wb-cloud-agent", "add-provider", base_url])

        options = parse_args()

        assert options.base_url == base_url

        options.func(options, None)

        _mock.assert_called_once_with(options, None)
