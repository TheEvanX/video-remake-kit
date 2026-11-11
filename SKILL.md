---
name: video-recreate
display_name: AI视频逆向拆解与复刻
display_name_en: AI Video Reverse Engineering and Recreation
description: 从视频链接或本地文件做镜头级视觉拆解，生成逐镜头 AI 视频复刻任务，通过用户自己的模型 API Key 提交、轮询、下载并输出可装配的结果。适用于视频复刻、换角色、首尾帧生成、短视频逆向制作和 AI 视频模型接入。
description_zh: 从链接/本地视频切镜、抽首尾帧、对齐字幕转写，再把拆解结果转成可执行的 AI 视频复刻任务；支持方舟及通用 JSON 视频 API，用户自行填写接口地址、模型 ID 和 API Key。
description_en: Cut a source video into shots from a URL or local file, extract first/last frames, align timed transcripts, then turn the breakdown into executable shot-level AI video jobs with configurable Ark or generic JSON APIs, private API-key input, polling and download validation.
examples_zh:
  - "从这个视频链接拆解镜头并生成 AI 复刻任务"
  - "把这份拆解清单生成 AI 复刻任务，先 dry-run 预检"
  - "接入我的方舟 API Key，先预检第 1 个镜头"
  - "用我的视频模型 API 生成这些首尾帧镜头"
  - "检查这份拆解清单里哪些镜头符合 AI 生成条件"
examples_en:
  - "Cut this video link into shots and build AI recreation jobs."
  - "Turn this video breakdown into AI recreation jobs."
  - "Connect my Ark API key and dry-run shot 1."
  - "Generate these first-last-frame shots with my video API."
  - "Check which shots in this breakdown are eligible for AI generation."
version: 0.3.2
author: "Evan AI"
allowed-tools: Bash, Read, Write
---

# AI 视频逆向拆解与复刻

这个 Skill 的主线是**视频拆解与复刻**：

```text
链接 / 本地视频 → 场景切镜 + 首尾帧 + 转写对齐 → breakdown.json
                → 镜头晋级门（客观证据自动判定 / 主观适配性人工确认）
                → 逐镜头 AI 生成任务 → results.json + 逐镜头 mp4 → （外部装配）
```

它**不做**方法论蒸馏、不生成 skill、不碰账号体系；也**不含**下游成片装配脚本。转写只用来给分镜提示词补语义。

## 环境依赖

- Python 3.10+，仅标准库，无需 pip 安装。
- **ffmpeg / ffprobe 必须在 PATH 中**：ffprobe 探测元数据与校验下载视频，ffmpeg 做场景切镜与首尾帧抽取。Windows 可用 `choco install ffmpeg`。
- `yt-dlp` **可选**：只有从 URL 取视频时才需要；本地文件和外部适配器不依赖它。
- 以下命令均在 skill 目录下执行；在其他目录执行时把 `scripts/` 换成脚本的绝对/相对路径。

```bash
python scripts/doctor.py --source /path/to/remake/source/source.json \
  --config video-api.local.json --jobs /path/to/remake/recreation_jobs.json \
  --gate-report /path/to/remake/gate_report.json
```

`doctor` 离线检查 Python、ffmpeg/ffprobe、yt-dlp、source.json、配置、模型 ID、图片路径、提示词、最小时长，以及晋级报告里被降级镜头的原因；不联网、不读取 API Key、不提交任务。退出码 0 为 PASS。

## 安全规则

- API Key 只通过环境变量 `VIDEO_API_KEY` 或隐藏式终端输入提供；不要写进 JSON、日志、提示词或压缩包。
- 默认先执行 `--dry-run`，只做字段检查，不联网、不扣费。
- 真实执行前先只生成一个镜头；失败或网络中断时复用 task ID，不自动重复提交。
- 不自动上传本地参考图到第三方存储。`data_uri` 模式将图片直接放入请求体，`url` 模式要求用户提供自己的 HTTPS 图片 URL 映射。
- 只自动处理 `ai_video` 路线。实拍、3D、数字人、代码渲染和人工复核路线保持独立。
- 只处理自己拥有或已获授权的视频。不实现微信客户端自动化，不保存 Cookie / Token / 签名 URL，不绕过登录或访问控制。微信视频号链接必须经外部适配器或手动保存为本地文件。
- 自动切镜**不会**自动判定生产路线：`base.label` 默认 `unknown` → `manual_review`。用 `--promote` 让客观证据齐备的镜头自动成为 `ai_video` **候选**，其余留 `manual_review` 并写明缺什么；`--assume-ai-video` 是不做证据检查的兼容开关，慎用。
- 晋级门只验证「能不能跑」，**不验证「该不该用 AI 生成」**。实拍、录屏、UI 文字、3D 渲染是否适合重生成，必须由人或 VLM 复核；自动标签一律记 `v3_status: assumed`，并在 `summary.promotion.assumed_suitability` 列出。
- 被降级的镜头保留提示词、素材和事件，写入 `summary.unresolved`，不会静默消失。

## 文件准备

先读取 `@references/source-intake.md`（链接入口与平台支持矩阵）、`@references/breakdown-schema.md`（输入契约）、`@references/promotion-gate.md`（镜头晋级规则）和 `@references/video-api.md`（接口配置），再选择一个配置模板：

- `assets/video-api.ark.example.json`：火山方舟兼容的异步视频任务接口；
- `assets/video-api.generic.example.json`：可按供应商文档调整字段的通用 JSON 接口。

向导默认**不提问**：必须显式传 `--base-url` 和 `--model`，缺任意一个会直接报错（退出码 2），不会在 Agent / CI 里挂住。需要在终端里逐项输入时加 `--interactive`：

```bash
python scripts/configure_video_api.py --preset ark \
  --base-url https://ark.cn-beijing.volces.com/api/v3 \
  --model <你的模型ID> --out video-api.local.json
```

向导只写入接口地址和模型 ID。API Key 在执行时通过环境变量或隐藏输入提供。配置中的 `supported_ratios` / `ratio_aliases` 需按模型文档调整；`extra_body` 可传 `generate_audio`、`watermark`、`resolution` 等模型相关字段，旧模型若不支持这些字段请把 `extra_body` 清空。

## 工作流

### 0. 从链接 / 本地视频得到拆解结果

```bash
python scripts/fetch_source.py --source '<视频链接或本地 mp4>' --out-dir /path/to/remake/source --dry-run
python scripts/fetch_source.py --source '<视频链接或本地 mp4>' --out-dir /path/to/remake/source

python scripts/build_breakdown.py \
  --source /path/to/remake/source/source.json \
  --out /path/to/remake/breakdown.json \
  --transcript /path/to/remake/source/transcript.segments.jsonl \
  --promote --gate-config video-api.local.json
```

`fetch_source.py` 用 yt-dlp 取单条视频与可选字幕，或用 `--downloader` 交接给外部适配器（微信视频号等）。模板支持两种产物形态：`{out}` 表示适配器直接写出单个 mp4，`{out_dir}` 表示适配器自己管理输出目录（`video-downloader` 这类 CLI）。可用 `--downloader-scope adapter-only` 让外部模板只用于视频号、其它平台仍走 yt-dlp。先跑 `--dry-run` 可只打印计划命令、不联网。完整契约见 `@references/source-intake.md`。`build_breakdown.py` 用 ffmpeg 做场景切镜、抽每镜首尾帧、把转写按时间窗对齐写入 `shots[].transcript`。

拆解器有三个来自真实视频的约束，使用前应知道：

- **尾帧回退**：镜头末尾时间点可能越过最后一帧而抽不到图；脚本会自动向前回退重试，实际抽帧秒数写在 `shots[].frame_times`，不再静默丢尾帧。
- **长镜头自动分段**：默认 `--max-shot-s 10`，无硬切的长镜头会按模型 `max_duration_s` 上限切成可复刻片段，`provenance.forced_splits` 会标记。这些边界**不是真实转场**，必须复核；模型最小生成时长过短时不自动切碎，改由晋级门列明时长缺口。
- **渐变/溶解转场**：ffmpeg 场景检测对溶解、淡入淡出和大幅运镜不可靠，实测一支有多镜头的 20 秒视频在阈值 0.3 下检出 0 个切点。看完 `frames/` 后用 `--cuts "4.0,13.5"` 传入复核过的精确边界，会完全跳过自动检测（`provenance.cut_source: manual`）。

`--promote` 跑镜头晋级门：时间窗、首帧、标签、时长、比例、尾帧能力、素材授权等**客观条件**齐备的镜头自动标成 `base.label: ai_video` 候选，其余保持 `unknown` 并在 `shots[].promotion.missing / fix_hints` 写明缺什么。不带 `--promote` 时全部 `unknown` → `manual_review`。

**产出后仍必须人工复核 AI 适配性**：晋级门不判断这条镜头是不是实拍、录屏或 UI 文字，自动标签一律记 `v3_status: assumed`。不加 `--gate-config` 时只用保守默认（4–30 秒，不强制比例白名单）。平台支持范围与视频号限制见 `@references/source-intake.md`。

已有 `breakdown.json` 时，可先出一份只读晋级报告，不修改任何文件：

```bash
python scripts/promotion_gate.py \
  --breakdown /path/to/remake/breakdown.json \
  --config video-api.local.json \
  --out /path/to/remake/gate_report.json
```

报告的 `candidates` 列出客观条件齐备但尚未标 label 的镜头，`demoted` 列出被拦镜头及修法。

### 1. 生成复刻清单

```bash
python scripts/build_recreation_manifest.py \
  --breakdown /path/to/breakdown.json \
  --out /path/to/recreation_manifest.json \
  --gate-config video-api.local.json \
  --gate-report /path/to/remake/gate_report.json
```

清单会为每个镜头保留路由、首尾帧、时长、包装事件、音效事件、提示词状态和人工复核标记。fallback prompt 可以用于预检，但正式生成前应检查。

**晋级门默认开启**，是任何来源的 `breakdown.json` 进入付费任务前的硬关卡：V1 来源充分性、V2 可执行性、晋级门五判据任一不过，该镜头降为 `manual_review`，`missing` 同时写入 `summary.unresolved`，不会生成付费任务。`--gate-config` 让门槛直接复用模型的时长/比例/尾帧配置；`--gate-report` 另存逐镜报告；`--gate-policy` 可覆盖单项（如 `{"require_first_frame": false}` 允许纯文生视频）；`--no-gate` 关闭检查（不建议）。判定规则与被降级镜头的排查表见 `@references/promotion-gate.md`。

### 2. 生成 API 任务包

```bash
python scripts/build_recreation_jobs.py \
  --manifest /path/to/recreation_manifest.json \
  --out /path/to/remake/recreation_jobs.json \
  --engine seedance2.5 --min-duration 4
```

任务包不包含 API Key。它会为 AI 视频镜头写入模型名、首尾帧、提示词、比例和时长；3D、实拍和其他路线不会被转换成 AI 任务，但会保留已复核提示词供人工交接。`--min-duration` 必须与配置中的 `min_duration_s` 一致，否则预检会拒绝时长越界的镜头。

### 3. 预检

```bash
python scripts/video_api.py \
  --config video-api.local.json \
  --jobs /path/to/remake/recreation_jobs.json \
  --dry-run
```

预检会验证模型、时长、首尾帧能力、图片路径、比例、协议字段和待处理镜头，不进行网络请求。它逐镜头收集全部问题：输出中的 `errors` 会带上 shot 号与具体原因，退出码 1；任一镜头有问题时整批不提交，而不是只抛出第一条错误。预检通过后再跑 `doctor.py` 可额外检查 ffmpeg 等环境依赖。

### 4. 执行一个镜头

```bash
VIDEO_API_KEY="你的Key" python scripts/video_api.py \
  --config video-api.local.json \
  --jobs /path/to/remake/recreation_jobs.json \
  --only 1 \
  --execute \
  --accept-fallback
```

`--accept-fallback` 只在用户已经检查 fallback prompt 后使用。运行器会写入 `results.json` 和 `api_videos/shot_XXX.mp4`，并用 ffprobe 检查视频流和时长有效性。

### 5. 装配

生成结果可以交给已有的装配工具；没有生成结果的镜头必须保留为原片占位，并在报告中标明。不能把“任务提交成功”表述成“视频复刻完成”。

## 供应商适配

通用 JSON 模板通过 `submit_path`、`status_path`、`task_id_field`、`status_field`、`video_url_field`、鉴权头、请求模板和时长限制适配供应商。不同视频厂商的请求协议、图片托管要求、尾帧能力和时长限制并不统一；遇到 multipart、WebSocket、专有签名或 SDK 私有协议时，需要新增适配器，不要把 Key 或签名写进公共 Skill。

## 上游与下游协作

- **上游**：Stage 0 已能从链接/本地视频产出镜头级 `breakdown.json`（场景切镜 + 首尾帧）。配套 `video-downloader` 可提供视频文件、平台字幕与 ASR 转写，作为 `--transcript` 输入；`cangjie-skill` 可把长视频蒸馏成方法论 skill，但那是**另一条语义轨道**，本 Skill 只借它的时间戳文本，不做蒸馏。cangjie 仓库本身没有抽帧/场景切分/ffprobe 代码，不能替代视觉拆解。平台矩阵见 `@references/source-intake.md`，字段对齐见 `@references/breakdown-schema.md`。
- **下游**：`results.json` + `api_videos/shot_XXX.mp4` 可交给兼容 `results.json` 的装配工具。本包不含装配脚本，`still_motion` 路线标记的 `local_ffmpeg` 执行器也需自备实现。

## 自检与测试

```bash
python scripts/doctor.py --source /path/to/remake/source/source.json --config video-api.local.json --jobs /path/to/remake/recreation_jobs.json
python scripts/test_video_api.py       # 本地模拟 HTTP，不联网、不需要 Key
python scripts/test_build_scripts.py   # 清单、任务包、向导
python scripts/test_intake_scripts.py  # 链接入口、切镜、首尾帧、转写对齐、--promote（本地合成视频，不联网）
python scripts/test_promotion_gate.py # 三重验证、晋级门五判据、降级与报告
```

`test-prompts.json` 是触发测试用例（含「总结视频」「蒸馏方法论」等不应触发本 Skill 的诱饵）；`references/integration-cangjie.md` 说明与 cangjie-skill 的字段对齐和协作边界。

详细字段说明见 `@references/breakdown-schema.md`、`@references/promotion-gate.md`、`@references/video-api.md`、`@references/recreate-schema.md` 和 `@references/routes.md`。版本变更见 `BUILD_INFO.json`。
