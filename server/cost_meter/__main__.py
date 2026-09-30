"""Command-line entry point for the image-shipped cost meter."""
import argparse
import sys

from . import collect_storage_main, publish_main


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m cost_meter")
    parser.add_argument("command", choices=["publish", "collect-storage"])
    args = sys.argv[1:] if argv is None else argv
    command = parser.parse_args(args[:1]).command
    if command == "collect-storage":
        return collect_storage_main.main(args[1:])
    return publish_main.main(args[1:])


if __name__ == "__main__":
    sys.exit(main())
