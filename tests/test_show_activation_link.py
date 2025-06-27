# pylint: disable=unused-argument,line-too-long

from unittest.mock import patch

from wb.cloud_agent.main import show_providers_table


def test_table_show_activation_link_with_unknown():
    awaiting_activation = "https://staging.wirenboard.com/controllers?add=396c20993ff8dc6e08d45686f93973d6cff6e179f1fc1724502f91b8bf5d"
    already_activated = "https://wirenboard.cloud/organizations/controllers/A25NDEMJ"

    with patch("builtins.print") as mock_print:
        show_providers_table(
            {
                "awaiting_activation": awaiting_activation,
                "already_activated": already_activated,
            }
        )
    expected_table = (
        "| Provider            | Controller Url / Activation Url                                                                             |\n"
        "|---------------------|-------------------------------------------------------------------------------------------------------------|\n"
        f"| awaiting_activation | {awaiting_activation} |\n"
        f"| already_activated   | {already_activated}                                                 |"
    )
    mock_print.assert_any_call(expected_table)
