"""CLI for listing and editing BMP TOML configuration profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .configuration import ConfigurationRegistry


def _registry(args: argparse.Namespace) -> ConfigurationRegistry:
    return ConfigurationRegistry(args.project_root / "registries" / "configurations")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bmp-config")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="MagentaBench project root (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="list configuration profiles")

    get_parser = subparsers.add_parser("get", help="print one configuration profile")
    get_parser.add_argument("id")

    put_parser = subparsers.add_parser("put", help="create or replace a TOML profile")
    put_parser.add_argument("name")
    put_parser.add_argument("file", type=Path)

    delete_parser = subparsers.add_parser("delete", help="delete one profile")
    delete_parser.add_argument("id")

    args = parser.parse_args(argv)
    registry = _registry(args)
    if args.command == "list":
        print(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in registry.list()
                ],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
    elif args.command == "get":
        item = registry.get(args.id)
        print(
            json.dumps(
                {
                    "name": item.name,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "configuration": item.configuration,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
    elif args.command == "put":
        print(registry.upsert(args.name, args.file.read_bytes()).path)
    elif args.command == "delete":
        print(registry.delete(args.id))
    return 0


__all__ = ["main"]
