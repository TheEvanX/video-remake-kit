# video-remake-kit

> Turn a video link or a local file into a **shot-by-shot breakdown** (shot list, first/last frames, aligned transcript), then into **per-shot AI video generation jobs** — with a promotion gate that stops paid API calls until the evidence is complete.

`video-remake-kit` is an Agent skill for **video breakdown and AI recreation**. It takes one input — a video URL or an `.mp4` — and produces an auditable chain of artifacts: `source.json` → `breakdown.json` → `manifest.json` → `jobs.json` → per-shot MP4 results. It is built for people who need to *reverse-engineer a video into reproducible shots*, not for summarizing what a video says.

**Keywords**: video breakdown · shot list · shot-by-shot recreation · first frame / last frame · AI video generation · Seedance · Volcengine Ark · ffmpeg · yt-dlp · dry-run · Agent skill / WorkBuddy skill / Claude-style `SKILL.md`.

## What it does

```text
link or local video
  └─ fetch_source.py        download or reuse media + subtitles, ffprobe → source.json
      └─ build_breakdown.py scene cuts, per-shot first/last frames, transcript alignment → breakdown.json
          └─ promotion_gate.py objective evidence checks (V1/V2), human check for AI suitability (V3)
              └─ build_recreation_manifest.py → build_recreation_jobs.py → jobs.json
                  └─ video_api.py --dry-run (0 network calls) → --execute → results.json + shot_XXX.mp4
```

The whole chain is local, offline-testable and stdlib-only. `video_api.py` speaks two protocols: Volcengine Ark (`content[]`, `role: first_frame/last_frame`) and a configurable generic JSON API. API keys are read from the environment or a hidden prompt, never written to disk.

## Quick start

```bash
# requires ffmpeg / ffprobe in PATH; yt-dlp optional for URL intake
python scripts/fetch_source.py --source ./my-video.mp4 --out-dir remake/source
python scripts/build_breakdown.py --source remake/source/source.json \
  --out remake/breakdown.json --promote --gate-config video-api.local.json
python scripts/build_recreation_manifest.py --breakdown remake/breakdown.json \
  --out remake/manifest.json --gate-config video-api.local.json
python scripts/build_recreation_jobs.py --manifest remake/manifest.json --out remake/jobs.json
python scripts/video_api.py --config video-api.local.json --jobs remake/jobs.json --dry-run
python scripts/doctor.py --config video-api.local.json --jobs remake/jobs.json
```

`--dry-run` builds and preflights every request without a single network call, so no paid job is submitted by accident. See `references/video-api.md` for provider configuration and `references/source-intake.md` for platform support.

## How this is different

| | Typical "viral video breakdown" tools | This repo |
|---|---|---|
| Output | why a video went viral, rewritten scripts | shot boundaries, first/last frames, generation jobs |
| Unit of work | whole video / text | one shot, with its own frames and time window |
| Before spending money | n/a | promotion gate + per-shot `preflight()` + `--dry-run` |
| Labels | auto-marked as ready | `unknown` by default; auto-labelled shots are **candidates** only |
| Vendor | one model | Ark + generic JSON, configurable durations, ratios, frame support |

The gate deliberately does **not** decide whether a shot is suitable for AI regeneration (real footage, screen recordings and UI text need a human). Those labels are always recorded as `v3_status: assumed`.

## FAQ

**How do I split a video into shots for AI recreation?**
Run `fetch_source.py` then `build_breakdown.py`. ffmpeg scene detection gives a coarse cut; for dissolves and fade transitions add reviewed cut points with `--cuts "4.0,13.5"`. Every shot gets a first and last frame under `frames/`.

**Can it take a link instead of a file?**
Yes. Public URLs and yt-dlp-supported sites (Bilibili, YouTube, Douyin, Xiaohongshu, etc.) work out of the box. WeChat Channels (`weixin.qq.com/sph/...`) needs an **authorized external downloader**: this repo never automates WeChat, never stores cookies, and never bypasses access control. `references/source-intake.md` documents the `{out_dir}` adapter contract and a ready-to-use command for `kangarooking/video-downloader`.

**Which video models are supported?**
Volcengine Ark / Seedance-style APIs out of the box, plus any JSON API you can describe with `submit_path`, `status_path`, `task_id_field`, `status_field` and `video_url_field`. Non-integer durations, unsupported aspect ratios and missing frames are rejected before submission rather than silently altered.

**Does it assemble the final video?**
No. It produces per-shot clips and `results.json`. Editing, audio and final assembly are intentionally out of scope.

**Does it transcribe speech?**
It consumes SRT / VTT / JSONL transcripts to add semantics to shot prompts, but transcript text never enters a "reviewed" prompt source — `prompt_status` stays `fallback` and human review is still required.

**Will it charge my API key during testing?**
No. `--dry-run` makes zero network calls. `--execute` requires a real model ID and an API key supplied at runtime; submissions are idempotent via a `.lock` file and a request fingerprint.

## Repository layout

```
SKILL.md                       skill entrypoint and usage rules
scripts/
  fetch_source.py              Stage 0a: link/local file → source.json
  build_breakdown.py           Stage 0b: scene cuts + frames + transcript → breakdown.json
  promotion_gate.py            objective evidence checks, read-only reports
  build_recreation_manifest.py prompt assembly + hard gate before paid jobs
  build_recreation_jobs.py     per-shot job construction
  video_api.py                 Ark / generic JSON submit, poll, download, resume
  doctor.py                    diagnostics
  configure_video_api.py       non-interactive config generator
  test_*.py                    75 offline tests
references/                    schemas, platform support, provider config, gate rules
assets/                        example API configs
test-prompts.json              trigger / decoy cases for skill routing
BUILD_INFO.json                version history and verification record
```

## Requirements

- Python 3.10+, standard library only
- ffmpeg / ffprobe in `PATH` (required)
- yt-dlp (optional, for URL intake)
- A video API account only if you actually run `--execute`

Run the offline suite with `python -B -m unittest discover -s scripts -p "test_*.py"`.

## Known limits

- Scene detection is a coarse signal. Gradient/dissolve transitions need human cut points.
- Non-standard aspect ratios are blocked, not silently cropped — silent cropping would break visual fidelity.
- WeChat Channels and login-gated / paid / private content are not fetched by this repo.
- No real paid provider call is part of the test suite; verification stops at `--dry-run`.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文速览

这是一个面向**视频拆解与复刻**的 Agent skill：把一条链接或本地视频拆成逐镜头清单，抽取每镜首尾帧，按时间窗对齐字幕，再用晋级门筛出客观证据齐备的镜头，最后生成逐镜 AI 视频生成任务。核心设计是**不假装自动化**：AI 适配性一律留人工复核，付费调用前有 `--dry-run` 与逐镜预检，视频号链接不自动抓取、需有授权的外部下载器或手动保存本地文件。

完整链路与命令见 `SKILL.md` 和 `references/source-intake.md`。本包不含成片装配脚本。
