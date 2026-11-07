"""Bounded photo intake and immutable storage, without provider access."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata

MAX_BYTES = 16 * 1024 * 1024
DECODE_TIMEOUT = 15


class InvalidImage(ValueError):
    pass


class MaterialStoreUnavailable(RuntimeError):
    pass


def display_name(name):
    name = unicodedata.normalize('NFC', name).strip()
    if not name or len(name) > 255 or any(unicodedata.category(c).startswith('C') or c in '/\\' for c in name):
        raise ValueError('filename must be a plain name of 1 to 255 characters')
    return name


def decode_image(raw):
    if not 0 < len(raw) <= MAX_BYTES:
        raise InvalidImage('Image must contain 1 byte to 16 MiB.')
    with tempfile.TemporaryDirectory(prefix='leaf-photo-') as temporary:
        directory = Path(temporary)
        (directory / 'input').write_bytes(raw)
        # Explicit allowlist, isolated interpreter, no inherited handles or
        # application/provider environment. HOME cannot expose cached grants.
        env = {'HOME': temporary, 'TMPDIR': temporary, 'TEMP': temporary, 'TMP': temporary,
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
        if os.name == 'nt':
            system_root = os.environ.get('SYSTEMROOT')
            if system_root:
                env['SYSTEMROOT'] = system_root
        try:
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('project_material_image_decoder.py'))],
                                    cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, close_fds=True, timeout=DECODE_TIMEOUT, check=False)
            if result.returncode != 0 or len(result.stdout) > 1024:
                raise InvalidImage('Image must be a complete, single JPEG, PNG, or HEIF photo of at most 40 million pixels.')
            info = json.loads(result.stdout)
            preview = (directory / 'preview.jpg').read_bytes()
            if (set(info) != {'media_type', 'width', 'height'} or info['media_type'] not in {'image/jpeg', 'image/png', 'image/heif'}
                    or any(type(info[k]) is not int or info[k] <= 0 for k in ('width', 'height'))
                    or info['width'] * info['height'] > 40_000_000 or not 0 < len(preview) <= MAX_BYTES):
                raise InvalidImage('Invalid decoded image.')
            return info, preview
        except subprocess.TimeoutExpired:
            raise InvalidImage('Image decoding exceeded its time limit.') from None
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            raise InvalidImage('Image could not be decoded.') from None


def object_key(org_id, project_id, digest, variant):
    from uuid import UUID
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or variant not in {'original', 'preview'}:
        raise ValueError('invalid material object identity')
    return f'tenants/{UUID(str(org_id))}/projects/{UUID(str(project_id))}/materials/{variant}/{digest}'


def prepare_and_store(org_id, project_id, name, raw):
    name = display_name(name)
    info, preview = decode_image(raw)
    result = dict(info, name=name, byte_size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                  preview_media_type='image/jpeg', preview_sha256=hashlib.sha256(preview).hexdigest())
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(str(org_id))
        backend.put_if_absent_or_verify(object_key(org_id, project_id, result['sha256'], 'original'), raw)
        backend.put_if_absent_or_verify(object_key(org_id, project_id, result['preview_sha256'], 'preview'), preview)
    except Exception:
        raise MaterialStoreUnavailable('Photo storage is unavailable. Retry this file.') from None
    return result


def read_content(org_id, project_id, material, variant):
    import write_loop
    digest = material['sha256' if variant == 'original' else 'preview_sha256']
    try:
        raw = write_loop.upload_backend_for_tenant(str(org_id)).get(object_key(org_id, project_id, digest, variant))
        if not 0 < len(raw) <= MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('material integrity')
        return raw
    except Exception:
        raise MaterialStoreUnavailable('Photo content is unavailable.') from None
