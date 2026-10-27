"""One bounded, credential-free image decode process. No application imports."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import warnings


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from PIL import Image, ImageFile, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener(thumbnails=False)
    Image.MAX_IMAGE_PIXELS = 40_000_000
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    raw = Path('input').read_bytes()
    if not 0 < len(raw) <= 16 * 1024 * 1024:
        raise ValueError('size')
    mime = {'JPEG': 'image/jpeg', 'MPO': 'image/jpeg', 'PNG': 'image/png', 'HEIF': 'image/heif'}
    with Image.open(io.BytesIO(raw)) as check:
        # Phone HDR JPEGs can contain a second auxiliary image. Preview the
        # primary JPEG only; keep the entire original in immutable storage.
        frames = getattr(check, 'n_frames', 1)
        if check.format not in mime or frames not in ((1, 2) if check.format == 'MPO' else (1,)):
            raise ValueError('type or frames')
        if check.width * check.height > 40_000_000:
            raise ValueError('pixels')
        media_type = mime[check.format]
        check.verify()
    with Image.open(io.BytesIO(raw)) as source:
        source.seek(0)
        source.load()
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
        oriented.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        # Copy pixels into a fresh RGB image; no EXIF, ICC, XMP, comments or GPS.
        preview = Image.new('RGB', oriented.size, 'white')
        if 'A' in oriented.getbands() or 'transparency' in oriented.info:
            rgba = oriented.convert('RGBA')
            preview.paste(rgba, mask=rgba.getchannel('A'))
        else:
            preview.paste(oriented.convert('RGB'))
        preview.save('preview.jpg', format='JPEG', quality=85)
    print(json.dumps({'media_type': media_type, 'width': width, 'height': height}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Decoder internals and attacker-controlled metadata never enter logs.
        sys.exit(2)
