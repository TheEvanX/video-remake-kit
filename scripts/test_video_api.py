"""Local HTTP integration tests. No paid endpoints, no real credentials."""
import argparse
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

import video_api as api


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.media = cls.root / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=24:d=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.media)], check=True)
        cls.image = cls.root / 'frame.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(cls.media), '-frames:v', '1', str(cls.image)], check=True)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def send_json(self, obj, code=200):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(obj).encode())
            def do_POST(self):
                cls.posts += 1
                cls.auth.append(self.headers.get('Authorization'))
                cls.payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if cls.reject:
                    return self.send_json({'error': 'FAKE_SECRET'}, 401)
                if cls.sync:
                    return self.send_json({'video_url': cls.base + '/video.mp4'})
                self.send_json({'id': 'test-task'})
            def do_GET(self):
                if self.path == '/video.mp4':
                    cls.media_auth = self.headers.get('Authorization')
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'not-video' if cls.invalid else cls.media.read_bytes())
                else:
                    cls.gets += 1
                    self.send_json({'status': 'running'} if cls.pending else {'status': cls.status, 'content': {'video_url': cls.base + '/video.mp4'}})
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        cls = type(self)
        cls.posts = cls.gets = 0
        cls.auth = []
        cls.pending = cls.reject = cls.invalid = cls.sync = False
        cls.status = 'succeeded'
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        self.config = api.read(Path(__file__).resolve().parents[1] / 'assets/video-api.ark.example.json')
        self.config.update(base_url=self.base, model='test-model', poll_timeout_s=0, poll_interval_s=0.01)
        self.params = {'prompt': 'Test animation', 'duration': 4, 'start_image_path': str(self.image), 'end_image_path': str(self.image), 'aspect_ratio': '1:1'}
        self.jobs = {'jobs': [{'shot': 1, 'id': 'shot_001', 'route': 'ai_video', 'params': self.params, 'trim_to': 3.2}, {'shot': 2, 'id': 'shot_002', 'route': 'cg_3d', 'params': {}}]}
        self.args = argparse.Namespace(config=self.case/'config.json', jobs=self.case/'jobs.json', results=None, only=None, dry_run=False, execute=True, accept_fallback=False)
        api.save(self.args.config, self.config)
        api.save(self.args.jobs, self.jobs)

    def run_api(self):
        stream = io.StringIO()
        # Windows caps one env var at 32767 chars while hosts may inject much
        # larger blobs (e.g. ACC_PRODUCT_CONFIG_V3). patch.dict(os.environ, ...)
        # re-putenv()s EVERY key on exit and raises ValueError in that case,
        # so only touch the single key this test actually needs.
        previous = os.environ.get('VIDEO_API_KEY')
        os.environ['VIDEO_API_KEY'] = 'FAKE_SECRET'
        try:
            with redirect_stdout(stream):
                rc = api.run(self.args)
        finally:
            if previous is None:
                os.environ.pop('VIDEO_API_KEY', None)
            else:
                os.environ['VIDEO_API_KEY'] = previous
        self.assertNotIn('FAKE_SECRET', stream.getvalue())
        result = self.case/'results.json'
        if result.exists():
            self.assertNotIn('FAKE_SECRET', result.read_text())
        return rc

    def test_submit_poll_download_resume(self):
        self.assertEqual(self.run_api(), 0)
        result = api.read(self.case/'results.json')['results']
        self.assertTrue(Path(result['1']['video']).is_file())
        self.assertNotIn('2', result)
        self.assertEqual(type(self).auth, ['Bearer FAKE_SECRET'])
        self.assertIsNone(type(self).media_auth)
        self.assertEqual(type(self).payload['content'][2]['role'], 'last_frame')
        self.assertEqual(self.run_api(), 0)
        self.assertEqual(type(self).posts, 1)

    def test_timeout_resumes_task_without_resubmit(self):
        type(self).pending = True
        self.assertEqual(self.run_api(), 1)
        type(self).pending = False
        self.assertEqual(self.run_api(), 0)
        self.assertEqual(type(self).posts, 1)

    def test_dry_run_no_network_no_secret(self):
        self.args.dry_run = True
        self.args.execute = False
        self.assertEqual(self.run_api(), 0)
        self.assertEqual(type(self).posts, 0)

    def test_unknown_submission_not_retried(self):
        type(self).reject = True
        self.assertEqual(self.run_api(), 1)
        with self.assertRaises(api.ApiError):
            self.run_api()
        self.assertEqual(type(self).posts, 1)

    def test_failed_task_not_resubmitted(self):
        type(self).status = 'failed'
        self.assertEqual(self.run_api(), 1)
        self.assertEqual(self.run_api(), 1)
        self.assertEqual(type(self).posts, 1)

    def test_invalid_download_and_retry_existing_task(self):
        type(self).invalid = True
        self.assertEqual(self.run_api(), 1)
        self.assertFalse((self.case/'api_videos/shot_001.mp4').exists())
        type(self).invalid = False
        self.assertEqual(self.run_api(), 0)
        self.assertEqual(type(self).posts, 1)

    def test_constraints(self):
        c = copy.deepcopy(self.config)
        c['supports_last_frame'] = False
        with self.assertRaises(api.ApiError):
            api.payload_for(self.jobs['jobs'][0], c, self.case)
        c['supports_last_frame'] = True
        c['max_duration_s'] = 3
        with self.assertRaises(api.ApiError):
            api.payload_for(self.jobs['jobs'][0], c, self.case)

    def test_generic_sync(self):
        c = api.read(Path(__file__).resolve().parents[1] / 'assets/video-api.generic.example.json')
        c.update(base_url=self.base, model='test-model', image_mode='data_uri', video_url_field='video_url')
        api.save(self.args.config, c)
        type(self).sync = True
        self.assertEqual(self.run_api(), 0)
        self.assertTrue(type(self).payload['first_frame_url'].startswith('data:image/'))

    def test_changed_payload_is_blocked(self):
        self.assertEqual(self.run_api(), 0)
        self.jobs['jobs'][0]['params']['prompt'] = 'Different prompt'
        api.save(self.args.jobs, self.jobs)
        with self.assertRaises(api.ApiError):
            self.run_api()

    def test_template_types_and_urls(self):
        self.assertEqual(api.render({'n': '{{duration}}'}, {'duration': 4}), {'n': 4})
        self.assertEqual(api.field({'data': [{'url': 'x'}]}, 'data.0.url'), 'x')
        with self.assertRaises(api.ApiError):
            api.check_url('http://example.com')
        with self.assertRaises(api.ApiError):
            api.check_url('https://user:secret@example.com')

    def test_concurrent_execution_lock(self):
        results = self.case/'results.json'
        with api.execution_lock(results):
            with self.assertRaises(api.ApiError):
                with api.execution_lock(results):
                    pass
        self.assertFalse(Path(str(results) + '.lock').exists())


    def test_ark_duration_is_integer(self):
        body = api.payload_for(self.jobs['jobs'][0], self.config, self.case)
        self.assertIsInstance(body['duration'], int)
        self.assertIn('"duration": 4', json.dumps(body))
        self.assertEqual(body['ratio'], '1:1')

    def test_ark_rejects_fractional_duration(self):
        job = copy.deepcopy(self.jobs['jobs'][0])
        job['params']['duration'] = 4.5
        with self.assertRaises(api.ApiError):
            api.payload_for(job, self.config, self.case)

    def test_ratio_reduction_alias_and_rejection(self):
        job = copy.deepcopy(self.jobs['jobs'][0])
        job['params']['aspect_ratio'] = '1080:1920'
        self.assertEqual(api.payload_for(job, self.config, self.case)['ratio'], '9:16')
        job['params']['aspect_ratio'] = '4:5'
        with self.assertRaises(api.ApiError) as ctx:
            api.payload_for(job, self.config, self.case)
        self.assertIn('4:5', str(ctx.exception))
        mapped = copy.deepcopy(self.config)
        mapped['ratio_aliases'] = {'4:5': '3:4'}
        self.assertEqual(api.payload_for(job, mapped, self.case)['ratio'], '3:4')
        self.assertEqual(api.seconds(5.0), 5)
        self.assertEqual(api.seconds(4.5), 4.5)

    def test_preflight_reports_every_bad_shot_and_never_calls_network(self):
        base = copy.deepcopy(self.jobs['jobs'][0])
        too_long = copy.deepcopy(base)
        too_long.update(shot=5, id='shot_005')
        too_long['params']['duration'] = 35
        no_image = copy.deepcopy(base)
        no_image.update(shot=6, id='shot_006')
        no_image['params']['start_image_path'] = 'missing.jpg'
        self.jobs['jobs'] = [base, too_long, no_image]
        api.save(self.args.jobs, self.jobs)
        self.args.dry_run, self.args.execute = True, False
        stream = io.StringIO()
        with redirect_stdout(stream):
            rc = api.run(self.args)
        report = json.loads(stream.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(report['jobs'], 1)
        self.assertEqual(report['network_calls'], 0)
        self.assertEqual([e['shot'] for e in report['errors']], [5, 6])
        self.assertEqual(type(self).posts, 0)

    def test_execute_preflight_failure_does_not_request_key(self):
        job = copy.deepcopy(self.jobs['jobs'][0])
        job['params']['duration'] = 35
        api.save(self.args.jobs, {'jobs': [job]})
        previous = os.environ.get('VIDEO_API_KEY')
        os.environ['VIDEO_API_KEY'] = ''
        try:
            with patch.object(api.getpass, 'getpass',
                              side_effect=AssertionError('should not prompt')):
                with self.assertRaises(api.ApiError) as ctx:
                    api.run(self.args)
        finally:
            if previous is None:
                os.environ.pop('VIDEO_API_KEY', None)
            else:
                os.environ['VIDEO_API_KEY'] = previous
        self.assertIn('shot 1', str(ctx.exception))
        self.assertEqual(type(self).posts, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
