"""Byte validation and receipt-backed project-file delivery for campaign releases."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import PurePosixPath

MAX_BYTES = 1048576
MEDIA = {'.dxf': 'image/vnd.dxf', '.json': 'application/json',
         '.csv': 'text/csv', '.txt': 'text/plain', '.md': 'text/plain'}


class DeliveryConflict(ValueError):
    pass


def safe_path(path):
    if (not isinstance(path, str) or not 1 <= len(path) <= 512
            or any(ord(c) < 32 for c in path) or any(c in path for c in '\\:%?#')
            or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/'))):
        raise ValueError('Artifact reference must be a safe relative project path')
    return path


def validate_bytes(path, raw):
    safe_path(path)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise DeliveryConflict('Artifact must contain 1 to 1048576 bytes')
    text = raw.decode('utf-8', errors='strict')
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in MEDIA:
        raise DeliveryConflict('Artifact format has no validator')
    if suffix == '.json':
        def invalid_constant(value):
            raise ValueError('Nonfinite JSON number')
        parsed = json.loads(text, parse_constant=invalid_constant)
        if not isinstance(parsed, (dict, list)) or not parsed:
            raise DeliveryConflict('JSON must be a nonempty object or list')
    elif suffix == '.csv':
        rows = list(csv.reader(io.StringIO(text), strict=True))
        if (len(rows) < 2 or not rows[0] or not all(x.strip() for x in rows[0])
                or any(len(row) != len(rows[0]) for row in rows[1:])
                or not any(any(x.strip() for x in row) for row in rows[1:])):
            raise DeliveryConflict('CSV requires a header and coherent data rows')
    elif suffix == '.dxf':
        from dxf_intake import parse_dxf_bytes
        text.encode('ascii')
        lines = text.splitlines()
        if len(lines) % 2:
            raise DeliveryConflict('Incomplete DXF group pair')
        pairs = [(int(lines[i].strip()), lines[i + 1].strip()) for i in range(0, len(lines), 2)]
        section = None
        entities = False
        for i, (code, value) in enumerate(pairs):
            if code == 0 and value == 'SECTION':
                if section is not None or i + 1 >= len(pairs) or pairs[i + 1][0] != 2:
                    raise DeliveryConflict('Invalid DXF section')
                section = pairs[i + 1][1]
                entities = entities or section == 'ENTITIES'
            elif code == 0 and value == 'ENDSEC':
                if section is None:
                    raise DeliveryConflict('Invalid DXF section end')
                section = None
            elif code == 0 and value == 'EOF' and (section is not None or i != len(pairs) - 1):
                raise DeliveryConflict('Invalid DXF EOF')
            if 10 <= code <= 59 or 110 <= code <= 149 or 210 <= code <= 239:
                if not math.isfinite(float(value)):
                    raise DeliveryConflict('Nonfinite DXF coordinate')
            if section == 'ENTITIES' and code == 0 and value in ('LINE', 'LWPOLYLINE', 'CIRCLE', 'ARC'):
                end = next((j for j in range(i + 1, len(pairs)) if pairs[j][0] == 0), len(pairs))
                fields = [c for c, _ in pairs[i + 1:end]]
                required = (10, 20, 11, 21) if value == 'LINE' else (10, 20, 40) if value in ('CIRCLE', 'ARC') else (10, 20)
                if any(c not in fields for c in required) or (value == 'LWPOLYLINE' and fields.count(10) != fields.count(20)):
                    raise DeliveryConflict('Incomplete DXF entity coordinates')
        if not entities or section is not None or not pairs or pairs[-1] != (0, 'EOF'):
            raise DeliveryConflict('Incomplete DXF structure')
        parsed = parse_dxf_bytes(raw, source_name=PurePosixPath(path).name)
        if not any(parsed.get(k) for k in ('polylines', 'circles', 'arcs', 'texts')):
            raise DeliveryConflict('DXF contains no usable entity')
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                raise DeliveryConflict('Nonfinite geometry')
            if isinstance(value, dict):
                for item in value.values():
                    finite(item)
            elif isinstance(value, list):
                for item in value:
                    finite(item)
        finite(parsed)
    elif not text.strip() or '\x00' in text:
        raise DeliveryConflict('Text must be nonempty UTF-8')
    return {'path': path, 'name': PurePosixPath(path).name, 'media_type': MEDIA[suffix],
            'format': suffix[1:], 'sha256': hashlib.sha256(raw).hexdigest(),
            'size_bytes': len(raw), 'content_valid': True, 'bytes_verified': True}


def file_bytes(snapshot, path):
    files = [row for row in snapshot.get('files', []) if row.get('path') == path]
    if len(files) != 1 or not isinstance(files[0].get('content'), str):
        raise DeliveryConflict('Saved artifact is unavailable')
    return files[0]['content'].encode('utf-8')


def select_artifact(snapshot, refs):
    candidates = refs or sorted(row['path'] for row in snapshot.get('files', [])
                                if not row.get('path', '').startswith('releases/'))
    for path in candidates:
        try:
            raw = file_bytes(snapshot, safe_path(path))
            return validate_bytes(path, raw)
        except (ValueError, UnicodeError, csv.Error):
            continue
    return None


def input_digest(path, media_type, source_revision):
    raw = json.dumps({'path': path, 'media_type': media_type,
                      'content_sha256': source_revision}, sort_keys=True,
                     separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def receipt_for(snapshot, path, media_type, source_revision):
    digest = input_digest(path, media_type, source_revision)
    return next((r for r in snapshot.get('receipts', [])
                 if r.get('action') == 'file_put' and r.get('input_digest') == digest), None)


def read_verified(snapshot, artifact):
    raw = file_bytes(snapshot, artifact['path'])
    observed = validate_bytes(artifact['path'], raw)
    if any(observed[k] != artifact[k] for k in ('sha256', 'size_bytes', 'media_type')):
        raise DeliveryConflict('Saved artifact version changed')
    if not receipt_for(snapshot, artifact['path'], artifact['media_type'], artifact['sha256']):
        raise DeliveryConflict('Matching lifecycle publication receipt is unavailable')
    return raw, dict(observed, retrieved=True, observed_revision=observed['sha256'])
