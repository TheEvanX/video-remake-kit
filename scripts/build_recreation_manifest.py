#!/usr/bin/env python3
"""Build a deterministic execution manifest from a video breakdown."""

from __future__ import annotations

import argparse
import hashlib
import json
from math import gcd
from pathlib import Path
from typing import Any

import promotion_gate as gate


ROUTES = {
    "ai_video": "ai_video",
    "live_action": "film",
    "avatar": "avatar",
    "cg_3d": "cg_3d",
    "code_render": "code_render",
    "screen_capture": "screen_capture",
    "still_motion": "still_motion",
    "static_hold": "still_motion",
    "animation_2d": "ai_video",
    "anim_2d": "ai_video",
    "stop_motion": "film",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def route_for(label: str) -> str:
    return ROUTES.get(label, "manual_review")


def make_asset(asset_id: str, kind: str, status: str, source: str, shot_id: str | None = None) -> dict[str, Any]:
    item = {"id": asset_id, "kind": kind, "status": status, "source": source}
    if shot_id:
        item["shot_id"] = shot_id
    return item


def source_ratio(source: dict[str, Any]) -> tuple[str, str, str]:
    """Return (reduced ratio, Chinese orientation, English orientation)."""
    width, height = source.get("width"), source.get("height")
    if not width or not height:
        return "9:16", "竖屏", "vertical"
    divisor = gcd(int(width), int(height))
    text = f"{int(width) // divisor}:{int(height) // divisor}"
    if int(width) > int(height):
        return text, "横屏", "horizontal"
    if int(height) > int(width):
        return text, "竖屏", "vertical"
    return text, "方形", "square"


def motion_phrases(motion: Any) -> tuple[str, str]:
    """Return (Chinese, English) camera-motion phrases; never mix languages in one prompt."""
    text = str(motion or "").strip().lower()
    rules = [
        (("static", "locked", "fixed", "静止", "固定", "不动", "定格"),
         ("固定机位、主体仅有克制的动作", "a locked camera with restrained subject movement")),
        (("handheld", "手持", "晃动"),
         ("轻微的手持感镜头运动", "subtle handheld camera movement")),
        (("push", "zoom in", "推镜", "推近", "推进"),
         ("缓慢稳定的推镜", "a slow controlled push-in")),
        (("pull", "zoom out", "拉远", "拉出", "拉镜"),
         ("缓慢稳定的拉镜", "a slow controlled pull-back")),
        (("pan", "tilt", "smooth", "move", "摇", "横移", "跟拍", "平滑", "运镜"),
         ("平滑可控的运镜", "a smooth controlled camera move")),
    ]
    for keys, phrases in rules:
        if any(key in text for key in keys):
            return phrases
    return ("与参考镜头一致的镜头运动", "the measured camera motion from the reference")


def fallback_prompts(shot: dict[str, Any], label: str, source: dict[str, Any],
                     has_first_frame: bool, has_last_frame: bool,
                     transcript: str = "") -> dict[str, str]:
    """Create conservative executable prompts when no VLM/reviewer prompt exists.

    Reference frames stay the visual source of truth. Duration is deliberately
    NOT written into the prompt: it is controlled by the API `duration` field,
    and a literal number here would contradict the rounded model duration.
    """
    features = shot.get("features") or {}
    aspects = shot.get("aspects") or {}
    camera = aspects.get("camera") or {}
    lighting = aspects.get("lighting") or {}
    motion = features.get("camera") or camera.get("result", {}).get("move") or "natural motion"
    motion_zh, motion_en = motion_phrases(motion)
    low_light = bool((lighting.get("result") or {}).get("low_light"))
    light_zh = "低调布光并保留深阴影" if low_light else "与参考一致的光照与对比度"
    light_en = ("Use low-key practical lighting with deep shadows." if low_light
                else "Match the lighting and contrast of the reference.")
    ratio, orientation, orientation_en = source_ratio(source)

    if has_first_frame and has_last_frame:
        anchor_zh = "以附带首帧作为视觉锚点、尾帧作为结束状态"
        anchor_en = "using the attached first frame as the visual anchor and the last frame as the end state"
    elif has_first_frame:
        anchor_zh = "以附带首帧作为视觉锚点"
        anchor_en = "using the attached first frame as the visual anchor"
    else:
        anchor_zh = "以参考画面作为视觉锚点"
        anchor_en = "using the reference imagery as the visual anchor"

    subjects = {
        "cg_3d": ("材质与光照物理一致的干净电影感三维渲染", "a clean cinematic 3D render with physically consistent materials and lighting"),
        "live_action": ("主体、构图与背景一致的授权实拍画面", "authorized live-action footage with the same subject, framing, and background"),
        "avatar": ("口型与身份一致的主持人形象", "a consistent presenter avatar with accurate lip sync and identity"),
    }
    subject_zh, subject_en = subjects.get(
        label,
        ("与参考一致的主体、环境、色彩与视觉风格", "the same subject, environment, color palette, and visual style as the reference"),
    )

    transcript = " ".join(str(transcript or "").split())[:240]
    semantic_zh = (f"本镜头的口播/字幕内容是「{transcript}」，据此理解场景语义即可，"
                   "不要在画面中生成文字。" if transcript else "")
    semantic_en = (f" The spoken or subtitle content of this shot is: \"{transcript}\". "
                   "Use it only to understand the scene; do not render any text on screen."
                   if transcript else "")
    video_en = (
        f"Create a {ratio} {orientation_en} shot {anchor_en}."
        f" Match {subject_en}. Use {motion_en}; preserve the composition, subject scale and timing of the reference."
        f" {light_en} Keep the shot rhythm aligned with the reference."
        f"{semantic_en}"
        " Do not add text, logos, watermarks, or new objects."
    )
    video = (
        f"生成 {ratio} {orientation}镜头，{anchor_zh}。"
        f"保持{subject_zh}。{motion_zh}；{light_zh}。"
        f"镜头节奏与时长严格对齐参考。{semantic_zh}"
        f"不新增文字、Logo、水印或无关物体。"
    )
    image = (f"Use the attached reference frame as the exact composition and appearance source for a {ratio} shot; "
             "preserve subject identity, framing, lighting, and palette.")
    return {"image": image, "video": video, "video_en": video_en}


def build(breakdown: dict[str, Any], breakdown_path: Path, *,
          gate_config: dict | None = None, gate_enabled: bool = True,
          gate_policy: dict | None = None) -> dict[str, Any]:
    """Build the execution manifest.

    With gate_enabled, every `ai_video` shot must pass the objective V1/V2
    promotion checks before it stays on the billable route. Demoted shots are
    NOT dropped: they keep their prompts and become `manual_review` with the
    exact missing items recorded in `summary.unresolved`.
    """
    source = breakdown.get("source") or {}
    source_path = Path(str(source.get("path") or ""))
    source_meta = {
        "path": str(source_path),
        "hash": sha256(source_path) if source_path.is_file() else "",
        "duration_s": source.get("duration"),
        "width": source.get("width"),
        "height": source.get("height"),
        "fps": source.get("fps"),
        "has_audio": bool(source.get("has_audio")),
    }
    assets: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    route_counts: dict[str, float] = {}
    unresolved: list[dict[str, Any]] = []
    prompt_status_counts: dict[str, int] = {"reviewed": 0, "fallback": 0}
    policy = gate.policy_from_config(gate_config, **(gate_policy or {}))
    label_assignment = str((breakdown.get("provenance") or {}).get("label_assignment") or "explicit")
    shot_count = len(breakdown.get("shots") or [])
    promotion_counts: dict[str, int] = {}
    promotion_demoted: list[dict[str, Any]] = []
    promotion_assumed: list[str] = []

    for shot in breakdown.get("shots", []):
        base = shot.get("base") or {}
        images = shot.get("assets") or {}
        verdict = shot.get("verdict")
        verdict_label = verdict.get("label") if isinstance(verdict, dict) else verdict if isinstance(verdict, str) else None
        label = str(first_nonempty(base.get("label"), verdict_label, "unknown"))
        route = route_for(label)
        declared_route = route
        replication = shot.get("replication") or {}
        review = shot.get("review") or {}
        vlm = shot.get("vlm") or {}
        replication_sources = [review.get("replication") or {}, vlm.get("replication") or {}, replication.get("capsule") or {}]
        prompts = {
            "image": str(first_nonempty(*(x.get("image_prompt") for x in replication_sources)) or ""),
            "video": str(first_nonempty(*(x.get("video_prompt") for x in replication_sources)) or ""),
            "video_en": str(first_nonempty(*(x.get("video_prompt_en") for x in replication_sources)) or ""),
        }
        prompt_status = "reviewed" if any(prompts.values()) else "fallback"
        prompt_status_counts[prompt_status] = prompt_status_counts.get(prompt_status, 0) + 1
        if not any(prompts.values()) and route in {"ai_video", "avatar", "cg_3d", "film"}:
            prompts = fallback_prompts(shot, label, source,
                                       bool(images.get("first_frame")), bool(images.get("last_frame")),
                                       str(shot.get("transcript") or ""))

        shot_assets: list[str] = []
        for key in ("first_frame", "last_frame", "sheet"):
            rel = images.get(key)
            if rel:
                aid = f"{shot['id']}_{key}"
                assets.append(make_asset(aid, "reference_frame", "available", str((breakdown_path.parent / rel).resolve()), shot["id"]))
                shot_assets.append(aid)

        material = shot.get("material") or {}
        if material.get("has_real_material") or material.get("origin") in {"photo", "real_footage", "screenshot"}:
            aid = f"{shot['id']}_replacement_source"
            assets.append(make_asset(aid, "source_material", "replacement_required", "authorized replacement asset required", shot["id"]))
            shot_assets.append(aid)

        packaging_ids: list[str] = []
        packaging = breakdown.get("packaging") or {}
        for collection_name in ("text", "stickers"):
            for item in packaging.get(collection_name, []) or []:
                t0 = float(item.get("t0", 0))
                t1 = float(item.get("t1", t0))
                if t1 >= float(shot.get("start", 0)) and t0 <= float(shot.get("end", 0)):
                    packaging_ids.append(str(item.get("id")))

        audio_ids: list[str] = []
        for event in ((breakdown.get("audio") or {}).get("sfx") or {}).get("events", []) or []:
            t = float(event.get("t", -1))
            if float(shot.get("start", 0)) <= t <= float(shot.get("end", 0)):
                audio_ids.append(f"sfx_{t:.3f}")

        confidence = float(first_nonempty(base.get("confidence"), (shot.get("deterministic") or {}).get("confidence"), 0) or 0)
        constraints = {
            "aspect_ratio": source_ratio(source)[0] if source.get("width") and source.get("height") else "",
            "duration_s": shot.get("duration"),
            "continuity": ["preserve subject identity", "preserve shot composition"],
        }
        shot_unresolved: list[str] = []
        review_flags: list[str] = []
        if gate_enabled and declared_route == "ai_video":
            decision = gate.evaluate(shot, source=source, route=declared_route,
                                     policy=policy, label_assignment=label_assignment,
                                     shot_count=shot_count)
            promotion = {key: decision[key] for key in (
                "decision", "route", "checks", "gate", "missing", "fix_hints",
                "warnings", "v3_status", "evidence")}
            if decision["decision"] != "promoted":
                route = "manual_review"
                shot_unresolved.extend(decision["missing"] or ["promotion gate: evidence insufficient"])
                review_flags.append("promotion gate demoted this shot; see promotion.fix_hints")
            promotion["route"] = route
            promotion_counts[decision["decision"]] = promotion_counts.get(decision["decision"], 0) + 1
            if decision["decision"] == "needs_review":
                promotion_demoted.append({"shot_id": shot["id"], "missing": decision["missing"],
                                          "fix_hints": decision["fix_hints"]})
            if decision["decision"] == "promoted" and decision["v3_status"] in ("assumed", "unverified"):
                promotion_assumed.append(shot["id"])
        elif gate_enabled:
            promotion = {"decision": "reference", "route": route,
                         "note": "non-AI route; no billable promotion evaluated"}
        else:
            promotion = {"decision": "skipped", "route": route, "note": "--no-gate"}
        if route == "manual_review":
            shot_unresolved.append("production method has no safe automated route")
        if prompt_status == "fallback" and declared_route in {"ai_video", "avatar", "cg_3d", "film"}:
            note = "fallback prompt should be checked before generation"
            if shot.get("transcript"):
                note += "; transcript-derived context may contain ASR errors"
            review_flags.append(note)
        if shot_unresolved:
            unresolved.append({"shot_id": shot["id"], "items": shot_unresolved})

        shots.append({
            "id": shot["id"],
            "start_s": shot.get("start"),
            "end_s": shot.get("end"),
            "duration_s": shot.get("duration"),
            "method": label,
            "route": route,
            "confidence": confidence,
            "evidence": list(((shot.get("deterministic") or {}).get("evidence")) or [])[:8],
            "assets": shot_assets,
            "prompts": prompts,
            "prompt_status": prompt_status,
            "generation": {
                "engine": "seedance2.5" if route == "ai_video" else None,
                "generation_type": "first_last_frame" if route == "ai_video" else "route_specific",
                "start_image": str((breakdown_path.parent / images["first_frame"]).resolve()) if images.get("first_frame") else None,
                "end_image": str((breakdown_path.parent / images["last_frame"]).resolve()) if images.get("last_frame") else None,
                "duration_s": shot.get("duration"),
                "requires_human_review": bool(review_flags),
            },
            "constraints": constraints,
            "packaging_ids": sorted(set(packaging_ids)),
            "audio_ids": sorted(set(audio_ids)),
            "validation": ["duration matches source shot", "aspect ratio matches source", "packaging and audio events remain aligned"],
            "review_flags": review_flags,
            "unresolved": shot_unresolved,
            "promotion": promotion,
        })
        route_counts[route] = route_counts.get(route, 0.0) + float(shot.get("duration") or 0)

    return {
        "schema_version": "1.0",
        "source": source_meta,
        "summary": {
            "shot_count": len(shots), "routes": route_counts,
            "prompt_status": prompt_status_counts, "unresolved": unresolved,
            "promotion": {
                "enabled": gate_enabled,
                "policy": policy if gate_enabled else None,
                "decisions": promotion_counts,
                "demoted": promotion_demoted,
                "assumed_suitability": promotion_assumed,
            },
        },
        "assets": assets,
        "shots": shots,
        "assembly": {"audio_policy": "original", "transition_policy": "preserve", "results": "remake/results.json"},
        "provenance": {"breakdown": str(breakdown_path.resolve()), "tool": "video-recreate"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--breakdown", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--gate-config", type=Path,
                    help="video-api config; the gate then reuses its duration/ratio limits")
    ap.add_argument("--gate-policy", type=Path, help="optional JSON overriding individual gate policy fields")
    ap.add_argument("--gate-report", type=Path, help="optional standalone promotion report")
    ap.add_argument("--no-gate", action="store_true",
                    help="disable promotion checks (v0.2.0 behaviour; not recommended)")
    args = ap.parse_args()
    breakdown = json.loads(args.breakdown.read_text(encoding="utf-8-sig"))
    gate_config = json.loads(args.gate_config.read_text(encoding="utf-8-sig")) if args.gate_config else None
    gate_policy = json.loads(args.gate_policy.read_text(encoding="utf-8-sig")) if args.gate_policy else None
    manifest = build(breakdown, args.breakdown, gate_config=gate_config,
                     gate_enabled=not args.no_gate, gate_policy=gate_policy)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.gate_report:
        args.gate_report.parent.mkdir(parents=True, exist_ok=True)
        args.gate_report.write_text(json.dumps(
            {"summary": manifest["summary"]["promotion"],
             "shots": [s["promotion"] for s in manifest["shots"]]},
            ensure_ascii=False, indent=2), encoding="utf-8")
    promotion = manifest["summary"]["promotion"]
    print(json.dumps({
        "out": str(args.out), "shots": len(manifest["shots"]),
        "unresolved": len(manifest["summary"]["unresolved"]),
        "gate_enabled": promotion["enabled"],
        "promoted": promotion["decisions"].get("promoted", 0),
        "demoted": len(promotion["demoted"]),
        "assumed_suitability": len(promotion["assumed_suitability"]),
        "gate_report": str(args.gate_report) if args.gate_report else None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


