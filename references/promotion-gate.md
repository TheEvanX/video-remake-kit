# 镜头晋级门（Promotion Gate）

适用于 `v0.3.0+`。规则借鉴 `cangjie-skill` v2.5.0（MIT）的 RIA-TV++ 阶段 1.5 三重验证与阶段 1.6 独立 Skill 晋级门，映射到「一个镜头能不能变成付费 AI 生成任务」这个问题上。

## 它解决什么

v0.2.0 的标签只有两种状态：

| 旧做法 | 问题 |
|---|---|
| 默认全部 `unknown` → `manual_review` | 30 个镜头要人工逐个填 `base.label`，等于没自动化 |
| `--assume-ai-video` 一刀切 | 把实拍、录屏、UI 文字、3D 渲染全送进付费接口，且完全不检查时长/比例/帧 |

晋级门把机器能判的和机器判不了的分开：**客观证据自动判定并自动晋级，主观适配性明确标注为待人工确认，客观不合格的镜头写清缺什么后留在 `manual_review`。**

## 三重验证的映射

| cangjie 原判据 | 本 Skill 的镜头版 | 硬门槛 | 具体检查项 |
|---|---|---|---|
| **V1 来源充分性**：可定位的原文足以支持候选 | 可回查的时间窗 + 视觉锚点 + 明确标签 | **是** | `start/end/duration` 为正、有序且一致；`assets.first_frame` 存在；`base.label` 属于生成类。证据写进 `evidence`（含 `location`、`time_start_ms`、`asset_ref`、`confidence`） |
| **V2 可执行性**：换一组输入能按契约完成并检验 | 模型能真的跑出可验收结果 | **是** | `ceil(duration) <= max_duration_s`；比例约简后在 `supported_ratios`（或有 `ratio_aliases`）；有尾帧时模型必须 `supports_last_frame`；不含需授权替换的真实素材。**无尾帧不拦截**，只写入 `warnings`（首帧驱动的长运动镜头约束较弱） |
| **V3 任务增益**：固化后对任务有具体帮助 | AI 重生成对这条镜头是否合适 | **否（仅告警）** | 只记录状态：`verified_by_reviewer`（有 `review.replication.video_prompt`）、`assumed`（自动/一刀切标签）、`unverified`（其余）。**脚本不声称自己验证过 AI 适配性** |

V1/V2 不通过 → `needs_review`，`route` 降为 `manual_review`。V3 永远不拦，但会写进 `warnings`，最终仍要过 `video_api.py` 的 `--accept-fallback` 用户确认才会付费。

### 晋级门五判据（阶段 1.6）

| # | cangjie | 本 Skill | 要求 |
|---|---|---|---|
| 1 | 独立意图 | 一个自包含时间窗 + 自己的首帧 | 必过 |
| 2 | 独立契约 | 固定输入（帧/提示词/比例/时长）+ 可验收输出 | 必过 |
| 3 | 独立运行 | 不依赖其他镜头的生成结果（无 `depends_on` 等） | 必过 |
| 4 | 独立复用 | 同批次 ≥2 个镜头共用引擎 | 与 5 至少一条 |
| 5 | 独立评测 | 存在正/负断言（帧存在、时长合规、比例合规） | 与 4 至少一条 |

第 5 条对本 Skill 恒真是有意的：镜头级任务天然可断言，负例在 `test_promotion_gate.py` 里逐条测。它不构成「凑数通过」，因为 1/2/3 才是真正的筛选器。

## 判定去向

| decision | 条件 | 下游处理 |
|---|---|---|
| `promoted` | V1 + V2 + 晋级门全过 | 保留 `ai_video`，进入任务包 |
| `needs_review` | V1/V2/晋级门任一不过 | 降为 `manual_review`；`missing` / `fix_hints` 写入 `shots[].promotion`，`missing` 同时写入 `summary.unresolved`；**提示词、素材、事件全部保留**，不静默删除 |
| `reference` | 非 AI 路线（`film` / `avatar` / `cg_3d` / `still_motion` …） | 原 route 不变，不参与付费晋级 |
| `skipped` | 显式 `--no-gate` | v0.2.0 行为 |
| `candidate` | 仅独立报告 CLI 对 `unknown` 镜头的只读探测 | 不改文件，只提示「客观条件齐备，把 `base.label` 改成 `ai_video` 即可晋级」 |

## 三个入口

### 1. 先看报告，不改任何文件（推荐第一步）

```bash
python scripts/promotion_gate.py \
  --breakdown /path/to/breakdown.json \
  --config video-api.local.json \
  --out /path/to/gate_report.json
```

对未标 label 的镜头做**只读候选探测**。stdout 给出 `decisions` / `candidates` / `demoted` / `assumed_suitability`；完整逐镜检查在报告文件里。不联网、不读 API Key、不修改 `breakdown.json`。

### 2. 拆解时自动晋级（`build_breakdown.py --promote`）

```bash
python scripts/build_breakdown.py \
  --source /path/to/source/source.json \
  --out /path/to/breakdown.json \
  --transcript /path/to/transcript.srt \
  --promote --gate-config video-api.local.json
```

客观条件齐备的镜头自动写 `base.label: ai_video` + `label_source: promotion_gate:auto_candidate`，其余保持 `unknown`。每个镜头都带 `shots[].promotion`。`--promote` 与 `--assume-ai-video` 互斥；后者仍保留作兼容开关，但会打印「未经证据检查」警告。

不带 `--gate-config` 时用保守默认（4–30 秒、**不强制比例白名单**，避免给别的供应商凭空造限制）。传了配置才按 `supported_ratios` / `ratio_aliases` / `supports_last_frame` 校验。

### 3. 付费前的硬门槛（`build_recreation_manifest.py`）

```bash
python scripts/build_recreation_manifest.py \
  --breakdown /path/to/breakdown.json \
  --out /path/to/recreation_manifest.json \
  --gate-config video-api.local.json \
  --gate-report /path/to/gate_report.json
```

**默认开启**，是任何来源的 `breakdown.json`（手写、VLM、Stage 0）都要经过的关卡。被降级的镜头不会出现在 `recreation_jobs.json` 的 `video_api` 执行器里。

| 参数 | 作用 |
|---|---|
| `--gate-config` | 复用 `min_duration_s` / `max_duration_s` / `supported_ratios` / `ratio_aliases` / `supports_last_frame`，与 `video_api.py` 预检共用一套约束 |
| `--gate-report` | 另存逐镜晋级报告 |
| `--gate-policy` | JSON 覆盖单项策略，如 `{"require_first_frame": false}` 允许纯文生视频 |
| `--no-gate` | 完全关闭（不建议，仅用于排查或兼容旧流程） |

### 4. 把晋级结果并入环境自检

```bash
python scripts/doctor.py --config video-api.local.json \
  --jobs /path/to/remake/recreation_jobs.json \
  --gate-report /path/to/gate_report.json
```

`doctor` 会打印 `promotion gate: N promoted, M demoted, K with unverified AI suitability`，并逐条列出被降级镜头的原因与修法。`--gate-report` 同时接受 `promotion_gate.py --out` 和 `build_recreation_manifest.py --gate-report` 两种报告。降级本身不算环境错误（doctor 仍为 PASS），但未验证的 AI 适配性会给出 warn。

## 排查被降级的镜头

`summary.promotion.demoted` 和 `shots[].promotion.missing` 会给出形如 `V2.duration: ...` 的条目，对应关系：

| missing | 原因 | 修法 |
|---|---|---|
| `V1.timing` | 时间窗缺失/倒序/与 duration 不符 | 重新拆解或修正 `breakdown.json` |
| `V1.reference_frame` | 没有首帧路径 | 抽帧；纯文生视频用 `--gate-policy '{"require_first_frame": false}'` |
| `V1.label` | label 不是生成类或仍是 `unknown` | 人工复核后设 `base.label: ai_video` |
| `V2.duration` | 超过 `max_duration_s` | 用 `--max-shot-s` 拆镜，或提高配置上限（模型必须支持） |
| `V2.aspect_ratio` | 画幅不在白名单 | 加 `ratio_aliases`，或把参考帧裁切/补边到目标比例；不要静默改构图 |
| `V2.last_frame` | 有尾帧但模型不支持 | 配置 `supports_last_frame: false` 并移除尾帧，或换支持尾帧的模型 |
| 缺尾帧（告警，不拦截） | 只有首帧，长运动镜头约束弱 | 重跑 `build_breakdown.py`（v0.3.1 起末尾抽帧会自动回退），或手工补 `assets.last_frame`；`doctor` 也会提示 |
| `V2.material` | 涉及需授权替换的真实素材 | 提供有授权的替代素材 |
| `independent_run` | 镜头依赖其他镜头的生成结果 | 拆成独立镜头，或保持 `manual_review` 人工装配 |

## 输出字段

`shots[].promotion`：`decision` / `route` / `checks`（V1、V2、V3 每项含 `passed` 与 `reason`）/ `gate`（五判据）/ `missing` / `fix_hints` / `warnings` / `v3_status` / `evidence`（对齐 cangjie `source-document` 契约的 `location`、`time_start_ms`、`asset_ref`、`confidence`）。

`summary.promotion`：`enabled` / `policy` / `decisions` 计数 / `demoted` / `assumed_suitability`（AI 适配性未经视觉验证却已晋级的镜头 ID 列表）。

## 明确不做的事

- **不判断「这条镜头该不该用 AI 生成」**。那是 VLM 或人的工作；自动标签一律标 `assumed`。
- **不修改输入文件**。`promotion_gate.py` 是只读报告；只有 `build_breakdown.py --promote` 会写自己新生成的 `breakdown.json`。
- **不静默丢弃任何镜头**。降级镜头保留提示词、素材、包装/音效事件和完整的缺失原因。
- **不读取 API Key、不联网、不调用模型**。
- **不替 `video_api.py` 做预检**。比例/时长在这里提前拦一道，但 dry-run 仍是最终校验。

## 与 cangjie 的对应源码

| 本文件机制 | 上游文件 |
|---|---|
| V1/V2/V3 与 `verified / reference / needs_review / rejected` | `methodology/03-stage1.5-triple-verify.md` |
| 五判据、1/2/3 必过 + 4/5 至少一条、不静默丢弃 | `methodology/03b-stage1.6-promotion-gate.md` |
| `evidence` 字段命名 | `schemas/contracts/source-document.schema.json` |
| 正/负断言的用例格式 | `methodology/06-stage4-pressure-test.md`、`templates/test-prompts.json.template` |

跨界点全貌见 `@references/integration-cangjie.md`。
