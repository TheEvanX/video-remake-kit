# `breakdown.json` 输入契约

`breakdown.json` 是 Stage 0 与执行链之间的契约。v0.2.0 起本包自带 `scripts/build_breakdown.py`，可从链接/本地视频生成该文件（场景切镜 + 首尾帧 + 可选转写对齐）；也可以由任何外部拆解器按本文格式产出。本文定义的是 `build_recreation_manifest.py` 实际读取的字段，字段命名以源码为准。

> Stage 0 默认把 `base.label` 写成 `unknown`（`manual_review`）。加 `--promote` 可让客观证据齐备的镜头自动成为 `ai_video` 候选；**无论哪种方式，AI 适配性仍需人工复核**，因为自动判定不识别实拍/录屏/UI 文字。详见 `@references/promotion-gate.md`。

所有时间单位为**秒**（float）。图片路径**相对于 `breakdown.json` 所在目录**解析，绝对路径也可。

## 顶层结构

```json
{
  "source":     { "path": "", "duration": 0, "width": 0, "height": 0, "fps": 0, "has_audio": true },
  "packaging":  { "text": [], "stickers": [] },
  "audio":      { "sfx": { "events": [] } },
  "shots":      [],
  "provenance": { "tool": "", "url": "", "platform": "", "scene_cuts": [],
                  "label_assignment": "manual_review_required | promotion_gate | assumed:ai_video",
                  "promotion": { "enabled": false, "promoted": 0, "demoted": 0 } }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source.path` | string | 建议 | 源视频绝对路径；文件存在时计算 SHA-256 写入 manifest 的 `source.hash` |
| `source.width` / `height` | int | 建议 | 用于推导画面比例；缺失时按 `9:16` 兜底 |
| `source.duration` / `fps` / `has_audio` | float / float / bool | 否 | 仅作元数据传递 |
| `packaging.text[]` / `stickers[]` | array | 否 | 每项需 `id`、`t0`、`t1`；与镜头时间窗相交则写入该镜头的 `packaging_ids` |
| `audio.sfx.events[]` | array | 否 | 每项需 `t`；落在镜头时间窗内则写入 `audio_ids` |
| `provenance` | object | 否 | Stage 0 写入来源 URL、平台、场景切点、转写文件与标签分配方式。**v0.3.0 起 `label_assignment` 会被晋级门读取**：`assumed:*` 或 `promotion_gate` 会把 AI 适配性标为未验证（`v3_status: assumed`），不加已复核提示词就自动放行 |
| `provenance.promotion` | object | 否 | `--promote` 的统计（`enabled` / `promoted` / `demoted` / `policy` / `note`），仅作交接追溯 |

## `shots[]` 每个镜头

```json
{
  "id": "shot_001",
  "start": 0.0,
  "end": 4.2,
  "duration": 4.2,
  "base": { "label": "ai_video", "confidence": 0.86 },
  "verdict": "ai_video",
  "assets": { "first_frame": "frames/shot_001_first.jpg", "last_frame": "frames/shot_001_last.jpg", "sheet": "frames/shot_001_sheet.jpg" },
  "features": { "camera": "slow push in" },
  "aspects": { "camera": { "result": { "move": "smooth" } }, "lighting": { "result": { "low_light": false } } },
  "deterministic": { "confidence": 0.8, "evidence": ["frame diff", "optical flow"] },
  "review": { "replication": { "video_prompt": "", "video_prompt_en": "", "image_prompt": "" } },
  "vlm": { "replication": { "video_prompt": "", "video_prompt_en": "", "image_prompt": "" } },
  "replication": { "capsule": { "video_prompt": "", "video_prompt_en": "", "image_prompt": "" } },
  "material": { "has_real_material": false, "origin": "generated" },
  "transcript": "",
  "promotion": { "decision": "promoted", "v3_status": "assumed", "missing": [], "fix_hints": [] }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | **是** | 全片唯一，作为 asset id 前缀和 `results.json` 的关联键；缺失会抛 `KeyError` |
| `start` / `end` / `duration` | **是** | 决定包装事件、音效事件的归属与 `trim_to` |
| `base.label` | **是** | 生产路线判定依据；缺失时回退 `verdict.label` / `verdict`，再兜底 `unknown` → `manual_review` |
| `base.confidence` | 否 | 回退到 `deterministic.confidence` |
| `deterministic.evidence[]` | 否 | 最多保留前 8 条写入 manifest |
| `assets.first_frame` / `last_frame` / `sheet` | 否 | 相对路径；`first_frame` + `last_frame` 同时存在才标记 `first_last_frame` 生成类型 |
| `features.camera` | 否 | 没有已复核提示词时用于生成 fallback 提示词的运镜描述 |
| `aspects.lighting.result.low_light` | 否 | 控制 fallback 提示词的布光描述 |
| `material.has_real_material` / `origin` | 否 | 命中时登记 `replacement_required` 素材，提醒需要授权替换素材 |
| `transcript` | 否 | Stage 0 按镜头时间窗对齐后的口播/字幕文本；仅用于合成 fallback 提示词语义 |
| `transcript_segments[]` | 否 | 该镜头的转写片段 `{t, text}`，便于人工复核 ASR 结果 |
| `review.replication` / `vlm.replication` / `replication.capsule` | 否 | **提示词来源，优先级依次降低**；三处任一有 `image_prompt` / `video_prompt` / `video_prompt_en` 即视为 `reviewed`。有 `review.replication.video_prompt` 时晋级门把 V3 记为 `verified_by_reviewer` |
| `base.label_source` | 否 | `--promote` 写入 `promotion_gate:auto_candidate`；晋级门据此认定标签来自自动规则而非人工判断 |
| `promotion` | 否 | `--promote` 写入的逐镜晋级记录（`decision` / `checks` / `gate` / `missing` / `fix_hints` / `warnings` / `v3_status` / `evidence`）。执行链不依赖它，manifest 会重新判定一次 |

**转写文本的安全约束**：`transcript` 是 ASR/字幕的自动产物，可能有错。它**不会**被写进 `review` / `vlm` / `replication.capsule` 三个「已复核」提示词来源，只参与 `fallback_prompts()` 的语义句子；因此 `prompt_status` 仍是 `fallback`，`review_flags` 仍要求人工复核。这样转写能提升提示词质量，却不会绕过付费前的复核门。

### 提示词优先级

`review.replication` → `vlm.replication` → `replication.capsule`，逐字段取第一个非空值。三处都为空则标记 `prompt_status: fallback`，由 `fallback_prompts()` 生成保守提示词，并在 `review_flags` 中登记；运行器默认拒绝执行这类镜头，需人工检查后加 `--accept-fallback`。

## `base.label` → `route` 映射

| label | route | 是否自动转 AI 任务 |
|---|---|---|
| `ai_video`, `animation_2d`, `anim_2d` | `ai_video` | 是 |
| `live_action`, `stop_motion` | `film` | 否 |
| `avatar` | `avatar` | 否 |
| `cg_3d` | `cg_3d` | 否 |
| `code_render` | `code_render` | 否 |
| `screen_capture` | `screen_capture` | 否 |
| `still_motion`, `static_hold` | `still_motion` | 否（`executor: local_ffmpeg`，需自备渲染实现） |
| 其他 / 缺失 | `manual_review` | 否，并写入 `summary.unresolved` |

`video_api.py` **只处理 `route == "ai_video"`**，其余路线一律不静默转换。

**v0.3.0 起 `ai_video` 还要过晋级门**：`build_recreation_manifest.py` 默认检查 V1 来源充分性（时间窗、首帧、标签）、V2 可执行性（时长、比例、尾帧能力、素材授权）和晋级门五判据；任一不过就降为 `manual_review`，把 `missing` 写进 `summary.unresolved`。V3（这条镜头该不该用 AI 生成）只告警、不拦截。完整规则与排查表见 `@references/promotion-gate.md`。

## 最小可运行示例

只要一个镜头也能跑通全链路：

```json
{
  "source": { "path": "source.mp4", "width": 1080, "height": 1920, "fps": 30, "duration": 4.0, "has_audio": true },
  "shots": [{
    "id": "shot_001",
    "start": 0.0, "end": 4.0, "duration": 4.0,
    "base": { "label": "ai_video", "confidence": 0.9 },
    "assets": { "first_frame": "frames/first.jpg", "last_frame": "frames/last.jpg" },
    "review": { "replication": { "video_prompt_en": "A slow push-in on the same subject, no text or watermark." } }
  }]
}
```

运行前用 `python scripts/doctor.py --config video-api.local.json --jobs remake/recreation_jobs.json` 检查图片路径、提示词、时长和 ffmpeg 依赖。

## 与 cangjie-skill 契约的字段对齐

cangjie-skill 的 `schemas/contracts/source-document.schema.json` 用 `time_start_ms` / `time_end_ms` / `asset_ref` / `confidence` / `content_hash` 表达「时间轴上的一个证据单元」。如果上游拆解器同时服务两套流程，建议在上游保留这套命名，输出本 Skill 时做一次薄转换：

| cangjie 字段 | 本 Skill 字段 | 转换 |
|---|---|---|
| `time_start_ms` / `time_end_ms` | `start` / `end` | 除以 1000 |
| `asset_ref`（frame 元素） | `assets.first_frame` / `last_frame` | 按帧在镜头内的位置取首尾 |
| `confidence` | `base.confidence` | 直接映射 |
| `content_hash` | `source.hash` | 由 `build_recreation_manifest.py` 自行计算 |
| `type: transcript` | 分镜级提示词 | 按时间窗拼接字幕文本，写入 `review.replication.video_prompt` |

注意：cangjie-skill 仓库**不包含**任何 ffprobe / 抽帧 / 场景切分的可执行代码，它的 `source-document` 中这些字段目前是 `None` 占位。它能提供的是字段契约与验证方法，不能替代上游拆解器。
