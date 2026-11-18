"""Bounded Studio product-route client; all I/O belongs to the transport."""

from contextlib import contextmanager
import math
import time
from typing import Callable
from urllib.parse import quote
from uuid import uuid4


Transport = Callable[..., tuple[int, object]]


class StudioError(RuntimeError):
    def __init__(self, status, error_code=None, message="", *, reason_code=None,
                 next_action=None, body=None):
        self.status = status
        self.reason_code = reason_code
        self.error_code = error_code
        self.message = message
        self.next_action = next_action
        self.body = body
        super().__init__(str(self))

    def __str__(self):
        codes = " ".join(str(code) for code in (self.error_code, self.reason_code)
                         if code is not None)
        return f"{self.status} {codes}: {self.message}"


def parse_error(status, body):
    envelope = body.get("error") if isinstance(body, dict) else None
    if isinstance(envelope, dict):
        return StudioError(status, envelope.get("error_code"),
                           str(envelope.get("message", "")),
                           reason_code=envelope.get("reason_code"),
                           next_action=envelope.get("next_action"), body=body)
    if isinstance(body, dict) and "detail" in body:
        return StudioError(status, "AUTH_DENIED", str(body["detail"]), body=body)
    return StudioError(status, message="Studio request failed", body=body)


def new_idempotency_key(prefix="xbench"):
    return f"{prefix}-{uuid4().hex}"


def _redact(body, secrets=()):
    """Do not let an echoed checkout capability enter error diagnostics."""
    if isinstance(body, dict):
        secrets = (*secrets, *(value for key, value in body.items()
                              if key.lower() in ("checkout_capability", "x-checkout-capability")
                              and isinstance(value, str) and value))
        return {key: ("[redacted]" if key.lower() in
                      ("checkout_capability", "x-checkout-capability")
                      else _redact(value, secrets)) for key, value in body.items()}
    if isinstance(body, list):
        return [_redact(value, secrets) for value in body]
    if isinstance(body, str):
        for secret in secrets:
            body = body.replace(secret, "[redacted]")
    return body


def _id(value):
    return quote(str(value), safe="")


class StudioClient:
    def __init__(self, transport: Transport, *, clock=time.monotonic, sleep=time.sleep):
        self._transport = transport
        self._clock = clock
        self._sleep = sleep

    def _request(self, method, path, *, json_body=None, data=None, headers=None,
                 timeout_s=60.0):
        status, body = self._transport(method, path, json_body=json_body, data=data,
                                       headers=headers, timeout_s=timeout_s)
        if not 200 <= status < 300:
            capability = (headers or {}).get("X-Checkout-Capability")
            raise parse_error(status, _redact(body, (capability,) if capability else ()))
        return status, body

    def create_project(self, name):
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError("name must contain 1 to 200 characters")
        _, body = self._request("POST", "/api/projects", json_body={"name": name})
        if body.get("created") is not True:
            raise StudioError(409, "PROJECT_NAME_REUSED", "Project name already exists")
        return body["project"]

    def project(self, project_id):
        # platform/api.py exposes the Studio project listing under /api.
        _, body = self._request("GET", "/api/projects")
        projects = body.get("projects") if isinstance(body, dict) else None
        if not isinstance(projects, list):
            raise StudioError(502, "INVALID_PROJECT_RESPONSE", "Expected a project listing")
        for project in projects:
            if isinstance(project, dict) and project.get("project_id") == project_id:
                if not project.get("org_id"):
                    raise StudioError(502, "INVALID_PROJECT_RESPONSE", "Project organization missing")
                return project
        raise StudioError(404, "PROJECT_NOT_FOUND", "Project not found in Studio listing")

    def delete_project(self, project_id):
        return self._request("DELETE", f"/api/projects/{_id(project_id)}",
                             headers={"Idempotency-Key": new_idempotency_key()})[1]

    def upload_drawing(self, filename, content: bytes, *, engine=None):
        if len(content) > 25 * 1024 * 1024:
            raise ValueError("drawing exceeds 25 MiB")
        if engine is not None and engine not in ("local", "aps"):
            raise ValueError("engine must be local or aps")
        if any(char in filename for char in ('\r', '\n', '"')):
            raise ValueError("invalid multipart filename")
        boundary = "----xbench" + uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode() + content + b"\r\n"
        if engine is not None:
            data += (f'--{boundary}\r\nContent-Disposition: form-data; name="engine"'
                     f'\r\n\r\n{engine}\r\n').encode()
        data += f"--{boundary}--\r\n".encode()
        status, body = self._request("POST", "/api/drawings/upload", data=data,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if status != 202:
            raise StudioError(status, "INVALID_UPLOAD_RESPONSE", "Expected upload acceptance")
        return body["drawing_id"]

    def _poll(self, path, timeout_s, poll_s, *, upload=False):
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and nonnegative")
        if not math.isfinite(poll_s) or poll_s <= 0:
            raise ValueError("poll_s must be finite and positive")
        deadline = self._clock() + timeout_s
        code = "UPLOAD_TIMEOUT" if upload else "JOB_TIMEOUT"
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise StudioError(504, code, "Polling deadline reached")
            _, body = self._request("GET", path, timeout_s=min(60.0, remaining))
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise StudioError(504, code, "Polling deadline reached")
            if body.get("status") == "failed":
                if upload:
                    safe_body = _redact(body)
                    error = parse_error(422, safe_body)
                    message = (error.message if isinstance(safe_body.get("error"), dict)
                               else str(safe_body.get("error", "Upload failed")))
                    raise StudioError(422, "UPLOAD_FAILED", message,
                                      reason_code=error.reason_code,
                                      next_action=error.next_action, body=safe_body)
                return body
            if body.get("status") == ("ready" if upload else "complete"):
                return body
            self._sleep(min(poll_s, remaining))

    def wait_upload(self, drawing_id, *, timeout_s=900, poll_s=3):
        return self._poll(f"/api/drawings/{_id(drawing_id)}/upload-status",
                          timeout_s, poll_s, upload=True)

    def adopt_drawing(self, project_id, source_drawing_id, name, *, version=1):
        return self._request(
            "POST", f"/api/projects/{_id(project_id)}/drawing-versions/import",
            headers={"Idempotency-Key": new_idempotency_key()},
            json_body={"name": name, "source": {"kind": "account_upload",
                       "drawing_id": source_drawing_id, "version": version}})[1]["drawing_version"]

    def drawing_context(self, project_id, version_id, drawing_id):
        return self._request("GET", f"/api/projects/{_id(project_id)}/drawing-versions/"
                             f"{_id(version_id)}/context?drawing_id={_id(drawing_id)}")[1]

    def checkout(self, project_id, drawing_id, *, ttl_s=3600, holder="xbench grader"):
        if not 0 < ttl_s <= 86400:
            raise ValueError("ttl_s must be greater than zero and at most 86400")
        _, body = self._request("POST", f"/api/projects/{_id(project_id)}/drawings/"
                                f"{_id(drawing_id)}/checkout",
                                json_body={"holder": holder, "ttl_s": ttl_s})
        return body["checkout_capability"], body["checkout"]

    def release(self, project_id, drawing_id, capability):
        return self._request("DELETE", f"/api/projects/{_id(project_id)}/drawings/"
                             f"{_id(drawing_id)}/checkout",
                             headers={"X-Checkout-Capability": capability})[1]

    def tools(self):
        return self._request("GET", "/api/tools")[1]["tools"]

    def catalog_digest(self, tool_name):
        for tool in self.tools():
            if tool.get("name") == tool_name:
                return tool["catalog_digest"]
        raise StudioError(404, "TOOL_NOT_IN_CATALOG", "Tool is absent from catalog")

    def run(self, tool, *, org_id, project_id, version_id, params, capability=None,
            idempotency_key=None, persist=False, wait=False):
        headers = {"X-Org-Id": org_id, "X-Project-Id": project_id}
        if capability is not None:
            headers["X-Checkout-Capability"] = capability
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        flags = [flag for flag, enabled in (("persist=1", persist), ("wait=1", wait)) if enabled]
        path = "/api/run" + ("?" + "&".join(flags) if flags else "")
        return self._request("POST", path, headers=headers, json_body={
            "tool": tool, "catalog_digest": self.catalog_digest(tool),
            "dwg": version_id, "params": params})

    def wait_job(self, job_id, *, timeout_s=900, poll_s=2):
        return self._poll(f"/api/jobs/{_id(job_id)}", timeout_s, poll_s)

    def dxf_bytes(self, project_id, drawing_id, version_id):
        status, body = self._request("GET", f"/api/projects/{_id(project_id)}/drawings/"
                                     f"{_id(drawing_id)}/versions/{_id(version_id)}/dxf")
        if not isinstance(body, bytes):
            raise StudioError(status, "INVALID_DXF_RESPONSE", "Expected DXF bytes")
        return body


@contextmanager
def disposable_project(client, name):
    project = client.create_project(name)
    try:
        yield project
    except BaseException as exc:
        try:
            client.delete_project(project["project_id"])
        except Exception as cleanup_error:
            exc.add_note(f"Disposable project cleanup failed: {cleanup_error}")
        raise
    else:
        client.delete_project(project["project_id"])
