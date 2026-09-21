"""Resolve stored solar graphs for the drawing version rail."""
import json

import write_loop
import store
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from tenant_id_validator import validate_tenant_id


def resolve_graph_context(backend, tenant_id, drawing_id, version="head", *, project_id=None):
    try:
        validate_tenant_id(tenant_id)
        validate_tenant_id(drawing_id, kind="drawing id")
    except GraphValidationError:
        raise
    except ValueError:
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    if not (version == "head" or (type(version) is int and version >= 1)):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE")
    if project_id is not None and (type(project_id) is not str or not 1 <= len(project_id) <= 100):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE")
    try:
        resolved, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, version)
        manifest = store.load_manifest(
            backend, store.sanitize_id(tenant_id), store.sanitize_id(drawing_id))
        head = manifest["head"]
        note = entry.get("note") or ""
        if type(head) is not int or head < 1 or type(note) is not str:
            raise ValueError("invalid manifest")
    except GraphValidationError:
        raise
    except (KeyError, ValueError, TypeError, OSError):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    if note.startswith("solar-bundle:"):
        try:
            graph = store.read_graph_bundle(
                backend, tenant_id, drawing_id, resolved, project_id=project_id)["graph"]
        except GraphValidationError:
            raise
        except ValueError as exc:
            if project_id is not None and str(exc) == "graph bundle scope mismatch":
                # Verify the stored scope independently before classifying a caller mismatch.
                try:
                    bundle = store.read_graph_bundle(backend, tenant_id, drawing_id, resolved)
                except GraphValidationError:
                    raise
                except (KeyError, ValueError, TypeError, OSError):
                    raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
                if bundle["graph"]["project"]["id"] != project_id:
                    raise GraphValidationError("PROJECT_MISMATCH") from None
            raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
        except (KeyError, TypeError, OSError):
            raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
        graph = validate_graph(graph)
        representation = "dwg-bundle"
    else:
        try:
            data = backend.get(key)
        except GraphValidationError:
            raise
        except (KeyError, ValueError, TypeError, OSError):
            raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
        try:
            intake = json.loads(data.decode("utf-8"))
        except GraphValidationError:
            raise
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise GraphValidationError("GRAPH_NOT_EMBEDDED") from None
        if type(intake) is not dict or type(intake.get("solar_design_graph")) is not dict:
            raise GraphValidationError("GRAPH_NOT_EMBEDDED")
        graph = validate_graph(intake["solar_design_graph"])
        stored_digest = intake.get("solar_design_graph_sha256")
        if type(stored_digest) is not str or stored_digest != digest(graph):
            raise GraphValidationError("GRAPH_DIGEST_MISMATCH")
        representation = "intake"
    if project_id is not None and project_id != graph["project"]["id"]:
        raise GraphValidationError("PROJECT_MISMATCH")
    reason = ("licensed_graph_commit_required" if representation == "dwg-bundle"
              else "not_current_head" if resolved != head else None)
    return {"resolved_version": resolved, "current_head": head,
            "representation": representation, "graph": graph,
            "graph_sha256": digest(graph), "project_id": graph["project"]["id"],
            "local_commit_ready": reason is None, "refusal_reason": reason}
