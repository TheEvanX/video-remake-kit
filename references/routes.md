# Recreation routes

| Route | Use when | Required inputs | Acceptance check |
|---|---|---|---|
| `ai_video` | Motion, environment, or stylized imagery must be generated | start/end frames or references, prompt, model, duration | identity/subject continuity, duration, composition, motion |
| `film` | A controllable real scene is the most faithful route | performer, product, location, camera and lighting plan | framing, lighting, movement, product/identity fidelity |
| `avatar` | A presenter is voice- or lip-sync-driven | portrait/identity asset, voice, script, pose and camera notes | lip-sync, face consistency, timing |
| `cg_3d` | Geometry, repeatable camera motion, or product animation matters | model or reconstruction plan, materials, camera, lights | silhouette, materials, camera path, render/composite |
| `code_render` | Text, UI, charts, or template animation must be exact and editable | data, layout, typography, easing, timing | OCR/text correctness, timing, editability |
| `screen_capture` | The real interface or operation is part of the evidence | authorized app/project, viewport, cursor plan | readable UI, correct operation, cursor timing |
| `still_motion` | A still image can reproduce the shot with measured movement | source image, crop, scale/position curves | crop, movement, duration |
| `audio` | Speech, music, or effects must be rebuilt independently | transcript/script, voice, music/effect specs, event timestamps | duration, loudness, timing, sync |
| `manual_review` | Evidence is insufficient or an automated route is unsafe | unresolved question and requested user decision | explicit decision recorded before generation |

Use replacement assets for protected source material and keep its status visible in the manifest.

**v0.3.0 起 `ai_video` 还要过晋级门**（`references/promotion-gate.md`）：V1 来源充分性、V2 可执行性或晋级门五判据任一不通过，镜头降为 `manual_review`，原因写入 `shots[].promotion.missing` 与 `summary.unresolved`，不会生成付费任务。V3「这条镜头该不该用 AI 生成」只作告警，仍需人工复核首尾帧。

