#!/usr/bin/env python3
"""Stage 0a: turn a video link or local file into a probed source package.

This is an intake adapter, not a platform scraper. It delegates network work to
yt-dlp (or a user-supplied external downloader command). It never automates a
messaging client, never stores cookies, and never persists credentials.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlparse

from video_api import check_url, save


PLATFORMS = (
    (("weixin.qq.com", "channels.weixin.qq.com", "video.weixin.qq.com", "finder.video.qq.com"), "wechat_channels"),
    (("douyin.com", "iesdouyin.com"), "douyin"),
    (("bilibili.com", "b23.tv"), "bilibili"),
    (("youtube.com", "youtu.be"), "youtube"),
    (("xiaohongshu.com", "xhslink.com"), "xiaohongshu"),
    (("twitter.com", "x.com"), "x"),
    (("tiktok.com",), "tiktok"),
)

# Domains we refuse to hand to a generic downloader without an explicit adapter.
REQUIRES_ADAPTER = {"wechat_channels"}

SUBTITLE_SUFFIXES = ('.srt', '.vtt', '.ass', '.ssa', '.json3', '.lrc')
VIDEO_SUFFIXES = ('.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi')


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or '').lower()
    for domains, name in PLATFORMS:
        if any(host == d or host.endswith('.' + d) for d in domains):
            return name
    return 'generic'


def probe(path: Path) -> dict:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('ffprobe not found in PATH; install ffmpeg first')
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type,width,height,r_frame_rate',
         '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=120, check=True)
    data = json.loads(result.stdout)
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    has_audio = any(s.get('codec_type') == 'audio' for s in streams)
    fps = 0.0
    raw = str(video.get('r_frame_rate') or '0/1')
    try:
        num, _, den = raw.partition('/')
        fps = round(float(num) / float(den or 1), 3)
    except ValueError:
        pass
    try:
        duration = float(data.get('format', {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        'duration': round(duration, 3),
        'width': int(video.get('width') or 0),
        'height': int(video.get('height') or 0),
        'fps': fps,
        'has_audio': has_audio,
    }


def resolve_downloader(args, platform: str) -> tuple[str, list[str], bool]:
    """Return (description, argv, dir_mode).

    No shell=True, so URLs cannot inject commands. dir_mode is True when the
    template uses {out_dir}: those tools own the whole output directory and put
    media in a provider-specific subfolder (e.g. video-downloader).
    """
    template = args.downloader or os.environ.get('VIDEO_RECREATE_DOWNLOADER') or ''
    scope = getattr(args, 'downloader_scope', 'all')
    usable_template = template and (scope == 'all' or platform in REQUIRES_ADAPTER)
    if usable_template:
        # Windows paths use backslashes, which posix shlex would eat as escapes.
        tokens = shlex.split(template.replace('\\', '/'))
        # Validate the raw template, not the token list: a placeholder can be
        # embedded in a token (--output={out_dir}) and would be missed there.
        if '{url}' not in template:
            raise RuntimeError('Downloader template must contain the {url} placeholder')
        if '{out}' not in template and '{out_dir}' not in template:
            raise RuntimeError(
                'Downloader template must contain {out} (a single .mp4 path) '
                'or {out_dir} (an output root directory)')
        out_file = str(args.out_dir / 'source.mp4')
        out_root = str(args.out_dir)
        argv = [token.replace('{url}', args.source)
                     .replace('{out_dir}', out_root)
                     .replace('{out}', out_file) for token in tokens]
        return 'external', argv, '{out_dir}' in template
    if platform in REQUIRES_ADAPTER:
        raise RuntimeError(
            'WeChat Channels links need an authorized external downloader. '
            'This skill never automates WeChat or stores cookies. Either pass '
            '--source <local.mp4> after saving the video yourself, or set '
            '--downloader / VIDEO_RECREATE_DOWNLOADER to an authorized tool. '
            'kangarooking/video-downloader accepts WX_CHANNELS_API_URL + '
            'WX_CHANNELS_API_TOKEN, WX_CHANNELS_COOKIE, or an interactive '
            'Yuanbao login; see references/source-intake.md for the exact command.')
    yt_dlp = shutil.which('yt-dlp')
    if not yt_dlp:
        raise RuntimeError('yt-dlp not found in PATH; install it or pass --downloader / a local file')
    argv = [yt_dlp, '--no-playlist', '--no-progress',
            '-f', 'bv*+ba/b', '--merge-output-format', 'mp4',
            '-o', str(args.out_dir / 'source.%(ext)s')]
    if not args.no_subtitles:
        argv += ['--write-subs', '--write-auto-subs', '--sub-langs', 'all',
                 '--sub-format', 'srt/vtt/best']
    argv += ['--write-info-json']
    if args.ytdlp_args:
        argv += shlex.split(args.ytdlp_args)  # never persisted into source.json
    argv.append(args.source)
    return 'yt-dlp', argv, False


def find_outputs(out_dir: Path) -> tuple[Path | None, list[str]]:
    videos, subtitles = [], []
    for p in sorted(out_dir.glob('source.*')):
        name = p.name.lower()
        if name.endswith(SUBTITLE_SUFFIXES) or name.endswith(('.json', '.part', '.ytdl', '.tmp')):
            if name.endswith(SUBTITLE_SUFFIXES):
                subtitles.append(str(p.resolve()))
            continue
        videos.append(p)
    return (videos[0].resolve() if videos else None), subtitles


def stdout_video_path(stdout: str) -> str | None:
    """Read video_path from a downloader's JSON result without persisting stdout.

    Downloader output may contain signed URLs or tokens, so only this one field
    is read and it is never written into source.json.
    """
    text = (stdout or '').strip()
    if '{' not in text:
        return None
    lines = text.splitlines()
    candidates = [text]
    candidates += ['\n'.join(lines[i:]) for i, line in enumerate(lines)
                   if line.lstrip().startswith('{')][:5]
    for raw in candidates[:6]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            value = data.get('video_path') or data.get('video')
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def is_within(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (AttributeError, ValueError):
        return str(path).lower().startswith(str(root).lower())


def collect_external_outputs(out_dir: Path, stdout: str,
                             dir_mode: bool) -> tuple[Path | None, list[str], list[str]]:
    """Return (video, subtitles, warnings) for an external downloader."""
    warnings: list[str] = []
    video = None
    if dir_mode:
        claimed = stdout_video_path(stdout)
        if claimed:
            candidate = Path(claimed).expanduser().resolve()
            if candidate.is_file() and candidate.suffix.lower() in VIDEO_SUFFIXES and is_within(candidate, out_dir):
                video = candidate
            else:
                warnings.append(
                    'Downloader reported a video_path outside --out-dir or a missing file; '
                    'only media placed inside --out-dir is accepted')
        if video is None:
            found = sorted(
                (p for p in out_dir.rglob('*')
                 if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
                 and not p.name.lower().endswith(('.part', '.ytdl'))),
                key=lambda p: (p.stat().st_size, str(p)), reverse=True)
            video = found[0].resolve() if found else None
        subtitles = [str(p.resolve()) for p in out_dir.rglob('*')
                     if p.is_file() and p.suffix.lower() in SUBTITLE_SUFFIXES]
    else:
        video = (out_dir / 'source.mp4').resolve()
        subtitles = [str(p.resolve()) for p in out_dir.iterdir()
                     if p.name.lower().startswith('source.')
                     and p.name.lower().endswith(SUBTITLE_SUFFIXES)]
    return video, sorted(set(subtitles)), warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', required=True, help='HTTPS video URL or local video file path')
    ap.add_argument('--out-dir', required=True, type=Path)
    ap.add_argument('--downloader', help='External command template with {url} and {out} or {out_dir}')
    scope_default = os.environ.get('VIDEO_RECREATE_DOWNLOADER_SCOPE', 'all')
    if scope_default not in ('all', 'adapter-only'):
        scope_default = 'all'
    ap.add_argument('--downloader-scope', choices=('all', 'adapter-only'),
                    default=scope_default,
                    help='all: use the external template for every URL (default); '
                         'adapter-only: use it only for platforms that require an adapter, '
                         'leaving the rest to yt-dlp')
    ap.add_argument('--ytdlp-args', help='Extra yt-dlp flags (never written to source.json)')
    ap.add_argument('--no-subtitles', action='store_true')
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--dry-run', action='store_true', help='Print the planned command only, no network')
    args = ap.parse_args()

    try:
        out_dir = args.out_dir.resolve()
        args.out_dir = out_dir  # resolve_downloader embeds this into the {out} placeholder
        local = Path(args.source).expanduser()
        is_local = local.is_file()
        platform = 'local' if is_local else detect_platform(args.source)
        if not is_local:
            check_url(args.source)  # HTTPS only, no embedded credentials

        if args.dry_run:
            dir_mode = False
            if is_local:
                plan = ['<local file, no network>']
                downloader = 'local'
            else:
                downloader, plan, dir_mode = resolve_downloader(args, platform)
            print(json.dumps({'platform': platform, 'kind': 'local' if is_local else 'url',
                              'downloader': downloader,
                              'output_mode': 'dir' if dir_mode else 'file',
                              'network_calls': 0 if is_local else 1,
                              'command': plan}, ensure_ascii=False))
            return 0

        out_dir.mkdir(parents=True, exist_ok=True)
        extra_warnings: list[str] = []
        if is_local:
            video = local.resolve()
            downloader, subtitles = 'local', []
        else:
            downloader, command, dir_mode = resolve_downloader(args, platform)
            completed = subprocess.run(command, capture_output=True, text=True,
                                       errors='replace', timeout=args.timeout, cwd=out_dir)
            if completed.returncode != 0:
                # Provider output may contain signed URLs or tokens. Never persist it.
                raise RuntimeError(f'Downloader failed with exit code {completed.returncode}; '
                                   'rerun with --dry-run to inspect the command, or pass a local file')
            if downloader == 'external':
                video, subtitles, extra_warnings = collect_external_outputs(
                    out_dir, completed.stdout, dir_mode)
                if video is None and dir_mode:
                    raise RuntimeError(
                        'External downloader produced no video inside --out-dir. '
                        'Pass --asr none --vision none for video-downloader, and make sure '
                        'it downloads the media (not --metadata-only).')
            else:
                video, subtitles = find_outputs(out_dir)
        if video is None or not Path(video).is_file():
            raise RuntimeError('Downloader produced no source video; check --dry-run and the output directory')
        video = Path(video)
        meta = probe(video)
        package = {
            'schema_version': '1.0',
            'input': str(args.source),
            'kind': 'local' if is_local else 'url',
            'platform': platform,
            'url': '' if is_local else args.source,
            'video': str(video),
            'downloader': downloader,
            'subtitles': subtitles,
            'probe': meta,
            'warnings': extra_warnings,
        }
        if meta['duration'] <= 0:
            raise RuntimeError('Source video has no measurable duration')
        if not is_local:
            package['warnings'].append('Confirm you own or are authorized to reproduce this video')
        if platform == 'wechat_channels':
            package['warnings'].append('WeChat Channels support is experimental and adapter-dependent')
        save(out_dir / 'source.json', package)
        print(json.dumps({'source': str(out_dir / 'source.json'), 'platform': platform,
                          'downloader': downloader, 'probe': meta,
                          'subtitles': len(subtitles)}, ensure_ascii=False))
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f'Source intake failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
