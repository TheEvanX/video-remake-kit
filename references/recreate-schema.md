# 中间产物 schema

执行链上的文件：`source.json`（Stage 0 源包，见 `source-intake.md`）→ `breakdown.json`（输入契约，见 `breakdown-schema.md`）→ `recreation_manifest.json`（执行视图）→ `recreation_jobs.json`（付费任务）→ `results.json`（生成结果）。全部为 UTF-8 JSON。

## 0. `source.json`

由 `fetch_source.py` 生成，记录视频文件的绝对路径、来源 URL/平台、下载方式、字幕文件与 ffprobe 探测结果。**不含** API Key、Cookie、Token 或下载器原始输出（可能含签名 URL）。

```json
{
  "schema_version": "1.0",
  "input": "https://... 或本地路径",
  "kind": "url|local",
  "platform": "bilibili|douyin|youtube|xiaohongshu|wechat_channels|generic|local",
  "url": "",
  "video": "/abs/path/source.mp4",
  "downloader": "yt-dlp|external|local",
  "subtitles": ["/abs/path/source.zh.srt"],
  "probe": {"duration": 4.5, "width": 1080, "height": 1920, "fps": 30.0, "has_audio": true},
  "warnings": []
}
```

## 1. `recreation_manifest.json`

由 `build_recreation_manifest.py` 确定性生成；原始证据仍留在 `breakdown.json`，manifest 只是执行视图。

```json
{
  "schema_version": "1.0",
  "source": {"path": "", "hash": "", "duration_s": 0, "width": 0, "height": 0, "fps": 0, "has_audio": true},
  "summary": {
    "shot_count": 0,
    "routes": {"ai_video": 4.2},
    "prompt_status": {"reviewed": 0, "fallback": 0},
    "unresolved": [{"shot_id": "shot_004", "items": ["production method has no safe automated route"]}],
    "promotion": {
      "enabled": true, "policy": {"min_duration_s": 4, "max_duration_s": 30},
      "decisions": {"promoted": 3, "needs_review": 1},
      "demoted": [{"shot_id": "shot_004", "missing": ["V1.reference_frame: no first frame"], "fix_hints": []}],
      "assumed_suitability": ["shot_001"]
    }
  },
  "assets": [{"id": "shot_001_first_frame", "kind": "reference_frame", "status": "available",
              "source": "/abs/path/frame.jpg", "shot_id": "shot_001"}],
  "shots": [{
    "id": "shot_001",
    "start_s": 0.0, "end_s": 4.2, "duration_s": 4.2,
    "method": "ai_video", "route": "ai_video", "confidence": 0.82,
    "evidence": [],
    "assets": ["shot_001_first_frame"],
    "prompts": {"image": "", "video": "", "video_en": ""},
    "prompt_status": "reviewed|fallback",
    "generation": {
      "engine": "seedance2.5", "generation_type": "first_last_frame|route_specific",
      "start_image": "/abs/path/first.jpg", "end_image": "/abs/path/last.jpg",
      "duration_s": 4.2, "requires_human_review": true
    },
    "constraints": {"aspect_ratio": "9:16", "duration_s": 4.2, "continuity": []},
    "packaging_ids": [], "audio_ids": [],
    "validation": ["duration matches source shot"],
    "review_flags": [], "unresolved": [],
    "promotion": {"decision": "promoted|needs_review|reference|skipped|candidate", "route": "ai_video",
                  "checks": {"V1_source_sufficiency": {}, "V2_executability": {}, "V3_task_utility": {}},
                  "gate": {}, "missing": [], "fix_hints": [], "warnings": [],
                  "v3_status": "verified_by_reviewer|assumed|unverified", "evidence": []}
  }],
  "assembly": {"audio_policy": "original", "transition_policy": "preserve", "results": "remake/results.json"},
  "provenance": {"breakdown": "/abs/path/breakdown.json", "tool": "video-recreate"}
}
```

要点：
- `routes` 的值是**该路线的总时长（秒）**，不是镜头数。
- `prompt_status: fallback` 且 route 属于 `ai_video / avatar / cg_3d / film` 时，`review_flags` 会要求人工复核，`video_api.py` 默认拒绝执行。
- `constraints.aspect_ratio` 是约简后的比例（如 `9:16`），不是像素尺寸。
- 命中 `material.has_real_material` 的镜头会多一条 `status: replacement_required` 素材，晋级门同时以 `V2.material` 拦下该镜头。
- **晋级门默认开启**：`summary.promotion.decisions` 是各判定计数，`demoted` 是客观证据不足被降为 `manual_review` 的镜头，`assumed_suitability` 是 AI 适配性未经视觉验证却已晋级的镜头 ID。逐镜判定见 `shots[].promotion`，规则与排查表见 `promotion-gate.md`。
- `v3_status: assumed` 不代表镜头有问题，只表示标签来自自动规则、需要人看一眼首尾帧。

## 2. `recreation_jobs.json`

由 `build_recreation_jobs.py` 生成，**不含任何密钥**。

```json
{
  "breakdown": "/abs/path/breakdown.json",
  "manifest": "/abs/path/recreation_manifest.json",
  "engine": "seedance2.5",
  "mode": "first_last_frame",
  "created_at": "2026-11-09T21:00:00",
  "jobs": [{
    "shot": 1, "id": "shot_001",
    "executor": "video_api|local_ffmpeg|manual_review",
    "label": "ai_video", "route": "ai_video", "tool": "VideoAPI",
    "params": {
      "prompt": "", "generation_type": "first_last_frame", "engine": "seedance2.5",
      "aspect_ratio": "9:16", "duration": 5,
      "start_image_path": "/abs/path/first.jpg", "end_image_path": "/abs/path/last.jpg"
    },
    "trim_to": 4.2,
    "prompt_source": "fallback",
    "user_assets": [],
    "warning": ""
  }]
}
```

要点：
- `shot` 是**全部镜头的 1-based 序号**（含非 AI 镜头），`--only` 用它筛选；`id` 才是镜头稳定 ID。
- `params.duration` 是 `max(--min-duration, ceil(实际时长))`，通常比 `trim_to` 长；装配时按 `trim_to` 裁回原长。
- 只有 `route == "ai_video"` 会变成付费任务；其它路线保留 `review_prompt`（若有）供人工交接。
- `executor: local_ffmpeg` 只是标记，本包**没有**对应渲染实现。

## 3. `results.json`

由 `video_api.py` 增量写入，键为 `shot` 序号的字符串。

```json
{
  "results": {
    "1": {
      "id": "shot_001", "fingerprint": "sha256...", "trim_to": 4.2,
      "task_id": "cgt-...", "status": "completed",
      "video": "/abs/path/api_videos/shot_001.mp4"
    }
  }
}
```

状态流转：`submitting` → `submitted` → `completed` / `failed`。
- `submitting` 表示提交结果未知，重跑会**拒绝重试**并要求先去服务商后台核对。
- `failed` 不会被自动重提；需要换一份新的 results 文件。
- `error` 仅在失败时写入，且内容是脱敏后的原因（不含服务商响应体或签名 URL）；成功后会被移除。
- `fingerprint` 绑定配置与请求体，任务包被改动后拒绝继续，避免用旧任务 ID 生成错误内容。
- 成功下载的 mp4 在同目录 `api_videos/shot_XXX.mp4`，已通过 ffprobe 校验存在视频流且时长 > 0。
