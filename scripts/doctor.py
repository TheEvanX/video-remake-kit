#!/usr/bin/env python3
"""Offline self-check for the video-recreate environment, config and job package.

Modelled on cangjie-skill's `cangjie.py doctor`: plain-text [ok]/[FAIL]/[warn]
lines plus a single PASS/FAIL verdict. Never contacts a provider, never reads
VIDEO_API_KEY, never submits a billable task.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import video_api as api


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', type=Path, help='video-api.local.json to validate')
    ap.add_argument('--jobs', type=Path, help='recreation_jobs.json to validate')
    ap.add_argument('--source', type=Path, help='source.json from fetch_source.py to validate')
    ap.add_argument('--gate-report', type=Path,
                    help='promotion report from promotion_gate.py or build_recreation_manifest.py --gate-report')
    args = ap.parse_args()

    failures = 0
    warnings = 0

    def ok(message):
        print(f'[ok]   {message}')

    def fail(message, hint=''):
        nonlocal failures
        failures += 1
        print(f'[FAIL] {message}' + (f'  -> {hint}' if hint else ''))

    def warn(message, hint=''):
        nonlocal warnings
        warnings += 1
        print(f'[warn] {message}' + (f'  -> {hint}' if hint else ''))

    if sys.version_info >= (3, 10):
        ok(f'Python {sys.version.split()[0]}')
    else:
        fail(f'Python {sys.version.split()[0]} is too old', 'Python 3.10+ required')

    for binary in ('ffmpeg', 'ffprobe'):
        found = shutil.which(binary)
        if found:
            ok(f'{binary} found: {found}')
        else:
            fail(f'{binary} not found in PATH',
                 'ffprobe is required to validate downloaded videos and cut shots; install ffmpeg (Windows: choco install ffmpeg)')

    ytdlp = shutil.which('yt-dlp')
    if ytdlp:
        ok(f'yt-dlp found: {ytdlp}')
    else:
        warn('yt-dlp not found in PATH',
             'only needed for URL intake; local files and --downloader adapters still work')

    if args.source:
        source_path = args.source
        if not source_path.is_file():
            fail(f'source.json not found: {source_path}', 'run fetch_source.py first')
        else:
            try:
                package = api.read(source_path)
                ok(f'source.json parsed: {source_path}')
                video = Path(package.get('video') or '')
                if video.is_file():
                    ok(f'source video: {video}')
                else:
                    fail(f'source video missing: {video}')
                probe = package.get('probe') or {}
                if float(probe.get('duration') or 0) > 0 and int(probe.get('width') or 0) > 0:
                    ok(f"probe: {probe.get('width')}x{probe.get('height')}, {probe.get('duration')}s")
                else:
                    fail('source probe has no usable duration/resolution')
                for warning in package.get('warnings') or []:
                    warn(warning)
            except (OSError, ValueError, TypeError) as exc:
                fail(f'source.json is not valid JSON: {exc}')

    if args.config:
        config_path = args.config
        if not config_path.is_file():
            fail(f'config not found: {config_path}', 'run configure_video_api.py --preset ark --out video-api.local.json')
            config = None
        else:
            try:
                config = api.read(config_path)
                ok(f'config parsed: {config_path}')
            except (OSError, ValueError) as exc:
                fail(f'config is not valid JSON: {exc}')
                config = None
        if config is not None:
            protocol = config.get('protocol')
            if protocol in ('ark', 'generic_json'):
                ok(f'protocol: {protocol}')
            else:
                fail(f'unknown protocol: {protocol}', 'use ark or generic_json')
            model = config.get('model') or ''
            if model and not model.startswith('YOUR_'):
                ok(f'model: {model}')
            else:
                fail('model is a placeholder', 'set the real model / endpoint ID in the config')
            try:
                api.check_url(config.get('base_url', ''))
                ok(f"base_url: {config['base_url']}")
            except api.ApiError as exc:
                fail(f'base_url: {exc}')
            if protocol == 'generic_json' and not config.get('request_template'):
                fail('generic_json requires request_template')
            extra = config.get('extra_body') or {}
            if protocol == 'ark':
                if extra:
                    warn(f'extra_body is set: {sorted(extra)}',
                         'verify these fields are supported by your model; older models may reject unknown fields')
                if 'generate_audio' not in extra:
                    warn('generate_audio is left at the provider default (often true)',
                         'for an original-audio remake add "generate_audio": false to extra_body if your model supports it')
    elif args.jobs:
        warn('no --config supplied, config checks skipped', 'pass --config video-api.local.json for a full check')

    if args.jobs:
        jobs_path = args.jobs
        if not jobs_path.is_file():
            fail(f'jobs not found: {jobs_path}', 'run build_recreation_jobs.py first')
            spec = None
        else:
            try:
                spec = api.read(jobs_path)
                ok(f'jobs parsed: {jobs_path}')
            except (OSError, ValueError) as exc:
                fail(f'jobs is not valid JSON: {exc}')
                spec = None
        if spec is not None:
            all_jobs = spec.get('jobs') or []
            selected = [j for j in all_jobs if j.get('route', j.get('label')) == 'ai_video']
            if selected:
                ok(f'ai_video shots: {len(selected)} / {len(all_jobs)} total')
            else:
                fail('no ai_video shots in jobs', 'the runner only handles the ai_video route')
            config_dir = (args.config.resolve().parent if args.config and args.config.is_file()
                          else jobs_path.resolve().parent)
            missing_images, missing_prompts, short_shots = [], [], []
            no_last_frame, long_shots = [], []
            for job in selected:
                params = job.get('params') or {}
                for key in ('start_image_path', 'end_image_path'):
                    value = params.get(key)
                    if not value:
                        continue
                    path = Path(value).expanduser()
                    if not path.is_absolute():
                        path = config_dir / path
                    if not path.is_file():
                        missing_images.append(f"shot {job.get('shot')}: {value}")
                if not params.get('prompt'):
                    missing_prompts.append(str(job.get('shot')))
                if params.get('start_image_path') and not params.get('end_image_path'):
                    no_last_frame.append(str(job.get('shot')))
                duration = params.get('duration', 0)
                if args.config and config and duration < config.get('min_duration_s', 1):
                    short_shots.append(str(job.get('shot')))
                if args.config and config and duration > config.get('max_duration_s', 10**9):
                    long_shots.append(f"{job.get('shot')} ({duration}s)")
            if missing_images:
                fail(f'{len(missing_images)} reference image(s) missing', '; '.join(missing_images[:4]))
            else:
                ok('reference images present')
            supports_last = (not args.config or not config
                             or config.get('supports_last_frame', True))
            if no_last_frame and supports_last:
                warn(f'{len(no_last_frame)} ai_video shot(s) have no last frame: '
                     + ', '.join(no_last_frame[:5]),
                     'first-frame-only generation is weakly constrained; re-run '
                     'build_breakdown.py or supply end_image_path')
            if long_shots:
                fail(f'{len(long_shots)} shot(s) exceed max_duration_s: '
                     + ', '.join(long_shots[:5]),
                     'split long shots with build_breakdown.py --max-shot-s before generating')
            if missing_prompts:
                fail('shots without prompt: ' + ', '.join(missing_prompts))
            else:
                ok('every ai_video shot has a prompt')
            if short_shots:
                fail('shots shorter than min_duration_s: ' + ', '.join(short_shots),
                     'rebuild jobs with --min-duration matching the config')

    if args.gate_report:
        if not args.gate_report.is_file():
            fail(f'gate report not found: {args.gate_report}',
                 'run promotion_gate.py --out ... or build_recreation_manifest.py --gate-report ...')
        else:
            try:
                report = api.read(args.gate_report)
                info = report.get('summary') or {}
                decisions = info.get('decisions') or {}
                demoted = info.get('demoted') or []
                assumed = info.get('assumed_suitability') or []
                candidates = info.get('candidates') or []
                enabled = info.get('enabled', True)
                if not enabled:
                    warn('promotion gate was disabled for this manifest',
                         'rebuild without --no-gate before any paid execution')
                promoted = decisions.get('promoted', 0)
                ok(f'promotion gate: {promoted} promoted, {len(demoted)} demoted, '
                   f'{len(candidates)} candidate(s), {len(assumed)} with unverified AI suitability')
                for item in demoted[:3]:
                    reasons = '; '.join((item.get('missing') or [])[:2]) or 'see report'
                    warn(f"demoted {item.get('shot_id', '?')}: {reasons}",
                         '; '.join((item.get('fix_hints') or [])[:1]))
                if len(demoted) > 3:
                    warn(f'... and {len(demoted) - 3} more demoted shot(s)',
                         'see the full gate report')
                if assumed:
                    warn(f'{len(assumed)} shot(s) promoted with v3_status=assumed',
                         'AI suitability is NOT verified; review first/last frames before paying')
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                fail(f'gate report is not valid JSON: {exc}')

    print(f'doctor: {"FAIL" if failures else "PASS"} ({failures} error(s), {warnings} warning(s))')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
