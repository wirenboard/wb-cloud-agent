# pylint: disable=unused-argument

from unittest.mock import patch

from wb.cloud_agent.main import show_activation_link


@patch("wb.cloud_agent.main.read_activation_link", return_value="https://activation.link")
def test_show_activation_link_with_valid_link(mock_read, mock_print, settings):
    show_activation_link(settings)
    mock_print.assert_called_once_with("Link for connect controller to cloud:\nhttps://activation.link")


@patch("wb.cloud_agent.main.get_ctrl_serial_number", return_value="ART6DDNT")
@patch("wb.cloud_agent.main.read_activation_link", return_value="unknown")
@patch("wb.cloud_agent.main.get_providers", return_value=["default", "staging"])
@patch(
    "wb.cloud_agent.main.load_providers_configs",
    return_value={
        "default": {"CLOUD_BASE_URL": "https://cloud.example.com"},
        "staging": {"CLOUD_BASE_URL": "https://staging.wirenboard.com"},
    },
)
def test_show_activation_link_with_unknown(mock_load, mock_get, mock_read, mock_serial, settings):
    with patch("builtins.print") as mock_print:
        show_activation_link(settings)

    mock_print.assert_any_call("Connected providers:")
    mock_print.assert_any_call("+----------------------------------------------------------------+")
    mock_print.assert_any_call("| Provider | Controller Url                                      |")
    mock_print.assert_any_call("|----------|-----------------------------------------------------|")
    mock_print.assert_any_call("| default  | https://cloud.example.com/controllers/ART6DDNT      |")
    mock_print.assert_any_call("| staging  | https://staging.wirenboard.com/controllers/ART6DDNT |")
    mock_print.assert_any_call("+----------------------------------------------------------------+")
