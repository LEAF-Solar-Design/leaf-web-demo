"""W4e parity instrument: score a cockpit screenshot against the reference.

Usage:
    python scripts/overlay_parity.py <reference.png> <ours.png> [--out DIR] [--json]

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

import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

REFERENCE_EDGES_Y = (28, 123, 155, 181, 909)
REFERENCE_EDGE_X = 250
EDGE_TOLERANCE = 2
CHROME_GATE = 6.0
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


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    flags = {a for a in argv if a.startswith('--')}
    out_dir = None
    if '--out' in argv:
        out_dir = Path(argv[argv.index('--out') + 1])
        args = [a for a in args if a != str(out_dir)]
    if len(args) < 2:
        print(__doc__)
        return 2
    ref_path, ours_path = Path(args[0]), Path(args[1])
    if not ref_path.is_file() or not ours_path.is_file():
        print('missing image', file=sys.stderr)
        return 2
    ref = Image.open(ref_path).convert('RGB')
    ours = Image.open(ours_path).convert('RGB')
    if ours.size != ref.size:
        ours = ours.resize(ref.size, Image.LANCZOS)
    w, h = ref.size

    # Chrome mask: everything except the canvas box.
    mask = Image.new('L', ref.size, 255)
    canvas = Image.new('L', (w - REFERENCE_EDGE_X, 880 - 155), 0)
    mask.paste(canvas, (REFERENCE_EDGE_X, 155))

    whole = mean_abs_diff(ref, ours)
    chrome = mean_abs_diff(ref, ours, mask)

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
    ok = ok and chrome <= CHROME_GATE

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
        'chrome_gate_pct': CHROME_GATE, 'edge_tolerance_px': EDGE_TOLERANCE,
        'edges': rows, 'our_edges_y': our_y[:60], 'our_edges_x': our_x[:20], 'gate': ok,
    }
    if '--json' in flags:
        print(json.dumps(report, indent=2))
    else:
        for r in rows:
            mark = 'ok ' if r['ok'] else 'MISS'
            print(f"{mark} {r['axis']}={r['reference']:>4}  ours={r['ours']}  delta={r['delta']}")
        print(f"chrome diff {chrome:.2f}% (gate {CHROME_GATE}%)   whole frame {whole:.2f}%")
        print(f"our y edges: {our_y[:60]}")
        print(f"our x edges (y=500): {our_x[:20]}")
        print('GATE ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
