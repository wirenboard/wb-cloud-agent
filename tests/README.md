# Tests

The test suite uses pytest and does not require a controller, MQTT broker, cloud account, or hardware.

- `test_metrics_collector.py` renders the shipped collector template and checks MQTT CONNACK mapping,
  reconnect subscription recovery, interruptible startup/shutdown, and the systemd exit policy.
- The remaining files cover the cloud agent commands, provider lifecycle, settings, MQTT publications,
  cloud requests, and generated service configuration with mocks and temporary files.

Run the same unit suite used by the Debian package build with:

```sh
python3 -m pytest tests
```
