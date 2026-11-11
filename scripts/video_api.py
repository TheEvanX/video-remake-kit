#!/usr/bin/env python3
"""Configurable JSON video API adapter. Standard library only; ffprobe validates downloads."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import getpass
import hashlib
import json
from math import gcd
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ApiError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API credentials through redirects.


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


@contextmanager
def execution_lock(results_path):
    """Prevent concurrent runners from submitting the same billable shots."""
    path = Path(str(results_path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ApiError('Results file is locked by another runner. If a process crashed, stop it and remove the .lock file before resuming') from None
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def field(value, dotted, default=None):
    try:
        for part in dotted.split('.'):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def reduced_ratio(value, default='9:16'):
    """Accept a provider token ('9:16') or a pixel-style pair ('1080:1920')."""
    text = str(value or '').strip()
    if not text:
        return default
    if ':' not in text:
        return text
    left, _, right = text.partition(':')
    try:
        a, b = int(float(left)), int(float(right))
    except ValueError:
        return text
    if a <= 0 or b <= 0:
        raise ApiError(f'Invalid aspect ratio: {text}')
    divisor = gcd(a, b)
    return f'{a // divisor}:{b // divisor}'


def provider_ratio(value, config, default='9:16'):
    """Reject ratios the configured model cannot produce instead of failing at the paid call."""
    text = reduced_ratio(value, default)
    text = (config.get('ratio_aliases') or {}).get(text, text)
    allowed = config.get('supported_ratios') or []
    if allowed and text not in allowed:
        raise ApiError(f"Aspect ratio {text} is not supported by this model; allowed: {', '.join(allowed)}. "
                       "Map it with ratio_aliases, resize the reference frames, or replan the shot")
    return text


def seconds(value):
    """Keep JSON numbers integral: most video APIs reject 5.0 where an int is required."""
    number = float(value)
    return int(number) if number.is_integer() else number


def render(value, variables):
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    if not isinstance(value, str):
        return value
    exact = re.fullmatch(r'\{\{([a-z_]+)\}\}', value)
    if exact:
        return variables[exact.group(1)]
    return re.sub(r'\{\{([a-z_]+)\}\}', lambda m: str(variables[m.group(1)]), value)


def check_url(url):
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ApiError('Do not embed credentials in URLs')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ApiError('Use HTTPS (HTTP allowed only for localhost tests)')
    return url


class Client:
    def __init__(self, config, key):
        self.config, self.key = config, key
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        url = check_url(self.config['base_url'].rstrip('/') + '/' + path.lstrip('/'))
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers[self.config.get('auth_header', 'Authorization')] = self.config.get('auth_prefix', 'Bearer ') + self.key
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            with self.opener.open(Request(url, data=body, headers=headers, method=method), timeout=self.config.get('request_timeout_s', 60)) as response:
                return json.load(response)
        except HTTPError as exc:
            # Provider bodies/URLs can contain credentials or signed URLs. Do not persist them.
            raise ApiError(f'API HTTP {exc.code}; inspect provider console for details') from None
        except Exception as exc:
            raise ApiError(f'API transport/JSON error ({type(exc).__name__}); submission is not automatically retried') from None


def image_ref(path, config, config_dir):
    if not path:
        return None
    aliases = config.get('media_urls', {})
    if path in aliases:
        return check_url(aliases[path])
    if str(path).startswith(('https://', 'http://')):
        return check_url(path)
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = config_dir / p
    if config.get('image_mode', 'data_uri') == 'url':
        raise ApiError('Provider requires hosted images: add a media_urls mapping or HTTPS image URL')
    if not p.is_file():
        raise ApiError('Reference image not found; regenerate jobs on this machine')
    if p.stat().st_size > config.get('max_image_bytes', 10000000):
        raise ApiError('Reference image exceeds configured size limit')
    mime = mimetypes.guess_type(p.name)[0] or ''
    if not mime.startswith('image/'):
        raise ApiError('Reference file must be an image')
    return f'data:{mime};base64,' + base64.b64encode(p.read_bytes()).decode()


def payload_for(job, config, config_dir):
    params = job.get('params', {})
    duration = float(params.get('duration', job.get('trim_to', 0)))
    if not config.get('min_duration_s', 1) <= duration <= config.get('max_duration_s', 30):
        raise ApiError('Duration outside provider limits; split/replan this shot')
    duration = seconds(duration)
    if config['protocol'] == 'ark' and not isinstance(duration, int):
        raise ApiError('Ark requires integer duration in seconds; replan this shot')
    prompt = params.get('prompt')
    if not prompt:
        raise ApiError('Prompt missing')
    first = image_ref(params.get('start_image_path'), config, config_dir)
    last = image_ref(params.get('end_image_path'), config, config_dir)
    if last and not first:
        raise ApiError('End frame requires a start frame')
    if last and not config.get('supports_last_frame', False):
        raise ApiError('This provider/model does not support end frames; replan instead of dropping the frame')
    ratio = provider_ratio(params.get('aspect_ratio'), config)
    variables = {'model': config['model'], 'prompt': prompt, 'duration': duration,
                 'aspect_ratio': ratio, 'first_frame': first, 'last_frame': last}
    if config['protocol'] == 'ark':
        content = [{'type': 'text', 'text': prompt}]
        for role, value in (('first_frame', first), ('last_frame', last)):
            if value:
                content.append({'type': 'image_url', 'image_url': {'url': value}, 'role': role})
        body = dict(config.get('extra_body', {}))
        body.update(model=config['model'], content=content, duration=duration, ratio=ratio)
        return body
    return render(config['request_template'], variables)


def download_video(url, target, config):
    check_url(url)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix('.part')
    try:
        # No API auth header goes to the media host. Redirects are rejected.
        with build_opener(NoRedirect()).open(url, timeout=config.get('request_timeout_s', 60)) as response, part.open('wb') as out:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.get('max_download_bytes', 1073741824):
                    raise ApiError('Video download exceeds size limit')
                out.write(chunk)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type:format=duration', '-of', 'json', str(part)], capture_output=True, check=True)
        info = json.loads(probe.stdout)
        if not any(s.get('codec_type') == 'video' for s in info.get('streams', [])) or float(info.get('format', {}).get('duration', 0)) <= 0:
            raise ApiError('Downloaded file has no valid video stream')
        part.replace(target)
        return str(target.resolve())
    except Exception as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, ApiError):
            raise
        raise ApiError(f'Video download/validation failed ({type(exc).__name__})') from None


def preflight(jobs, config, config_dir):
    """Validate every billable shot before any network call, reporting all problems at once."""
    prepared, errors = [], []
    for job in jobs:
        try:
            prepared.append((job, payload_for(job, config, config_dir)))
        except ApiError as exc:
            errors.append({'shot': job.get('shot'), 'id': job.get('id', ''), 'error': str(exc)})
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({'shot': job.get('shot'), 'id': job.get('id', ''),
                           'error': f'Malformed job/config field ({type(exc).__name__}: {exc})'})
    return prepared, errors


def run(args):
    config = read(args.config)
    if config.get('protocol') not in ('ark', 'generic_json'):
        raise ApiError('Supported protocols: ark, generic_json')
    if not config.get('model') or config['model'].startswith('YOUR_'):
        raise ApiError('Set a model ID supported by your provider account')
    check_url(config['base_url'])
    config_dir = Path(args.config).resolve().parent
    spec = read(args.jobs)
    selected = {int(x) for x in args.only.split(',') if x} if args.only else None
    # Only AI video is automatic; other production routes must not be silently converted.
    jobs = [j for j in spec['jobs'] if j.get('route', j.get('label')) == 'ai_video' and (not selected or j['shot'] in selected)]
    if not jobs:
        raise ApiError('No AI video jobs selected')
    prepared, errors = preflight(jobs, config, config_dir)  # preflight ALL before billing
    if args.dry_run:
        print(json.dumps({'jobs': len(prepared), 'protocol': config['protocol'], 'model': config['model'], 'network_calls': 0,
                          'shots': [j['shot'] for j, _ in prepared], 'review_required': [j['shot'] for j, _ in prepared if j.get('warning')],
                          'errors': errors}, ensure_ascii=False))
        return 1 if errors else 0
    if errors:
        raise ApiError('Preflight failed; nothing was submitted: ' + '; '.join(
            f"shot {e['shot']} ({e['id']}): {e['error']}" for e in errors))
    if any(j.get('warning') for j, _ in prepared) and not args.accept_fallback:
        raise ApiError('Jobs need review. Review prompts first; --accept-fallback permits draft prompts')
    if not args.execute:
        raise ApiError('Use --dry-run or --execute to explicitly select paid execution')
    key = os.environ.get(config.get('api_key_env', 'VIDEO_API_KEY'))
    if not key:
        key = getpass.getpass('Video API Key (hidden, memory only): ')
    if not key:
        raise ApiError('API Key required')
    results_path = Path(args.results or Path(args.jobs).with_name('results.json')).resolve()
    state = read(results_path) if results_path.exists() else {'results': {}}
    client = Client(config, key)
    failures = 0
    for job, body in prepared:
        sid = str(job['shot'])
        fingerprint = hashlib.sha256(json.dumps({'base': config['base_url'], 'protocol': config['protocol'], 'body': body}, sort_keys=True).encode()).hexdigest()
        record = state['results'].get(sid, {})
        if record.get('fingerprint') and record['fingerprint'] != fingerprint:
            raise ApiError('Job/config changed since previous submission; use a new results file')
        if record.get('video') and Path(record['video']).is_file():
            print(f'Shot {sid}: already downloaded')
            continue
        if record.get('status') == 'submitting':
            raise ApiError('Previous submission outcome unknown. Check provider console before retrying; attach task_id to results to resume')
        if record.get('status') == 'failed':
            failures += 1
            continue  # Explicit new results file required for a new billable submission.
        record.update(id=job['id'], fingerprint=fingerprint, trim_to=job.get('trim_to'), video=None)
        state['results'][sid] = record
        try:
            response = None
            if not record.get('task_id'):
                record['status'] = 'submitting'
                save(results_path, state)
                response = client.request('POST', config['submit_path'], body)
                task_id = field(response, config.get('task_id_field', 'id'))
                if task_id:
                    record.update(task_id=str(task_id), status='submitted')
                    save(results_path, state)
                    if not field(response, config.get('video_url_field', 'video_url')):
                        response = None
                elif not field(response, config.get('video_url_field', 'video_url')):
                    raise ApiError('Submit returned neither task ID nor video URL; inspect provider console')
            deadline = time.monotonic() + config.get('poll_timeout_s', 1800)
            while True:
                if response is None:
                    path = config['status_path'].replace('{task_id}', quote(record['task_id'], safe=''))
                    response = client.request('GET', path)
                status = str(field(response, config.get('status_field', 'status'), '')).lower()
                if status in config.get('failure_statuses', ['failed', 'cancelled', 'expired']):
                    record['status'] = 'failed'
                    raise ApiError('Provider task failed; check provider console')
                media = field(response, config.get('video_url_field', 'video_url'))
                success = status in config.get('success_statuses', ['succeeded', 'completed', 'success'])
                if media and (success or not status):
                    target = results_path.parent / 'api_videos' / f"shot_{int(sid):03d}.mp4"
                    record['video'] = download_video(media, target, config)
                    record['status'] = 'completed'
                    record.pop('error', None)
                    save(results_path, state)
                    print(f'Shot {sid}: downloaded')
                    break
                if time.monotonic() >= deadline:
                    raise ApiError('Polling timeout; rerun to resume the existing task without resubmission')
                time.sleep(max(0.01, min(30, config.get('poll_interval_s', 5))))
                response = None
        except ApiError as exc:
            failures += 1
            record['error'] = str(exc)
            save(results_path, state)
            print(f'Shot {sid}: {exc}')
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', required=True, type=Path)
    ap.add_argument('--jobs', required=True, type=Path)
    ap.add_argument('--results', type=Path)
    ap.add_argument('--only', help='Comma-separated source shot indices')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--execute', action='store_true')
    ap.add_argument('--accept-fallback', action='store_true')
    args = ap.parse_args()
    try:
        if args.dry_run:
            return run(args)
        results = Path(args.results or args.jobs.with_name('results.json')).resolve()
        with execution_lock(results):
            return run(args)
    except (ApiError, KeyError, ValueError, OSError) as exc:
        print(f'Configuration/execution failed: {str(exc) if isinstance(exc, ApiError) else type(exc).__name__}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
