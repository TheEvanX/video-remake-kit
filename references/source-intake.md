# 从链接到拆解结果（Stage 0）

本 Skill 的长期缺口是上游：`video_api.py` 只消费 `breakdown.json`。Stage 0 补上这段，让「一条链接 / 一个本地文件」能变成可复刻的镜头清单。

```text
链接或本地文件
  → fetch_source.py     下载 / 复用视频 + 字幕，ffprobe 探测 → source.json
  → build_breakdown.py  场景切镜 + 首尾帧 + 转写按时间窗对齐 → breakdown.json
  → 人工/VLM 复核每个 base.label
  → 原有链路：manifest → jobs → dry-run → execute → results.json
```

**边界**：Stage 0 只做「视觉拆解所需的素材准备」。转写文本仅用于给分镜提示词补语义，**不做**方法论蒸馏、不生成 skill、不碰账号体系。这是与 cangjie-skill 的分工线。

## 平台支持矩阵

事实依据：`kangarooking/kangarooking-skills` 的 `video-downloader/SKILL.md`（v2.1）与 `scripts/providers/` 源码；本 Skill 自己**不实现**任何平台爬虫。

| 输入 | 本 Skill 默认路径 | 实际依赖 | 稳定性 |
|---|---|---|---|
| 本地文件 | 直接 ffprobe，不复制、不联网 | 无 | 高 |
| B站 `bilibili.com` / `b23.tv` | `yt-dlp` | yt-dlp；部分清晰度需 Cookie | 中 |
| YouTube `youtube.com` / `youtu.be` | `yt-dlp` | yt-dlp；可能需要 Node / 浏览器 Cookie | 中 |
| 抖音 `douyin.com` / `iesdouyin.com` | `yt-dlp`（上游 skill 优先走 H5 无水印路由再回退 yt-dlp） | 分享短链、风控、Cookie | 中 |
| 小红书 `xiaohongshu.com` / `xhslink.com` | `yt-dlp` | 上游 skill 有浏览器 Cookie 回退 | 中 |
| 其它 yt-dlp 支持的站点 | `yt-dlp` | X/Twitter、TikTok、Vimeo、Instagram、Twitch、Reddit 等，随 yt-dlp 提取器版本变化 | 中 |
| **微信视频号** `weixin.qq.com/sph/...` | **不直接处理**，需外部适配器 | 见下节 | **实验性** |
| 登录可见 / 好友可见 / 付费 / 私密内容 | **不支持** | — | — |

「抓取」在这里的含义很窄：**取单个视频文件 + 可选字幕轨 + 元数据**。不做账号登录态复用、不做批量采集、不抓评论/粉丝/推荐流、不绕过访问控制。

## 关于微信视频号：必须说清的现实

上游 `video-downloader` 把视频号明确标为 `experimental`，README 原话是：仅在 2026-09-07 用**一条公开分享链接**走通过端到端验证，「证明的是被测试的那条路径，不代表对每种账号、链接类型或未来平台变更都兼容」。它有三条路线，**没有一条是开箱即用的通用解析**：

1. 自建兼容解析 API：`WX_CHANNELS_API_URL` + `WX_CHANNELS_API_TOKEN`；
2. 已有的元宝会话 Cookie：`WX_CHANNELS_COOKIE`（`hy_user` / `hy_token`）；
3. 首次交互式授权：Playwright 打开隔离浏览器完成一次微信/元宝登录，之后只存三个字段，Cookie 文件权限 `0600`。

上游自己还写了兜底建议：直链解析失效时，用合规工具把视频保存到本地，再当本地文件传入。**这正是本 Skill 采用的策略**。社区里也有通过在线解析服务或 `yt-dlp weixin_video` 提取器处理 `sph` 链接的做法，但都依赖第三方服务与平台策略，随时可能失效，不能作为承诺。

因此本 Skill 的决定是：

- 遇到 `weixin.qq.com` / `channels.weixin.qq.com` / `video.weixin.qq.com` 域名时，`fetch_source.py` **拒绝直接下载**并给出明确指引，不假装能抓。
- 不实现微信客户端自动化、不读取用户浏览器 Profile、不保存任何 Cookie。
- 通过 `--downloader` / `VIDEO_RECREATE_DOWNLOADER` 把这一步交接给你自己选择的、有授权的工具；或者你手动保存成 mp4 后用 `--source <本地文件>` 走完全相同的后续链路。

## 实测记录：一条真实视频号分享链接（2026-11-11）

对 `https://weixin.qq.com/sph/Al2SLbRNDD` 做了完整只读探测，**在无授权条件下抓不到**：

| 检查项 | 实际结果 |
|---|---|
| 跳转 | 302 → `https://channels.weixin.qq.com/finder-preview/pages/sph?id=Al2SLbRNDD` |
| 分享页正文 | 2692 字节 SPA 壳：只有 `<title>视频号</title>` 和几个 JS bundle；无 `og:title` / `og:image` / `og:description`，无视频标题、作者、封面或播放地址 |
| HTTP HEAD | `405 Method Not Allowed`；GET 才返回上面的 HTML |
| yt-dlp 2026.07.04 | `ERROR: Unsupported URL: .../finder-preview/pages/sph?id=...`，没有对应提取器 |
| 本 Skill `fetch_source.py --dry-run` | 明确拒绝，退出码 1，提示改用外部适配器或本地 mp4 |
| 上游 `video-downloader --metadata-only --no-wechat-auto-login` | `YuanbaoAuthenticationError: WeChat Channels needs Yuanbao authorization` |
| 本机授权条件 | `WX_CHANNELS_API_URL` / `WX_CHANNELS_API_TOKEN` / `WX_CHANNELS_COOKIE` 均未设置；默认 cookie 文件不存在；Playwright 未安装 |

关键点：分享页把视频信息放在**需要鉴权的后端接口**之后，通用下载器连媒体地址都拿不到，所以这不是「换个阈值或解析器」能绕过的问题。要走通，必须先具备上文三条授权路线之一，或由使用者把视频保存到本地。

本次探测**未尝试交互式登录、未复用任何登录态、未调用第三方解析服务、未绕过访问控制**。

对照：同一环境下，普通公开直链 `https://www.w3schools.com/html/mov_bbb.mp4` 用 yt-dlp 一次下载成功（10.027s / 320x176 / 有音轨），`source.json` 带授权提醒；随后 `build_breakdown.py` 切出 2 镜并自动抽齐首尾帧，晋级门因 `20:11` 不在 `supported_ratios` 把两镜都留在 `manual_review`，dry-run 因此不产生付费任务。**链接入口能跑通，卡住的只有视频号的授权这一环。**

## 外部下载器适配器契约

模板有**两种输出模式**，对应两类工具的产物形态：

| 占位符 | 含义 | 适用工具 |
|---|---|---|
| `{url}` | 原始链接（必填） | 全部 |
| `{out}` | 单个 mp4 文件绝对路径 `.../source.mp4` | 自己写的简易适配器 |
| `{out_dir}` | 输出根目录，由工具自己建子目录 | `video-downloader` 这类 CLI |

模板必须含 `{url}`，`{out}` / `{out_dir}` 至少含一个；占位符可以嵌在参数里（如 `--output={out_dir}`）。命令按 `shlex.split` 解析、**不经过 shell**，URL 无法注入命令。Windows 路径里的反斜杠在解析前会自动归一化为 `/`。

约定：

- `{out}` 模式：适配器把最终 mp4 原样写到该路径；同目录的 `source.*.srt` / `source.*.vtt` 会被自动收集。
- `{out_dir}` 模式：适配器把媒体放进该目录（允许子目录）。`fetch_source.py` 优先读 stdout JSON 里的 `video_path`，**只接受 `--out-dir` 内的真实视频文件**；否则递归扫描该目录取最大的视频文件，字幕文件递归收集。
- 下载器的 stdout/stderr **不会**写进 `source.json`：其中可能含签名 URL 或 token，只读取 `video_path` 一个字段，且外部路径会被拒绝并留 warning。
- 退出码非 0 即失败。凭据由适配器自己从环境变量读取；本 Skill 不接收、不记录、不回显。
- `--downloader-scope adapter-only`（或 `VIDEO_RECREATE_DOWNLOADER_SCOPE=adapter-only`）：只对视频号这类需要适配器的平台用外部模板，B站 / YouTube / 抖音等仍走 yt-dlp。默认 `all` 表示所有 URL 都走外部模板。

### 直接对接 kangarooking/video-downloader

上游 CLI 的契约实测为：`download_video.py <URL> --output-dir <ROOT>` → 视频落在 `<ROOT>/wechat-channels-<id>/*.mp4`，stdout 打印含 `video_path` 的 JSON（见 `scripts/providers/wechat_channels.py:fetch` 与 `scripts/download_video.py`）。因此**必须用 `{out_dir}`，用 `{out}` 会接不上**：后者期望一个 mp4 文件路径，而上游要的是目录。

```bash
# 三条授权路线任选其一：
export WX_CHANNELS_API_URL="https://your-parser.example/api/channels/parse_sph"
export WX_CHANNELS_API_TOKEN="..."     # 或 WX_CHANNELS_COOKIE="hy_user=...; hy_token=..."
# 路线三（交互式元宝登录）则去掉下面的 --no-wechat-auto-login，并在真实终端运行

export VIDEO_RECREATE_DOWNLOADER_SCOPE=adapter-only
export VIDEO_RECREATE_DOWNLOADER='python /path/to/video-downloader/scripts/download_video.py {url} --output-dir {out_dir} --asr none --vision none --no-wechat-auto-login'

python scripts/fetch_source.py --source 'https://weixin.qq.com/sph/...' \
  --out-dir remake/source --dry-run     # 先只打印命令，不联网
python scripts/fetch_source.py --source 'https://weixin.qq.com/sph/...' --out-dir remake/source
```

两个必须注意的坑：

- **`--asr none --vision none` 不能省**：上游默认会跑 ASR 与视觉模型，在非交互环境下可能卡在后端选择菜单或因 `VisionInteractionError` 退出码 2，我们会误判成「下载失败」。
- **不要加 `--metadata-only`**：该模式下 `video_path` 为 `null`，不会下载视频。

代价是上游不产出 `transcript.segments.jsonl` / `platform_subtitles.json`。若需要转写补提示词，自行配置 ASR Key 后去掉 `--asr none`，再把对应文件用 `build_breakdown.py --transcript` 传入；`multimodal_transcript.md` 无结构化时间码，本链路不直接吃。

`--dry-run` 只打印将要执行的命令，不联网、不落盘，适合先确认参数：

```bash
python scripts/fetch_source.py --source 'https://www.bilibili.com/video/BV...' --out-dir remake/source --dry-run
```

## 转写 / 字幕输入

`build_breakdown.py --transcript` 可重复传入，也自动收集 `source.json` 里的 `subtitles`：

| 来源 | 字段 | 说明 |
|---|---|---|
| yt-dlp 下载的平台字幕 | SRT / VTT cue 时间 | `--no-subtitles` 可关闭 |
| `transcript.segments.jsonl`（video-downloader） | `start_ms` / `end_ms` / `text` | 云 ASR 结果是 60 秒分块精度，不是句级对齐 |
| `platform_subtitles.json`（video-downloader） | `segments[].start/end/text` | 平台真字幕，精度最高，优先使用 |
| 任意 SRT / VTT / JSONL / JSON | 自动识别 `start_ms/end_ms/from/to/t0/t1` 等 | 见 `build_breakdown.py:parse_transcript` |

`multimodal_transcript.md` 是给人和 cangjie 看的**无结构化时间码**的 Markdown，本链路不直接吃它；请改用同目录的 `transcript.segments.jsonl`。

转写片段按镜头时间窗对齐后写入 `shots[].transcript`，再由 `build_recreation_manifest.py` 合成中英文提示词。**关键安全约束**：它不会写进 `review/vlm/replication` 三个「已复核」提示词来源，因此 `prompt_status` 仍是 `fallback`、`review_flags` 仍要求人工复核。ASR 会错，自动生成的语义不能直接放行成付费任务。

## 切镜阈值需要按内容调

场景检测走 `fps=4,scale=384:-2,select='gt(scene,X)',showinfo`，默认 `X=0.32`。它只是**粗切**，不是完整镜头标注：硬切明显的内容能切对，渐变转场、快速蒙太奇、同一场景内的大动作可能漏切或过切。实测一段三段纯色硬切视频在 `0.2` 阈值下只检出 1 个切点。

调法：

- 漏切（一个窗口里明显有多个镜头）→ 降低 `--scene-threshold`，如 `0.15`；
- 过切（同一镜头被切成好几段）→ 提高，如 `0.45`；
- 太碎的短片段用 `--min-shot-s` 合并（默认 0.6 秒）；
- 超过模型最小时长上限的长镜头用 `--max-shot-s` 拆分（默认 30 秒），被拆出的片段在 `deterministic.evidence` 里标 `split_from_long_shot`。

每次调完看 `breakdown.json` 的 `provenance.scene_cuts` 和 `frames/` 里的首尾帧，确认切点后再往下走。**不要**把自动切镜结果当成已复核的镜头清单。

## 非标准画幅要先处理

社媒视频常见 `1080x1350`（4:5）、`1080x1260`（6:7）甚至 `320x568`（40:71）这类比例，而视频模型通常只接受 `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9` 等枚举。`video_api.py` 会先约简比例再按 `supported_ratios` 校验，**不会静默改成别的构图**——因为那会让复刻结果与参考画面不一致。

三种正确做法，按保真度排序：

1. 把参考帧裁切/补边到目标比例，再重跑 `build_breakdown.py`；
2. 在配置里显式写 `ratio_aliases`，例如 `{"4:5": "3:4"}`，并在装配阶段按 `trim_to` 与构图差异做检查；
3. 换用支持该比例的模型，并把它加进 `supported_ratios`。

`doctor.py` 不会替你做这个判断，它只检查镜头时长与图片路径；`--dry-run` 会在不联网的情况下把不合规的比例连同 shot 号一起报出来。v0.3.0 起传了 `--gate-config` 的晋级门会**提前一步**用 `V2.aspect_ratio` 拦下这些镜头，不需要等到 dry-run。

## 标签复核是一道硬门槛

`build_breakdown.py` 只能可靠地给出**镜头边界和首尾帧**，无法判断一个镜头该走 `ai_video` 还是实拍/3D/数字人/录屏。v0.3.0 起用晋级门把「机器能判的」和「人必须看的」分开：

- 默认每个镜头 `base.label: "unknown"` → 路由 `manual_review`，不会产生付费任务。
- `--promote`（推荐）跑客观证据检查：时间窗、首帧、标签、时长、比例、尾帧能力、素材授权都合规的镜头自动标成 `ai_video` **候选**，其余保持 `unknown`，并在 `shots[].promotion.missing / fix_hints` 写明缺什么。加 `--gate-config video-api.local.json` 可直接复用模型的时长/比例限制。
- `--assume-ai-video` 是**不做任何证据检查**的兼容开关，会把全部镜头标成 `ai_video` 并打印更强警告。它和 `--promote` 互斥，正常流程不要用。
- `build_recreation_manifest.py` 默认再跑一次晋级门，作为任何来源 `breakdown.json` 进入付费任务前的硬关卡；不合格镜头降为 `manual_review` 并写进 `summary.unresolved`，不会静默消失。

**晋级门不判断 AI 适配性**。自动标签一律记 `v3_status: assumed`，在 `summary.promotion.assumed_suitability` 里列出。推荐做法：跑一份只读 `promotion_gate.py` 报告，再看 `frames/` 里的首尾帧和 `multimodal_transcript.md` 之类的语义材料，确认候选镜头不是实拍、录屏或 UI 文字。完整判定规则与排查表见 `@references/promotion-gate.md`。

## 合规与授权

只处理你自己拥有、或已取得复制/改编授权的内容。不要绕过登录、付费墙、访问控制或反爬机制；不要把 Cookie、Token、签名 URL 写进配置、日志或压缩包。平台字幕与音频转写同样受版权与个人信息规则约束。生成结果里若含他人肖像、商标、音乐，需另行取得授权。

## 完整命令

```bash
# 0a. 取得源视频与字幕
python scripts/fetch_source.py --source '<URL 或本地 mp4>' --out-dir remake/source
python scripts/doctor.py --source remake/source/source.json

# 0b. 视觉拆解
python scripts/build_breakdown.py \
  --source remake/source/source.json \
  --out remake/breakdown.json \
  --transcript remake/source/transcript.segments.jsonl \
  --scene-threshold 0.32 --min-shot-s 0.6 --max-shot-s 30

# 0c. 复核并改写 remake/breakdown.json 里每个 shots[].base.label
# 然后接原有工作流
python scripts/build_recreation_manifest.py --breakdown remake/breakdown.json --out remake/recreation_manifest.json
python scripts/build_recreation_jobs.py --manifest remake/recreation_manifest.json --out remake/recreation_jobs.json --min-duration 4
python scripts/video_api.py --config video-api.local.json --jobs remake/recreation_jobs.json --dry-run
```
