# Codex 执行说明（视频管线 v2）

把这份文件整段交给 Codex 即可。根目录的 `AGENTS.md` 是同一套规则的精简版，Codex 会自动读取。

## 0. 版本

- 仓库：`https://github.com/captain1688/captain001`
- 分支：`claude/epic-goodall-thd8a8`
- 管线代码提交：`c21ee636696e0377cd79795b371351085de346b3`（管线版本 2.0.0）。本说明所在的提交在它之后，只增加了文档。
- 拉取：
  ```powershell
  cd ~\captain001
  git fetch origin
  git checkout claude/epic-goodall-thd8a8
  git pull
  git log --oneline -3     # 应能看到 c21ee63
  ```

## 1. 本轮边界（必须遵守）

- **不要启动、重投或修改用户正在制作的 19 条工程。** 只处理用户本次明确点名的工程。
- 正式生成会花钱。每次正式生成前，都要把预检结果告诉用户，**得到明确同意**后再运行。
- 不读取、不打印、不写入 `APIMART_API_KEY` 的值。
- 不发布成片。

## 2. 路径

| 内容 | 位置 |
|---|---|
| 入口脚本 | `video-pipeline/make_video.py` |
| 工程 | `video-pipeline/projects/<工程名>/`（示例：`projects/example_v2/`） |
| 每段提示词 | `projects/<工程名>/seg1.txt`、`seg2.txt`…… |
| 首帧图 | `projects/<工程名>/refs/`（文件名以 `project.json` 的 `first_frame` 为准） |
| 角色参考图 | 用户本机任意位置，登记在 `video-pipeline/assets.local.json`（模板：`assets.local.example.json`，不进仓库） |
| 批次预算 | `video-pipeline/batches/<批次>.batch.json`（示例：`example.batch.json`） |
| 配乐库 | `video-pipeline/music/<情绪>/` |
| 输出 | `projects/<工程名>/output/`：`final.mp4`、`timeline.json`、`state.json`、`qc/report.md`、`qc/*.png`、`attempts/` |

## 3. 预算（生成前必须就绪）

1. 复制 `batches/example.batch.json` 为本批次文件，比如 `batches/2026-10-a.batch.json`，填写以下字段：
   - `budget.limit`：本批次总上限。
   - `budget.currency`：币种，要和 apimart 实际扣费的币种一致。
   - `budget.max_repairs_per_segment`、`budget.max_repairs_total`：修复次数上限。
   - `pricing[].price`：用户在 apimart 模型页核对过的单价。**价格为 `null` 时管线会拒绝提交**。另外填 `unit`（`second` 或 `task`）、`source`、`checked_at`。
2. 工程的 `project.json` 里写 `"batch": "../../batches/2026-10-a.batch.json"`。
3. 价格、上限、修复次数只能按用户给的数值填，Codex 不得自行估填。

## 4. 素材核对

1. `project.json` 的 `characters` 里，每个角色都有 `image`（`asset:<key>` 或工程内路径），每段的 `characters` 写明出场角色。
2. `assets.local.json` 里有对应的 key，指向的图片在本机真实存在。
3. 每段的首帧图在 `refs/` 里。

任何一项缺失，预检都会报错。把报错原样告诉用户，**不要自己生成或替换图片，也不要把段改成 `mode: text`**。

## 5. 命令（都在仓库根目录 `~\captain001` 运行）

```powershell
# 预检：不联网、不花钱、不需要密钥
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --dry-run

# 正式生成（用户同意后）：生成 → 旁白 → 剪辑 → 验收
python video-pipeline/make_video.py video-pipeline/projects/<工程名>

# 只合成旁白（edge-tts，免费，需联网；需先 python -m pip install edge-tts）
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --tts-only

# 独立重剪：只用已有片段和已缓存旁白，不访问 apimart、不合成 TTS
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --edit-only

# 只重新验收
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --qc-only

# 查看每段尝试记录和预算
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --status

# 离线自测（不联网、不花钱，约 3 分钟）
python -m unittest discover -s video-pipeline/tests -v
```

只有用户明确点名时才能使用的命令（都会花钱，或者会改变核对结论）：

```powershell
# 上次失败或被拒的段，用户要求重试
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --authorize-retry seg2
# 提示词或参考图改了，用户要求重新生成
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --allow-regenerate seg2
# 用户在 apimart 控制台核对了"状态不明"的任务
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --resolve seg2 --attempt <attempt_id> --task-id <任务号>
python video-pipeline/make_video.py video-pipeline/projects/<工程名> --resolve seg2 --attempt <attempt_id> --not-created
```

## 6. 退出码与处理

| 码 | 含义 | Codex 应该做 |
|---|---|---|
| 0 | 完成，自动验收通过 | 打开 `output` 文件夹，念 `qc/report.md` 里的待人工确认清单 |
| 1 | 配置错误或被拒（缺图、坏图、旁白念了符号名、旁白越界、时间轴无法对齐、重复启动） | 原样转告，按提示改配置；不要绕过 |
| 2 | 有段未完成或被阻塞（超预算、价格不明、未授权重试、输入已变化、状态不明、等待超时） | 原样转告"!"开头的行；等待超时可以直接重跑同一命令（只查询、不重投） |
| 3 | 成片已出，但自动验收不通过 | 汇报失败项和联系图位置；**不要自动返工** |

## 7. 实现与验证状态

### 已实现，且有离线测试覆盖（31 项全部通过，测试环境为 Linux + ffmpeg 6.1）

- **参考图**：缺图、坏图、素材清单缺 key 时停止；纯文字模式带角色时拒绝；参考图模式自动生成"图N＝角色"映射；参考图变化后旧结果不算完成，需要 `--allow-regenerate`。
- **预算**：价格不明、没有预算、单工程超预算时都拒绝；两个进程并发共用一个批次预算只放行一个；旧版在途任务计入预算。
- **防重复扣费**：
  - 重复启动被拒。
  - 写状态时断电，文件仍完整，而且不重投。
  - 提交成功但没存下 ID，会标为"状态不明"并阻塞（授权重试也不重投），人工 `--resolve` 后继续。
  - 502 状态不明时保留预占；确认未创建后释放，但仍需授权才重投。
  - 失败后保留原 task ID，授权重试受修复上限限制。
  - 明确拒绝时释放预占。
  - 旧版 `state.json` 自动迁移。
  - 上传失败时干净地报错，不提交。
- **时间轴**：同一行旁白里三次"这个"分别对到三个词；第 N 次不存在时报错而不是猜；旁白念符号名被拒；没有词时间戳时拒绝合成。全功能合成验证了 5 种符号都画进了画面，并加上了第二段的偏移。
- **音频与剪辑**：有声和无声片段混剪时原声保留、无声段补静音；独立重剪不访问接口和 TTS；旁白越界报错且不截断；接尾帧模式去掉重复帧；验收失败只报告不返工；预检不需要密钥。

### 已实现，但未测试（第一次真实使用时要盯着看）

- **edge-tts 真实合成**：需要联网，离线测试只用了模拟提供方。中文 WordBoundary 的切词粒度（"这个"会是一个词还是两个字）没有实测。定位逻辑按字拼接匹配，两种情况都能处理，但要看第一次的 `--tts-only` 输出确认。
- **apimart 真实返回**：
  - 视频地址和尾帧地址的字段路径，是按文档推测、宽松解析的。原始返回保存在 `output/attempts/<段>/<attempt_id>_result.json`。
  - `cost` 字段的币种和单位未确认，所以默认按配置价格估算记账。
  - 失败任务是否扣费未知，所以默认保守地计为已花费。
- **Windows**：文件锁走 msvcrt 分支，没有在 Windows 上跑过；路径和中文文件名只在 Linux 上验证过。第一次在 Windows 上建议先跑一遍离线测试。
- **参考图模式**：提示词前加"图N＝角色"能否被 Seedance 正确理解，没有实测。
- **验收阈值**：夜景等暗场面可能被黑帧检测误判，需要时调整 `qc.black_min`；符号叠加在真实画面上的观感、侧链压低配乐的力度，也都还没在真实素材上看过。

### 仍需人工确认（管线不会、也不能自动判断）

- 角色是否变脸、换装；动作是否兑现；画面里由模型生成的符号是否正确；口型；段间衔接；整体音量。管线会在 `qc/report.md` 里逐段列为待确认，并给出联系图和符号检查图。
- "状态不明"的任务：只有用户能在 apimart 控制台核对。
- apimart 的真实单价和扣费币种：由用户核对后填入批次文件。
- **服务端没有幂等支持**：在"服务端已接单、但回包丢失"这个窗口里，管线能做到不自动重投，但不能保证服务端那边绝对没有重复任务。
