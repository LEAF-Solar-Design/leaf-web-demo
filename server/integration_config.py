"""Nonsecret image settings, loaded lazily with presence-based env overrides."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

MAX_SOURCE_BYTES = 4096
_SCHEMA = "leaf.studio-integration-config.v1"
_KEY_ARN = re.compile(
    r"arn:aws(?:-us-gov|-cn)?:kms:[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+:[0-9]{12}:key/"
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|mrk-[0-9a-f]{32})\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9:/_-]{1,2048}\Z")


class IntegrationConfigError(Exception):
    """Fixed nonsecret configuration failure."""


@dataclass(frozen=True)
class IntegrationSettings:
    enabled: bool
    api_origin: str
    kms_key_id: str


def _origin(value):
    if (type(value) is not str or not 1 <= len(value) <= 2048
            or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError()
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError()
    if ":" in host:
        if str(ipaddress.IPv6Address(host)) != host:
            raise ValueError()
        canonical_host = "[" + host + "]"
    else:
        if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
                or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                       for label in host.split("."))):
            raise ValueError()
        canonical_host = host
    port = parsed.port
    canonical = "https://" + canonical_host + (":" + str(port) if port is not None else "")
    if value != canonical or port == 443 or port == 0:
        raise ValueError()
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def load_integration_config():
    """Read the bounded packaged source on demand; never cache environment state."""
    source = None
    try:
        with Path(__file__).with_name("integration_config.json").open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if not 1 <= len(raw) <= MAX_SOURCE_BYTES:
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (type(data) is not dict or set(data) != {"schema", "enabled", "apiOrigin", "kmsKeyArn"}
                or type(data["schema"]) is not str or data["schema"] != _SCHEMA
                or type(data["enabled"]) is not bool
                or type(data["kmsKeyArn"]) is not str or not _KEY_ARN.fullmatch(data["kmsKeyArn"])):
            raise ValueError()
        source = IntegrationSettings(data["enabled"], _origin(data["apiOrigin"]), data["kmsKeyArn"])
    except Exception:
        pass
    if source is None:
        raise IntegrationConfigError("INTEGRATION_CONFIG_SOURCE") from None

    effective = None
    try:
        enabled = (os.environ["LEAF_INTEGRATION_TURN_GRANTS_ENABLED"] == "1"
                   if "LEAF_INTEGRATION_TURN_GRANTS_ENABLED" in os.environ else source.enabled)
        origin = (os.environ["LEAF_INTEGRATION_SERVICE_ORIGIN"]
                  if "LEAF_INTEGRATION_SERVICE_ORIGIN" in os.environ else source.api_origin)
        key = (os.environ["LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID"]
               if "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID" in os.environ else source.kms_key_id)
        if not _KEY_ID.fullmatch(key):
            raise ValueError()
        effective = IntegrationSettings(enabled, _origin(origin), key)
    except Exception:
        pass
    if effective is None:
        raise IntegrationConfigError("INTEGRATION_CONFIG_OVERRIDE") from None
    return effective
