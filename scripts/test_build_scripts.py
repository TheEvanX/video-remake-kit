"""Offline tests for manifest/jobs generation. No network, no API key, no ffmpeg required."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import build_recreation_jobs as jobs_mod
import build_recreation_manifest as manifest_mod


BREAKDOWN = {
    "source": {"path": "source.mp4", "width": 1080, "height": 1920, "fps": 30,
               "duration": 9.6, "has_audio": True},
    "packaging": {"text": [{"id": "txt_01", "t0": 0.5, "t1": 3.5}], "stickers": []},
    "audio": {"sfx": {"events": [{"t": 1.2}, {"t": 6.0}]}},
    "shots": [
        {"id": "shot_001", "start": 0.0, "end": 4.2, "duration": 4.2,
         "base": {"label": "ai_video", "confidence": 0.82},
         "assets": {"first_frame": "frames/first.jpg", "last_frame": "frames/last.jpg"},
         "features": {"camera": "slow push in"},
         "deterministic": {"confidence": 0.8, "evidence": ["frame diff"]}},
        {"id": "shot_002", "start": 4.2, "end": 6.2, "duration": 2.0,
         "base": {"label": "live_action"}, "assets": {},
         "review": {"replication": {"video_prompt": "真人手持产品特写，暖光"}}},
        {"id": "shot_003", "start": 6.2, "end": 8.0, "duration": 1.8,
         "base": {"label": "static_hold"}, "assets": {}},
        {"id": "shot_004", "start": 8.0, "end": 9.6, "duration": 1.6,
         "base": {"label": "something_weird"}, "assets": {}},
    ],
}


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.breakdown_path = self.root / 'breakdown.json'
        self.breakdown_path.write_text(json.dumps(BREAKDOWN, ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_source_ratio(self):
        self.assertEqual(manifest_mod.source_ratio({"width": 1080, "height": 1920}), ("9:16", "竖屏", "vertical"))
        self.assertEqual(manifest_mod.source_ratio({"width": 1920, "height": 1080}), ("16:9", "横屏", "horizontal"))
        self.assertEqual(manifest_mod.source_ratio({"width": 1080, "height": 1080}), ("1:1", "方形", "square"))
        self.assertEqual(manifest_mod.source_ratio({}), ("9:16", "竖屏", "vertical"))

    def test_motion_phrases_are_bilingual_but_not_mixed(self):
        self.assertIn('推镜', manifest_mod.motion_phrases('slow push in')[0])
        self.assertIn('固定', manifest_mod.motion_phrases('静止')[0])
        self.assertIn('手持', manifest_mod.motion_phrases('handheld')[0])
        zh, en = manifest_mod.motion_phrases('unclassifiable motion')
        self.assertNotIn('the ', zh)
        self.assertNotIn('参考', en)

    def test_fallback_prompt_quality(self):
        shot = BREAKDOWN['shots'][0]
        prompts = manifest_mod.fallback_prompts(shot, 'ai_video', BREAKDOWN['source'], True, True)
        video, video_en = prompts['video'], prompts['video_en']
        for phrase in ('the measured camera motion', 'lighting and contrast matched', 'Create a', '4.200'):
            self.assertNotIn(phrase, video)
        self.assertIn('9:16', video)
        self.assertNotIn('1080:1920', video)
        self.assertTrue(video_en.startswith('Create a 9:16 vertical shot'))
        self.assertNotIn('1080:1920', video_en)
        self.assertNotIn('. lighting', video_en)
        no_frames = manifest_mod.fallback_prompts(shot, 'ai_video', BREAKDOWN['source'], False, False)
        self.assertIn('参考画面', no_frames['video'])
        self.assertNotIn('first frame', no_frames['video_en'])

    def test_manifest_routes_and_cross_references(self):
        manifest = manifest_mod.build(BREAKDOWN, self.breakdown_path)
        shots = {s['id']: s for s in manifest['shots']}
        self.assertEqual([shots[f'shot_00{i}']['route'] for i in range(1, 5)],
                         ['ai_video', 'film', 'still_motion', 'manual_review'])
        self.assertEqual(shots['shot_001']['prompt_status'], 'fallback')
        self.assertIn('fallback prompt', ' '.join(shots['shot_001']['review_flags']))
        self.assertEqual(shots['shot_002']['prompt_status'], 'reviewed')
        self.assertEqual(shots['shot_001']['constraints']['aspect_ratio'], '9:16')
        self.assertEqual(shots['shot_001']['packaging_ids'], ['txt_01'])
        self.assertEqual(shots['shot_001']['audio_ids'], ['sfx_1.200'])
        self.assertEqual(shots['shot_002']['audio_ids'], ['sfx_6.000'])
        self.assertEqual(manifest['summary']['prompt_status'], {'reviewed': 1, 'fallback': 3})
        self.assertEqual(manifest['summary']['unresolved'][0]['shot_id'], 'shot_004')
        self.assertTrue(Path(shots['shot_001']['generation']['start_image']).is_absolute())


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = manifest_mod.build(BREAKDOWN, self.root / 'breakdown.json')

    def tearDown(self):
        self.temp.cleanup()

    def test_ratio_helper(self):
        self.assertEqual(jobs_mod.ratio(1080, 1920), '9:16')
        self.assertEqual(jobs_mod.ratio(1920, 1080), '16:9')
        self.assertEqual(jobs_mod.ratio(None, None), '9:16')

    def test_ai_jobs_and_no_dead_fields(self):
        spec = jobs_mod.build(self.manifest, self.root / 'remake/recreation_jobs.json', 'seedance2.5')
        ai = [j for j in spec['jobs'] if j['route'] == 'ai_video']
        self.assertEqual(len(ai), 1)
        self.assertEqual(ai[0]['executor'], 'video_api')
        self.assertNotIn('output_dir', ai[0]['params'])
        self.assertNotIn('run_dir', spec)
        self.assertEqual(ai[0]['params']['duration'], 5)  # ceil(4.2), above min 4
        self.assertEqual(ai[0]['params']['aspect_ratio'], '9:16')
        self.assertEqual(ai[0]['params']['generation_type'], 'first_last_frame')
        self.assertEqual(ai[0]['trim_to'], 4.2)

    def test_min_duration_override(self):
        spec = jobs_mod.build(self.manifest, self.root / 'j.json', 'seedance2.5', min_duration=6)
        ai = [j for j in spec['jobs'] if j['route'] == 'ai_video'][0]
        self.assertEqual(ai['params']['duration'], 6)

    def test_non_ai_routes_keep_reviewed_prompt(self):
        spec = jobs_mod.build(self.manifest, self.root / 'j.json', 'seedance2.5')
        by_id = {j['id']: j for j in spec['jobs']}
        self.assertEqual(by_id['shot_002']['executor'], 'manual_review')
        self.assertEqual(by_id['shot_002']['params']['review_prompt'], '真人手持产品特写，暖光')
        self.assertEqual(by_id['shot_003']['executor'], 'local_ffmpeg')
        self.assertEqual(by_id['shot_003']['params']['duration'], 1.8)
        self.assertEqual(by_id['shot_004']['executor'], 'manual_review')


class ConfigureTests(unittest.TestCase):
    def test_missing_args_fail_fast_without_prompting(self):
        import subprocess
        import sys
        script = Path(__file__).with_name('configure_video_api.py')
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, str(script), '--preset', 'ark', '--out', str(Path(folder) / 'c.json')],
                input='', text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--base-url', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((Path(folder) / 'c.json').exists())

    def test_explicit_args_write_config_without_secret(self):
        import subprocess
        import sys
        script = Path(__file__).with_name('configure_video_api.py')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'c.json'
            result = subprocess.run(
                [sys.executable, str(script), '--preset', 'ark',
                 '--base-url', 'https://ark.cn-beijing.volces.com/api/v3',
                 '--model', 'endpoint-test', '--out', str(target)],
                input='', text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(config['model'], 'endpoint-test')
            self.assertEqual(config['protocol'], 'ark')
            self.assertNotIn('api_key', config)  # only api_key_env (a variable name), never a value


class DoctorTests(unittest.TestCase):
    def test_doctor_runs_offline(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('doctor.py'))],
                                capture_output=True, text=True)
        self.assertIn('doctor:', result.stdout + result.stderr)

    def test_doctor_summarizes_gate_report(self):
        import subprocess
        import sys
        script = Path(__file__).with_name('doctor.py')
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'gate.json'
            report.write_text(json.dumps({'summary': {
                'enabled': True, 'decisions': {'promoted': 1, 'needs_review': 1},
                'demoted': [{'shot_id': 'shot_002',
                             'missing': ['V1.reference_frame: no first frame'],
                             'fix_hints': ['extract the first frame']}],
                'assumed_suitability': ['shot_001'], 'candidates': [],
            }}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(script), '--gate-report', str(report)],
                                    capture_output=True, text=True, timeout=20)
            self.assertIn('promotion gate: 1 promoted, 1 demoted', result.stdout)
            self.assertIn('demoted shot_002', result.stdout)
            self.assertIn('v3_status=assumed', result.stdout)
            self.assertIn('doctor: PASS', result.stdout)

    def test_doctor_flags_missing_last_frame_and_overlong_duration(self):
        import subprocess
        import sys
        script = Path(__file__).with_name('doctor.py')
        example = (Path(__file__).resolve().parent.parent / 'assets'
                   / 'video-api.ark.example.json')
        config_data = json.loads(example.read_text(encoding='utf-8'))
        config_data['max_duration_s'] = 10
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / 'first.jpg'
            image.write_bytes(b'frame')
            config = root / 'config.json'
            config.write_text(json.dumps(config_data), encoding='utf-8')
            jobs = root / 'jobs.json'
            jobs.write_text(json.dumps({'jobs': [{
                'shot': 1, 'id': 'shot_001', 'route': 'ai_video',
                'executor': 'video_api',
                'params': {'prompt': 'p', 'duration': 21, 'aspect_ratio': '9:16',
                           'start_image_path': str(image)},
            }]}), encoding='utf-8')
            result = subprocess.run(
                [sys.executable, str(script), '--config', str(config), '--jobs', str(jobs)],
                capture_output=True, text=True, timeout=30)
            out = result.stdout + result.stderr
            self.assertIn('have no last frame', out)
            self.assertIn('exceed max_duration_s', out)
            self.assertIn('doctor: FAIL', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
