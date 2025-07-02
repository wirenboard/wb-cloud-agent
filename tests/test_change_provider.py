from unittest.mock import MagicMock

from wb.cloud_agent.main import parse_args


def test_add_provider_cmd(set_argv):
    base_url = "https://cloud-staging.wirenboard.com/"
    set_argv(["wb-cloud-agent", "add-provider", base_url])

    options = parse_args()

    assert options.base_url == base_url

    _mock = MagicMock()
    options.func = _mock

    options.func(options)

    _mock.assert_called_once_with(options)
