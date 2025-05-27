from unittest.mock import patch

from wb.cloud_agent.main import show_activation_link


@patch(
    "wb.cloud_agent.main.read_activation_link", return_value="https://activation.link"
)
def test_show_activation_link_with_valid_link(mock_read, mock_print, settings):
    show_activation_link(settings)
    mock_print.assert_called_once_with(
        "Link for connect controller to cloud:\nhttps://activation.link"
    )


@patch(
    "wb.cloud_agent.main.get_controller_url",
    return_value="https://cloud.example.com/org/controllers/CTRL123",
)
@patch("wb.cloud_agent.main.read_activation_link", return_value="unknown")
def test_show_activation_link_with_unknown(
    mock_read, mock_get_url, mock_print, settings
):
    show_activation_link(settings)
    mock_print.assert_called_once_with(
        "Controller already connect to cloud:\nhttps://cloud.example.com/org/controllers/CTRL123"
    )
