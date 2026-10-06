# 自动生成视频 + 剪辑

用 apimart 的 Seedance 2.0 生成视频，默认 mini、480p、9:16。多段会**同时提交、同时生成**，生成完自动下载，再用 ffmpeg 拼成一条成片。

## 一次性准备（在你自己的电脑上）

1. **装 Python 3 和 ffmpeg**
   - Windows：`winget install Python.Python.3.12` 和 `winget install Gyan.FFmpeg`
   - Mac：`brew install python ffmpeg`
2. **拉取仓库**：`git clone https://github.com/captain1688/captain001.git`，然后切到 `claude/epic-goodall-thd8a8` 分支。
3. **设置 apimart 的 Key**（在 apimart 网站的 `/zh/keys` 页面获取）：
   - Windows（PowerShell，永久保存）：`setx APIMART_API_KEY "你的key"`，设置后重开终端才生效。
   - Mac：在 `~/.zshrc` 里加一行 `export APIMART_API_KEY="你的key"`。

Codex 用你的 ChatGPT 会员登录就行，不需要 OpenAI 的 API Key。仓库根目录的 `AGENTS.md` 会告诉 Codex 怎么操作。

## 让 Codex 干活

在仓库目录里打开 Codex，直接说，例如：

> 用 projects/xueren 生成视频。

> 把《刀盾狗的宝箱》做成一个新工程并生成，首帧图我放在 refs 里了。

Codex 会先预检，告诉你要花多少秒的生成量，等你同意后才正式生成。

## 自己手动跑

```bash
python video-pipeline/make_video.py video-pipeline/projects/xueren --dry-run    # 预检，不花钱
python video-pipeline/make_video.py video-pipeline/projects/xueren              # 生成 + 剪辑
python video-pipeline/make_video.py video-pipeline/projects/xueren --edit-only  # 只重新剪辑
```

## 工程目录长这样

```
projects/xueren/
├── project.json        # 配置：模型、分辨率、每段时长、首帧图、配乐
├── seg1.txt            # 上段提示词
├── seg2.txt            # 下段提示词
├── refs/               # 首帧图，以及首帧图的图片提示词（README.md）
└── output/             # 自动生成：seg1.mp4、seg2.mp4、final.mp4、state.json
```

`project.json` 里每段可以写：

| 字段 | 作用 |
|---|---|
| `first_frame` | 首帧图。推荐每段都给一张，各段就能并发生成 |
| `continue_from_previous: true` | 用上一段的尾帧当首帧，衔接最顺，但要等上一段生成完才能开始 |
| `reference_images` | 参考图，最多 9 张。**不能和上面两项同时用**（apimart 的限制） |
| `duration` | 4～15 秒 |

## 要知道的几件事

- **中断不怕**：中途关掉终端，重新运行会接着查询已提交的任务，不会重复扣费。
- **重做某一段**：删掉 `output/seg2.mp4`，再删掉 `state.json` 里 `seg2` 那一项，然后重新运行。
- **加配乐**：把音频文件放进工程目录，在 `project.json` 的 `edit.bgm` 里写文件名，再加 `--edit-only` 重新剪辑。
- **上传的首帧图在 apimart 只保存 72 小时**：脚本会在过期前自动重新上传。
- **提示词长度**：官方建议 mini 模型的中文提示词控制在 500 字以内，太长可能导致部分细节被忽略。现在每段大约 1800 字，如果发现某些细节总是没出来，可以考虑精简。
