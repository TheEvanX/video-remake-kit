#!/usr/bin/env python3
"""Create a portable video API config without storing a secret.

Non-interactive by default: --base-url and --model are required. On Windows a
redirected stdin can still report isatty() == True, so terminal detection is
not used; prompts are opt-in via --interactive instead.
"""
import argparse
from pathlib import Path
from video_api import check_url, read, save


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--preset', choices=['ark', 'generic'], default='ark')
    ap.add_argument('--base-url')
    ap.add_argument('--model')
    ap.add_argument('--interactive', action='store_true',
                    help='Prompt for missing --base-url / --model in a terminal')
    ap.add_argument('--out', type=Path, default=Path('video-api.local.json'))
    args = ap.parse_args()
    if args.out.exists():
        ap.error('Config exists; edit it directly or select another --out path')
    config = read(Path(__file__).resolve().parents[1] / 'assets' / f'video-api.{args.preset}.example.json')

    missing = [flag for flag, value in (('--base-url', args.base_url), ('--model', args.model)) if not value]
    if missing and not args.interactive:
        ap.error('Pass ' + ' and '.join(missing) + ' explicitly, or use --interactive in a terminal')

    base_url = args.base_url
    if not base_url:
        base_url = input(f"API base URL [{config['base_url']}]: ").strip() or config['base_url']
    model = args.model or input('Model / endpoint ID: ').strip()
    config['base_url'] = check_url(base_url)
    config['model'] = model
    if not config['model'] or config['model'].startswith('YOUR_'):
        ap.error('A real model / endpoint ID is required')
    save(args.out, config)
    print(f'Config created: {args.out}. API Key will be requested privately at execution time.')


if __name__ == '__main__':
    main()
