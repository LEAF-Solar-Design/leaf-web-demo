"""Admit native frame pose edits without rewriting the submitted DXF bytes."""
from copy import deepcopy
import json
import math

import solar_native_layout_dxf as native


class NativeEditError(ValueError):
    pass


def _same(left, right):
    return native._json(left) == native._json(right)


def _rigid_pose(before, after):
    if len(before) != 4 or len(after) != 4:
        return False
    for i in range(4):
        for j in range(i):
            if not math.isclose(math.dist(before[i], before[j]),
                                math.dist(after[i], after[j]), rel_tol=1e-9, abs_tol=1e-7):
                return False

    def area(points):
        # Translate first to avoid cancellation at a large drawing origin.
        x0, y0 = points[0]
        shifted = [(x - x0, y - y0) for x, y in points]
        return sum(x * shifted[(i + 1) % 4][1] - y * shifted[(i + 1) % 4][0]
                   for i, (x, y) in enumerate(shifted))

    return area(before) * area(after) > 0


def reconcile_native_intake(context, raw, intake, *, metadata_key,
                            trusted_document, trusted_parameters):
    """The caller supplies parent state from the verified canonical reader.

    Scope, generation inputs, provenance, units and non-frame state stay bound
    to that parent. Only rigid frame pose changes are admitted. A stale worker
    export still fails the existing native decoder; this does not repair it.
    """
    try:
        decoded = native.decode_native_layout(raw)
        document = decoded.document
        if decoded.scope != native._scope(context):
            raise NativeEditError("scope changed")
        if not _same(decoded.parameters, trusted_parameters):
            raise NativeEditError("generation or provenance changed")
        if not _same({k: v for k, v in document.items() if k != "state"},
                     {k: v for k, v in trusted_document.items() if k != "state"}):
            raise NativeEditError("document attributes changed")
        state, before = document["state"], trusted_document["state"]
        if not _same({k: v for k, v in state.items() if k != "frames"},
                     {k: v for k, v in before.items() if k != "frames"}):
            raise NativeEditError("non-frame state changed")
        frames, previous = state["frames"], before["frames"]
        if len(frames) != len(previous):
            raise NativeEditError("frame membership changed")
        for frame, old in zip(frames, previous):
            if not _same({k: v for k, v in frame.items() if k != "vertices"},
                         {k: v for k, v in old.items() if k != "vertices"}):
                raise NativeEditError("frame attributes changed")
            if not _rigid_pose(old["vertices"], frame["vertices"]):
                raise NativeEditError("frame shape changed")
        result = json.loads(intake)
        metadata = deepcopy(context.intake[metadata_key])
        metadata["document"] = native.ps.encode_document(document).decode("utf-8")
        metadata["parameters"] = deepcopy(trusted_parameters)
        result[metadata_key] = metadata
        return json.dumps(result, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (native.NativeLayoutError, KeyError, TypeError, AttributeError) as exc:
        raise NativeEditError("native frame edit cannot be verified") from exc
