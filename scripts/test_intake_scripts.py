"""Offline Stage 0 tests. Builds a synthetic 3-scene video with ffmpeg; never contacts a network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import build_breakdown as breakdown_mod
import fetch_source as source_mod
import video_api as api

HERE = Path(__file__).resolve().parent


def run(script: str, *args: str, cwd: Path | None = None, env: dict | None = None):
    return subprocess.run([sys.executable, str(HERE / script), *map(str, args)],
                          cwd=cwd or HERE, env=env, capture_output=True, text=True, timeout=240)


class Stage0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / 'synthetic.mp4'
        command = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=red:s=320x568:r=24:d=1.2',
            '-f', 'lavfi', '-i', 'color=c=green:s=320x568:r=24:d=1.2',
            '-f', 'lavfi', '-i', 'color=c=blue:s=320x568:r=24:d=1.2',
            '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
            '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.video),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        cls.srt = cls.root / 'captions.srt'
        cls.srt.write_text(
            '1\n00:00:00,200 --> 00:00:01,000\n第一镜口播内容\n\n'
            '2\n00:00:01,400 --> 00:00:02,200\n第二镜口播内容\n\n'
            '3\n00:00:02,600 --> 00:00:03,400\n第三镜口播内容\n',
            encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_platform_detection(self):
        self.assertEqual(source_mod.detect_platform('https://weixin.qq.com/sph/ABC'), 'wechat_channels')
        self.assertEqual(source_mod.detect_platform('https://www.douyin.com/video/123'), 'douyin')
        self.assertEqual(source_mod.detect_platform('https://b23.tv/abc'), 'bilibili')
        self.assertEqual(source_mod.detect_platform('https://youtu.be/abc'), 'youtube')
        self.assertEqual(source_mod.detect_platform('https://www.xiaohongshu.com/explore/1'), 'xiaohongshu')
        self.assertEqual(source_mod.detect_platform('https://example.com/v'), 'generic')

    def test_probe(self):
        info = source_mod.probe(self.video)
        self.assertAlmostEqual(info['duration'], 3.6, delta=0.2)
        self.assertEqual((info['width'], info['height']), (320, 568))
        self.assertEqual(info['fps'], 24)
        self.assertFalse(info['has_audio'])

    def test_scene_detection_and_windows(self):
        cuts = breakdown_mod.scene_timestamps(self.video, 0.2)
        self.assertGreaterEqual(len(cuts), 1)
        self.assertLessEqual(len(cuts), 4)
        windows = breakdown_mod.make_windows([2.0], 10.0, 0.6, 30.0)
        self.assertEqual(windows, [(0.0, 2.0, False), (2.0, 10.0, False)])
        split = breakdown_mod.make_windows([], 10.0, 0.6, 4.0)
        self.assertEqual(len(split), 3)
        self.assertTrue(all(flag for _, _, flag in split))
        merged = breakdown_mod.make_windows([0.2], 5.0, 0.6, 30.0)
        self.assertEqual(merged, [(0.0, 5.0, False)])

    def test_transcript_parsers(self):
        srt = breakdown_mod.parse_transcript(self.srt)
        self.assertEqual(len(srt), 3)
        self.assertAlmostEqual(srt[0]['start'], 0.2)
        self.assertEqual(srt[1]['text'], '第二镜口播内容')

        vtt = self.root / 'captions.vtt'
        vtt.write_text('WEBVTT\n\n00:00:00.100 --> 00:00:01.100\n字幕A\n\n'
                       '00:00:01.500 --> 00:00:02.500\n字幕B\n', encoding='utf-8')
        self.assertEqual(len(breakdown_mod.parse_transcript(vtt)), 2)

        jsonl = self.root / 'transcript.segments.jsonl'
        jsonl.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in [
            {'start_ms': 0, 'end_ms': 1200, 'text': 'ASR第一块'},
            {'start_ms': 1200, 'end_ms': 2400, 'text': 'ASR第二块'},
        ]), encoding='utf-8')
        parsed = breakdown_mod.parse_transcript(jsonl)
        self.assertEqual(parsed[0]['start'], 0.0)
        self.assertEqual(parsed[1]['text'], 'ASR第二块')

        platform = self.root / 'platform_subtitles.json'
        platform.write_text(json.dumps({'status': 'done', 'segments': [
            {'start': 0.1, 'end': 1.1, 'text': '平台字幕'},
        ]}, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(breakdown_mod.parse_transcript(platform)[0]['text'], '平台字幕')

    def test_fetch_source_local_file(self):
        out = self.root / 'source_local'
        result = run('fetch_source.py', '--source', self.video, '--out-dir', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        package = json.loads((out / 'source.json').read_text(encoding='utf-8'))
        self.assertEqual(package['platform'], 'local')
        self.assertEqual(package['downloader'], 'local')
        self.assertEqual(package['kind'], 'local')
        self.assertGreater(package['probe']['duration'], 3)

    def test_fetch_source_dry_run_makes_no_network_call(self):
        out = self.root / 'source_dry'
        result = run('fetch_source.py', '--source', 'https://www.bilibili.com/video/BV1xx',
                     '--out-dir', out, '--dry-run',
                     '--downloader', f'{sys.executable} -c "pass" {{url}} {{out}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['network_calls'], 1)
        self.assertTrue(Path(plan['command'][-1]).is_absolute(), plan['command'])
        self.assertFalse(out.exists())

    def test_wechat_requires_external_adapter(self):
        env = dict(os.environ)
        env.pop('VIDEO_RECREATE_DOWNLOADER', None)
        result = run('fetch_source.py', '--source', 'https://weixin.qq.com/sph/ABC',
                     '--out-dir', self.root / 'source_wx', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WeChat Channels', result.stderr)

    def test_external_adapter_contract(self):
        adapter = self.root / 'adapter.py'
        adapter.write_text(
            'import shutil, sys\n'
            'from pathlib import Path\n'
            f'shutil.copy(r"{self.video}", sys.argv[2])\n'
            'Path(sys.argv[2]).parent.joinpath("source.zh.srt").write_text(\n'
            f'    Path(r"{self.srt}").read_text(encoding="utf-8"), encoding="utf-8")\n',
            encoding='utf-8')
        out = self.root / 'source_external'
        out.mkdir(parents=True, exist_ok=True)
        template = f'{sys.executable} {adapter} {{url}} {{out}}'
        result = run('fetch_source.py', '--source', 'https://example.com/video',
                     '--out-dir', out, '--downloader', template)
        self.assertEqual(result.returncode, 0, result.stderr)
        package = json.loads((out / 'source.json').read_text(encoding='utf-8'))
        self.assertEqual(package['downloader'], 'external')
        self.assertEqual(len(package['subtitles']), 1)
        self.assertGreater(package['probe']['duration'], 3)

    def test_downloader_template_requires_both_placeholders(self):
        out = self.root / 'source_bad_template'
        for template, marker in (
            (f'{sys.executable} -c "pass" {{url}}', '{out}'),
            (f'{sys.executable} -c "pass" {{out}}', '{url}'),
        ):
            result = run('fetch_source.py', '--source', 'https://example.com/video',
                         '--out-dir', out, '--dry-run', '--downloader', template)
            self.assertNotEqual(result.returncode, 0, template)
            self.assertIn('must contain', result.stderr)
            self.assertIn(marker, result.stderr)

    def test_embedded_out_dir_placeholder_is_accepted(self):
        out = self.root / 'source_embedded'
        template = f'{sys.executable} -c "pass" {{url}} --output={{out_dir}}'
        result = run('fetch_source.py', '--source', 'https://weixin.qq.com/sph/ABC',
                     '--out-dir', out, '--dry-run', '--downloader', template)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['output_mode'], 'dir')
        self.assertTrue(any(tok.startswith('--output=') for tok in plan['command']),
                        plan['command'])

    def test_adapter_only_scope_keeps_other_platforms_on_yt_dlp(self):
        out = self.root / 'source_scope'
        template = f'{sys.executable} -c "pass" {{url}} {{out_dir}}'
        env = dict(os.environ)
        env.pop('VIDEO_RECREATE_DOWNLOADER_SCOPE', None)
        common = ['--out-dir', out, '--dry-run', '--downloader', template,
                  '--downloader-scope', 'adapter-only']
        wx = run('fetch_source.py', '--source', 'https://weixin.qq.com/sph/ABC',
                 *common, env=env)
        self.assertEqual(wx.returncode, 0, wx.stderr)
        self.assertEqual(json.loads(wx.stdout)['downloader'], 'external')
        other = run('fetch_source.py', '--source', 'https://www.bilibili.com/video/BV1xx',
                    *common, env=env)
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertEqual(json.loads(other.stdout)['downloader'], 'yt-dlp')
        self.assertFalse(out.exists())

    def test_stdout_video_path_ignores_log_prefix_and_nested_fields(self):
        payload = json.dumps({'video_path': 'X', 'video': {'probe': 'nested'}})
        self.assertEqual(source_mod.stdout_video_path(payload), 'X')
        self.assertEqual(source_mod.stdout_video_path('[1.0s] downloading\n' + payload), 'X')
        self.assertIsNone(source_mod.stdout_video_path('plain text output'))

    def test_dir_mode_adapter_matches_video_downloader_contract(self):
        """video-downloader writes <out>/<provider-folder>/*.mp4 and prints a JSON result."""
        adapter = self.root / 'dir_adapter.py'
        adapter.write_text(
            'import json, shutil, sys\n'
            'from pathlib import Path\n'
            'url, out_dir = sys.argv[1], Path(sys.argv[2])\n'
            'folder = out_dir / "wechat-channels-ABC"\n'
            'folder.mkdir(parents=True, exist_ok=True)\n'
            'dest = folder / "clip.mp4"\n'
            f'shutil.copy(r"{self.video}", dest)\n'
            '(folder / "clip.zh.srt").write_text("captions", encoding="utf-8")\n'
            'print(json.dumps({"video_path": str(dest), '
            '"signed_url": "SECRET_TOKEN_SHOULD_NOT_BE_PERSISTED"}))\n',
            encoding='utf-8')
        out = self.root / 'source_dir_mode'
        template = f'{sys.executable} {adapter} {{url}} {{out_dir}}'
        result = run('fetch_source.py', '--source', 'https://weixin.qq.com/sph/ABC',
                     '--out-dir', out, '--downloader', template)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = (out / 'source.json').read_text(encoding='utf-8')
        package = json.loads(raw)
        self.assertEqual(package['platform'], 'wechat_channels')
        self.assertEqual(package['downloader'], 'external')
        self.assertEqual(Path(package['video']).name, 'clip.mp4')
        self.assertTrue(source_mod.is_within(Path(package['video']), out.resolve()))
        self.assertEqual(len(package['subtitles']), 1)
        self.assertNotIn('SECRET_TOKEN_SHOULD_NOT_BE_PERSISTED', raw)
        self.assertTrue(any('authorized' in w for w in package['warnings']))

    def test_dir_mode_rejects_out_of_tree_video_path_and_scans_fallback(self):
        out = self.root / 'source_out_of_tree'
        out.mkdir(parents=True, exist_ok=True)
        folder = out / 'wechat-channels-ABC'
        folder.mkdir(parents=True, exist_ok=True)
        inside = folder / 'clip.mp4'
        import shutil
        shutil.copy(self.video, inside)
        stdout = json.dumps({'video_path': str(self.video)})
        video, subtitles, warnings = source_mod.collect_external_outputs(
            out.resolve(), stdout, True)
        self.assertEqual(video, inside.resolve())
        self.assertEqual(subtitles, [])
        self.assertTrue(any('outside --out-dir' in w for w in warnings))

    def test_wechat_refusal_names_the_authorization_routes(self):
        env = dict(os.environ)
        env.pop('VIDEO_RECREATE_DOWNLOADER', None)
        result = run('fetch_source.py', '--source', 'https://weixin.qq.com/sph/ABC',
                     '--out-dir', self.root / 'source_wx_routes', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WX_CHANNELS_API_URL', result.stderr)
        self.assertIn('WX_CHANNELS_COOKIE', result.stderr)

    def test_full_offline_chain_link_to_dry_run(self):
        work = self.root / 'chain'
        out = work / 'source'
        result = run('fetch_source.py', '--source', self.video, '--out-dir', out)
        self.assertEqual(result.returncode, 0, result.stderr)

        result = run('build_breakdown.py', '--source', out / 'source.json',
                     '--out', work / 'breakdown.json', '--transcript', self.srt,
                     '--assume-ai-video', '--scene-threshold', '0.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertGreaterEqual(summary['shots'], 2)
        self.assertEqual(summary['shots'], summary['with_frames'])
        self.assertGreaterEqual(summary['with_transcript'], 1)

        breakdown = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        self.assertEqual(breakdown['source']['width'], 320)
        self.assertEqual(breakdown['provenance']['tool'], 'video-recreate/build_breakdown')
        for shot in breakdown['shots']:
            self.assertEqual(shot['base']['label'], 'ai_video')
            for key in ('first_frame', 'last_frame'):
                self.assertTrue((work / shot['assets'][key]).is_file())

        result = run('build_recreation_manifest.py', '--breakdown', work / 'breakdown.json',
                     '--out', work / 'recreation_manifest.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((work / 'recreation_manifest.json').read_text(encoding='utf-8'))
        ai = [s for s in manifest['shots'] if s['route'] == 'ai_video']
        self.assertEqual(len(ai), len(breakdown['shots']))
        self.assertTrue(all(s['prompt_status'] == 'fallback' for s in ai))
        self.assertTrue(any('ASR' in ' '.join(s['review_flags']) for s in ai))
        with_transcript = next(s for s in manifest['shots'] if '口播内容' in s['prompts']['video'])
        self.assertIn('口播内容', with_transcript['prompts']['video'])
        self.assertIn('spoken or subtitle content', with_transcript['prompts']['video_en'])

        result = run('build_recreation_jobs.py', '--manifest', work / 'recreation_manifest.json',
                     '--out', work / 'recreation_jobs.json', '--min-duration', '4')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = api.read(HERE.parent / 'assets/video-api.ark.example.json')
        # 320x568 reduces to 40:71, which no video model accepts; an explicit
        # alias is required rather than silently changing the composition.
        config.update(model='offline-test-model', ratio_aliases={'40:71': '9:16'})
        api.save(work / 'config.json', config)
        result = run('video_api.py', '--config', work / 'config.json',
                     '--jobs', work / 'recreation_jobs.json', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report['network_calls'], 0)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['review_required'], report['shots'])

    def test_default_label_is_manual_review(self):
        work = self.root / 'labels'
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', work / 'breakdown.json', '--scene-threshold', '0.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        breakdown = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        self.assertTrue(all(s['base']['label'] == 'unknown' for s in breakdown['shots']))
        self.assertEqual(breakdown['provenance']['label_assignment'], 'manual_review_required')

    def test_promote_auto_labels_eligible_shots_and_stays_in_manifest(self):
        work = self.root / 'promote'
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', work / 'breakdown.json', '--scene-threshold', '0.2', '--promote')
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertGreater(summary['promoted'], 0)
        self.assertEqual(summary['demoted'], 0)
        data = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        for item in data['shots']:
            self.assertEqual(item['base']['label'], 'ai_video')
            self.assertEqual(item['base']['label_source'], 'promotion_gate:auto_candidate')
            self.assertEqual(item['promotion']['decision'], 'promoted')
            # The gate must never pretend AI suitability was visually verified.
            self.assertEqual(item['promotion']['v3_status'], 'assumed')
            self.assertTrue(any('NOT verified' in w for w in item['promotion']['warnings']))
        self.assertEqual(data['provenance']['label_assignment'], 'promotion_gate')

        result = run('build_recreation_manifest.py', '--breakdown', work / 'breakdown.json',
                     '--out', work / 'manifest.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((work / 'manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(all(s['route'] == 'ai_video' for s in manifest['shots']))
        self.assertEqual(manifest['summary']['promotion']['decisions']['promoted'],
                         len(manifest['shots']))

    def test_promote_respects_provider_limits_and_lists_gaps(self):
        work = self.root / 'promote_limited'
        config = work / 'config.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({'max_duration_s': 1}), encoding='utf-8')
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', work / 'breakdown.json', '--scene-threshold', '0.2',
                     '--promote', '--gate-config', config)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        self.assertTrue(all(s['base']['label'] == 'unknown' for s in data['shots']))
        for item in data['shots']:
            self.assertEqual(item['promotion']['decision'], 'needs_review')
            self.assertTrue(any('V2.duration' in x for x in item['promotion']['missing']))
            self.assertTrue(any('split' in x for x in item['promotion']['fix_hints']))

    def test_manual_cuts_override_scene_detection(self):
        self.assertEqual(breakdown_mod.parse_cut_points('1.2, 2.4 1.2'), [1.2, 2.4])
        with self.assertRaises(RuntimeError):
            breakdown_mod.parse_cut_points('not-a-number')
        work = self.root / 'manual_cuts'
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', work / 'breakdown.json', '--cuts', '1.2,2.4')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        self.assertEqual(data['provenance']['cut_source'], 'manual')
        self.assertEqual(data['provenance']['scene_cuts'], [1.2, 2.4])
        ends = [s['end'] for s in data['shots']]
        self.assertEqual(ends[:2], [1.2, 2.4])
        self.assertAlmostEqual(ends[2], data['source']['duration'], delta=0.05)

    def test_tail_frame_fallback_recovers_last_frame(self):
        work = self.root / 'tail_frame'
        work.mkdir(parents=True, exist_ok=True)
        target = work / 'last.jpg'
        ok, at = breakdown_mod.extract_frame_near(
            self.video, 3.6, target, min_seconds=0.0)
        self.assertTrue(ok)
        self.assertTrue(target.is_file() and target.stat().st_size > 0)
        self.assertLessEqual(at, 3.6)

    def test_auto_split_never_fragments_below_model_min(self):
        work = self.root / 'no_fragments'
        config = work / 'config.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({'max_duration_s': 1, 'min_duration_s': 4}), encoding='utf-8')
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', work / 'breakdown.json', '--promote', '--gate-config', config)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((work / 'breakdown.json').read_text(encoding='utf-8'))
        self.assertEqual(data['provenance']['forced_splits'], 0)
        self.assertTrue(all(s['duration'] >= 0.6 for s in data['shots']))
        self.assertTrue(all(s['base']['label'] == 'unknown' for s in data['shots']))

    def test_promote_and_assume_are_mutually_exclusive(self):
        result = run('build_breakdown.py', '--video', self.video,
                     '--out', self.root / 'x.json', '--promote', '--assume-ai-video')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
