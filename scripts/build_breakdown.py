#!/usr/bin/env python3
"""Stage 0b: deterministic visual breakdown from a source video.

Produces the `breakdown.json` consumed by build_recreation_manifest.py.
Cuts come from ffmpeg scene-change detection; first/last frames are extracted;
timed transcripts (platform subtitles, ASR JSONL, SRT/VTT) are aligned to shot
windows so the recreation prompts gain semantic context.

It deliberately does NOT guess production routes. Shots default to
`manual_review` until a human or VLM assigns a label. `--promote` runs the
objective promotion gate (V1/V2) and auto-labels only shots with complete
evidence as `ai_video` CANDIDATES; AI suitability itself still needs a human.
`--assume-ai-video` is the legacy blunt opt-in and prints a stronger warning.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from fetch_source import probe
from video_api import save
import promotion_gate as gate

PTS_TIME_RE = re.compile(r'pts_time:([0-9.]+)')
CUE_RE = re.compile(r'([\d:.,]+)\s+-->\s+([\d:.,]+)')


def stamp_seconds(value: str) -> float:
    parts = value.replace(',', '.').split(':')
    return sum(float(part) * (60 ** index) for index, part in enumerate(reversed(parts)))


def scene_timestamps(video: Path, threshold: float, timeout: int = 600) -> list[float]:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg not found in PATH; install ffmpeg first')
    value = min(max(threshold, 0.05), 0.95)
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-i', str(video), '-an', '-vf',
         f"fps=4,scale=384:-2,select='gt(scene,{value})',showinfo", '-f', 'null', '-'],
        capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg scene detection failed (exit {result.returncode})')
    return sorted({round(float(t), 3) for t in PTS_TIME_RE.findall(result.stderr)})


def make_windows(cuts: list[float], duration: float, min_shot_s: float,
                 max_shot_s: float) -> list[tuple[float, float, bool]]:
    points = [0.0] + sorted({c for c in cuts if 0 < c < duration}) + [duration]
    merged: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        if end - start < min_shot_s:
            if merged:
                merged[-1] = (merged[-1][0], end)
            # A too-short leading interval must not be dropped: the next
            # accepted window still has to start at 0, or we lose footage.
            continue
        merged.append((0.0 if not merged else start, end))
    windows: list[tuple[float, float, bool]] = []
    for start, end in merged:
        count = max(1, math.ceil((end - start) / max_shot_s))
        step = (end - start) / count
        for index in range(count):
            windows.append((round(start + index * step, 3),
                            round(min(duration, start + (index + 1) * step), 3),
                            count > 1))
    return [(a, b, flag) for a, b, flag in windows if b - a > 0.05]


def extract_frame(video: Path, seconds: float, destination: Path) -> bool:
    ffmpeg = shutil.which('ffmpeg')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-ss', f'{seconds:.3f}',
         '-i', str(video), '-frames:v', '1', '-q:v', '3', str(destination)],
        capture_output=True, text=True, timeout=120)
    ok = result.returncode == 0 and destination.is_file() and destination.stat().st_size > 0
    if not ok and destination.is_file():
        # A failed seek can leave a zero-byte file behind; never let a later
        # stage mistake it for a real reference frame.
        try:
            destination.unlink()
        except OSError:
            pass
    return ok


def extract_frame_near(video: Path, seconds: float, destination: Path,
                       min_seconds: float = 0.0) -> tuple[bool, float]:
    """Extract at `seconds`, stepping back if that timestamp is past the last
    decodable frame. `duration - 0.05` overshoots the final PTS on many MP4s,
    which used to silently drop the tail frame of the last shot.
    Returns (success, timestamp actually used)."""
    for offset in (0.0, 0.1, 0.3, 0.7, 1.5, 3.0):
        candidate = max(min_seconds, round(seconds - offset, 3))
        if extract_frame(video, candidate, destination):
            return True, candidate
        if candidate <= min_seconds:
            break
    return False, seconds


def parse_cut_points(text: str) -> list[float]:
    """Parse reviewed shot boundaries ('4.0,13.5' or '4 13.5') in seconds."""
    values: list[float] = []
    for token in re.split(r'[,\s]+', (text or '').strip()):
        if not token:
            continue
        try:
            value = float(token)
        except ValueError as exc:
            raise RuntimeError(f'Invalid cut point {token!r}: expected seconds') from exc
        if value < 0:
            raise RuntimeError(f'Cut point must be non-negative: {token!r}')
        values.append(round(value, 3))
    return sorted(set(values))


def parse_transcript(path: Path) -> list[dict]:
    text = path.read_text(encoding='utf-8-sig')
    suffix = path.suffix.lower()
    records: list[dict] = []
    if suffix in ('.srt', '.vtt', '.txt'):
        for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n')):
            lines = block.splitlines()
            for index, line in enumerate(lines):
                match = CUE_RE.match(line.strip())
                if match:
                    body = re.sub(r'<[^>]+>', '', ' '.join(lines[index + 1:])).strip()
                    start, end = stamp_seconds(match.group(1)), stamp_seconds(match.group(2))
                    if body and 0 <= start < end:
                        records.append({'start': start, 'end': end, 'text': body})
                    break
    elif suffix == '.jsonl':
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    elif suffix == '.json':
        payload = json.loads(text)
        items = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            for key in ('segments', 'cues', 'events', 'body', 'subtitles'):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        records = [item for item in items if isinstance(item, dict)]
    else:
        raise RuntimeError(f'Unsupported transcript format: {suffix}')

    normalized = []
    for item in records:
        start = pick_time(item, ('start_ms', 'startMs', 't_start_ms'), 1000)
        if start is None:
            start = pick_time(item, ('from', 'start', 'start_s', 't0', 'begin', 'time'), 1)
        end = pick_time(item, ('end_ms', 'endMs', 't_end_ms'), 1000)
        if end is None:
            end = pick_time(item, ('to', 'end', 'end_s', 't1', 'finish'), 1)
        if end is None and start is not None and item.get('dDurationMs') is not None:
            try:
                end = start + float(item['dDurationMs']) / 1000
            except (TypeError, ValueError):
                end = None
        body = str(item.get('text') or item.get('content') or item.get('caption') or '').strip()
        if not body:
            segs = item.get('segs')
            if isinstance(segs, list):
                body = ''.join(str(s.get('utf8') or '') for s in segs if isinstance(s, dict)).strip()
        if start is not None and end is not None and 0 <= start < end and body:
            normalized.append({'start': round(start, 3), 'end': round(end, 3), 'text': body})
    return sorted(normalized, key=lambda r: (r['start'], r['end']))


def pick_time(item: dict, keys: tuple[str, ...], divisor: float):
    for key in keys:
        if item.get(key) is not None:
            try:
                return float(item[key]) / divisor
            except (TypeError, ValueError):
                continue
    return None


def align_transcript(segments: list[dict], start: float, end: float) -> list[dict]:
    return [s for s in segments
            if s['end'] > start + 0.05 and s['start'] < end - 0.05]


def build(video: Path, out_path: Path, frames_dir: Path, *, threshold: float,
          min_shot_s: float, max_shot_s: float, transcripts: list[Path],
          default_label: str, source_meta: dict | None, provenance: dict,
          promote: bool = False, gate_config: dict | None = None,
          manual_cuts: list[float] | None = None) -> dict:
    meta = source_meta or probe(video)
    duration = float(meta.get('duration') or 0)
    if duration <= 0:
        raise RuntimeError('Source video has no measurable duration')
    policy = gate.policy_from_config(gate_config)
    # One uncut 20s take is useless for shot-level recreation: most video models
    # cannot generate that long, and a single first frame cannot constrain it.
    # Keep windows inside the model's real duration ceiling (leave 1s for the
    # runner's ceil()) so long takes split into recreatable segments.
    effective_max = max(min_shot_s + 0.1, max_shot_s)
    if gate_config:
        model_max = float(policy.get('max_duration_s') or effective_max)
        model_min = float(policy.get('min_duration_s') or 0)
        ceiling = model_max - 1.0
        # Never auto-split into fragments shorter than the model can generate:
        # keep the original window and let the gate report the duration gap
        # instead of hiding it behind a pile of sub-second "shots".
        if ceiling >= max(min_shot_s, model_min, 1.0):
            effective_max = max(min_shot_s + 0.1, min(effective_max, ceiling))
        else:
            effective_max = max_shot_s
    if manual_cuts is not None:
        cuts = sorted({round(float(c), 3) for c in manual_cuts if 0 < c < duration})
        cut_source = 'manual'
    else:
        cuts = scene_timestamps(video, threshold)
        cut_source = 'scene_detection'
    windows = make_windows(cuts, duration, min_shot_s, effective_max)
    if not windows:
        windows = [(0.0, duration, False)]
    segments: list[dict] = []
    for path in transcripts:
        segments.extend(parse_transcript(path))
    segments.sort(key=lambda r: (r['start'], r['end']))

    frames_dir.mkdir(parents=True, exist_ok=True)
    frames_rel = os.path.relpath(frames_dir, out_path.parent).replace(os.sep, '/')
    source_info = {'width': meta.get('width'), 'height': meta.get('height')}
    shots = []
    for index, (start, end, split) in enumerate(windows, start=1):
        shot_id = f'shot_{index:03d}'
        first_rel = f'{frames_rel}/{shot_id}_first.jpg'
        last_rel = f'{frames_rel}/{shot_id}_last.jpg'
        first_want = min(start + 0.02, max(0.0, end - 0.05))
        last_want = max(start + 0.02, end - 0.05)
        first_ok, first_at = extract_frame_near(
            video, first_want, frames_dir / f'{shot_id}_first.jpg', min_seconds=start)
        last_ok, last_at = extract_frame_near(
            video, last_want, frames_dir / f'{shot_id}_last.jpg', min_seconds=start + 0.02)
        assets = {}
        if first_ok:
            assets['first_frame'] = first_rel
        if last_ok:
            assets['last_frame'] = last_rel
        overlapping = align_transcript(segments, start, end)
        seen, parts = set(), []
        for item in overlapping:
            if item['text'] not in seen:
                seen.add(item['text'])
                parts.append(item['text'])
        evidence = [f'scene_change@{c:.3f}' for c in cuts if start <= c < end][:8]
        if split:
            evidence.append('split_from_long_shot')
        if last_ok and last_at < last_want - 0.05:
            evidence.append(f'last_frame_fallback@{last_at:.3f}')
        if not last_ok:
            evidence.append('last_frame_extraction_failed')
        shot = {
            'id': shot_id,
            'start': start, 'end': end, 'duration': round(end - start, 3),
            'base': {'label': default_label, 'confidence': 0.0 if default_label == 'unknown' else 0.5},
            'assets': assets,
            'frame_times': {'first': first_at if first_ok else None,
                            'last': last_at if last_ok else None},
            'features': {'camera': ''},
            'aspects': {},
            'deterministic': {'confidence': 0.0, 'evidence': evidence},
            'material': {},
        }
        if parts:
            shot['transcript'] = ' '.join(parts)[:600]
            shot['transcript_segments'] = [{'t': s['start'], 'text': s['text']} for s in overlapping][:12]
        if promote:
            # Evaluate a CANDIDATE label. V1/V2 are objective and binding; V3
            # (is AI regeneration right for this shot?) is left to a human.
            candidate = dict(shot)
            candidate['base'] = {**shot['base'], 'label': 'ai_video'}
            decision = gate.evaluate(candidate, source=source_info, route='ai_video',
                                     policy=policy, label_assignment='auto_promote',
                                     shot_count=max(1, len(windows)))
            shot['promotion'] = {
                'decision': decision['decision'],
                'checks': decision['checks'],
                'gate': decision['gate'],
                'missing': decision['missing'],
                'fix_hints': decision['fix_hints'],
                'warnings': decision['warnings'],
                'v3_status': decision['v3_status'],
                'evidence': decision['evidence'],
            }
            if decision['decision'] == 'promoted':
                shot['base'] = {'label': 'ai_video', 'confidence': 0.5,
                                'label_source': 'promotion_gate:auto_candidate'}
        shots.append(shot)

    promoted = sum(1 for s in shots if (s.get('promotion') or {}).get('decision') == 'promoted')
    demoted = sum(1 for s in shots if (s.get('promotion') or {}).get('decision') == 'needs_review')
    return {
        'source': {
            'path': str(video.resolve()),
            'duration': duration,
            'width': meta.get('width'), 'height': meta.get('height'),
            'fps': meta.get('fps'), 'has_audio': bool(meta.get('has_audio')),
        },
        'packaging': {'text': [], 'stickers': []},
        'audio': {'sfx': {'events': []}},
        'shots': shots,
        'provenance': {
            'tool': 'video-recreate/build_breakdown',
            'scene_threshold': threshold,
            'cut_source': cut_source,
            'scene_cuts': cuts,
            'transcripts': [str(p.resolve()) for p in transcripts],
            'min_shot_s': min_shot_s, 'max_shot_s': max_shot_s,
            'effective_max_shot_s': effective_max,
            'forced_splits': sum(1 for _, _, s in windows if s),
            'label_assignment': ('promotion_gate' if promote else
                                 'manual_review_required' if default_label == 'unknown' else
                                 f'assumed:{default_label}'),
            'promotion': {'enabled': promote, 'promoted': promoted, 'demoted': demoted,
                          'policy': policy if promote else None,
                          'note': 'V1/V2 machine-checked; V3 (AI suitability) requires human review'},
            **provenance,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, help='source.json from fetch_source.py')
    ap.add_argument('--video', type=Path, help='Local video file (alternative to --source)')
    ap.add_argument('--out', required=True, type=Path, help='breakdown.json output path')
    ap.add_argument('--frames-dir', type=Path, help='Frame directory (default: <out parent>/frames)')
    ap.add_argument('--scene-threshold', type=float, default=0.32)
    ap.add_argument('--cuts',
                    help='Reviewed shot boundaries in seconds (e.g. "4.0,13.5"); '
                         'overrides automatic scene detection. Use after inspecting frames '
                         'when the video uses dissolves, fades or heavy camera motion.')
    ap.add_argument('--min-shot-s', type=float, default=0.6)
    ap.add_argument('--max-shot-s', type=float, default=10.0,
                    help='Max window length in seconds; long uncut takes are split into '
                         'recreatable segments. Automatically tightened to the model '
                         'ceiling when --gate-config is supplied.')
    ap.add_argument('--transcript', action='append', type=Path, default=[],
                    help='Repeatable: .srt/.vtt/.jsonl/.json timed transcript')
    ap.add_argument('--default-label', default='unknown',
                    help='base.label for every shot; unknown keeps all shots in manual_review')
    ap.add_argument('--gate-config', type=Path,
                    help='video-api config: reuse its duration/ratio limits when promoting shots')
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--promote', action='store_true',
                       help='Auto-label objectively eligible shots as ai_video CANDIDATES; '
                            'the rest stay unknown/manual_review with reasons')
    group.add_argument('--assume-ai-video', action='store_true',
                       help='LEGACY blunt switch: label every shot ai_video without checking evidence')
    args = ap.parse_args()
    try:
        source_meta, transcripts, provenance = None, list(args.transcript), {}
        if args.source:
            package = json.loads(args.source.read_text(encoding='utf-8-sig'))
            video = Path(package['video'])
            source_meta = package.get('probe')
            provenance = {'url': package.get('url', ''), 'platform': package.get('platform', ''),
                          'source_package': str(args.source.resolve())}
            transcripts = [Path(p) for p in package.get('subtitles', [])] + transcripts
        elif args.video:
            video = args.video
        else:
            raise RuntimeError('Pass --source source.json or --video <file>')
        video = video.expanduser().resolve()
        if not video.is_file():
            raise RuntimeError(f'Video not found: {video}')
        label = 'ai_video' if args.assume_ai_video else args.default_label
        gate_config = None
        if args.gate_config:
            gate_config = json.loads(args.gate_config.read_text(encoding='utf-8-sig'))
        out_path = args.out.expanduser().resolve()
        frames_dir = (args.frames_dir or out_path.parent / 'frames').expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        breakdown = build(video, out_path, frames_dir, threshold=args.scene_threshold,
                          min_shot_s=args.min_shot_s, max_shot_s=args.max_shot_s,
                          transcripts=transcripts, default_label=label,
                          source_meta=source_meta, provenance=provenance,
                          promote=args.promote, gate_config=gate_config,
                          manual_cuts=parse_cut_points(args.cuts) if args.cuts else None)
        save(out_path, breakdown)
        with_frames = sum(1 for s in breakdown['shots'] if s['assets'].get('first_frame'))
        with_text = sum(1 for s in breakdown['shots'] if s.get('transcript'))
        gate_info = breakdown['provenance']['promotion']
        prov = breakdown['provenance']
        print(json.dumps({'out': str(out_path), 'shots': len(breakdown['shots']),
                          'scene_cuts': len(prov['scene_cuts']),
                          'cut_source': prov.get('cut_source', 'scene_detection'),
                          'forced_splits': prov.get('forced_splits', 0),
                          'effective_max_shot_s': prov.get('effective_max_shot_s'),
                          'with_frames': with_frames,
                          'with_last_frames': sum(1 for s in breakdown['shots']
                                                  if s['assets'].get('last_frame')),
                          'with_transcript': with_text,
                          'default_label': label,
                          'promoted': gate_info['promoted'], 'demoted': gate_info['demoted'],
                          'review_required': label == 'unknown' and not args.promote}, ensure_ascii=False))
        if prov.get('forced_splits') and not prov.get('scene_cuts'):
            print(f'No hard cuts detected in this '
                  f'{breakdown["source"]["duration"]:.1f}s video; '
                  f'{prov["forced_splits"]} window(s) were split at '
                  f'{prov["effective_max_shot_s"]:.1f}s so each shot is recreatable. '
                  'Review the forced boundaries - they are NOT real scene changes; '
                  'use --cuts "4.0,13.5" with reviewed boundaries for exact shots.',
                  file=sys.stderr)
        if args.promote:
            print(f'Promotion gate: {gate_info["promoted"]} shot(s) auto-labelled ai_video CANDIDATE, '
                  f'{gate_info["demoted"]} kept as manual_review with reasons in shots[].promotion. '
                  'V1/V2 evidence was checked; AI suitability (V3) is NOT verified. '
                  'Review the candidates before any paid job.', file=sys.stderr)
        elif label == 'ai_video':
            print('WARNING: --assume-ai-video labels every shot as ai_video WITHOUT evidence checks. '
                  'Prefer --promote. Real footage, 3D, presenters and screen recordings are NOT '
                  'suitable for AI regeneration. Review and relabel breakdown.json before paid jobs.',
                  file=sys.stderr)
        elif label == 'unknown':
            print('All shots are manual_review by default. Use --promote for evidence-based '
                  'auto-labelling, or assign base.label per shot '
                  '(see references/breakdown-schema.md).', file=sys.stderr)
        return 0
    except (RuntimeError, OSError, KeyError, ValueError, subprocess.SubprocessError,
            json.JSONDecodeError) as exc:
        print(f'Visual breakdown failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
