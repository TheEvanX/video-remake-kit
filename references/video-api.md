# 视频模型 API 接入

不依赖 capsule-cinema。Python 3.10+ 运行提交和轮询（仅标准库）；**下载后必须能用 `ffprobe` 检验视频流与时长，因此 ffmpeg/ffprobe 需预先加入 PATH**。结果是 assemble 可读取的 `results.json`。以下命令均在 skill 目录下执行，也可把 `scripts/` 换成脚本实际路径。

先跑一次离线自检：`python scripts/doctor.py --config video-api.local.json --jobs output/remake/recreation_jobs.json`。

## 方舟接口：三项配置

先运行配置向导（也支持 `--base-url`、`--model` 参数）：

```bash
python scripts/configure_video_api.py --preset ark \
  --base-url https://ark.cn-beijing.volces.com/api/v3 \
  --model <你的模型ID> --out video-api.local.json
```

向导会填写 `base_url` 和账户可用的 `model`（模型 ID 或接入点 ID）。也可复制 `assets/video-api.ark.example.json` 手动修改。模型实际的时长、首尾帧能力、比例枚举必须和配置一致，示例的 4–30 秒不是所有模型的保证。向导默认不提问：缺 `--base-url` 或 `--model` 会直接报错，只有加 `--interactive` 才在终端逐项输入。Windows 下重定向 stdin 时 `isatty()` 仍可能返回 True，因此这里不依赖终端检测，避免 Agent / CI 静默挂起。

API Key 不写入 JSON；真实运行时终端隐藏输入，或设置 `VIDEO_API_KEY` 环境变量。不要将 Key 发到对话、放进任务文件或提交到代码仓库。

```bash
python scripts/video_api.py --config video-api.local.json --jobs output/remake/recreation_jobs.json --dry-run
python scripts/video_api.py --config video-api.local.json --jobs output/remake/recreation_jobs.json --only 1 --execute
```

默认拒绝未复核的 fallback prompt。检查提示词后可加 `--accept-fallback` 进行试生成。`--execute` 会调用用户账户的付费接口；dry-run 不联网、不需要 Key。

预检会先对**全部**待提交镜头做校验，再发起任何网络请求。若有问题，dry-run 输出的 `errors` 按 shot 号列出所有原因并以退出码 1 结束；`--execute` 会把同样的问题汇总成一条错误，且不提交、不索要 Key。这样 30 个镜头里只有 1 个越界时，也能一次看清是哪一个、为什么，而不是整批被一条无头错误挡住。

### 方舟协议的两个易错点

- `duration` 必须是**整数秒**。运行器会把 5.0 规范成 5；非整数时长会直接拒绝，避免付费调用时返回 400。生成任务包时的 `--min-duration` 应与配置 `min_duration_s` 一致，镜头时长会按 `max(min_duration, ceil(实际时长))` 向上取整，再靠 `trim_to` 裁回原长。
- `ratio` 必须是模型支持的比例枚举。运行器先把 `1080:1920` 这类像素比约简为 `9:16`，再按配置 `supported_ratios` 校验；不在白名单内直接拒绝，不会静默改成别的构图。非标准画幅可用 `ratio_aliases` 显式映射到最近的受支持比例，例如 `{"4:5": "3:4"}`。

`extra_body` 会原样并入方舟请求体，可用于关闭模型默认生成的音轨（`"generate_audio": false`）、设置 `watermark`、`resolution` 等。这些字段并非所有模型都支持，旧模型若被拒绝请把 `extra_body` 清空为 `{}`；默认值保持 `{}` 以避免误发未知字段。`doctor.py` 会在 `generate_audio` 未设置时给出提醒。

重新运行同一命令会复用保存的 task_id，继续轮询或下载，不重复提交。网络中断导致创建结果未知时会暂停，要求先到服务商后台查证；可将查到的 task_id 写回该镜头记录并将 status 改成 submitted。确定创建失败才用新的结果文件重提，避免重复扣费。

## 其他 JSON 视频接口

复制 `assets/video-api.generic.example.json`，根据供应商官方文档调整一次字段映射，即可供其他用户只填地址、模型、Key 使用：

| 参数 | 含义 |
|---|---|
| `submit_path` | POST 创建任务的路径 |
| `status_path` | GET 查询路径，`{task_id}` 自动替换 |
| `request_template` | 请求 JSON；支持 model/prompt/duration/aspect_ratio/first_frame/last_frame 模板变量 |
| `task_id_field` | 创建响应的任务 ID，例如 data.id |
| `status_field` | 查询响应的状态字段 |
| `video_url_field` | 成功响应的视频下载地址；支持数组下标如 data.videos.0.url |
| `auth_header` / `auth_prefix` | 例如 Authorization + Bearer 或 x-api-key + 空字符串 |
| `image_mode` | data_uri：本地图转 Base64；url：必须填 HTTPS 图片 URL |
| `media_urls` | 本地路径到用户自己的 HTTPS 图片地址映射，不自动上传到第三方存储 |
| `min_duration_s` / `max_duration_s` | 模型约束，越界拒绝执行，需重新拆分镜头 |
| `supported_ratios` / `ratio_aliases` | 允许的比例枚举白名单与非标准比例显式映射；为空数组表示不校验 |
| `extra_body`（仅 ark） | 额外的顶层请求字段，例如 `generate_audio`、`watermark`、`resolution` |
| `supports_last_frame` | 不支持尾帧时明确拒绝，不静默丢弃尾帧 |

同步返回视频 URL 也支持。专有 JWT 签名、multipart、WebSocket、SDK 私有协议还需单独适配；不存在所有厂商统一的视频 API。示例 generic 的路径和字段是协议模板，不代表任何供应商已通过认证。

只自动执行 ai_video 路线；实拍、3D、数字人、代码渲染不会被静默转换为 AI 视频。参考图中的字幕/包装需先按目标工作流处理，API 层不会自动抹除。

## 装配

API 运行器默认写入任务文件同目录的 `results.json` 和 `api_videos/shot_XXX.mp4`。在同目录提供 `jobs.json`（从 recreation_jobs.json 复制）后：

使用你当前分析/剪辑工具中兼容 `results.json` 的装配命令，生成 `remake.mp4`、`compare.mp4` 和装配日志。

没有生成结果的镜头仍是原片占位。报告必须区分已生成与占位，不把装配成功说成复刻完成。

## 验证范围

- `python scripts/test_video_api.py`：本地模拟 HTTP 服务，覆盖提交、轮询、下载、ffprobe 校验、结果恢复、鉴权、失败分支、比例与时长约束、逐镜头预检汇总。
- `python scripts/test_build_scripts.py`：清单与任务包生成的路由映射、提示词语言、比例约简、最小时长、素材路径。
- `python scripts/test_intake_scripts.py`：本地合成视频跑通 Stage 0（链接入口、平台识别、场景切镜、首尾帧、SRT/VTT/JSONL 转写对齐、`--promote` 自动晋级）直到 dry-run，不联网。
- `python scripts/test_promotion_gate.py`：三重验证、晋级门五判据、降级后不生成付费任务、只读候选报告。
- `python scripts/doctor.py [--source source.json]`：环境与配置自检，不联网。

v0.3.0 起 `build_recreation_manifest.py` 会在生成任务包前先跑晋级门，并用 `--gate-config` 复用本文件的 `min_duration_s` / `max_duration_s` / `supported_ratios` / `ratio_aliases` / `supports_last_frame`。两处共用一套约束，避免「门槛放行、预检才拒」。规则见 `@references/promotion-gate.md`。

以上都不需要真实 Key。真实供应商仍需用各自账户做一条小样验证；未填写 Key 不会自动发起付费生成。

方舟协议依据官方文档：
- https://docs.volcengine.com/docs/ark/video-generation-tutorial?lang=en
- https://console.volcengine.com/ark/region%3Acn-beijing/docs/create-video-generation-task-api
- https://console.volcengine.com/ark/region%3Acn-beijing/docs/get-video-generation-task-api
