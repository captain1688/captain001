# AI 视频生成提示词：机甲女孩 × 白色小动物（9 秒短片）

> 视频规格：9:16 竖屏 ｜ 总时长 9 秒 ｜ 60fps ｜ 3D 皮克斯风格渲染
> 生成策略：总时长在单次生成上限（15 秒）以内，**不拆分，整段一次生成**。

---

## 一、逐镜头拆解

| 时间码 | 景别 | 运镜 | 镜头动作 | 转场 |
| --- | --- | --- | --- | --- |
| 00:00–00:01 | 中景 | 固定机位 | 左侧机甲女孩双手端科幻手枪，抬头眨眼微笑，向画面左侧开枪；右侧白色拟人化小动物静止站立，呆萌注视女孩 | 硬切 |
| 00:01–00:02 | 特写 | 快速左移（Whip Pan） | 2D 霓虹发光图标（红十字、粉心、黄星）带高速运动模糊向左飞速穿梭 | 硬切 |
| 00:02–00:04 | 中景 | 固定机位 | 黄色星星击中小动物胸口，双眼变成眩晕螺旋状，头顶出现环绕的金色小星星特效 | 硬切 |
| 00:04–00:06 | 特写 | 缓慢推镜 | 小动物表情惊恐呆滞，头顶出现全息血条并迅速清空报警 | 硬切 |
| 00:06–00:09 | 中景 | 固定机位 | 女孩低头看左腕全息手表，抬头，双手摊开耸肩，无奈地说话 | 结束 |

---

## 二、全局风格设定

### 主体

- **机甲女孩**：3D 动漫风格；紫黑相间高科技紧身机甲（硬表面 + 柔性材质）；粉紫色短发；佩戴白色猫耳/天线造型头戴设备；表情灵动丰富。
- **白色小动物**：圆润毛茸茸的拟人化卡通形象；头顶两个棕色小螺旋角；豆豆眼；粉色腮红。

### 环境与画面

- **场景**：极简科幻数字空间，浅蓝灰纯色背景，垂直下落的二进制/数字雨全息投影，地面轻微倒影。
- **光线**：均匀柔和面光；轻微冷色调顶光，突出机甲反光与小动物的边缘轮廓光（Rim light）。
- **色调**：高明度、中等饱和度；主色冷蓝/紫，干净清透。
- **质感**：高精度 3D 渲染（Pixar 风格）；材质区分明显（金属高光 vs 哑光毛发）；浅景深，背景虚化。

### 声音与节奏

- **声音**：无背景音乐；UI 操作音、激光枪声、卡通击中音效、警报声；结尾女孩中文台词：**「啊，没能量了，谁还有能量借我一点」**。
- **节奏**：剪辑明快，前段动作迅速，后段留表演余量，标准短视频强节奏。

---

## 三、视频生成提示词（Segment 1：00:00–00:09）

### English Prompt

> Vertical 9:16 aspect ratio, high-quality 3D Pixar-style animation. Soft studio lighting, light blue sci-fi background with falling holographic digital code. Two main subjects. Subject 1: A stylized 3D anime girl wearing purple and black sleek sci-fi armor, pink-purple hair, cat-ear headset. Subject 2: A round, fluffy, white anthropomorphic animal cartoon figure with small brown horns and pink cheeks.
> Sequence of events: The girl holds a sci-fi gun, winks, and shoots to the left. Neon 2D icons (red cross, pink heart, yellow star) fly across the screen. The yellow star hits the white fluffy animal. The animal becomes dizzy, eyes turn into spirals, with small stars circling its head. A holographic decreasing health bar appears floating strictly above the animal's head. Cut back to the girl: she checks a holographic watch on her left wrist, looks up, and shrugs her shoulders helplessly with her hands open. High fidelity, highly detailed, smooth motion.

### 中文对照

> 竖屏 9:16，高质量 3D 皮克斯风格动画。柔和影棚光线，浅蓝色科幻背景，垂直下落的全息数字代码雨。两个主体。主体 1：3D 动漫风格女孩，紫黑相间流线型科幻机甲，粉紫色头发，猫耳耳机。主体 2：圆润毛茸茸的白色拟人化卡通动物，棕色小角，粉色腮红。
> 事件顺序：女孩持科幻手枪，眨眼，向左开枪。霓虹 2D 图标（红十字、粉红心、黄星）飞过屏幕。黄星击中白色小动物。动物眩晕，眼睛变螺旋状，小星星绕头环绕。头顶正上方出现全息且不断减少的血条。切回女孩：查看左腕全息手表，抬头，双手摊开无奈耸肩。高保真，细节丰富，动作流畅。

**注**：主流视频模型难以在单一提示词内理解 5 次硬切转场，实操中可用「一镜到底」的运镜引导替代硬切，或依赖后期剪辑拼接。

---

## 四、首帧文生图提示词（Text-to-Image, Frame 1）

> Vertical 9:16, 3D Pixar-style rendering, masterpiece, ultra-detailed. A stylized anime girl in purple and black sci-fi sleek armor, pink-purple hair, cat-ear headset, holding a sci-fi gun, standing on the left. On the right, a cute, round, fluffy white anthropomorphic animal cartoon figure with small brown horns. Light blue background with digital holographic vertical rain. Soft ambient lighting, shallow depth of field.

---

## 五、一致性与穿帮补救

| 风险点 | 表现 | 解决方案 |
| --- | --- | --- |
| 机甲纹路与配色漂移 | 动作转换时紫黑色块分布改变 | 用文生图固定首帧装甲样式，视频模型开启最高「首帧一致性」权重 |
| 小动物的角与体态丢失 | 中途变成纯球体或角消失 | 提示词中紧密绑定 `anthropomorphic animal cartoon figure with small brown horns`；避免 monster、blob 等歧义词 |
| UI 元素漂移 | 星星、血条融入背景或贴到角色脸上 | 优先用局部重绘/运动笔刷（Motion Brush）后期合成 UI 图层；若一键生成，提示词强调 `holographic decreasing health bar floating strictly ABOVE the animal's head` |
