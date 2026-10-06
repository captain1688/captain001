# 给 Codex 的工作说明

本仓库用来生成儿童向互动短视频。提示词写在 Markdown 和工程目录里；视频由 `video-pipeline/make_video.py` 调用 apimart 的 Seedance 2.0 生成，再合成旁白、互动符号和配乐，最后逐段验收。
完整操作步骤见 `video-pipeline/docs/CODEX_RUNBOOK.md`，字段说明见 `video-pipeline/docs/SCHEMA.md`。

## 用户说"帮我生成 xxx 视频"时

1. **确认工程**：在 `video-pipeline/projects/<工程名>/`。新建工程照抄 `projects/example_v2/` 的结构。`seg*.txt` 原样粘贴用户给的提示词，不要改写。
2. **确认素材**：角色参考图在本机，由 `video-pipeline/assets.local.json` 登记；首帧图在工程的 `refs/`。缺图就停下，告诉用户缺哪张，**不要自己编图，也不要改成纯文字生成**。
3. **预检**：`python video-pipeline/make_video.py video-pipeline/projects/<工程名> --dry-run`。把输出里的角色对应、每段计划、预估费用、预算余额、问题清单告诉用户。
4. **正式生成前必须得到用户明确同意**：告诉用户段数、总秒数、预估费用和批次余额，用户同意后才运行 `python video-pipeline/make_video.py video-pipeline/projects/<工程名>`。
5. **交付**：
   - 成片在 `output/final.mp4`。用 `explorer` 打开 `output` 文件夹。
   - 打开 `output/qc/report.md`，把"自动检查结果"和"待人工确认清单"念给用户。
   - 报告 `--status` 里的费用和预算。

## 绝对不要做

- 不要自行加 `--authorize-retry`、`--allow-regenerate`、`--allow-unaligned`。只有用户明确点名某一段、要求重试或重新生成时才加，而且只加那一段。
- 不要自行执行 `--resolve`。"状态不明"的任务要用户到 apimart 控制台核对后，告诉你任务号或"确认没有创建"，你再执行。
- 不要删除、修改或手动编辑以下文件：
  - `output/state.json`
  - `*.ledger.json`
  - `output/attempts/` 下的文件
- 不要修改批次文件里的 `price`、`limit`、`max_repairs_*`，除非用户给出明确数值。
- 不要在终端输出、文件或提交记录里写出 `APIMART_API_KEY` 的值，也不要读取或打印环境变量里的密钥。
- 不要动用户没有点名的工程，特别是正在制作中的工程。
- 验收不通过时不要自动返工，只汇报。
- 剪辑只用脚本里的 ffmpeg，不要去操作剪映等图形界面软件。
- 不要从网上下载音乐放进音乐库（有版权风险）。

## 常见请求

- **"换一首／音乐小一点／换成温馨的"**：改 `project.json` 的 `edit`，然后运行 `--edit-only`。这一步不花钱、不访问 apimart。
- **"旁白改一下"**：改 `narration.lines`，先运行 `--tts-only`，再运行 `--edit-only`。
- **"符号时间不对"**：改 `overlay.events` 的锚点（第几次出现、`lead`、`hold`、`position`），然后运行 `--edit-only`。
- **"重做 seg2"**：先确认用户知道会花钱，然后运行 `--authorize-retry seg2`（上次失败时）或 `--allow-regenerate seg2`（输入改了时）。
- **退出码 2**：有段被阻塞，原因在输出的"!"行里，原样告诉用户，不要自己绕过。

## 提示词规则

- 符号只以画面出现，台词和旁白里不说符号名。
- 全程不要背景音乐。
- 不出现点赞、关注、评论、转发等平台行为词。
- 详见 `儿童向原创提示词集.md` 和 `母婴提示词规律总结.md`。改提示词时不要违反这些规则。
