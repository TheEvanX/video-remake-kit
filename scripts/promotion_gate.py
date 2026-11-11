#!/usr/bin/env python3
"""Shot-level promotion gate: decide which shots may become billable AI jobs.

Adapted from cangjie-skill RIA-TV++ (MIT): stage 1.5 Triple Verification and
stage 1.6 Promotion Gate. The mapping is deliberately explicit and conservative:

  V1 Source sufficiency -> can this shot be traced back to a frame and a time window?
  V2 Executability      -> can a model actually run it (duration, ratio, frames)?
  V3 Task utility       -> is AI regeneration plausible for this shot?

V1/V2 are machine-checkable and are HARD gates. V3 needs a VLM or a human, so it
is NEVER claimed as verified by this script: an automatically assigned label is
recorded as `assumed`, kept visible in `warnings`, and still has to pass the
runner's `--accept-fallback` confirmation before any paid call.

A demoted shot is never dropped: `missing` and `fix_hints` say exactly what to
fix, and the manifest copies them into `summary.unresolved`.

Pure functions only. No network, no API key, no ffmpeg, no file writes except
the optional `--out` report from the CLI.
"""
from __future__ import annotations

import argparse
import json
import math
from math import gcd
from pathlib import Path
import sys
from typing import Any

GENERATIVE_LABELS = {"ai_video", "animation_2d", "anim_2d"}
REPLACEMENT_ORIGINS = {"photo", "real_footage", "screenshot"}

DEFAULT_POLICY: dict[str, Any] = {
    "min_duration_s": 4,
    "max_duration_s": 30,
    "supported_ratios": [],
    "ratio_aliases": {},
    "supports_last_frame": True,
    "enforce_ratio": False,
    "require_first_frame": True,
}


def policy_from_config(config: dict | None, **overrides: Any) -> dict[str, Any]:
    """Build a gate policy from a video-api config so both stages share ONE constraint set."""
    policy = dict(DEFAULT_POLICY)
    if config:
        for key in ("min_duration_s", "max_duration_s", "supported_ratios",
                    "ratio_aliases", "supports_last_frame"):
            if config.get(key) is not None:
                policy[key] = config[key]
        # Only enforce the whitelist when the user supplied the real model config:
        # the built-in default list must not silently reject other providers.
        policy["enforce_ratio"] = bool(config.get("supported_ratios"))
    policy.update({k: v for k, v in overrides.items() if v is not None})
    return policy


def reduced_ratio(width: Any, height: Any, aliases: dict | None = None) -> str:
    try:
        w, h = int(width), int(height)
        if w <= 0 or h <= 0:
            return ""
    except (TypeError, ValueError):
        return ""
    divisor = gcd(w, h)
    text = f"{w // divisor}:{h // divisor}"
    return str((aliases or {}).get(text, text))


def _check(passed: bool, reason: str) -> dict[str, Any]:
    return {"passed": bool(passed), "reason": reason}


def evaluate(shot: dict, *, source: dict, route: str, policy: dict,
             label_assignment: str = "explicit", shot_count: int = 1) -> dict[str, Any]:
    """Return a full, auditable promotion decision for one shot."""
    shot_id = str(shot.get("id") or "unknown_shot")
    base = shot.get("base") or {}
    label = str(base.get("label") or "unknown")
    images = shot.get("assets") or {}
    deterministic = shot.get("deterministic") or {}
    evidence_items = list(deterministic.get("evidence") or [])
    start, end = shot.get("start"), shot.get("end")
    duration = shot.get("duration")
    try:
        start_f, end_f, duration_f = float(start), float(end), float(duration)
    except (TypeError, ValueError):
        start_f, end_f, duration_f = 0.0, 0.0, 0.0

    location = f"{shot_id}@{start_f:.3f}-{end_f:.3f}"
    evidence = [{
        "source_id": "breakdown.json",
        "location": location,
        "time_start_ms": round(start_f * 1000),
        "time_end_ms": round(end_f * 1000),
        "asset_ref": str(images.get("first_frame") or ""),
        "confidence": base.get("confidence", deterministic.get("confidence", 0)),
    }]

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "shot_id": shot_id,
        "decision": "reference",
        "route": route,
        "suggested_label": label,
        "checks": {},
        "gate": {},
        "missing": [],
        "fix_hints": [],
        "warnings": [],
        "v3_status": "not_applicable",
        "evidence": evidence,
    }
    if route != "ai_video":
        result["warnings"].append(
            f"route={route} is not a billable AI-video route; kept as-is for human/other executors")
        return result

    missing: list[str] = []
    hints: list[str] = []

    # ---- V1 Source sufficiency: traceable original + a usable visual anchor ----
    timing_ok = (start is not None and end is not None and duration is not None
                 and start_f >= 0 and end_f > start_f and duration_f > 0
                 and abs(duration_f - (end_f - start_f)) <= 0.5)
    v1 = {
        "timing": _check(timing_ok, "start/end/duration are positive, ordered and mutually consistent"),
        "reference_frame": _check(
            not policy.get("require_first_frame", True) or bool(images.get("first_frame")),
            "first_frame path present as the visual anchor"),
        "label": _check(label in GENERATIVE_LABELS,
                        f"base.label={label!r} explicitly maps to the ai_video route"),
    }
    if not timing_ok:
        missing.append("V1.timing: missing or inconsistent start/end/duration")
        hints.append("regenerate the breakdown with valid shot windows")
    if not v1["reference_frame"]["passed"]:
        missing.append("V1.reference_frame: no first frame")
        hints.append("extract the shot's first frame (build_breakdown.py does this) or supply a reference image")
    if not v1["label"]["passed"]:
        missing.append(f"V1.label: {label!r} is not a generative label")
        hints.append("assign base.label=ai_video after reviewing the shot, or leave it unknown for manual_review")
    v1["passed"] = _check(all(x["passed"] for x in v1.values()), "V1 requires all three items")

    # ---- V2 Executability: fixed inputs -> verifiable output ----
    min_d = float(policy.get("min_duration_s") or 4)
    max_d = float(policy.get("max_duration_s") or 30)
    model_duration = max(min_d, math.ceil(duration_f)) if duration_f > 0 else 0
    duration_ok = 0 < math.ceil(duration_f) <= max_d
    ratio_text = reduced_ratio(source.get("width"), source.get("height"),
                               policy.get("ratio_aliases") or {})
    allowed = [str(r) for r in (policy.get("supported_ratios") or [])]
    ratio_ok = (not policy.get("enforce_ratio") or not ratio_text
                or not allowed or ratio_text in allowed)
    last_frame_present = bool(images.get("last_frame"))
    supports_last = bool(policy.get("supports_last_frame", True))
    last_frame_ok = not (last_frame_present and not supports_last)
    if last_frame_present and supports_last:
        last_frame_reason = "last frame supplied and the model supports it"
    elif last_frame_present:
        last_frame_reason = "last frame supplied but the model does not support it"
    elif supports_last:
        last_frame_reason = "no last frame supplied; generation is first-frame driven only"
    else:
        last_frame_reason = "no last frame supplied; model does not support last frames either"
    if not last_frame_present and supports_last:
        result["warnings"].append(
            "No last frame for this shot: a long moving shot regenerated from its first frame "
            "alone is weakly constrained. Re-run build_breakdown.py or supply assets.last_frame.")
    material = shot.get("material") or {}
    material_ok = not (material.get("has_real_material")
                       or material.get("origin") in REPLACEMENT_ORIGINS)
    v2 = {
        "duration": _check(duration_ok,
                           f"ceil({duration_f:.3f}) <= max {max_d:g}s; runner will clamp to {model_duration:g}s"),
        "aspect_ratio": _check(ratio_ok, f"source reduces to {ratio_text or 'unknown'}"),
        "last_frame": _check(last_frame_ok, last_frame_reason),
        "material": _check(material_ok, "no protected source material requiring an authorized replacement"),
    }
    if not duration_ok:
        missing.append(f"V2.duration: {duration_f:.3f}s exceeds max_duration_s={max_d:g}")
        hints.append("split this shot into shorter windows (build_breakdown.py --max-shot-s) or raise max_duration_s")
    if not ratio_ok:
        missing.append(f"V2.aspect_ratio: {ratio_text} not in supported_ratios {allowed}")
        hints.append("add a ratio_aliases mapping, or crop/pad the reference frames to a supported ratio")
    if not last_frame_ok:
        missing.append("V2.last_frame: last frame present but model does not support it")
        hints.append("set supports_last_frame=true in config, or remove the last frame from this shot")
    if not material_ok:
        missing.append("V2.material: protected/real source material needs an authorized replacement")
        hints.append("supply a licensed replacement asset before generating")
    v2["passed"] = _check(all(x["passed"] for x in v2.values()), "V2 requires all four items")

    # ---- V3 Task utility: AI regeneration must plausibly help. NOT machine-verifiable. ----
    review = shot.get("review") or {}
    vlm = shot.get("vlm") or {}
    reviewed_prompt = bool((review.get("replication") or {}).get("video_prompt")
                           or (vlm.get("replication") or {}).get("video_prompt"))
    label_source = str(base.get("label_source") or "")
    assumed = (label_assignment.startswith("assumed")
               or label_assignment in ("auto_promote", "promotion_gate")
               or label_source.startswith("promotion_gate:"))
    if reviewed_prompt and not assumed:
        v3_status = "verified_by_reviewer"
    elif assumed:
        v3_status = "assumed"
    else:
        v3_status = "unverified"
    v3 = {
        "route_fit": _check(label in GENERATIVE_LABELS,
                            "label claims this shot should be regenerated, not filmed or rendered"),
        "independent_judgement": _check(reviewed_prompt,
                                        "review/VLM prompt exists as an independent judgement"),
        "task_utility": _check(True,
                               "saves per-shot triage and blocks un-runnable shots before paid calls"),
    }
    v3["passed"] = _check(v3["route_fit"]["passed"],
                          "V3 is advisory: AI suitability cannot be proven from frames alone")
    if v3_status in ("assumed", "unverified"):
        result["warnings"].append(
            "AI suitability is NOT verified: this label was "
            + ("assigned by an automatic/assumed rule" if assumed else "not backed by a reviewed prompt")
            + ". Check for live action, screen recordings, UI text and 3D renders before paying.")
    if shot.get("transcript"):
        result["warnings"].append(
            "transcript gives semantic context only; ASR errors must not be treated as a visual judgement")
    result["v3_status"] = v3_status

    # ---- Promotion gate (cangjie stage 1.6): 1/2/3 mandatory, 4 or 5 ----
    no_cross_dependency = not (shot.get("depends_on") or shot.get("continuity_from")
                               or shot.get("requires_previous_shot"))
    gate = {
        "independent_intent": _check(v1["timing"]["passed"] and v1["reference_frame"]["passed"],
                                     "one self-contained time window with its own reference frame"),
        "independent_contract": _check(v2["passed"]["passed"],
                                       "fixed inputs (frames/prompt/ratio/duration) and checkable output"),
        "independent_run": _check(no_cross_dependency,
                                  "no dependency on another shot's generated output"),
        "independent_reuse": _check(shot_count >= 2,
                                    f"batch contains {shot_count} shot(s) using the same engine"),
        "independent_eval": _check(True,
                                   "positive/negative assertions exist: frame present, duration in range, ratio supported"),
    }
    gate_passed = (all(gate[k]["passed"] for k in
                       ("independent_intent", "independent_contract", "independent_run"))
                   and (gate["independent_reuse"]["passed"] or gate["independent_eval"]["passed"]))
    gate["passed"] = _check(gate_passed, "1/2/3 mandatory; at least one of 4/5")

    promoted = v1["passed"]["passed"] and v2["passed"]["passed"] and gate_passed
    result.update({
        "checks": {"V1_source_sufficiency": v1, "V2_executability": v2, "V3_task_utility": v3},
        "gate": gate,
        "decision": "promoted" if promoted else "needs_review",
        "route": "ai_video" if promoted else "manual_review",
        "suggested_label": label if promoted else "unknown",
        "missing": missing,
        "fix_hints": hints,
    })
    return result


def report(breakdown: dict, *, config: dict | None = None,
           policy_overrides: dict | None = None) -> dict:
    """Evaluate every shot, including a read-only probe of unlabelled shots.

    A shot with `base.label: unknown` is tried with a candidate `ai_video`
    label so the report can say "this one is objectively eligible, assign the
    label to promote it" or "this one lacks X". Nothing is written back.
    """
    policy = policy_from_config(config, **(policy_overrides or {}))
    source = breakdown.get("source") or {}
    shots = breakdown.get("shots") or []
    provenance = breakdown.get("provenance") or {}
    assignment = str(provenance.get("label_assignment") or "explicit")
    results = []
    for shot in shots:
        base = shot.get("base") or {}
        label = str(base.get("label") or "unknown")
        probe = label == "unknown"
        if label in GENERATIVE_LABELS or probe:
            route = "ai_video"
            candidate = dict(shot)
            if probe:
                candidate["base"] = {**base, "label": "ai_video", "label_source": "promotion_gate:probe"}
        else:
            # Only the generative labels are on the paid path; other routes are
            # handled by their own executors and must never be silently rewritten.
            route, candidate = "other", shot
        item = evaluate(candidate, source=source, route=route, policy=policy,
                        label_assignment="candidate_probe" if probe else assignment,
                        shot_count=len(shots))
        item["probe"] = probe
        if probe:
            if item["decision"] == "promoted":
                item["decision"] = "candidate"
                item["route"] = "manual_review"
                item["fix_hints"] = (item["fix_hints"] +
                                     ["objectively eligible: set base.label=ai_video to promote this shot"])
            item["note"] = "read-only probe; breakdown.json was not modified"
            item["suggested_label"] = "unknown"
        results.append(item)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    return {
        "schema_version": "1.0",
        "policy": policy,
        "label_assignment": assignment,
        "summary": {
            "shots": len(results),
            "decisions": counts,
            "demoted": [{"shot_id": r["shot_id"], "missing": r["missing"],
                         "fix_hints": r["fix_hints"]}
                        for r in results if r["decision"] == "needs_review"],
            "candidates": [r["shot_id"] for r in results if r["decision"] == "candidate"],
            "assumed_suitability": [r["shot_id"] for r in results
                                    if r["v3_status"] in ("assumed", "unverified")
                                    and r["decision"] in ("promoted", "candidate")],
        },
        "shots": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--breakdown', required=True, type=Path, help='breakdown.json to evaluate (read-only)')
    ap.add_argument('--config', type=Path, help='video-api config: reuse its duration/ratio constraints')
    ap.add_argument('--out', type=Path, help='optional full JSON report path')
    ap.add_argument('--min-duration-s', type=float)
    ap.add_argument('--max-duration-s', type=float)
    args = ap.parse_args()
    try:
        breakdown = json.loads(args.breakdown.read_text(encoding='utf-8-sig'))
        config = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else None
        result = report(breakdown, config=config,
                        policy_overrides={'min_duration_s': args.min_duration_s,
                                          'max_duration_s': args.max_duration_s})
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'summary': result['summary'], 'out': str(args.out) if args.out else None},
                         ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'Promotion gate failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
