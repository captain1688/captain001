# 香蕉猫 · 沙滩一画一毁：素材

本条用**参考图模式**：两个角色的图一起发给模型，提示词前会自动加一行"【参考图对应】图1＝香蕉猫；图2＝人类小女孩。"，不需要首帧图。

| 角色 | 图片 | 怎么来 |
|---|---|---|
| 香蕉猫 | `asset:xiangjiaomao_main` | 你已有的香蕉猫图，在 `video-pipeline/assets.local.json` 里登记本机路径 |
| 人类小女孩 | `refs/girl.png` | 由 Codex 运行 `--make-refs girl` 生成（Seedream-4.5，会花一次图片的钱），生成后**先人工确认长相**再生成视频 |

想换小女孩的样子：改 `project.json` 里 `characters.girl.generate.prompt`，再运行 `--make-refs girl --allow-regenerate girl`（旧图会移到 `refs/_old/`，不删除）。
