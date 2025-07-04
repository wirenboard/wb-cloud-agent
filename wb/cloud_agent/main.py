#!/usr/bin/env python3
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from urllib.parse import urlparse

from wb.cloud_agent.commands import (
    add_on_premise_provider,
    add_provider,
    del_all_providers,
    del_provider,
    run_daemon,
    show_providers,
)


def parse_args() -> Namespace:
    main_parser = ArgumentParser()
    main_parser.set_defaults(func=show_providers)

    subparsers = main_parser.add_subparsers(
        dest="command", title="Actions", help="Choose mode:\n", required=False
    )

    add_provider_parser = subparsers.add_parser("add-provider", help="Add new cloud service provider")
    add_provider_parser.add_argument(
        "base_url",
        type=validate_url,
        help="Cloud Provider base URL, e.g. https://wirenboard.cloud",
    )
    add_provider_parser.add_argument(
        "--name", help="Cloud Provider name to add (override url hostname)", required=False
    )
    add_provider_parser.set_defaults(func=add_provider)

    add_on_premise_provider_parser = subparsers.add_parser(
        "use-on-premise", help="Delete all cloud service providers and then add new cloud service provider"
    )
    add_on_premise_provider_parser.add_argument(
        "base_url",
        type=validate_url,
        help="On-Premise Cloud Provider base URL, e.g. https://on-premise.cloud",
    )
    add_on_premise_provider_parser.add_argument(
        "--name", help="On-Premise Cloud Provider name to add (override url hostname)", required=False
    )
    add_on_premise_provider_parser.set_defaults(func=add_on_premise_provider)

    del_provider_parser = subparsers.add_parser("del-provider", help="Delete cloud service provider")
    del_provider_parser.add_argument(
        "provider_name",
        help="Cloud Provider name to delete",
    )
    del_provider_parser.set_defaults(func=del_provider)

    del_all_providers_parser = subparsers.add_parser(
        "del-all-providers", help="Delete all cloud service providers"
    )
    del_all_providers_parser.set_defaults(func=del_all_providers)

    run_daemon_parser = subparsers.add_parser("run-daemon", help="Run cloud agent in daemon mode")
    run_daemon_parser.add_argument(
        "provider_name",
        help="Cloud Provider name to run",
    )
    run_daemon_parser.add_argument("--broker", help="MQTT broker url", required=False)
    run_daemon_parser.set_defaults(func=run_daemon)

    return main_parser.parse_args()


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
        raise ArgumentTypeError(f"Invalid URL: {value}")
    return value


def main() -> int:
    options = parse_args()
    return options.func(options)
