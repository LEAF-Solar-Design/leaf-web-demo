"""W4e parity instrument: score a cockpit screenshot against the reference.

Usage:
    python scripts/overlay_parity.py <reference.png> <ours.png> [--out DIR] [--json] [--mask L,T,W,H]
    python scripts/overlay_parity.py --selftest

Writes blend / diff / side-by-side PNGs next to <ours.png> (or into --out)
and prints:
  * the band edges (luminance steps along x=700 and x=100, plus the pane
    edge along y=500) for both images, and the delta of each reference edge
    to the nearest edge of ours;
  * the CHROME mean absolute pixel difference, with the canvas masked out
    (x > 250 and 155 < y < 880), because the canvas differs by content and
    the gate is about the chrome;
  * the whole-frame mean absolute difference for continuity with W4d.

Gate (W4e plan): every named reference edge within 2px, chrome diff <= 6%.
Exit code 0 when the gate holds, 1 otherwise, 2 on bad input. Pure PIL,
bounded to the two images given; nothing is fetched.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

REFERENCE_EDGES_Y = (28, 123, 155, 181, 909)
REFERENCE_EDGE_X = 250
EDGE_TOLERANCE = 2
CHROME_GATE = 6.0
# W4g-1b canvas parity (the same drawing from the intake and from the engine
# projection): mean absolute pixel difference over the canvas box alone.
CANVAS_GATE = 2.0
LUMA_STEP = 18


def luma(px):
    r, g, b = px[:3]
    return (r * 299 + g * 587 + b * 114) // 1000


def edges_along_column(im, x, thr=LUMA_STEP):
    w, h = im.size
    x = min(max(0, x), w - 1)
    out, prev = [], None
    for y in range(h):
        l = luma(im.getpixel((x, y)))
        if prev is not None and abs(l - prev) >= thr:
            out.append(y)
        prev = l
    return out


def edges_along_row(im, y, thr=LUMA_STEP):
    w, h = im.size
    y = min(max(0, y), h - 1)
    out, prev = [], None
    for x in range(w):
        l = luma(im.getpixel((x, y)))
        if prev is not None and abs(l - prev) >= thr:
            out.append(x)
        prev = l
    return out


def nearest(value, candidates):
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c - value))


def mean_abs_diff(a, b, mask=None):
    diff = ImageChops.difference(a, b).convert('L')
    if mask is not None:
        hist_src = Image.composite(diff, Image.new('L', diff.size, 0), mask)
        hist = hist_src.histogram()
        count = sum(mask.histogram()[255:256]) or 1
    else:
        hist = diff.histogram()
        count = diff.size[0] * diff.size[1]
    total = sum(i * n for i, n in enumerate(hist))
    return 100.0 * total / (255.0 * count)


def parse_mask(value):
    parts = value.split(',')
    if len(parts) != 4 or any(not part.isascii() or not part.isdecimal() for part in parts):
        raise argparse.ArgumentTypeError('--mask requires four non-negative integers: L,T,W,H')
    return tuple(int(part) for part in parts)


def selftest():
    # A small synthetic cockpit carries every named edge, so the normal
    # chrome gate is exercised as well as the canvas-only gate.
    reference = Image.new('RGB', (320, 912), 'black')
    for index, top in enumerate(REFERENCE_EDGES_Y):
        reference.paste('white' if index % 2 == 0 else 'black',
                        (0, top, reference.width, reference.height))
    reference.paste('white', (REFERENCE_EDGE_X, 500, reference.width, 501))

    def run(ours, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(['reference.png', 'ours.png', '--json', *options],
                        images=(reference, ours))
        return code, output.getvalue()

    code, output = run(reference.copy())
    report = json.loads(output)
    identical = (code == 0 and report['gate'] and report['whole_frame_diff_pct'] == 0
                 and report['chrome_diff_pct'] == 0)
    canvas_code, canvas_output = run(reference.copy(), '--canvas-only', '--mask', '0,0,8,8')
    identical = identical and canvas_code == 0 and json.loads(canvas_output)['canvas_diff_pct'] == 0
    code, output = run(Image.new('RGB', (321, 912)))
    mismatch = code == 2 and 'size mismatch: reference 320x912, ours 321x912' in output
    changed = reference.copy()
    changed.paste('white', (0, 0, 8, 8))
    _, unmasked = run(changed, '--mask', '0,0,0,0')
    _, masked = run(changed, '--mask', '0,0,8,8')
    mask_works = (json.loads(unmasked)['chrome_diff_pct'] > 0
                  and json.loads(masked)['chrome_diff_pct'] == 0
                  and json.loads(masked)['canvas_diff_pct'] == 100)
    invalid_masks = all(run(reference, '--mask', value)[0] == 2
                        for value in ('1,2,3', '-1,0,1,1', '0,0,1.5,1', '0,0,x,1'))
    ok = identical and mismatch and mask_works and invalid_masks
    print('SELFTEST ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main(argv, *, images=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', nargs='?')
    parser.add_argument('ours', nargs='?')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--canvas-only', action='store_true')
    parser.add_argument('--mask', type=parse_mask)
    parser.add_argument('--selftest', action='store_true')
    try:
        options = parser.parse_args(argv)
        if options.selftest:
            return selftest()
        if not options.reference or not options.ours:
            parser.error('reference and ours images are required')
    except SystemExit as error:
        return error.code
    out_dir = options.out
    ref_path, ours_path = Path(options.reference), Path(options.ours)
    if images is None:
        if not ref_path.is_file() or not ours_path.is_file():
            print('missing image', file=sys.stderr)
            return 2
        try:
            with Image.open(ref_path) as image:
                ref = image.convert('RGB')
            with Image.open(ours_path) as image:
                ours = image.convert('RGB')
        except (OSError, ValueError) as error:
            print(f'invalid image: {error}', file=sys.stderr)
            return 2
    else:
        ref, ours = images
    if ours.size != ref.size:
        print(f'size mismatch: reference {ref.width}x{ref.height}, ours {ours.width}x{ours.height}',
              file=sys.stderr)
        return 2
    w, h = ref.size

    # Chrome is everything outside the supplied canvas rectangle.
    left, top, width, height = options.mask if options.mask is not None else (REFERENCE_EDGE_X, 155, max(0, w - REFERENCE_EDGE_X), 880 - 155)
    mask = Image.new('L', ref.size, 255)
    right, bottom = min(w, left + width), min(h, top + height)
    if right > left and bottom > top:
        mask.paste(0, (left, top, right, bottom))

    whole = mean_abs_diff(ref, ours)
    chrome = mean_abs_diff(ref, ours, mask)
    # W4g-1b: the canvas box ALONE (the inverse mask), for the engine-view
    # parity gate: the same drawing drawn from the intake and from the
    # engine's projection must look the same on the canvas. Reported always;
    # `--canvas-only` makes it the gate (at or below CANVAS_GATE) instead of
    # the chrome number, because that comparison is ours-vs-ours, not
    # ours-vs-reference.
    canvas_only = options.canvas_only
    canvas_mask = ImageChops.invert(mask)
    canvas_diff = mean_abs_diff(ref, ours, canvas_mask)

    # Three probe columns: x=100 (the pane), x=700 (the bands over the
    # canvas), and x=300 for the viewport strip's bottom edge (y=181): the
    # strip is content-wide on BOTH images (reference ~x 250-440), so at
    # x=700 that edge can only come from drawing content, which passed the
    # staging shots by luck and failed a dev shot of the same chrome.
    probes = (700, 100, 300)
    ref_y = sorted(set(y for x in probes for y in edges_along_column(ref, x)))
    our_y = sorted(set(y for x in probes for y in edges_along_column(ours, x)))
    ref_x = [x for x in edges_along_row(ref, 500) if x < 400]
    our_x = [x for x in edges_along_row(ours, 500) if x < 400]

    rows = []
    ok = True
    for y in REFERENCE_EDGES_Y:
        got = nearest(y, our_y)
        delta = None if got is None else got - y
        within = delta is not None and abs(delta) <= EDGE_TOLERANCE
        ok = ok and within
        rows.append({'axis': 'y', 'reference': y, 'ours': got, 'delta': delta, 'ok': within})
    got_x = nearest(REFERENCE_EDGE_X, our_x)
    dx = None if got_x is None else got_x - REFERENCE_EDGE_X
    within_x = dx is not None and abs(dx) <= EDGE_TOLERANCE
    ok = ok and within_x
    rows.append({'axis': 'x', 'reference': REFERENCE_EDGE_X, 'ours': got_x, 'delta': dx, 'ok': within_x})
    if canvas_only:
        ok = canvas_diff <= CANVAS_GATE
    else:
        ok = ok and chrome <= CHROME_GATE

    if images is None:
        out_dir = out_dir or ours_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = ours_path.stem
        Image.blend(ref, ours, 0.5).save(out_dir / f'{stem}-blend.png')
        ImageChops.difference(ref, ours).save(out_dir / f'{stem}-diff.png')
        side = Image.new('RGB', (w * 2, h))
        side.paste(ref, (0, 0))
        side.paste(ours, (w, 0))
        side.save(out_dir / f'{stem}-side.png')

    report = {
        'reference': str(ref_path), 'ours': str(ours_path),
        'whole_frame_diff_pct': round(whole, 2), 'chrome_diff_pct': round(chrome, 2),
        'canvas_diff_pct': round(canvas_diff, 2), 'canvas_gate_pct': CANVAS_GATE,
        'gate_mode': 'canvas-only' if canvas_only else 'chrome',
        'chrome_gate_pct': CHROME_GATE, 'edge_tolerance_px': EDGE_TOLERANCE,
        'edges': rows, 'our_edges_y': our_y[:60], 'our_edges_x': our_x[:20], 'gate': ok,
    }
    if options.json:
        print(json.dumps(report, indent=2))
    else:
        for r in rows:
            mark = 'ok ' if r['ok'] else 'MISS'
            print(f"{mark} {r['axis']}={r['reference']:>4}  ours={r['ours']}  delta={r['delta']}")
        print(f"chrome diff {chrome:.2f}% (gate {CHROME_GATE}%)   whole frame {whole:.2f}%")
        print(f"canvas diff {canvas_diff:.2f}% (gate {CANVAS_GATE}%, {'THE gate' if canvas_only else 'reported'})")
        print(f"our y edges: {our_y[:60]}")
        print(f"our x edges (y=500): {our_x[:20]}")
        print('GATE ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
