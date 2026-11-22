"""Provision, mint and check staging investor links without echoing credentials."""
import argparse
import base64
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
import importlib.util
import os
from pathlib import Path
import secrets
import subprocess
import sys


_SPEC = importlib.util.spec_from_file_location(
    "investor_demo", Path(__file__).resolve().parents[1] / "server" / "investor_demo.py")
demo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(demo)
PARAMETER = "/stg/leaf/platform/investor-demo-secret"
LINK_PREFIX = demo.ORIGIN + "/app#investor="
_WINDOWS = os.name == "nt"


class Refused(Exception):
    """Operator-facing error with a credential-free message."""


def _resolve_secret(client, parameter):
    result = client.get_parameter(Name=parameter, WithDecryption=True)
    if result["Parameter"]["Type"] != "SecureString":
        raise Refused("Expected a SecureString parameter.")
    return result["Parameter"]["Value"]


@contextmanager
def _configured(secret):
    settings = {"LEAF_INVESTOR_DEMO_ENABLED": "1", "LEAF_RUNTIME_ENV": "staging",
                "LEAF_AUTH_LIVE": "1", "LEAF_INVESTOR_DEMO_SECRET": secret}
    previous = {key: os.environ.get(key) for key in settings}
    try:
        os.environ.update(settings)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _output_path(path):
    parent = path.parent.resolve(strict=True)
    work_tree = os.environ.get("GIT_WORK_TREE")
    if work_tree and parent.is_relative_to(Path(work_tree).resolve()):
        raise Refused("Choose an output path outside a git working tree.")
    # Include linked worktrees (.git is a file), repositories on mounted
    # volumes, and the repository metadata directory itself.
    if any((p / ".git").exists() or p.name.lower() == ".git" for p in (parent, *parent.parents)):
        raise Refused("Choose an output path outside a git working tree.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    probe = subprocess.run(["git", "-C", str(parent), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, env={**env, "LC_ALL": "C"})
    if probe.returncode != 128 or b"not a git repository" not in probe.stderr:
        raise Refused("Could not confirm an output path outside a git working tree.")
    return parent / path.name


def _windows_account():
    import ctypes
    from ctypes import wintypes

    # NameSamCompatible returns DOMAIN\user for the calling identity.
    get_name = ctypes.windll.secur32.GetUserNameExW
    get_name.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    get_name.restype = ctypes.c_ubyte
    size = wintypes.ULONG()
    get_name(2, None, ctypes.byref(size))
    name = ctypes.create_unicode_buffer(size.value)
    if not get_name(2, name, ctypes.byref(size)):
        raise OSError("Windows account lookup failed.")
    return name.value


def _write_link(path, token):
    # O_EXCL also refuses a final symlink and preserves any existing link file.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refused("Output file exists. Move it away first.") from None
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as dest:
            if _WINDOWS:
                try:
                    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", _windows_account() + ":F"],
                                   capture_output=True, timeout=15, check=True)
                except Exception:
                    raise Refused("Could not restrict the link file to the current user.") from None
            dest.write(LINK_PREFIX + token + "\n")
    except BaseException:
        path.unlink()
        raise


def _check(path, now):
    with path.open("rb") as source:
        raw = source.read(len(LINK_PREFIX) + demo.LINK_MAX_CHARS + 3)
    url = raw.decode("ascii").removesuffix("\n").removesuffix("\r")
    if not url.startswith(LINK_PREFIX):
        raise Refused("invalid")
    verified = demo.verify_link_token(url[len(LINK_PREFIX):], now=now)
    return datetime.fromtimestamp(verified.expires_at, timezone.utc).isoformat()


def main(argv=None, *, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("put-secret")
    put.add_argument("--rotate", action="store_true")
    mint = commands.add_parser("mint")
    mint.add_argument("--expires", required=True, metavar="YYYY-MM-DD")
    mint.add_argument("--out", required=True, type=Path)
    check = commands.add_parser("check")
    check.add_argument("--file", required=True, type=Path)
    for command in (put, mint, check):
        command.add_argument("--parameter", default=PARAMETER)
        command.add_argument("--region", default="us-east-1")
    args = parser.parse_args(argv)
    try:
        now = int(datetime.now(timezone.utc).timestamp())
        if args.command == "mint":
            day = date.fromisoformat(args.expires)
            expiry = datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)
            if day.isoformat() != args.expires or not 0 < int(expiry.timestamp()) - now <= demo.LINK_MAX_LIFETIME_S:
                raise Refused("Expiry must be a future UTC day end within 31 days.")
            path = _output_path(args.out)
        try:
            if client is None:
                import boto3
                client = boto3.client("ssm", region_name=args.region)
            if args.command != "put-secret":
                secret = _resolve_secret(client, args.parameter)
        except Exception:
            if args.command == "check":
                print("cannot check: secret unavailable")
                return 2
            raise
        if args.command == "put-secret":
            secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
            with _configured(secret):
                kid = demo.key_id()
            try:
                result = client.put_parameter(Name=args.parameter, Value=secret, Type="SecureString",
                                              Overwrite=args.rotate)
            except client.exceptions.ParameterAlreadyExists:
                raise Refused("Parameter exists. Use --rotate to replace it.") from None
            print(args.parameter, result["Version"], kid)
        else:
            with _configured(secret):
                if args.command == "check":
                    print("valid until", _check(args.file, now))
                else:
                    token = demo.mint_link_token(issued_at=now, expires_at=int(expiry.timestamp()))
                    _write_link(path, token)
                    print(path, demo.key_id(), expiry.isoformat())
    except Exception as error:
        # SDK, parser and file exceptions can include their input. Only our
        # fixed messages are suitable for operators.
        if args.command == "check":
            print("invalid")
        else:
            print(str(error) if isinstance(error, Refused) else "Investor demo operation failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
