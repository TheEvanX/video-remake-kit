#!/usr/bin/env python3
"""Convert a recreation manifest into configurable HTTP video jobs."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path


def ratio(width: int | None, height: int | None) -> str:
    if not width or not height:
        return "9:16"
    a, b = int(width), int(height)
    while b:
        a, b = b, a % b
    return f"{int(width)//a}:{int(height)//a}"


def build(manifest: dict, out_path: Path, engine: str, min_duration: int = 4) -> dict:
    source = manifest.get("source") or {}
    breakdown = (manifest.get("provenance") or {}).get("breakdown", "")
    jobs = []
    for index, shot in enumerate(manifest.get("shots", []), start=1):
        route = shot.get("route", "manual_review")
        generation = shot.get("generation") or {}
        duration = float(shot.get("duration_s") or 0)
        model_duration = max(min_duration, int(math.ceil(duration)))
        prompts = shot.get("prompts") or {}
        if route == "ai_video":
            executor = "video_api"
            tool = "VideoAPI"
            params = {
                "prompt": prompts.get("video_en") or prompts.get("video") or "",
                "generation_type": "first_last_frame" if generation.get("start_image") and generation.get("end_image") else "text_to_video",
                "engine": engine,
                "aspect_ratio": ratio(source.get("width"), source.get("height")),
                "duration": model_duration,
            }
            if generation.get("start_image"):
                params["start_image_path"] = generation["start_image"]
            if generation.get("end_image"):
                params["end_image_path"] = generation["end_image"]
        elif route == "still_motion":
            executor = "local_ffmpeg"
            tool = "render_still_motion"
            params = {"duration": duration}
        else:
            executor = "manual_review"
            tool = "manual_review"
            params = {}
        # Non-AI routes are not billable, but their reviewed prompt must survive to the manual handoff.
        if route != "ai_video" and (prompts.get("video") or prompts.get("video_en")):
            params["review_prompt"] = prompts.get("video") or prompts.get("video_en")
        jobs.append({
            "shot": index,
            "id": shot["id"],
            "executor": executor,
            "label": shot.get("method", "unknown"),
            "route": route,
            "tool": tool,
            "params": params,
            "trim_to": duration,
            "prompt_source": shot.get("prompt_status", "unknown"),
            "user_assets": shot.get("assets", []),
            "warning": "; ".join(shot.get("review_flags", [])),
        })
    return {
        "breakdown": breakdown,
        "manifest": "",
        "engine": engine,
        "mode": "first_last_frame",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": jobs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--engine", default="seedance2.5")
    ap.add_argument("--min-duration", type=int, default=4,
                    help="Provider minimum shot length in seconds; must match min_duration_s in video-api.local.json")
    args = ap.parse_args()
    if args.min_duration < 1:
        ap.error("--min-duration must be >= 1")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    jobs = build(manifest, args.out, args.engine, args.min_duration)
    jobs['manifest'] = str(args.manifest.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "jobs": len(jobs["jobs"]), "engine": args.engine,
                      "min_duration": args.min_duration}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
