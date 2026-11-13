"""Private original-video decoding. Linux namespace and rlimit cage, no bare fallback."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = 'leaf.project-video-frames.v1'
POLICY = 'full-decode-first-last-two-second-v1'
MAX_DURATION = 180
MAX_FRAMES = 96
MAX_FRAME_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 9_000_000
DECODE_SECONDS = 45
TOOLS = ('/usr/bin/prlimit', '/usr/bin/bwrap', '/usr/bin/ffprobe', '/usr/bin/ffmpeg')
_FRAME = re.compile(r'\[Parsed_showinfo_[^\]]+\].*?\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*([0-9.eE+-]+)')
_TIMEBASE = re.compile(r'\[Parsed_showinfo_[^\]]+\].*?config in time_base:\s*(\d+)/(\d+)')


class VideoError(ValueError):
    """Only fixed public error codes leave this module."""
    def __init__(self, code='video_unavailable'):
        self.code = code
        super().__init__(code)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _bounded(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
        raise VideoError('video_incomplete')
    # The namespace child has been reaped before any output is opened.
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) != info.st_size:
        raise VideoError('video_incomplete')
    return raw


def _finish(proc):
    """Reap our exact process and terminate its entire owned process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=2)
    end = time.monotonic() + 2
    while True:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= end:
            raise VideoError('video_cleanup_failed')
        time.sleep(.01)


def _run(scratch, binary, args, name, deadline, recheck):
    output = scratch / 'output'
    argv = [
        TOOLS[0], '--as=536870912', '--cpu=30', '--fsize=131072',
        '--core=0', '--nofile=64', '--nproc=32', '--',
        TOOLS[1], '--unshare-all', '--die-with-parent', '--cap-drop', 'ALL',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib',
        '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
        '--dir', '/tmp', '--ro-bind', str(scratch / 'original.mov'), '/input.mov',
        '--bind', str(output), '/output', '--chdir', '/output',
        '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LC_ALL', 'C',
        '--', binary, *args,
    ]
    proc = None
    with (output / (name + '.stdout')).open('xb') as stdout, (output / (name + '.stderr')).open('xb') as stderr:
        try:
            recheck()
            if time.monotonic() >= deadline:
                raise VideoError('video_timeout')
            proc = subprocess.Popen(argv, cwd=scratch, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, shell=False,
                                    start_new_session=True,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            while proc.poll() is None:
                recheck()
                if time.monotonic() >= deadline:
                    raise VideoError('video_timeout')
                time.sleep(.02)
            if proc.returncode:
                raise VideoError('video_incomplete')
            recheck()
        except PermissionError:
            raise
        except OSError:
            raise VideoError('video_unavailable') from None
        finally:
            if proc is not None:
                try:
                    _finish(proc)
                except Exception:
                    raise VideoError('video_cleanup_failed') from None


def _number(value, minimum, maximum):
    try:
        result = float(value)
        if isinstance(value, bool) or not math.isfinite(result) or not minimum <= result <= maximum:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise VideoError('video_incomplete') from None


def _probe(raw):
    try:
        value = json.loads(raw)
        rows = value['streams']
        if len(rows) != 1:
            raise ValueError()
        row = rows[0]
        width = int(_number(row['width'], 1, 8192))
        height = int(_number(row['height'], 1, 8192))
        if width * height > MAX_PIXELS:
            raise ValueError()
        duration = _number(row.get('duration', value.get('format', {}).get('duration')), .001, MAX_DURATION)
        count = int(_number(row['nb_read_frames'], 1, 21600))
        start = _number(row.get('start_time', 0), 0, MAX_DURATION)
        stream = int(_number(row['index'], 0, 32))
        return dict(width=width, height=height, duration_seconds=duration,
                    decoded_frame_count=count, start_seconds=start, stream_index=stream)
    except (ValueError, KeyError, TypeError):
        raise VideoError('video_incomplete') from None


def jpeg_dimensions(raw):
    """Read bounded JPEG markers only; never decode untrusted pixels in the app."""
    if not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):
        raise VideoError('video_incomplete')
    pos = 2
    while pos + 4 <= len(raw):
        if raw[pos] != 255:
            break
        while pos < len(raw) and raw[pos] == 255:
            pos += 1
        tag = raw[pos]
        pos += 1
        if tag in (0xD8, 0xD9) or 0xD0 <= tag <= 0xD7:
            continue
        length = int.from_bytes(raw[pos:pos + 2], 'big')
        if length < 2 or pos + length > len(raw):
            break
        if tag in (0xC0, 0xC1, 0xC2):
            if length < 8:
                break
            height = int.from_bytes(raw[pos + 3:pos + 5], 'big')
            width = int.from_bytes(raw[pos + 5:pos + 7], 'big')
            if 0 < width <= 768 and 0 < height <= 768:
                return width, height
            break
        if tag == 0xDA:
            break
        pos += length
    raise VideoError('video_incomplete')


def _available():
    return (sys.platform == 'linux' and os.geteuid() != 0
            and all(Path(tool).is_file() for tool in TOOLS))


def decode(raw, material, *, identity, transcripts, recheck, deadline):
    """Accept verified bytes, never a caller path or URL. No model calls here."""
    recheck()
    if (type(raw) is not bytes or not 0 < len(raw) <= 64 * 1024 * 1024
            or material.get('media_type') != 'video/quicktime'
            or len(raw) != material.get('byte_size') or _digest(raw) != material.get('sha256')):
        raise VideoError('video_changed')
    if not _available():
        raise VideoError('video_unavailable')
    deadline = min(deadline, time.monotonic() + DECODE_SECONDS)
    scratch = Path(tempfile.mkdtemp(prefix='leaf-video-'))
    os.chmod(scratch, 0o700)
    try:
        (scratch / 'output').mkdir(mode=0o700)
        with (scratch / 'original.mov').open('xb') as handle:
            os.chmod(scratch / 'original.mov', 0o600)
            handle.write(raw)
        inputs = ['-protocol_whitelist', 'file', '-enable_drefs', '0',
                  '-use_absolute_path', '0', '-f', 'mov']
        _run(scratch, TOOLS[2], [
            '-v', 'error', '-threads', '1', '-err_detect', 'explode', *inputs,
            '-select_streams', 'v:0', '-count_frames',
            '-show_entries', 'stream=index,width,height,duration,nb_read_frames,start_time:format=duration',
            '-of', 'json', '/input.mov',
        ], 'probe', deadline, recheck)
        out = scratch / 'output'
        if (out / 'probe.stderr').stat().st_size:
            raise VideoError('video_incomplete')
        info = _probe(_bounded(out / 'probe.stdout', MAX_FRAME_BYTES))
        last = info['decoded_frame_count'] - 1
        filters = f"select=eq(n\\,0)+gte(t-prev_selected_t\\,2)+eq(n\\,{last}),showinfo,scale=768:768:force_original_aspect_ratio=decrease"
        _run(scratch, TOOLS[3], [
            '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info', '-xerror',
            '-threads', '1', '-max_alloc', '67108864', '-err_detect', 'explode',
            *inputs, '-i', '/input.mov', '-map', '0:v:0', '-an', '-sn', '-dn',
            '-filter_threads', '1', '-vf', filters, '-fps_mode', 'vfr',
            '-threads', '1', '-frames:v', str(MAX_FRAMES + 1),
            '-q:v', '6', '-f', 'image2', '/output/frame-%03d.jpg',
        ], 'decode', deadline, recheck)
        log = _bounded(out / 'decode.stderr', MAX_FRAME_BYTES).decode('ascii', errors='strict')
        timestamps = _FRAME.findall(log)
        bases = _TIMEBASE.findall(log)
        paths = sorted(out.glob('frame-*.jpg'))
        if (not bases or not 1 <= len(paths) == len(timestamps) <= MAX_FRAMES
                or len(paths) > info['decoded_frame_count']):
            raise VideoError('video_incomplete')
        numerator, denominator = map(int, bases[0])
        if not 0 < numerator <= 1_000_000_000 or not 0 < denominator <= 1_000_000_000 or any(base != bases[0] for base in bases):
            raise VideoError('video_incomplete')
        frames, total, previous = [], 0, None
        for index, (path, (ordinal, pts, timestamp)) in enumerate(zip(paths, timestamps)):
            recheck()
            if path.name != f'frame-{index + 1:03d}.jpg' or int(ordinal) != index:
                raise VideoError('video_incomplete')
            seconds = _number(timestamp, 0, MAX_DURATION * 2)
            ticks = int(pts)
            if abs(seconds - ticks * numerator / denominator) > .002:
                raise VideoError('video_incomplete')
            if previous is not None and not 0 < seconds - previous <= 2.25:
                raise VideoError('video_incomplete')
            previous = seconds
            pixels = _bounded(path, MAX_FRAME_BYTES)
            width, height = jpeg_dimensions(pixels)
            total += len(pixels)
            if total > MAX_TOTAL_BYTES:
                raise VideoError('video_too_large')
            frames.append(dict(frame_id=f'frame-{index + 1:03d}', pts=ticks,
                               timestamp_ms=round(seconds * 1000), width=width, height=height,
                               sha256=_digest(pixels), size_bytes=len(pixels),
                               data=base64.b64encode(pixels).decode('ascii')))
        if (abs(frames[0]['timestamp_ms'] / 1000 - info['start_seconds']) > .25
                or abs(frames[-1]['timestamp_ms'] / 1000 - info['start_seconds'] - info['duration_seconds']) > 2.25):
            raise VideoError('video_incomplete')
        info['duration_ms'] = round(info.pop('duration_seconds') * 1000)
        info['start_ms'] = round(info.pop('start_seconds') * 1000)
        manifest = dict(schema=SCHEMA, policy=POLICY, **identity,
                        original=dict(material_id=material['material_id'], sha256=material['sha256'], size_bytes=len(raw)),
                        transcripts=transcripts, **info, time_base=[numerator, denominator],
                        frames=[{key: value for key, value in frame.items() if key != 'data'} for frame in frames])
        recheck()
        return dict(manifest=manifest, manifest_sha256=_digest(canonical(manifest)),
                    images=[dict(frame_id=frame['frame_id'], data=frame['data']) for frame in frames])
    except PermissionError:
        raise
    except (OSError, UnicodeError, KeyError, IndexError):
        raise VideoError('video_incomplete') from None
    finally:
        try:
            shutil.rmtree(scratch)
            if scratch.exists():
                raise OSError()
        except OSError:
            raise VideoError('video_cleanup_failed') from None
