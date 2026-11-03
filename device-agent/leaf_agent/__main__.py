"""Owner-only local command line. Enrollment secrets are never printed."""
import argparse
import json
import os
import subprocess
import sys

from . import launchd
from .client import Client, ClientError
from .config import Config
from .core import Agent, configure_logging
from .packages import Registry


def main(argv=None):
    parser = argparse.ArgumentParser(prog="leaf_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--server", required=True)
    enroll.add_argument("--token", help="Prefer LEAF_ENROLL_TOKEN to keep the token out of argv")
    for command in ("run", "pause", "resume", "status", "uninstall", "install-service"):
        sub.add_parser(command)
    args = parser.parse_args(argv)
    config = Config()
    configure_logging(config)
    try:
        if args.command == "enroll":
            token = os.environ.pop("LEAF_ENROLL_TOKEN", None) or args.token
            if not token and not (config.home / "config.json").exists():
                parser.error("Set LEAF_ENROLL_TOKEN or pass --token")
            print(Client(config, args.server).register(token or ""))
        elif args.command == "run":
            return Agent(config, Client(config), Registry()).run()
        elif args.command == "pause":
            config.pause()
        elif args.command == "resume":
            config.resume()
        elif args.command == "status":
            enrolled = (config.home / "config.json").exists()
            print(json.dumps({"enrolled": enrolled, "paused": config.disabled,
                              "device_id": config.load()["device_id"] if enrolled else None}))
        elif args.command == "install-service":
            launchd.install(config)
        elif args.command == "uninstall":
            launchd.uninstall()
            config.wipe_identity()
        return 0
    except (ClientError, ValueError, OSError, subprocess.SubprocessError):
        print("Agent operation failed; check local configuration and Studio availability.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
