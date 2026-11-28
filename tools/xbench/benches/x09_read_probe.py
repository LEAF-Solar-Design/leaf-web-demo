"""X09 normal-route layer probe, not a complete bench or nightly verdict.

The caller owns the supported login/browser context and persists the returned
private evidence. Expected names come from the pinned DXF, never API output.
Missing identity, drift and incomplete responses remain UNREADABLE.
"""

import hashlib
import io
import re


def _hex(value, size=64):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % size, value) is not None


def _names(value):
    return (type(value) is list
            and all(type(name) is str and name for name in value)
            and len(set(value)) == len(value))


def layer_probe(client, raw, fixture, tool, source_sha, health):
    """Run one pinned instant-list-layers sample in an owned scratch project.

    ``fixture`` supplies bytes/sha256 from the verified external manifest;
    ``tool`` supplies name/version/catalog_digest from its reviewed binding.
    Client is the shared StudioClient. This helper neither logs in nor grades
    other X09 steps and must not be used to claim complete golden coverage.
    """
    report = {"verdict": "UNREADABLE", "assertion": "exact-values",
              "stage": "binding", "source_sha": source_sha if _hex(source_sha, 40) else None,
              "project_cleanup": "not-created"}
    project_id = None
    try:
        if (type(raw) is not bytes or type(fixture) is not dict
                or type(fixture.get("bytes")) is not int or fixture["bytes"] != len(raw)
                or not _hex(fixture.get("sha256"))
                or hashlib.sha256(raw).hexdigest() != fixture["sha256"]
                or not _hex(source_sha, 40) or type(tool) is not dict
                or tool.get("name") != "instant-list-layers"
                or type(tool.get("version")) is not str
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", tool["version"]) is None
                or type(tool.get("catalog_digest")) is not str
                or not tool["catalog_digest"].startswith("sha256:")
                or not _hex(tool["catalog_digest"][7:])):
            return report
        identity = {key: tool[key] for key in ("name", "version", "catalog_digest")}
        report.update(tool=identity, sample_sha256=fixture["sha256"])
        if health() != source_sha:
            return report
        report["stage"] = "sample-oracle"
        import ezdxf
        # Match readfile's universal-newline behavior, including Windows DXFs.
        doc = ezdxf.read(io.StringIO(raw.decode("utf-8-sig"), newline=None))
        if doc.audit().has_errors:
            return report
        expected = sorted(layer.dxf.name for layer in doc.layers)
        if not _names(expected) or not expected:
            return report
        report["expected"] = expected

        def catalog_matches():
            rows = client.tools()
            if type(rows) is not list:
                return False
            matches = [row for row in rows if type(row) is dict
                       and row.get("name") == identity["name"]]
            return len(matches) == 1 and all(matches[0].get(k) == v for k, v in identity.items())

        report["stage"] = "catalog"
        if not catalog_matches():
            return report
        report["stage"] = "project"
        project = client.create_project("X09 layer probe")
        project_id = project["project_id"]
        report["project_cleanup"] = "pending"
        report["stage"] = "upload"
        drawing = client.upload_drawing("x09-anonymous.dxf", raw)
        client.wait_upload(drawing, timeout_s=180, poll_s=3)
        report["stage"] = "adopt"
        version = client.adopt_drawing(project_id, drawing, "X09 anonymous sample")
        report["stage"] = "context"
        context = client.drawing_context(project_id, version["version_id"], version["drawing_id"])
        intake_hash = context["context"]["intake_sha256"]
        if not _hex(intake_hash):
            return report
        report["intake_sha256"] = intake_hash
        report["stage"] = "run"
        status, body = client.run(identity["name"], org_id=project["org_id"],
                                  project_id=project_id, version_id=version["version_id"], params={})
        # This catalog entry is synchronous. Do not treat an unread job as a result.
        if (type(status) is not int or status != 200 or type(body) is not dict
                or body.get("tool") != identity["name"]
                or body.get("version") != identity["version"]
                or body.get("degraded_mode", False) is not False
                or body.get("error") is not None or body.get("ok", True) is not True
                or "detail" in body or "errors" in body
                or type(body.get("project_context")) is not dict
                or body["project_context"].get("intake_sha256") != intake_hash
                or type(body.get("result")) is not dict):
            return report
        actual = body["result"].get("layers")
        if not _names(actual):
            return report
        report["stage"] = "catalog-after"
        if not catalog_matches():
            return report
        report.update(stage="asserted", observed=actual,
                      verdict="PASS" if actual == expected else "FAIL")
    except Exception:
        # Never persist SDK, API, authentication or parser exception text.
        report["verdict"] = "UNREADABLE"
    finally:
        if project_id is not None:
            try:
                client.delete_project(project_id)
                report["project_cleanup"] = "delete-api-success"
            except Exception:
                report.update(project_cleanup="unconfirmed", verdict="UNREADABLE")
        try:
            after = health()
            report["source_sha_after"] = after if _hex(after, 40) else None
            if after != source_sha:
                report.update(verdict="UNREADABLE", source_stable=False)
            else:
                report["source_stable"] = True
        except Exception:
            report.update(verdict="UNREADABLE", source_stable=False)
    return report
