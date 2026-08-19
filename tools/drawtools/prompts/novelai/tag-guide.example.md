# NovelAI V4.5 图像生成 Tag 编写指南

核心原则：V4.5 采用混合式写法 (Hybrid Prompting)。
- 静态特征（外貌、固有属性）使用 Danbooru Tags 以确保精准。
- 动态行为（动作、互动、空间关系）使用简短自然语言短语以增强连贯性。
- 禁止输出质量词（如 best quality, masterpiece, highres），这些由系统自动添加。
- 格式：所有 Tag 使用英文，元素之间用英文逗号 `,` 分隔。

## V4.5 短语化描述
推荐用于：
- 复杂动作：`drinking from a white cup`, `holding a sword tightly`
- 空间关系：`sitting on a wooden chair`, `leaning against the wall`
- 属性绑定：`wearing a red scarf and blue gloves`
- 细腻互动：`hugging him from behind`, `wiping tears from face`

禁止：
- 否定句：不要写 `not holding`, `no shoes`，用正向可见 tag 替代。
- 时间/因果：不要写 `after bath`, `because she is sad`，直接描述视觉状态。
- 超长从句：拆成多个短语，逗号分隔。

## 外貌特征
- 头发：`short hair`, `medium hair`, `long hair`, `very long hair`, `ponytail`, `twintails`, `braid`, `messy hair`, `ahoge`, `blonde hair`, `black hair`, `silver hair`, `gradient hair`
- 眼睛：`blue eyes`, `red eyes`, `heterochromia`, `slit pupils`, `glowing eyes`, `closed eyes`, `half-closed eyes`
- 皮肤：`pale skin`, `tan`, `dark skin`, `freckles`, `mole`, `blush`

## 场景深化
不要只写 `indoors` 或 `room`，必须补充具体环境物体：
- 室内：`bookshelf`, `curtains`, `window`, `bed`, `carpet`, `clutter`
- 室外：`tree`, `flower`, `cloud`, `road`, `building`, `rubble`
- 幻想：`magic circle`, `floating objects`, `glowing particles`, `ruins`
- 风格/色调：`anime screencap`, `illustration`, `thick painting`, `monochrome`, `high contrast`, `warm theme`, `cool theme`

## 权重语法
- 强调：`1.3::tag::`
- 降低：`0.7::tag::`
- 移除角色自带特征可在 UC 中使用负权重：`-1::glasses::`
