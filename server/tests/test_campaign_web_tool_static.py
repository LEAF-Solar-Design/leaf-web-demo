import csv
import hashlib
import io
import json

import pytest

import campaign_delivery_service as delivery
import campaign_web_tool_static as static


def _snapshot(mapping):
    return {'files': [{'path': path, 'content': content} for path, content in mapping.items()]}


# --- RFC4180 bytes ------------------------------------------------------------

def test_basic_csv_bytes_are_exact_rfc4180():
    source = json.dumps([{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]).encode()
    csv_bytes = static.expected_output(source)
    assert csv_bytes == b'"name","age"\r\n"Alice","30"\r\n"Bob","25"\r\n'


def test_sparse_records_use_first_occurrence_header_union_and_missing_is_empty():
    source = json.dumps([{'a': 1, 'b': 2}, {'b': 3, 'c': 4}]).encode()
    csv_bytes = static.expected_output(source)
    assert csv_bytes == b'"a","b","c"\r\n"1","2",""\r\n"","3","4"\r\n'


def test_unicode_newline_and_quote_values_round_trip_through_csv_module():
    source = json.dumps([{'text': 'He said "hi"\nagain', 'emoji': 'é中'}]).encode()
    csv_bytes = static.expected_output(source)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode('utf-8'))))
    assert rows[0] == ['text', 'emoji']
    assert rows[1] == ['He said "hi"\nagain', 'é中']


def test_null_false_and_zero_render_correctly():
    source = json.dumps([{'active': False, 'count': 0, 'note': None}]).encode()
    csv_bytes = static.expected_output(source)
    assert csv_bytes == b'"active","count","note"\r\n"false","0",""\r\n'


def test_integral_float_matches_javascript_number_to_string():
    source = json.dumps([{'value': 2.0}, {'value': 2.5}, {'value': -3.0}]).encode()
    csv_bytes = static.expected_output(source)
    assert csv_bytes == b'"value"\r\n"2"\r\n"2.5"\r\n"-3"\r\n'


# --- Formula neutralization ----------------------------------------------------

def test_formula_and_control_leads_are_neutralized_in_strings_and_headers():
    record = {'=cmd': '=2+2', '+plus': '+1', '-minus': '-1', '@at': '@sum',
              '\ttab': '\tvalue', '\rcr': '\rvalue', 'safe': 'ok'}
    source = json.dumps([record]).encode()
    csv_bytes = static.expected_output(source)
    headers = list(record.keys())
    expected_header = ','.join(static._quote(static._neutralize(h)) for h in headers)
    expected_row = ','.join(static._quote(static._neutralize(record[h])) for h in headers)
    assert csv_bytes == (expected_header + '\r\n' + expected_row + '\r\n').encode('utf-8')


def test_leading_dash_number_is_not_neutralized_only_strings_are():
    source = json.dumps([{'delta': -5}]).encode()
    csv_bytes = static.expected_output(source)
    assert csv_bytes == b'"delta"\r\n"-5"\r\n'


# --- Malicious HTML/script injection -------------------------------------------

def test_render_neutralizes_script_closing_sequences_and_stays_pure_json():
    source = json.dumps([{'name': '</script><script>alert(1)</script>',
                           'note': '<img onerror=x>&amp;'}]).encode()
    html = static.render(source)
    text = html.decode('utf-8')
    assert text.lower().count('</script') == 2
    start = text.index('id="leaf-records-data">') + len('id="leaf-records-data">')
    end = text.index('</script>', start)
    payload = text[start:end]
    assert '<' not in payload
    assert '>' not in payload
    parsed = json.loads(payload)
    assert parsed[0]['name'] == '</script><script>alert(1)</script>'
    assert parsed[0]['note'] == '<img onerror=x>&amp;'


def test_validate_generated_accepts_exact_render_output():
    source = json.dumps([{'a': 1}]).encode()
    html = static.render(source)
    meta = static.validate_generated(html, source)
    assert meta['content_valid'] is True
    assert meta['bytes_verified'] is True
    assert meta['sha256'] == hashlib.sha256(html).hexdigest()
    assert meta['recipe_id'] == static.RECIPE_ID


def test_validate_generated_rejects_one_byte_injection():
    source = json.dumps([{'a': 1}]).encode()
    html = static.render(source)
    tampered = html[:-1] + bytes([html[-1] ^ 1])
    with pytest.raises(delivery.DeliveryConflict):
        static.validate_generated(tampered, source)


def test_validate_generated_rejects_non_bytes():
    source = json.dumps([{'a': 1}]).encode()
    with pytest.raises(delivery.DeliveryConflict):
        static.validate_generated('not bytes', source)


# --- Invalid schemas / limits ---------------------------------------------------

@pytest.mark.parametrize('payload', [
    b'[]',
    b'{}',
    b'[{}]',
    b'[{"a": [1, 2]}]',
    b'[{"a": {"b": 1}}]',
    b'[{"a": NaN}]',
    b'[{"a": Infinity}]',
    b'[{"a": -Infinity}]',
    b'"just a string"',
    b'not json',
    b'',
])
def test_invalid_schemas_are_rejected(payload):
    with pytest.raises((delivery.DeliveryConflict, ValueError)):
        static._parse_records(payload)


def test_too_many_records_rejected():
    source = json.dumps([{'a': 1}] * (static.MAX_RECORDS + 1)).encode()
    with pytest.raises(delivery.DeliveryConflict):
        static._parse_records(source)


def test_too_many_columns_rejected():
    record = {f'c{i}': 1 for i in range(static.MAX_COLUMNS + 1)}
    source = json.dumps([record]).encode()
    with pytest.raises(delivery.DeliveryConflict):
        static._parse_records(source)


def test_huge_cell_rejected():
    source = json.dumps([{'a': 'x' * (static.MAX_CELL_CHARS + 1)}]).encode()
    with pytest.raises(delivery.DeliveryConflict):
        static._parse_records(source)


def test_huge_input_rejected():
    source = json.dumps([{'a': 'x' * 2000}] * 900).encode()
    assert len(source) > delivery.MAX_BYTES
    with pytest.raises(delivery.DeliveryConflict):
        static._parse_records(source)


# --- select_source: exact one ref, snapshot-only, no I/O -----------------------

def test_select_source_reads_the_exact_one_ref():
    payload = json.dumps([{'a': 1}])
    snapshot = _snapshot({'data.json': payload, 'other.json': json.dumps([{'b': 2}])})
    meta = static.select_source(snapshot, ['data.json'])
    assert meta['path'] == 'data.json'
    assert meta['sha256'] == hashlib.sha256(payload.encode()).hexdigest()
    assert meta['recipe_id'] == static.RECIPE_ID
    assert meta['recipe_version'] == static.RECIPE_VERSION


def test_select_source_ignores_other_snapshot_files_even_if_invalid():
    snapshot = _snapshot({'good.json': json.dumps([{'a': 1}]), 'broken.json': 'not json at all'})
    meta = static.select_source(snapshot, ['good.json'])
    assert meta['path'] == 'good.json'


@pytest.mark.parametrize('refs', [[], ['a.json', 'b.json'], ['a.json', 'a.json']])
def test_select_source_rejects_anything_other_than_exactly_one_ref(refs):
    snapshot = _snapshot({'a.json': json.dumps([{'a': 1}]), 'b.json': json.dumps([{'b': 1}])})
    with pytest.raises(delivery.DeliveryConflict):
        static.select_source(snapshot, refs)


def test_select_source_rejects_unsupported_extension():
    snapshot = _snapshot({'a.txt': 'hello world'})
    with pytest.raises(delivery.DeliveryConflict):
        static.select_source(snapshot, ['a.txt'])


def test_select_source_rejects_missing_ref():
    snapshot = _snapshot({'a.json': json.dumps([{'a': 1}])})
    with pytest.raises(delivery.DeliveryConflict):
        static.select_source(snapshot, ['missing.json'])


def test_select_source_extension_is_case_insensitive():
    snapshot = _snapshot({'DATA.JSON': json.dumps([{'a': 1}])})
    meta = static.select_source(snapshot, ['DATA.JSON'])
    assert meta['format'] == 'json'
