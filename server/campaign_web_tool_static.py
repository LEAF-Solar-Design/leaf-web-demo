"""Trusted-template JSON-records-to-CSV browser tool: static recipe half.

Server-owned, fixed document. User JSON is always DATA embedded into a script
tag as escaped JSON text, never interpolated into executable JavaScript and
never assigned through innerHTML/eval. The paired CSV encoder below is
duplicated once in JavaScript inside the trusted template
(``_TRUSTED_SCRIPT``) so a browser click produces byte-identical output to
``expected_output`` here; keep the two encodings in lockstep if either
changes.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath

import campaign_delivery_service as delivery

RECIPE_ID = 'json-records-to-csv'
RECIPE_VERSION = 1

MAX_RECORDS = 1000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 2000
MAX_OUTPUT_BYTES = 1048576

# Opaque-origin sandbox delivered as a response header (not a <meta> tag) on the
# synthetic document response. `sandbox` with no `allow-same-origin` forces an
# opaque `window.origin`; `allow-scripts allow-downloads` are the only two
# capabilities the fixed template needs. No network surface: connect-src none,
# default-src none. img-src data: is unused by this recipe today but kept so a
# future inline preview does not need a CSP change.
CSP = ("sandbox allow-scripts allow-downloads; default-src 'none'; "
       "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'none'; img-src data:; frame-ancestors 'none'; "
       "form-action 'none'; base-uri 'none'")

_FORMULA_LEAD = ('=', '+', '-', '@', '\t', '\r')
_NEUTRALIZE_NOTE = (
    'Text values or column names that begin with =, +, -, @, a tab, or a '
    'carriage return are downloaded with a leading apostrophe so a '
    'spreadsheet never reads them as a formula.'
)


def _reject_nonfinite(_token):
    raise delivery.DeliveryConflict('Nonfinite JSON number')


def _parse_records(source_bytes):
    """Bytes -> validated list[dict]. Bounded, flat, finite-scalar only."""
    if not isinstance(source_bytes, bytes) or not 0 < len(source_bytes) <= delivery.MAX_BYTES:
        raise delivery.DeliveryConflict('Source JSON must contain 1 to 1048576 bytes')
    text = source_bytes.decode('utf-8', errors='strict')
    records = json.loads(text, parse_constant=_reject_nonfinite)
    if not isinstance(records, list) or not records:
        raise delivery.DeliveryConflict('Source JSON must be a nonempty list of objects')
    if len(records) > MAX_RECORDS:
        raise delivery.DeliveryConflict(f'Source JSON must hold at most {MAX_RECORDS} records')
    for record in records:
        if not isinstance(record, dict) or not record:
            raise delivery.DeliveryConflict('Every record must be a nonempty JSON object')
        for key, value in record.items():
            if not isinstance(key, str) or not key or len(key) > MAX_CELL_CHARS:
                raise delivery.DeliveryConflict('Field names must be nonempty bounded strings')
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, float) and not math.isfinite(value):
                raise delivery.DeliveryConflict('Nonfinite JSON number')
            if isinstance(value, (int, float)):
                continue
            if isinstance(value, str):
                if len(value) > MAX_CELL_CHARS:
                    raise delivery.DeliveryConflict(f'Cell value exceeds {MAX_CELL_CHARS} characters')
                continue
            raise delivery.DeliveryConflict('Nested JSON structures are not supported')
    headers = _header_union(records)
    if len(headers) > MAX_COLUMNS:
        raise delivery.DeliveryConflict(f'Source JSON must expose at most {MAX_COLUMNS} field names')
    return records


def _header_union(records):
    headers = []
    seen = set()
    for record in records:
        for key in record.keys():
            if key not in seen:
                seen.add(key)
                headers.append(key)
    return headers


def _neutralize(text):
    if text and text[0] in _FORMULA_LEAD:
        return "'" + text
    return text


def _format_cell(value):
    """Scalar -> CSV field text. Numeric rule matches JS `String(number)`:
    an integral float loses its trailing `.0` because JS has one numeric type.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else repr(value)
    if isinstance(value, int):
        return str(value)
    return _neutralize(value)


def _quote(field):
    return '"' + field.replace('"', '""') + '"'


def _encode_csv(records):
    headers = _header_union(records)
    lines = [','.join(_quote(_neutralize(h)) for h in headers)]
    for record in records:
        lines.append(','.join(_quote(_format_cell(record.get(h))) for h in headers))
    body = ('\r\n'.join(lines) + '\r\n').encode('utf-8')
    if len(body) > MAX_OUTPUT_BYTES:
        raise delivery.DeliveryConflict(f'Rendered CSV exceeds {MAX_OUTPUT_BYTES} bytes')
    return body


def expected_output(source_bytes):
    return _encode_csv(_parse_records(source_bytes))


def select_source(snapshot, artifact_refs):
    """Exactly one .json ref from the lifecycle snapshot's own files, no I/O."""
    if not isinstance(artifact_refs, (list, tuple)) or len(artifact_refs) != 1:
        raise delivery.DeliveryConflict('Recipe requires exactly one JSON artifact reference')
    path = delivery.safe_path(artifact_refs[0])
    if PurePosixPath(path).suffix.lower() != '.json':
        raise delivery.DeliveryConflict('Recipe accepts only a .json source reference')
    raw = delivery.file_bytes(snapshot, path)
    metadata = delivery.validate_bytes(path, raw)
    _parse_records(raw)
    return dict(metadata, recipe_id=RECIPE_ID, recipe_version=RECIPE_VERSION)


def _escape_for_script_tag(json_text):
    """Remove every literal '<' (and '&', U+2028/2029) so no substring of the
    embedded text can read as `</script` to the HTML parser, which closes a
    <script> tag on that literal sequence regardless of the tag's `type` or
    of any JS/JSON string-literal quoting inside it.
    """
    return (json_text.replace('&', '\\u0026').replace('<', '\\u003c')
                      .replace('>', '\\u003e').replace('\u2028', '\\u2028')
                      .replace('\u2029', '\\u2029'))


_TRUSTED_SCRIPT = """
(function () {
  'use strict';
  function neutralize(text) {
    if (text.length && '=+-@\\t\\r'.indexOf(text[0]) !== -1) { return "'" + text; }
    return text;
  }
  function formatCell(value) {
    if (value === null || value === undefined) { return ''; }
    if (typeof value === 'boolean') { return value ? 'true' : 'false'; }
    if (typeof value === 'number') { return String(value); }
    return neutralize(String(value));
  }
  function quote(field) { return '"' + field.replace(/"/g, '""') + '"'; }
  function headerUnion(records) {
    var headers = []; var seen = Object.create(null);
    for (var i = 0; i < records.length; i += 1) {
      var keys = Object.keys(records[i]);
      for (var j = 0; j < keys.length; j += 1) {
        if (!seen[keys[j]]) { seen[keys[j]] = true; headers.push(keys[j]); }
      }
    }
    return headers;
  }
  function encodeCsv(records) {
    var headers = headerUnion(records);
    var lines = [headers.map(function (h) { return quote(neutralize(h)); }).join(',')];
    for (var i = 0; i < records.length; i += 1) {
      var record = records[i];
      var row = headers.map(function (h) {
        var value = Object.prototype.hasOwnProperty.call(record, h) ? record[h] : null;
        return quote(formatCell(value));
      });
      lines.push(row.join(','));
    }
    return lines.join('\\r\\n') + '\\r\\n';
  }
  function fail(message) {
    preview.textContent = message;
    downloadBtn.disabled = true;
    lastCsv = null;
  }
  var dataEl = document.getElementById('leaf-records-data');
  var initialRecords = JSON.parse(dataEl.textContent);
  var textarea = document.getElementById('records-input');
  textarea.value = JSON.stringify(initialRecords, null, 2);
  var preview = document.getElementById('preview');
  var convertBtn = document.getElementById('convert-btn');
  var downloadBtn = document.getElementById('download-btn');
  var lastCsv = null;
  convertBtn.addEventListener('click', function () {
    var records;
    try { records = JSON.parse(textarea.value); }
    catch (error) { fail('Invalid JSON: ' + error.message); return; }
    if (!Array.isArray(records) || records.length === 0) {
      fail('Input must be a nonempty JSON array of objects.');
      return;
    }
    var headers = headerUnion(records);
    lastCsv = encodeCsv(records);
    preview.textContent = 'Rows: ' + records.length + ' | Columns: ' + headers.length;
    downloadBtn.disabled = false;
  });
  downloadBtn.addEventListener('click', function () {
    if (lastCsv === null) { return; }
    var blob = new Blob([lastCsv], { type: 'text/csv' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'records.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  });
}());
"""

_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Convert JSON records to CSV</title>
</head>
<body>
<h1>Convert JSON records to CSV</h1>
<p>Paste or edit a JSON array of flat objects below, then Convert to preview it
and Download CSV to save it. """ + _NEUTRALIZE_NOTE + """</p>
<script type="application/json" id="leaf-records-data">__RECORDS_JSON__</script>
<textarea id="records-input" rows="16" cols="80"></textarea>
<div>
<button id="convert-btn" type="button">Convert</button>
<button id="download-btn" type="button" disabled>Download CSV</button>
</div>
<div id="preview"></div>
<script>""" + _TRUSTED_SCRIPT + """</script>
</body>
</html>
"""


def render(source_bytes):
    records = _parse_records(source_bytes)
    embedded = _escape_for_script_tag(json.dumps(records, ensure_ascii=False))
    return _TEMPLATE.replace('__RECORDS_JSON__', embedded, 1).encode('utf-8')


def validate_generated(html_bytes, source_bytes):
    """Byte-exact match to `render(source_bytes)`; no partial or fuzzy check.

    Anything else -- a single injected byte, a reordered attribute, a stray
    comment -- means the producer must refuse to execute it.
    """
    expected_html = render(source_bytes)
    if not isinstance(html_bytes, bytes) or html_bytes != expected_html:
        raise delivery.DeliveryConflict('Generated document does not match the trusted template exactly')
    return {'path': 'index.html', 'name': 'index.html', 'media_type': 'text/html',
            'format': 'html', 'sha256': hashlib.sha256(html_bytes).hexdigest(),
            'size_bytes': len(html_bytes), 'content_valid': True, 'bytes_verified': True,
            'recipe_id': RECIPE_ID, 'recipe_version': RECIPE_VERSION}
