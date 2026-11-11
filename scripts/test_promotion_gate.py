"""Offline tests for the promotion gate. No network, no API key, no provider calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import build_recreation_jobs as jobs_mod
import build_recreation_manifest as manifest_mod
import promotion_gate as gate

HERE = Path(__file__).resolve().parent

SOURCE = {"path": "source.mp4", "width": 1080, "height": 1920, "fps": 30, "duration": 12}


def shot(**overrides):
    base = {
        "id": "shot_001", "start": 0.0, "end": 5.0, "duration": 5.0,
        "base": {"label": "ai_video", "confidence": 0.8},
        "assets": {"first_frame": "frames/first.jpg", "last_frame": "frames/last.jpg"},
        "deterministic": {"confidence": 0.8, "evidence": ["scene_change@0.000"]},
        "review": {"replication": {"video_prompt": "已复核的镜头描述"}},
    }
    base.update(overrides)
    return base


def breakdown(shots):
    return {"source": dict(SOURCE), "shots": shots,
            "provenance": {"tool": "test", "label_assignment": "explicit"}}


def evaluate(item, *, config=None, assignment="explicit", route="ai_video"):
    policy = gate.policy_from_config(config)
    return gate.evaluate(item, source=SOURCE, route=route, policy=policy,
                         label_assignment=assignment, shot_count=3)


class GateUnitTests(unittest.TestCase):
    def test_all_checks_pass(self):
        result = evaluate(shot())
        self.assertEqual(result["decision"], "promoted")
        self.assertEqual(result["route"], "ai_video")
        self.assertEqual(result["v3_status"], "verified_by_reviewer")
        self.assertEqual(result["missing"], [])
        self.assertTrue(all(result["gate"][k]["passed"] for k in
                            ("independent_intent", "independent_contract", "independent_run")))
        self.assertEqual(result["evidence"][0]["time_end_ms"], 5000)

    def test_missing_last_frame_warns_but_does_not_demote(self):
        result = evaluate(shot(assets={"first_frame": "frames/first.jpg"}))
        self.assertEqual(result["decision"], "promoted")
        check = result["checks"]["V2_executability"]["last_frame"]
        self.assertTrue(check["passed"])
        self.assertIn("no last frame supplied", check["reason"])
        self.assertTrue(any("No last frame" in w for w in result["warnings"]))

    def test_supplied_last_frame_reports_model_support(self):
        result = evaluate(shot())
        check = result["checks"]["V2_executability"]["last_frame"]
        self.assertIn("model supports it", check["reason"])
        self.assertFalse(any("No last frame" in w for w in result["warnings"]))
        blocked = evaluate(shot(), config={"supports_last_frame": False})
        self.assertEqual(blocked["decision"], "needs_review")
        self.assertTrue(any("V2.last_frame" in x for x in blocked["missing"]))

    def test_missing_first_frame_is_demoted_with_fix_hint(self):
        result = evaluate(shot(assets={}))
        self.assertEqual(result["decision"], "needs_review")
        self.assertEqual(result["route"], "manual_review")
        self.assertTrue(any("V1.reference_frame" in x for x in result["missing"]))
        self.assertTrue(any("first frame" in x for x in result["fix_hints"]))
        self.assertFalse(result["checks"]["V1_source_sufficiency"]["passed"]["passed"])

    def test_overlong_shot_is_demoted(self):
        result = evaluate(shot(start=0.0, end=35.0, duration=35.0))
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V2.duration" in x for x in result["missing"]))
        self.assertTrue(any("split" in x for x in result["fix_hints"]))

    def test_short_shot_is_clamped_not_rejected(self):
        result = evaluate(shot(start=0.0, end=2.0, duration=2.0))
        self.assertEqual(result["decision"], "promoted")
        self.assertIn("clamp to 4s", result["checks"]["V2_executability"]["duration"]["reason"])

    def test_unsupported_ratio_is_demoted_only_with_real_config(self):
        odd = {"width": 1080, "height": 1350}  # 4:5
        policy = gate.policy_from_config({"supported_ratios": ["16:9", "9:16", "1:1"]})
        result = gate.evaluate(shot(), source=odd, route="ai_video", policy=policy)
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V2.aspect_ratio" in x for x in result["missing"]))
        # Without a config the gate must not invent a whitelist for other providers.
        self.assertEqual(evaluate(shot())["decision"], "promoted")

    def test_ratio_alias_is_honoured(self):
        odd = {"width": 1080, "height": 1350}
        policy = gate.policy_from_config(
            {"supported_ratios": ["16:9", "9:16"], "ratio_aliases": {"4:5": "9:16"}})
        self.assertEqual(gate.evaluate(shot(), source=odd, route="ai_video",
                                       policy=policy)["decision"], "promoted")

    def test_last_frame_not_supported_is_not_silently_dropped(self):
        policy = gate.policy_from_config({"supports_last_frame": False})
        result = gate.evaluate(shot(), source=SOURCE, route="ai_video", policy=policy)
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V2.last_frame" in x for x in result["missing"]))

    def test_protected_material_needs_replacement(self):
        result = evaluate(shot(material={"origin": "real_footage"}))
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V2.material" in x for x in result["missing"]))

    def test_inconsistent_timing_is_caught(self):
        result = evaluate(shot(start=0.0, end=5.0, duration=1.0))
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V1.timing" in x for x in result["missing"]))

    def test_unknown_label_is_not_treated_as_generative(self):
        result = evaluate(shot(base={"label": "unknown"}))
        self.assertEqual(result["decision"], "needs_review")
        self.assertTrue(any("V1.label" in x for x in result["missing"]))

    def test_assumed_label_is_never_claimed_as_visually_verified(self):
        result = evaluate(shot(review={}), assignment="assumed:ai_video")
        self.assertEqual(result["decision"], "promoted")  # objective evidence is fine
        self.assertEqual(result["v3_status"], "assumed")
        self.assertTrue(any("NOT verified" in w for w in result["warnings"]))
        self.assertTrue(any("ASR" in w for w in evaluate(
            shot(review={}, transcript="口播文本"), assignment="assumed:ai_video")["warnings"]))

    def test_non_ai_route_is_reference_not_rewritten(self):
        result = evaluate(shot(base={"label": "live_action"}), route="film")
        self.assertEqual(result["decision"], "reference")
        self.assertEqual(result["route"], "film")

    def test_cross_shot_dependency_blocks_promotion(self):
        result = evaluate(shot(depends_on="shot_000"))
        self.assertEqual(result["decision"], "needs_review")
        self.assertFalse(result["gate"]["independent_run"]["passed"])


class ReportTests(unittest.TestCase):
    def test_probe_reports_candidates_without_modifying_breakdown(self):
        original = breakdown([shot(base={"label": "unknown"}), shot(id="shot_002", base={"label": "unknown"})])
        snapshot = copy.deepcopy(original)
        report = gate.report(original)
        self.assertEqual(report["summary"]["decisions"], {"candidate": 2})
        self.assertEqual(report["summary"]["candidates"], ["shot_001", "shot_002"])
        self.assertEqual(original, snapshot)
        self.assertTrue(all(s["probe"] for s in report["shots"]))
        self.assertTrue(all(s["route"] == "manual_review" for s in report["shots"]))

    def test_probe_exposes_objective_gaps(self):
        report = gate.report(breakdown([shot(base={"label": "unknown"}, assets={})]),
                             config={"supported_ratios": ["9:16"]})
        self.assertEqual(report["summary"]["decisions"], {"needs_review": 1})
        self.assertTrue(any("V1.reference_frame" in x
                            for x in report["shots"][0]["missing"]))

    def test_cli_writes_report_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / 'breakdown.json'
            src.write_text(json.dumps(breakdown([shot(base={"label": "unknown"})])), encoding='utf-8')
            out = root / 'report.json'
            result = subprocess.run([sys.executable, str(HERE / 'promotion_gate.py'),
                                     '--breakdown', str(src), '--out', str(out)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.is_file())
            self.assertIn('candidate', result.stdout)


class ManifestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'breakdown.json'
        self.data = breakdown([
            shot(),
            shot(id="shot_002", start=5.0, end=7.0, duration=2.0, assets={}),
            shot(id="shot_003", start=7.0, end=12.0, duration=5.0,
                 base={"label": "live_action"}),
        ])
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_demoted_shot_leaves_billable_route_and_keeps_reasons(self):
        manifest = manifest_mod.build(self.data, self.path)
        by_id = {s['id']: s for s in manifest['shots']}
        self.assertEqual(by_id['shot_001']['route'], 'ai_video')
        self.assertEqual(by_id['shot_001']['promotion']['decision'], 'promoted')
        self.assertEqual(by_id['shot_002']['route'], 'manual_review')
        self.assertEqual(by_id['shot_002']['promotion']['decision'], 'needs_review')
        self.assertTrue(any('V1.reference_frame' in x for x in by_id['shot_002']['unresolved']))
        self.assertEqual(by_id['shot_003']['promotion']['decision'], 'reference')
        self.assertEqual(by_id['shot_003']['route'], 'film')
        summary = manifest['summary']['promotion']
        self.assertEqual(summary['decisions'], {'promoted': 1, 'needs_review': 1})
        self.assertEqual(summary['demoted'][0]['shot_id'], 'shot_002')

    def test_demoted_shot_never_becomes_a_paid_job(self):
        manifest = manifest_mod.build(self.data, self.path)
        spec = jobs_mod.build(manifest, self.root / 'remake/jobs.json', 'seedance2.5')
        routes = {j['id']: j['route'] for j in spec['jobs']}
        self.assertEqual(routes['shot_001'], 'ai_video')
        self.assertEqual(routes['shot_002'], 'manual_review')
        self.assertNotIn('shot_002', [j['id'] for j in spec['jobs'] if j['executor'] == 'video_api'])

    def test_gate_config_propagates_provider_constraints(self):
        config = {'min_duration_s': 4, 'max_duration_s': 6,
                  'supported_ratios': ['16:9', '1:1'], 'supports_last_frame': False}
        manifest = manifest_mod.build(self.data, self.path, gate_config=config)
        by_id = {s['id']: s for s in manifest['shots']}
        # 9:16 not in whitelist + last frame unsupported -> both V2 items fail
        missing = ' '.join(by_id['shot_001']['promotion']['missing'])
        self.assertIn('V2.aspect_ratio', missing)
        self.assertIn('V2.last_frame', missing)
        self.assertEqual(by_id['shot_001']['route'], 'manual_review')

    def test_no_gate_restores_v020_behaviour(self):
        manifest = manifest_mod.build(self.data, self.path, gate_enabled=False)
        by_id = {s['id']: s for s in manifest['shots']}
        self.assertEqual(by_id['shot_002']['route'], 'ai_video')
        self.assertEqual(by_id['shot_002']['promotion']['decision'], 'skipped')
        self.assertFalse(manifest['summary']['promotion']['enabled'])

    def test_manifest_cli_emits_report(self):
        out = self.root / 'manifest.json'
        report = self.root / 'gate.json'
        result = subprocess.run(
            [sys.executable, str(HERE / 'build_recreation_manifest.py'),
             '--breakdown', str(self.path), '--out', str(out), '--gate-report', str(report)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary['promoted'], summary['demoted']), (1, 1))
        written = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(len(written['shots']), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
