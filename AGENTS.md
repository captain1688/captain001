# 给 Codex 的工作说明

本仓库用来生成儿童向互动短视频：提示词写在 Markdown 里，视频由 `video-pipeline/make_video.py` 调用 apimart 平台的 Seedance 2.0 生成，再用 ffmpeg 自动剪辑成片。

## 用户说"帮我生成 xxx 视频"时

1. **建工程**：在 `video-pipeline/projects/<英文短名>/` 下放：
   - `project.json`：照抄 `projects/xueren/project.json` 的结构。
   - `seg1.txt`、`seg2.txt`……：每段一个文件，内容是该段完整的视频提示词，原样粘贴，不要改写。
   - `refs/`：每段的首帧图。
2. **首帧图**：每段都用 `first_frame` 指定一张首帧图，这样各段互不依赖、会并发生成。图由用户提供，图片提示词写在 `refs/README.md`。缺图就停下来，告诉用户缺哪张，不要自己编图。
3. **先预检**：运行 `python video-pipeline/make_video.py video-pipeline/projects/<名字> --dry-run`。它不花钱，会检查配置并打印请求内容。
4. **正式生成要先问用户**：正式运行会按秒扣费。先告诉用户要生成几段、总共几秒，用户同意后再运行：
   `python video-pipeline/make_video.py video-pipeline/projects/<名字>`
5. **交付**：成片在 `projects/<名字>/output/final.mp4`。告诉用户路径，以及 `state.json` 里记录的每段费用。

## 规则

- 不要删除或手改 `output/state.json`：里面记着已提交的任务号。中断后重跑会接着查询原任务，不会重复扣费。
- 某段生成失败时，先读报错。用户要重做某一段，就只删 `output/<段名>.mp4`，再删 `state.json` 里这一段的记录，其他段不要动。
- 不要在终端输出、文件或提交记录里写出 `APIMART_API_KEY` 的值。
- 只想重新剪辑（比如换配乐）时，用 `--edit-only`，不会调用付费接口。
- 配乐：音乐库在 `video-pipeline/music/<情绪>/`，说明见 `video-pipeline/music/README.md`。`project.json` 里 `edit.bgm` 设为 `"auto"`、`edit.bgm_mood` 设为情绪文件夹名，就会自动选曲，并自动做循环、淡入淡出、说话时压低音乐、响度统一。用户说"换一首""换成温馨的""音乐小一点"时，改 `edit` 里对应字段，再用 `--edit-only` 重剪，不要重新生成视频。
- 不要从网上下载音乐放进音乐库（有版权风险），音乐只能由用户自己放进去。音乐库对应情绪的文件夹是空的话，脚本会先出无配乐的成片并提醒，这时告诉用户往哪个文件夹放音乐。
- 剪辑全部用脚本里的 ffmpeg 完成，不要去操作剪映等图形界面软件。
- 交付时用 `explorer` 打开成片所在的 `output` 文件夹，方便用户直接查看。
- 提示词规则（符号只以画面出现、台词不说符号名、全程无背景音乐等）见 `儿童向原创提示词集.md` 和 `母婴提示词规律总结.md`。改提示词时不要违反这些规则。
