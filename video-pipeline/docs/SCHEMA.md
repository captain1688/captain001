# 配置与状态 Schema（管线 v2.0.0）

所有 JSON 都是 UTF-8。以 `_` 开头的字段是注释，会被忽略。完整例子：`projects/example_v2/project.json`、`batches/example.batch.json`。

---

## 1. `project.json`（工程配置）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string | 否 | 显示用 |
| `model` | string | 否 | 默认 `seedance-2.0-mini` |
| `resolution` | string | 否 | `480p`/`720p`/`1080p`/`4k`，默认 `480p` |
| `size` | string | 否 | 画幅，默认 `9:16` |
| `generate_audio` | bool | 否 | 让模型生成原声，默认 `true` |
| `seed` | int | 否 | 随机种子 |
| `batch` | string | 提交时二选一 | 批次文件路径（相对工程目录），多条工程共用预算和账本 |
| `budget` / `pricing` | object / array | 提交时二选一 | 单条工程自带预算（格式同批次文件），账本在 `output/budget.ledger.json` |
| `assets_manifest` | string | 否 | 额外的本地素材清单路径 |
| `characters` | object | 提交时需要 | `{角色ID: {"name": "显示名", "image": "工程内路径" 或 "asset:<key>"}}` |
| `require_character_refs` | bool | 否 | 默认 `true`：每段必须声明出场角色才能提交；确实没有角色时设为 `false` |
| `ref_legend` | bool | 否 | 参考图模式下自动在提示词前加"图N＝角色"，默认 `true` |
| `ref_legend_template` | string | 否 | 默认 `【参考图对应】{items}。` |
| `segments` | array | 是 | 见下 |
| `narration` | object | 否 | 见下 |
| `overlay` | object | 否 | 见下 |
| `edit` | object | 否 | 见下 |
| `qc` | object | 否 | `duration_tolerance`（秒，默认 0.6）、`black_min`（最短计入的黑场秒数，默认 0.5） |

### `segments[]`

| 字段 | 说明 |
|---|---|
| `id` | 分段 ID，唯一 |
| `prompt_file` | 提示词文件（相对工程目录） |
| `duration` | 4–15 秒 |
| `mode` | `first_frame` / `reference` / `chain` / `text`。不写时按旧规则推断（有 `continue_from_previous`→chain，有 `first_frame`→first_frame，有参考图/角色→reference）；什么都没有时**报错，不会推断成 text** |
| `first_frame` | 首帧图（工程内路径或 `asset:<key>`），`first_frame` 模式必填 |
| `characters` | 本段出场角色 ID 列表，不写时默认为全部已声明角色 |
| `reference_images` | 额外参考图，仅 `reference` 模式；与角色图合计不超过 9 张 |
| `continue_from_previous` | 旧写法，等价于 `mode: chain` |

### `narration`

```json
{"provider": "edge", "voice": "zh-CN-XiaoyiNeural", "rate": "+0%", "pitch": "+0Hz", "volume": 1.0,
 "banned_words": ["额外禁止念出的词"],
 "lines": [{"id": "n1", "segment": "seg1", "start": 1.0, "text": "我有这个、这个，还有这个！"}]}
```
- `provider`：`edge`（需 `pip install edge-tts`）／`file`（每行再给 `audio` 和 `words`，`words` 是 `[{"text","offset","duration"}]`，单位秒）／`mock`（只用于测试）。
- 每行可单独覆盖 `voice`/`rate`/`pitch`。`start` 是相对所属分段开头的秒数。
- 文字中出现以下词会报错：加号、十字、爱心、红心、心形、五角星、星星、金星、六六六、666、分享、转发、箭头、点赞、关注、评论、收藏，以及 `banned_words` 里的词。

### `overlay`

```json
{"enabled_symbols": ["plus", "heart", "star", "666", "share"],
 "size": 0.24, "position": {"x": 0.5, "y": 0.3}, "lead": 0.0, "hold": 0.8, "fade": 0.12,
 "require_all_enabled": true, "assets_dir": null,
 "events": [{"id": "e1", "symbol": "heart", "segment": "seg1", "narration": "n1",
             "anchor": {"text": "这个", "occurrence": 2}, "end_anchor": null,
             "position": {"x": 0.5, "y": 0.28}, "size": 0.24, "lead": 0.0, "hold": 0.9, "fade": 0.12}]}
```
- `symbol` 必须在本条的 `enabled_symbols` 里。可选值：`plus` 红＋、`heart` 红心、`star` 金星、`666`、`share` 分享箭头。
- `anchor` 三选一：
  - `{"text","occurrence"}`：在 `narration` 指定的那一行里找第 N 次出现，从 1 开始数。
  - `{"word_index"}`：该行第几个词，从 0 开始数。
  - `{"time","end_time"}`：相对分段开头的手动时间，会标为"手动、未对齐"。
- 开始 = 锚点词开始 − `lead`；结束 = `end_anchor` 词结束，或锚点词结束 + `hold`。
- `position` 是符号中心点，取相对画面宽高的 0～1；`size` 是相对画面宽度的比例。
- `require_all_enabled`：`enabled_symbols` 中每个符号都至少要有一个可对齐的事件，否则报错。
- `assets_dir`：放自定义符号 PNG 的目录（同名覆盖默认贴图 `assets/symbols/*.png`）。

### `edit`

| 字段 | 默认 | 说明 |
|---|---|---|
| `output` | `final.mp4` | 成片文件名（在 `output/` 里） |
| `original_volume` | 1.0 | 模型原声音量 |
| `narration_volume` | 1.0 | 旁白音量 |
| `bgm` | 无 | `"auto"` 按 `bgm_mood` 从 `music/<情绪>/` 选曲，或填文件名，或 `null` 不配乐 |
| `bgm_mood` | `欢快` | |
| `bgm_volume` | 0.3 | |
| `bgm_fade_in` / `bgm_fade_out` | 0.5 / 1.5 | 秒 |
| `duck` | true | 人声（原声＋旁白）出现时压低配乐 |
| `loudnorm` | true | 统一到 −14 LUFS |

---

## 2. 批次文件 `batches/*.batch.json`

```json
{"batch_id": "2026-10-第一批",
 "budget": {"limit": 10, "currency": "USD", "max_repairs_per_segment": 1, "max_repairs_total": 3,
            "release_failed_without_cost": false, "actual_cost_currency": null},
 "pricing": [{"model": "seedance-2.0-mini", "resolution": "480p", "generate_audio": true,
              "unit": "second", "price": null, "currency": "USD", "source": "核对来源", "checked_at": null}]}
```
- `limit`：批次总上限，算法为"已花费 + 在途预占 + 本次"。
- `max_repairs_per_segment`：每段最多再提交几次，包括 retry 和 regenerate，默认 0。`max_repairs_total` 为全批次上限，可不填。
- `pricing` 按 `model`、`resolution` 匹配；若写了 `generate_audio`，也要求一致。`price` 为 `null` 时一律拒绝提交。
- `actual_cost_currency`：只有确认服务端返回的 `cost` 字段与预算同币种时才填，此时完成后按服务端费用记账，否则按估算记。
- `release_failed_without_cost`：服务端失败且没给费用时是否释放预占，默认 `false`，即按已花费保守记账。

## 3. 本地素材清单 `assets.local.json`（不进仓库）

```json
{"assets": {"xiangjiaomao_main": "D:/素材/角色/香蕉猫_正面.png"}}
```
查找顺序（同名 key 先找到的生效）：
1. `project.json` 的 `assets_manifest`
2. 工程目录 `assets.local.json`
3. `video-pipeline/assets.local.json`
4. 环境变量 `VP_ASSETS_MANIFEST`

相对路径按清单文件所在目录解析。

---

## 4. 状态 `output/state.json`（schema `vp-state/2`，不要手改）

```json
{"schema": "vp-state/2", "project": "example_v2",
 "uploads": {"<图片sha256>": {"url": "...", "at": 1730000000.0, "name": "seg1_first_frame.png"}},
 "segments": {"seg1": {"attempts": [ <attempt>, ... ]}},
 "events": [{"at": "...", "event": "migrated_from_v1"}]}
```

`attempt`：
| 字段 | 说明 |
|---|---|
| `attempt_id` | 每次尝试唯一 ID，例 `att_20261006153000_1a2b3c4d` |
| `origin` | `initial` / `retry` / `regenerate` / `legacy_v1` |
| `status` | `intent` → `submitted` → `completed`；或 `failed` / `cancelled` / `rejected` / `not_sent` / `unknown` / `not_created` |
| `fingerprint` | 本段生成输入的 sha256（模型、分辨率、画幅、音频、种子、时长、模式、提示词哈希、每张图哈希、角色图哈希、管线版本）。旧版记录为 `null` |
| `inputs` | `{fingerprint, spec, prompt（完整提示词）, images:[{role,label,character,path,sha256,from_attempt}], characters:[{id,name,ref,sha256,sent_to_api}], config_version}` |
| `request_sha256` | 实际请求体（去掉临时图片 URL）的哈希 |
| `price` | `{estimate, price, unit, currency, source, checked_at}` |
| `reservation` | `{key, amount, currency, ledger, batch_id}` |
| `task_id` | 服务端任务号（拿到即落盘；失败也保留） |
| `error` / `raw` | 失败时的服务端返回 |
| `video` / `video_sha256` / `last_frame` | 完成后的文件（`output/attempts/<段>/<attempt_id>.mp4`），并复制一份为 `output/<段>.mp4` |
| `cost` / `credits_cost` | 服务端返回的费用字段，原样记录 |
| `history` | `[{at, status, note}]` 每次状态变化 |

## 5. 账本 `*.ledger.json`（schema `vp-ledger/1`，不要手改）

```json
{"schema": "vp-ledger/1", "batch_id": "...", "currency": "USD",
 "entries": {"<工程>/<段>/<attempt_id>": {"project": "...", "segment": "seg1", "attempt_id": "...",
             "origin": "initial", "estimate": 0.8, "actual": null, "state": "reserved|spent|released",
             "at": "...", "history": [["时间", "reserved"], ["时间", "spent", "completed"]]}}}
```
- 已花费 = 所有 `spent` 条目之和：有 `actual` 用 `actual`，否则用 `estimate`。
- 在途 = 所有 `reserved` 条目的 `estimate` 之和。

## 6. 时间轴 `output/timeline.json`（每次剪辑生成）

记录：
- 各段实际使用的 attempt、偏移、时长，以及该段是否补了静音
- 每行旁白的绝对起止时间和对齐质量
- 每个符号事件的绝对起止时间、对齐方式（`word` / `partial` / `manual_time` / `unaligned`）、锚定的词和词时间
- 被跳过的事件、全部问题，以及混音参数

## 7. 验收报告 `output/qc/report.json` / `report.md`

- `status`：`fail` 表示自动检查有失败项；`pending_manual` 表示自动检查通过、等待人工确认。
- `segments[]` 记录每段的检查项、联系图和待人工项。`final` 记录成片的检查项、联系图和符号检查图。
- `auto_failures` 是失败项列表，`manual_pending` 是待人工确认清单。
