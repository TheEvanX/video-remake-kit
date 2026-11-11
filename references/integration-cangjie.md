# 与 cangjie-skill 的结合点

来源：`github.com/kangarooking/cangjie-skill` v2.5.0（MIT）。它把书、长视频、播客里的**方法论**蒸馏成可调用的 Agent Skills，核心是 RIA-TV++ 流水线 + Capability Bundle 单一事实源 + 确定性编译。

一句话边界：**它处理「视频里说了什么」，本 Skill 处理「视频里长什么样」。** 同一支视频可以做两条互补的拆解，但它不能替你产出 `breakdown.json`。

> v0.2.0 更新：本 Skill 已自带 Stage 0（`fetch_source.py` + `build_breakdown.py`）从链接/本地视频产出 `breakdown.json`。因此 cangjie / video-downloader 的角色从「可能的上游」变成**明确的下游素材提供方**：它们给的是带时间戳的文本，用来补分镜语义，不再是拆解结果的来源。平台支持矩阵见 `@references/source-intake.md`。
>
> v0.3.0 更新：结合点 3（三重验证 + 晋级门）已落地为 `scripts/promotion_gate.py`，并在拆解与清单两个阶段生效；`evidence` 字段直接对齐 `source-document` 契约。规则全文见 `@references/promotion-gate.md`。

## 关键事实（已核对源码）

- 仓库中**没有任何** ffmpeg / ffprobe / 抽帧 / 场景切分 / 图像处理的 Python 代码。`scripts/build_chunks.py` 里 `time_start_ms` / `time_end_ms` / `bbox` / `asset_ref` / `confidence` 全部是 `None` 占位；时间轴 IR 只存在于 `docs/plans/2026-08-23-...md` 的已移出主线设计草案里。
- `cangjie.py doctor` 只查 PyYAML / tiktoken / jsonschema 和 3 个 schema 文件，**不查 ffmpeg**。本 Skill 的 `scripts/doctor.py` 借用了它「`[ok]/[FAIL]/[warn]` + `doctor: PASS/FAIL` + 退出码」的骨架，补上了 ffmpeg/ffprobe 与配置检查。
- 配套 `video-downloader` 在另一个仓库；cangjie 仓库内只有 README 三处提及，没有字段约定、没有调用示例。

## 可落地的四个结合点

### 1. 字幕/转写 → 分镜级提示词（价值最高）

cangjie 处理长视频的入口是字幕或音频转写文本，`methodology/02-stage1-parallel-extract.md` 提供了按自然边界切块、每块附全局锚点、候选标 `chunk_id` 的做法。

**已在 v0.2.0 落地（方式更保守）**：`build_breakdown.py --transcript` 把 SRT/VTT/JSONL/`platform_subtitles.json` 按镜头时间窗对齐，写入 `shots[].transcript`，再由 `fallback_prompts()` 合成中英文语义句子。它**没有**直接写进 `review.replication.video_prompt`——那样会被当成「已复核提示词」，绕过付费前的复核门。转写只进入 fallback 提示词，`prompt_status` 保持 `fallback`，`review_flags` 追加 ASR 误差提醒。

这解决一个真实缺陷：原来的 fallback 提示词只有「主体、构图、色彩一致」这类视觉套话，完全不知道镜头在讲什么。对知识类、口播类短视频，语义信息比运镜描述更能决定复刻质量。

### 2. `source-document` 契约 → `breakdown.json` 字段对齐

`schemas/contracts/source-document.schema.json` 用 `time_start_ms` / `time_end_ms` / `asset_ref` / `confidence` / `content_hash` 描述「时间轴上的证据单元」，`type` 枚举含 `transcript` / `frame` / `image`。这套命名和本 Skill 的镜头字段几乎一一对应，具体映射表见 `references/breakdown-schema.md`。

如果上游拆解器同时服务两套流程，建议在上游保留 cangjie 契约，输出本 Skill 时做一次薄转换；不要反过来让本 Skill 去兼容两种格式。

### 3. 三重验证 + 晋级门 → 镜头路由的判定规则

`methodology/03-stage1.5-triple-verify.md` 的 V1 要求记录 `source_id` + 页码/**时间戳**/`chunk_id`，V2 先固定输入与完成标准再演练，V3 检查任务增益，分流为 `verified / reference / needs_review / rejected`；`methodology/03b-stage1.6-promotion-gate.md` 要求 `independent_intent / contract / run` 三项必过、`reuse / eval` 至少一项。

**已在 v0.3.0 落地**：`scripts/promotion_gate.py` 把 V1/V2/V3 与五判据映射成镜头级纯函数，`build_breakdown.py --promote` 用它自动标候选，`build_recreation_manifest.py` 默认再跑一次作为付费前硬门槛。

关键的诚实边界：cangjie 的 V3 要求「任务增益有可检查价值」，但「这条镜头该不该用 AI 生成」必须由 VLM 或人判断，**脚本不假装验证过**。因此 V3 只输出 `verified_by_reviewer / assumed / unverified` 状态与告警，不设硬门槛；真正拦下的是 V1/V2 的客观证据。同样按 cangjie「未晋级候选不进入 rejected、不能静默消失」的要求，被降级镜头保留提示词与素材，`missing` / `fix_hints` 写入 `shots[].promotion` 和 `summary.unresolved`。完整映射表见 `@references/promotion-gate.md`。

### 4. 压力测试与 SKILL 模板 → 触发质量

`methodology/06-stage4-pressure-test.md` 要求每个 skill 配 3–5 条 `should_trigger`、2–3 条 `should_not_trigger`（其中至少一条是「应触发兄弟 skill」的跨 skill 混淆）、1–3 条 `edge_case`；`templates/test-prompts.json.template` 和 `benchmarks/naval/.../test-prompts.json` 有现成格式（`minimum_pass_rate: 0.8`）。

本 Skill 的 `description` 同时含「视频复刻」和「视频模型接入」，很容易被「帮我总结这个视频」「把视频转成文字」误触发。加一份 `test-prompts.json`，把「总结视频内容」「提取字幕」列为应触发 cangjie/video-downloader 而非本 Skill 的诱饵，是有实际收益的。

`templates/SKILL.md.template` 的六段骨架（R / I / A1 / A2 触发场景 / E 可执行步骤 / B 边界）也可用于补强本 Skill 的「何时不调用」和「判停条件」——目前这两块偏弱。

## 不要指望它做什么

| 期待 | 现实 |
|---|---|
| 直接产出 `breakdown.json` | 不能，无视觉处理代码 |
| 提供镜头时间轴 / 首尾帧 | 不能，只有文本 chunk |
| 装配 `remake.mp4` / `compare.mp4` | 不能，完全不涉及 |
| 在 cangjie 仓库里定义 `video-downloader` 的字段约定 | 没有；本包 v0.3.2 已按上游源码自行定义 `{out_dir}` 适配器契约，见 `@references/source-intake.md` |

上游缺口现在由本包 Stage 0 的视觉拆解器（场景切分 + 抽帧 + 转写对齐）填上了；cangjie 能提供的是**语义侧输入**和**验证方法**，不是视觉侧输入。

## 落地进度

| 项 | 状态 |
|---|---|
| `test-prompts.json` 触发/诱饵用例 | 已完成（v0.1.1；v0.2.0 扩充链接入口；v0.3.0 增加晋级诊断触发用例与「要求全自动放行」边界诱饵） |
| 转写按时间窗进入分镜提示词 | 已完成（v0.2.0，且保持复核门有效） |
| `doctor` 自检骨架 | 已完成（v0.1.1；v0.2.0 增加 yt-dlp / source.json；v0.3.0 增加晋级报告诊断） |
| 三重验证 + 晋级门用于路由判定 | **已完成（v0.3.0）**：`promotion_gate.py` + `--promote` + manifest 默认硬门槛；V3 保持人工，证据字段对齐 `source-document` 契约 |
| 共用上游时的字段转换 | **待做**：映射表已在 `breakdown-schema.md`，但尚未写成可执行转换脚本 |

四个结合点里三个已落地。剩下的字段转换脚本优先级不高——Stage 0 已能直接从链接/本地视频产出 `breakdown.json`，两套流程共用上游的场景减少了。真正仍未解决的是 **V3 的视觉判定**：需要 VLM 或人工逐镜看首尾帧，判断实拍/录屏/UI 文字/3D 是否适合重生成。这是精度瓶颈，不是工程设计问题；在接入 VLM 之前，晋级门会一直把它标成 `assumed`，不会替用户下结论。
