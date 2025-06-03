# pylint: disable=unused-argument

from unittest.mock import patch

from wb.cloud_agent.main import show_activation_link


@patch("wb.cloud_agent.main.read_activation_link", return_value="https://activation.link")
def test_show_activation_link_with_valid_link(mock_read, mock_print, settings):
    show_activation_link(settings)
    mock_print.assert_called_once_with("Link for connect controller to cloud:\nhttps://activation.link")


@patch("wb.cloud_agent.main.read_activation_link", return_value="unknown")
@patch("wb.cloud_agent.main.get_providers", return_value=["default", "staging"])
@patch(
    "wb.cloud_agent.main.load_providers_configs",
    return_value={
        "default": {"CLOUD_BASE_URL": "https://cloud.example.com"},
        "staging": {"CLOUD_BASE_URL": "https://staging.wirenboard.com"},
    },
)
def test_show_activation_link_with_unknown(mock_load, mock_get, mock_read, mock_serial_number, settings):
    with patch("builtins.print") as mock_print:
        show_activation_link(settings)

    default_url = mock_load.return_value["default"]["CLOUD_BASE_URL"]
    staging_url = mock_load.return_value["staging"]["CLOUD_BASE_URL"]

    mock_print.assert_any_call("Connected providers:")
    expected_table = (
        "+------------+-----------------------------------------------------+\n"
        "| Provider   | Controller Url                                      |\n"
        "+============+=====================================================+\n"
        f"| default    | {default_url}/controllers/{mock_serial_number}      |\n"
        "+------------+-----------------------------------------------------+\n"
        f"| staging    | {staging_url}/controllers/{mock_serial_number} |\n"
        "+------------+-----------------------------------------------------+"
    )
    mock_print.assert_any_call(expected_table)
