import json
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
from wb.cloud_agent.constants import NOCONNECT_LINK
from wb.cloud_agent.settings import (
    PROVIDERS_CONF_DIR,
    get_provider_names,
    load_providers_data,
)


@patch("wb.cloud_agent.settings.Path.exists", return_value=True)
@patch("wb.cloud_agent.settings.Path.iterdir")
def test_get_provider_names(iterdir_mock, exists_mock):
    mock_dir = MagicMock()
    mock_dir.name = "staging"
    mock_dir.is_dir.return_value = True

    iterdir_mock.return_value = [mock_dir]

    assert get_provider_names() == ["staging"]


@pytest.fixture
def example_configs():
    return {
        "default": {
            "LOG_LEVEL": "INFO",
            "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:02:C0:00",
            "CLOUD_BASE_URL": "https://wirenboard.cloud/",
        },
        "staging": {
            "LOG_LEVEL": "INFO",
            "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:02:C0:00",
            "CLOUD_BASE_URL": "https://cloud-staging.wirenboard.com/",
        },
    }


def test_load_providers_data_with_mocks(example_configs: dict,):  # pylint: disable=redefined-outer-name
    provider_names = list(example_configs.keys())

    path_to_content = {
        str(Path(PROVIDERS_CONF_DIR) / provider / "wb-cloud-agent.conf"): json.dumps(cfg)
        for provider, cfg in example_configs.items()
    }

    def exists_side_effect(self: Path):
        return str(self) in path_to_content

    def open_side_effect(self: Path, *args, **kwargs):
        content = path_to_content.get(str(self))
        return mock_open(read_data=content)()

    with (
        patch.object(Path, "exists", new=exists_side_effect),
        patch.object(Path, "open", new=open_side_effect),
    ):
        #
        providers = load_providers_data(provider_names)

    assert {provider.name: provider.config for provider in providers} == example_configs
    assert [provider.activation_link for provider in providers] == [
        NOCONNECT_LINK,
        NOCONNECT_LINK,
    ]
