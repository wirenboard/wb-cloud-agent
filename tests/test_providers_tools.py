import json
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from wb.cloud_agent.settings import PROVIDERS_CONF_DIR, get_providers, load_providers_configs


@patch(
    'wb.cloud_agent.settings.os.path.isdir',
    return_value=True,
)
@patch(
    'wb.cloud_agent.settings.os.path.exists',
    return_value=True,
)
@patch(
    'wb.cloud_agent.settings.os.listdir',
    return_value=['staging', 'prod', 'test'],
)
def test_get_providers(mock_os_listdir, mock_os_exists, mock_os_isdir):
    providers = get_providers()
    assert providers == ['default', 'staging', 'prod', 'test']


@pytest.fixture
def example_configs():
    return {
        'default': {
            'LOG_LEVEL': 'INFO',
            'CLIENT_CERT_ENGINE_KEY': 'ATECCx08:00:02:C0:00',
            'CLOUD_BASE_URL': 'https://wirenboard.cloud/',
            'CLOUD_AGENT_URL': 'https://agent.wirenboard.cloud/api-agent/v1/',
        },
        'staging': {
            'LOG_LEVEL': 'INFO',
            'CLIENT_CERT_ENGINE_KEY': 'ATECCx08:00:02:C0:00',
            'CLOUD_BASE_URL': 'https://cloud-staging.wirenboard.com/',
            'CLOUD_AGENT_URL': 'https://agent.cloud-staging.wirenboard.com/api-agent/v1/',
        },
    }


def test_load_providers_configs_with_mocks(example_configs: dict):
    providers = list(example_configs.keys())

    path_to_content = {
        str(Path(PROVIDERS_CONF_DIR) / provider / 'wb-cloud-agent.conf'): json.dumps(cfg)
        for provider, cfg in example_configs.items()
    }

    def exists_side_effect(self: Path):
        return str(self) in path_to_content

    def open_side_effect(self: Path, *args, **kwargs):
        content = path_to_content.get(str(self))
        return mock_open(read_data=content)()

    with (
        patch.object(Path, 'exists', new=exists_side_effect),
        patch.object(Path, 'open', new=open_side_effect),
    ):
        #
        providers_configs = load_providers_configs(providers)

    assert providers_configs == example_configs
