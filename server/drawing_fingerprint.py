"""Small, fail-closed inventory of the model-space objects in a DXF."""
import hashlib


SCHEMA = "leaf.drawing-fingerprint.v1"
LAYER_ROWS_CAP = 64
LAYER_NAME_CHARS = 255


class _InvalidDXF(Exception):
    def __init__(self, reason):
        self.reason = reason


def _lines(text):
    """Yield LF-delimited lines without materializing a second input copy."""
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        if end < 0:
            end = len(text)
        line = text[start:end]
        yield line[:-1] if line.endswith("\r") else line
        start = end + 1


def _pairs(text):
    lines = iter(_lines(text))
    for code in lines:
        if not code.strip():
            if any(line.strip() for line in lines):
                raise _InvalidDXF("malformed")
            return
        try:
            number = int(code.strip())
        except ValueError:
            raise _InvalidDXF("malformed") from None
        try:
            value = next(lines)
        except StopIteration:
            raise _InvalidDXF("truncated") from None
        yield number, value


def _inventory(text):
    counts = {}
    section = None
    pending_section = False
    entities_seen = False
    record = None
    layer = None
    space = None
    subclasses = 0
    header = True

    def finish_record():
        if record is not None and record not in {"VERTEX", "ATTRIB", "SEQEND"}:
            if space != 1:
                name = "0" if layer is None else layer
                counts[name] = counts.get(name, 0) + 1

    for code, value in _pairs(text):
        if code == 999:
            continue
        token = value.strip().upper() if code in {0, 2} else None
        if pending_section:
            if code != 2:
                raise _InvalidDXF("malformed")
            section = token
            pending_section = False
            if section == "ENTITIES":
                if entities_seen:
                    raise _InvalidDXF("malformed")
                entities_seen = True
            continue
        if code == 0 and token == "SECTION":
            if section is not None:
                raise _InvalidDXF("malformed")
            pending_section = True
            continue
        if code == 0 and token == "ENDSEC":
            if section == "ENTITIES":
                finish_record()
                record = None
            section = None
            continue
        if section != "ENTITIES":
            continue
        if code == 0:
            if token == "EOF":
                raise _InvalidDXF("truncated")
            finish_record()
            record, layer, space = token, None, None
            subclasses, header = 0, True
        elif record is not None and record not in {"VERTEX", "ATTRIB", "SEQEND"}:
            if code == 100:
                subclasses += 1
                if subclasses >= 2:
                    header = False
            if code >= 1000:
                header = False
            if header and code == 8 and layer is None:
                layer = value.strip(" ")
                if len(layer) > LAYER_NAME_CHARS:
                    raise _InvalidDXF("malformed")
            elif header and code == 67 and space is None:
                try:
                    space = int(value.strip())
                except ValueError:
                    # An invalid space value is not the integer 1; retain the
                    # fact that the first group has already been consumed.
                    space = "invalid"
    if section == "ENTITIES":
        raise _InvalidDXF("truncated")
    if pending_section or not entities_seen:
        raise _InvalidDXF("malformed")
    return counts


def fingerprint_dxf(raw: bytes) -> dict:
    """Return the v1 fingerprint for any bytes, including invalid drawings."""
    result = {
        "schema": SCHEMA,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "status": "unavailable",
        "reason": None,
        "model_objects": None,
        "layer_count": None,
        "layers_digest": None,
        "layers": [],
        "layers_omitted": 0,
    }
    if raw.startswith(b"AutoCAD Binary DXF"):
        result["reason"] = "binary_dxf"
        return result
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        result["reason"] = "encoding"
        return result
    try:
        counts = _inventory(text)
        digest = hashlib.sha256((SCHEMA + "\n").encode("utf-8"))
        for name in sorted(counts, key=lambda name: name.encode("utf-8")):
            digest.update(f"{counts[name]}\t{name}\n".encode("utf-8"))
        rows = sorted(counts, key=lambda name: (-counts[name], name.encode("utf-8")))
        result.update(
            status="ok", model_objects=sum(counts.values()),
            layer_count=len(counts), layers_digest=digest.hexdigest(),
            layers=[{"name": name, "count": counts[name]} for name in rows[:LAYER_ROWS_CAP]],
            layers_omitted=max(0, len(counts) - LAYER_ROWS_CAP),
        )
    except _InvalidDXF as exc:
        result["reason"] = exc.reason
    except Exception:
        result["reason"] = "malformed"
    return result
