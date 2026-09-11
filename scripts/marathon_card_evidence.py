"""Offline structural validation of marathon card receipts, not provenance attestation."""
import argparse
import json
import math
from pathlib import Path
import re

SCHEMA = "leaf.marathon-card-proof.v1"
TIERS = ("fixture-replay", "retained-real-run-replay", "live-run")
KEYS = {"schema", "evidence_tier", "run_id", "source_sha", "tenant_binding",
        "source_artifacts", "observed_records", "screenshots", "assertions"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{%d}" % length, value) is not None


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def load_evidence(path):
    """Read JSON without allowing duplicate keys or non-JSON numeric constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("invalid JSON constant: " + value)

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=pairs, parse_constant=constant)


def validate_evidence(document):
    """Return the validated document; raise ValueError on a broken contract.

    Tenant binding is the reader's directory tenant, not a state.json field.
    Capture times are independent of optional manifest-derived start times.
    Files/digests and observer declarations need external provenance review.
    """
    require(isinstance(document, dict) and set(document) == KEYS, "receipt keys must match schema exactly")
    d = document
    require(d["schema"] == SCHEMA, "unknown schema")
    require(d["evidence_tier"] in TIERS, "unknown evidence tier")
    for key in ("run_id", "tenant_binding"):
        value = d[key]
        require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", value)
                and set(value) != {"."}, "invalid " + key)
    require(digest(d["source_sha"], 40) or digest(d["source_sha"], 64), "invalid source_sha")
    for key in ("source_artifacts", "observed_records", "screenshots", "assertions"):
        require(isinstance(d[key], list) and bool(d[key]), key + " must be a nonempty array")
    require(all(text(a) for a in d["assertions"]), "assertions must be text")
    artifacts = {}
    for a in d["source_artifacts"]:
        require(isinstance(a, dict) and text(a.get("filename")) and digest(a.get("sha256")), "invalid source artifact")
        require(a["filename"] not in artifacts, "duplicate artifact filename")
        artifacts[a["filename"]] = a
    observations = {}
    previous = 0
    states = []
    real = d["evidence_tier"] != "fixture-replay"
    for o in d["observed_records"]:
        require(isinstance(o, dict), "invalid observation")
        require(text(o.get("observation_id")) and o["observation_id"] not in observations, "invalid observation id")
        require(o.get("run_id") == d["run_id"] and o.get("tenant_binding") == d["tenant_binding"], "observation binding mismatch")
        require(timestamp(o.get("observed_at")) and o["observed_at"] > previous, "observations must be chronological")
        previous = o["observed_at"]
        require(o.get("synthetic") is False if real else type(o.get("synthetic")) is bool, "synthetic history refused")
        require(o.get("reconstructed") is False if real else type(o.get("reconstructed")) is bool, "reconstructed history refused")
        require(o.get("capture_mode") == ("live" if d["evidence_tier"] == "live-run" else "replay"), "capture mode mismatch")
        body = o.get("api_result")
        require(isinstance(body, dict) and isinstance(body.get("builds"), list), "missing actual API result")
        records = [r for r in body["builds"] if isinstance(r, dict) and r.get("id") == d["run_id"] and r.get("lane") == "fold"]
        require(len(records) == 1, "API result must contain exactly one matching fold run")
        record = records[0]
        require(record.get("state") in ("queued", "running", "verifying", "done", "failed"), "invalid API state")
        refs = o.get("source_artifacts")
        require(isinstance(refs, list) and refs and all(isinstance(r, str) and r in artifacts for r in refs), "missing observation artifacts")
        if real:
            require(all(artifacts[r].get("synthetic") is False and artifacts[r].get("reconstructed") is False for r in refs), "synthetic source refused")
            require(any(artifacts[r].get("kind") in ("producer", "oracle") for r in refs), "missing producer or oracle evidence")
        observations[o["observation_id"]] = record
        states.append(record["state"])
    visible = set()
    for s in d["screenshots"]:
        require(isinstance(s, dict) and text(s.get("filename")) and digest(s.get("sha256")), "invalid screenshot")
        oid = s.get("observation_id")
        require(isinstance(oid, str) and oid in observations, "screenshot observation mismatch")
        require(s.get("run_id") == d["run_id"] and s.get("tenant_binding") == d["tenant_binding"], "screenshot binding mismatch")
        require(s.get("card_visible") is True and s.get("card_state") == observations[oid]["state"], "missing visible card evidence")
        visible.add(oid)
    require(visible == set(observations), "every observation needs visible card evidence")
    if d["evidence_tier"] == "live-run":
        require(any(s in ("queued", "running", "verifying") and any(t in ("done", "failed") for t in states[i + 1:])
                    for i, s in enumerate(states)), "live-run requires nonterminal followed by terminal")
    return d


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    try:
        parser = Parser(add_help=False)
        parser.add_argument("--check", required=True)
        parser.add_argument("--require-tier", required=True, choices=TIERS)
        args = parser.parse_args(argv)
        document = validate_evidence(load_evidence(args.check))
        require(document["evidence_tier"] == args.require_tier, "evidence tier does not match required tier")
        verdict = {"ok": True, "evidence_tier": document["evidence_tier"]}
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError) as exc:
        verdict = {"ok": False, "error": str(exc)}
    print(json.dumps(verdict))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
