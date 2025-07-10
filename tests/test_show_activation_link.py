# pylint: disable=line-too-long

from unittest.mock import patch

from wb.cloud_agent.constants import UNKNOWN_LINK
from wb.cloud_agent.settings import Provider
from wb.cloud_agent.utils import show_providers_table


def test_table_show_activation_link_with_unknown(mock_serial_number):
    awaiting_activation_url = "https://staging.wirenboard.com/controllers?add=396c20993ff8dc6e08d45686f93973d6cff6e179f1fc1724502f91b8bf5d"
    already_activated_url = f"https://wirenboard.cloud/controllers/{mock_serial_number}"
    providers = [
        Provider(
            name="awaiting_activation",
            config={"CLOUD_BASE_URL": "https://staging.wirenboard.com"},
            activation_link=awaiting_activation_url,
        ),
        Provider(
            name="already_activated",
            config={"CLOUD_BASE_URL": "https://wirenboard.cloud"},
            activation_link=UNKNOWN_LINK,
        ),
    ]

    with patch("builtins.print") as mock_print:
        show_providers_table(providers)

    expected_table = (
        "| Provider            | Controller Url / Activation Url                                                                             |\n"
        "|---------------------|-------------------------------------------------------------------------------------------------------------|\n"
        f"| awaiting_activation | {awaiting_activation_url} |\n"
        f"| already_activated   | {already_activated_url}                                                               |"
    )
    mock_print.assert_any_call(expected_table)
