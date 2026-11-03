"""Original reference files; bounded validation, private immutable storage only.

MOV validation checks QuickTime container structure and video sample metadata.
It does not decode frames or claim preview/model access.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import unicodedata
import zlib
from uuid import UUID

VIDEO_MAX_BYTES = 64 * 1024 * 1024
TEXT_MAX_BYTES = 8 * 1024 * 1024
CORPUS_MAX_BYTES = 64 * 1024 * 1024
CORPUS_EXPANDED_MAX_BYTES = 2 * 1024 ** 3
CORPUS_LINE_MAX_BYTES = 1024 * 1024
CORPUS_MAX_ROWS = 6_000_000
TYPES = {'.mov': 'video/quicktime', '.txt': 'text/plain', '.json': 'application/json'}


class InvalidReference(ValueError):
    pass


class ReferenceStoreUnavailable(RuntimeError):
    pass


def upload_policy(name, content_type):
    name = unicodedata.normalize('NFC', name).strip()
    if not name or len(name) > 255 or any(unicodedata.category(c).startswith('C') or c in '/\\' for c in name):
        raise InvalidReference('Filename must be a plain name of 1 to 255 characters.')
    extension = '.' + name.rsplit('.', 1)[-1].lower()
    corpus = name.lower().endswith('.jsonl.gz')
    media_type = 'application/gzip' if corpus else TYPES.get(extension)
    if media_type is None:
        raise InvalidReference('Choose an original MOV, UTF-8 TXT/JSON or gzip JSONL corpus.')
    parts = [part.strip().lower() for part in (content_type or '').split(';')]
    if parts[0] != media_type or any(part not in {'charset=utf-8', 'charset="utf-8"'} for part in parts[1:]):
        raise InvalidReference('The file extension and content type must agree (MOV, UTF-8 TXT or JSON).')
    if media_type in {'video/quicktime', 'application/gzip'} and len(parts) != 1:
        raise InvalidReference('Binary original content type must match without parameters.')
    return name, media_type, CORPUS_MAX_BYTES if corpus else VIDEO_MAX_BYTES if extension == '.mov' else TEXT_MAX_BYTES


def validate_corpus(raw):
    """Validate one gzip member incrementally, retaining no expanded corpus.

    Output chunks and unfinished lines have separate bounds. zlib checks the
    gzip CRC/trailer; eof and unused_data reject truncation, extra members and
    trailing bytes. The uploaded compressed bytes remain the stored original.
    """
    expanded = rows = 0
    line = bytearray()
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def invalid_constant(value):
        raise ValueError('nonfinite')

    def unique_object(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = child
        return value

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite')
        return number

    def row(data):
        nonlocal rows
        if not data or len(data) > CORPUS_LINE_MAX_BYTES:
            raise ValueError('line size')
        value = json.loads(data.decode('utf-8'), parse_constant=invalid_constant,
                           parse_float=finite_float, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError('row object required')
        rows += 1
        if rows > CORPUS_MAX_ROWS:
            raise ValueError('row count')

    try:
        for offset in range(0, len(raw), 65536):
            pending = raw[offset:offset + 65536]
            while pending:
                output = decoder.decompress(pending, min(65536, CORPUS_EXPANDED_MAX_BYTES - expanded + 1))
                expanded += len(output)
                if expanded > CORPUS_EXPANDED_MAX_BYTES:
                    raise ValueError('expanded size')
                pending = decoder.unconsumed_tail
                for index, fragment in enumerate(output.split(b'\n')):
                    if index:
                        row(bytes(line))
                        line.clear()
                    line.extend(fragment)
                    if len(line) > CORPUS_LINE_MAX_BYTES:
                        raise ValueError('line size')
                if decoder.unused_data:
                    raise ValueError('trailing data or gzip members')
                if decoder.eof:
                    if offset + 65536 < len(raw) or pending:
                        raise ValueError('trailing data')
                    break
        if not decoder.eof:
            raise ValueError('incomplete gzip')
        if line:
            row(bytes(line))
        if not rows:
            raise ValueError('empty corpus')
    except (zlib.error, ValueError, UnicodeError, RecursionError, OverflowError):
        raise InvalidReference('Corpus must be one complete gzip JSONL stream within the byte, line and row limits.') from None


def _atoms(raw, start=0, end=None):
    end = len(raw) if end is None else end
    result = []
    while start < end:
        if end - start < 8:
            raise InvalidReference('MOV has a truncated atom header.')
        size, kind = struct.unpack_from('>I4s', raw, start)
        header = 8
        if size == 1:
            if end - start < 16:
                raise InvalidReference('MOV has a truncated extended atom.')
            size = struct.unpack_from('>Q', raw, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            raise InvalidReference('MOV has an invalid atom size.')
        result.append((kind, start + header, start + size))
        if len(result) > 100000:
            raise InvalidReference('MOV has too many atoms.')
        start += size
    return result


def _one(atoms, kind, minimum=0):
    matches = [(start, end) for tag, start, end in atoms if tag == kind]
    if len(matches) != 1 or matches[0][1] - matches[0][0] < minimum:
        raise InvalidReference('MOV is missing required video metadata.')
    return matches[0]


def validate_mov(raw):
    top = _atoms(raw)
    for kind, start, end in top:
        if kind == b'ftyp' and (end - start < 8 or (end - start) % 4 or
                                b'qt  ' not in [raw[start:start + 4]] + [raw[i:i + 4] for i in range(start + 8, end, 4)]):
            raise InvalidReference('MOV must use a QuickTime container.')
    if not any(kind == b'mdat' and end > start for kind, start, end in top):
        raise InvalidReference('MOV has no media data.')
    movie = _atoms(raw, *_one(top, b'moov'))
    _one(movie, b'mvhd', 100)
    video = False
    for kind, start, end in movie:
        if kind != b'trak':
            continue
        track = _atoms(raw, start, end)
        _one(track, b'tkhd', 84)
        media = _atoms(raw, *_one(track, b'mdia'))
        _one(media, b'mdhd', 24)
        handler, _ = _one(media, b'hdlr', 24)
        if raw[handler + 8:handler + 12] != b'vide':
            continue
        info = _atoms(raw, *_one(media, b'minf'))
        samples = _atoms(raw, *_one(info, b'stbl'))
        desc, desc_end = _one(samples, b'stsd', 8)
        entries = _atoms(raw, desc + 8, desc_end)
        if not entries or len(entries) != struct.unpack_from('>I', raw, desc + 4)[0] or any(e - s < 78 for _, s, e in entries):
            raise InvalidReference('MOV has invalid video sample descriptions.')
        for table, stride in [(b'stts', 8), (b'stsc', 12)]:
            pos, stop = _one(samples, table, 8)
            count = struct.unpack_from('>I', raw, pos + 4)[0]
            if not count or stop - pos != 8 + count * stride:
                raise InvalidReference('MOV has invalid video sample tables.')
        sizes, stop = _one(samples, b'stsz', 12)
        uniform, count = struct.unpack_from('>II', raw, sizes + 4)
        if not count or stop - sizes != 12 + (0 if uniform else count * 4):
            raise InvalidReference('MOV has invalid video sample sizes.')
        offsets = [a for a in samples if a[0] in {b'stco', b'co64'}]
        if len(offsets) != 1:
            raise InvalidReference('MOV has invalid video chunk offsets.')
        tag, pos, stop = offsets[0]
        stride = 4 if tag == b'stco' else 8
        if stop - pos < 8:
            raise InvalidReference('MOV has truncated video chunk offsets.')
        chunks = struct.unpack_from('>I', raw, pos + 4)[0]
        if not chunks or stop - pos != 8 + chunks * stride:
            raise InvalidReference('MOV has invalid video chunk offsets.')
        for index in range(chunks):
            offset = struct.unpack_from('>I' if stride == 4 else '>Q', raw, pos + 8 + index * stride)[0]
            if not any(k == b'mdat' and s <= offset < e for k, s, e in top):
                raise InvalidReference('MOV references missing media data.')
        video = True
    if not video:
        raise InvalidReference('MOV must contain a video track.')


def validate(name, content_type, raw):
    name, media_type, limit = upload_policy(name, content_type)
    if not 0 < len(raw) <= limit:
        raise InvalidReference('Reference file is empty or exceeds its size limit.')
    if media_type == 'application/gzip':
        validate_corpus(raw)
    elif media_type == 'video/quicktime':
        validate_mov(raw)
    else:
        try:
            text = raw.decode('utf-8-sig')
            if any(unicodedata.category(c) == 'Cc' and c not in '\t\r\n' for c in text):
                raise ValueError('binary text')
            if media_type == 'application/json':
                def invalid_constant(value):
                    raise ValueError('non-finite JSON')
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate JSON key')
                        result[key] = value
                    return result
                json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_object)
        except (UnicodeError, ValueError, RecursionError):
            raise InvalidReference('Reference must contain valid UTF-8 text or strict JSON.') from None
    return {'name': name, 'media_type': media_type, 'byte_size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def object_key(org_id, project_id, digest):
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('invalid reference digest')
    return f'tenants/{UUID(str(org_id))}/projects/{UUID(str(project_id))}/references/original/{digest}'


def prepare_and_store(org_id, project_id, name, content_type, raw):
    prepared = validate(name, content_type, raw)
    import write_loop
    try:
        write_loop.upload_backend_for_tenant(str(org_id)).put_if_absent_or_verify(
            object_key(org_id, project_id, prepared['sha256']), raw)
    except Exception:
        raise ReferenceStoreUnavailable('Original storage failed. Retry this file; existing originals are preserved.') from None
    return prepared


def read_content(org_id, project_id, material):
    import write_loop
    try:
        raw = write_loop.upload_backend_for_tenant(str(org_id)).get(object_key(org_id, project_id, material['sha256']))
        limit = CORPUS_MAX_BYTES if material['media_type'] == 'application/gzip' else VIDEO_MAX_BYTES if material['media_type'] == 'video/quicktime' else TEXT_MAX_BYTES
        if not 0 < len(raw) <= limit or len(raw) != material['byte_size'] or hashlib.sha256(raw).hexdigest() != material['sha256']:
            raise ValueError('reference integrity')
        return raw
    except Exception:
        raise ReferenceStoreUnavailable('Original content is unavailable or failed its integrity check.') from None
