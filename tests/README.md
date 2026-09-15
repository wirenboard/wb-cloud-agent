# Cloud Agent tests

Run from the repository root with the package dependencies and pytest installed:

```sh
python3 -m pytest tests/
```

No controller, test stand, MQTT broker or network access is required. Tests replace
MQTT, HTTP, subprocesses, controller paths and signal registration at their boundaries.
Temporary files belong to pytest's temporary directory. The collector tests render
the bundled template, including when pytest runs from the Debian `.pybuild` tree.

## Daemon behavior

`test_lifecycle_behavior.py` exercises the public daemon entry point and MQTT adapter.

| Test | Behavior |
|---|---|
| `test_broker_recovery_restores_the_latest_retained_snapshot` | Cache only the latest disconnected state; restore metadata, values and provider listing on CONNACK |
| `test_initial_authentication_refusal_is_terminal` | Initial authentication refusal reaches the main thread |
| `test_reconnect_after_cleanup_cannot_restore_deleted_topics` | A late CONNACK cannot undo cleanup |
| `test_hardware_revision_is_received_without_a_cloud_request` | MQTT callback caches hardware revision without blocking on cloud I/O |
| `test_shutdown_disconnects_before_stopping_the_mqtt_loop` | Graceful disconnect precedes thread shutdown |
| `test_startup_retries_without_changing_starting_status` | Keep `starting` during startup retries; then publish `connecting` and `ok` |
| `test_event_failure_recovers_in_the_same_daemon` | Retry timeout/network errors without restarting the daemon |
| `test_stop_while_cloud_unavailable_preserves_saved_link_then_cleans_up` | Publish the saved link before cloud availability; SIGINT/SIGTERM remove owned topics |
| `test_daemon_accepts_custom_broker` | Preserve the CLI broker override |
| `test_unreadable_activation_state_is_not_an_authentication_error` | Do not mistake a local state-file permission error for initial MQTT authentication refusal |
| `test_authentication_refusal_after_connection_is_not_terminal` | Runtime connection refusals do not become fatal authentication errors |
| `test_stop_during_initial_broker_outage` | Stop successfully and log unavailable cleanup |
| `test_invalid_configuration_exits_six` | Missing, malformed and non-object configurations exit with 6 |
| `test_initial_broker_failure_is_retried` | Retry socket connection failure on the same client |

`test_mqtt.py` covers MQTT state publication and cleanup.

| Test | Behavior |
|---|---|
| `test_provider_command_does_not_change_status` | Provider CLI does not publish daemon status or install its Last Will |
| `test_publish_vdev` | Preserve device/control metadata |
| `test_publish_ctrl` | Publish retained control state with the same QoS as cleanup |
| `test_update_providers_list` | Publish the shared provider listing |
| `test_cleanup_removes_owned_topics_but_preserves_provider_listing` | Confirm deletion of standard and dynamically published owned topics |
| `test_failed_cleanup_is_logged` | Report disconnection and unconfirmed deletion |
| `test_invalid_broker_url_is_rejected_before_client_creation` | Reject unusable broker URLs before constructing a client |

`test_ping.py` covers the cloud reachability wait.

| Test | Behavior |
|---|---|
| `test_reachable_cloud_returns_success` | Accept successful HTTP responses/redirects |
| `test_cloud_recovers_after_an_outage` | Retry HTTP and transport failures |
| `test_stop_while_waiting_for_cloud` | Interrupt the retry delay on shutdown |

## Generated collector

`test_collector_lifecycle.py` runs functions from `metrics_collector.py.tpl`.

| Test | Behavior |
|---|---|
| `test_initial_authentication_failure_exits_two` | Initial authentication failure exits with 2 and closes MQTT |
| `test_stop_without_connack_exits_zero` | Stop while waiting for actual connection confirmation |
| `test_authentication_refusal_after_success_is_retried` | Recreating a client does not reset the initial-authentication decision |
| `test_reconnect_invalidates_rpc_reply_subscriptions` | A new broker session requires fresh RPC reply subscriptions |
| `test_stop_during_rate_limit_retry_preserves_confirmed_checkpoint` | An interrupted HTTP 429 retry never checkpoints unsent metrics |
| `test_stop_interrupts_iteration_delay_and_disconnects` | Stop without starting another collection iteration |

## Existing CLI and cloud contracts

Related test-name families are grouped below; parametrized cases cover their input variants.

| File / production area | Tests → checks |
|---|---|
| `test_commands.py` / commands | `test_show_providers_*`, `test_add_provider_*`, `test_add_on_premise_provider`, `test_del_provider_*`, `test_del_all_providers_*`, `test_del_controller_from_cloud_success` → provider operations, duplicates, absent providers and MQTT failures |
| `test_main.py`, `test_prog_parser.py` / CLI | `test_validate_url_*`, `test_main_with_*`, `test_unrecognized_arg`, `test_base_url_validator_*`, `test_provider_name_validator_*` → dispatch and argument validation |
| `test_change_provider.py` / CLI | `test_add_provider_cmd` → add-provider entry point |
| `test_settings.py`, `test_settings_extended.py` / settings | `test_settings`, `test_app_settings_*`, `test_configure_app_*`, `test_setup_log_*`, `test_generate_provider_config`, `test_delete_provider_config_*`, `test_get_provider_names_*`, `test_provider_display_url_*`, `test_load_providers_data_*` → configuration, logging, provider storage and display |
| `test_providers_tools.py`, `test_show_activation_link.py` / provider utilities | `test_get_provider_names`, `test_load_providers_data_with_mocks`, `test_table_show_activation_link_with_unknown` → provider listing and missing activation link |
| `test_startup.py` / startup requests | `test_make_start_up_request_*`, `test_send_packages_version_*`, `test_collect_package_versions`, `test_send_hardware_revision_*` → activation response validation and unchanged request payloads |
| `test_do_curl.py` / curl | `test_do_curl_*`, `test_handle_curl_output_*` → HTTP methods, certificate/network failures, payloads and response parsing |
| `test_events.py` / events | `test_make_event_request*`, `test_event_confirm_invalid_status`, `test_event_delete_controller_*` → event dispatch, confirmation, empty responses and errors |
| `test_diagnostics_handler.py` / diagnostics | `test_upload_diagnostic_*` → archive selection and failed upload/status requests |
| `test_provider_deletion.py`, `test_lifecycle.py` / provider deletion | `test_delete_provider_*`, `test_stop_services_and_del_configs_*` → service stop, tunnel/link handling and config removal |
| `test_services.py` / activation, tunnel, metrics, diagnostics | `test_read_activation_link_*`, `test_update_activation_link`, `test_write_activation_link`, `test_update_tunnel_config`, `test_update_metrics_config_*`, `test_cloud_vars_*`, `test_reconcile_*`, `test_bundled_template_renders_with_all_substitutions`, `test_report_metrics_health_*`, `test_monitor_metrics_service_*`, `test_collect_service_journal_limits_utf8_bytes`, `test_fetch_diagnostics*` → persisted data, safe template rendering, metrics service health and diagnostics |
| `test_utils.py` / utilities | `test_base_url_to_agent_url`, `test_get_controller_url*`, `test_normalize_base_url`, `test_parse_headers_*`, `test_read_*`, `test_write_to_file*`, `test_start_and_enable_service*`, `test_stop_and_disable_service`, `test_show_providers_table_*` → URL/file/header helpers and system service commands |
