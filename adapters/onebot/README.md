# Clonoth OneBot 11 适配器

NoneBot2 插件，通过 OneBot 11 协议（NapCat / go-cqhttp 等实现）将 QQ 消息接入 Clonoth。

## 目录结构

```
adapters/onebot/
├── __init__.py       # 插件主体：消息处理、附件收集、EventRouter 回调
├── config.py         # 环境变量配置
└── emoji_handler.py  # QQ 自定义表情处理（表情包名称索引）
```

## 前置依赖

- Python 3.11+
- NoneBot2
- nonebot-adapter-onebot（OneBot V11 适配器）
- httpx（附件下载）
- OneBot 11 实现端（如 NapCat、go-cqhttp、Lagrange 等）
- clonoth_sdk（项目根目录下的 SDK 包）

```bash
pip install nonebot2 nonebot-adapter-onebot httpx
```

## 环境变量

运营期配置已经搬到 `config/qq.yaml`（声明表在 `live_config.py` 的 `LIVE_KEYS`），改完即时生效。
取值优先级 **`qq.yaml` > env > 内置默认**：下表标了热载键的四项，只要 yaml 里写了就不看 env。
完整清单见 `config/qq.example.yaml` 与 `docs/DEPLOY-QQ.md` §6。

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `CLONOTH_BASE_URL` | 否 | `http://127.0.0.1:8765` | Supervisor API 地址 |
| `CLONOTH_WORKSPACE` | 否 | 仓库根（由 `config.py` 位置推导） | 工作区路径，用于 SDK 导入和附件存储。只有适配器与工作区不在同一仓库时才要显式配 |
| `CLONOTH_ENTRY_NODE` | 否 | `qq.orchestrator` | QQ 入口节点 ID。默认使用综合入口，支持联网搜索、调度、重启、取消任务，并可委派项目/命令相关任务；如需搜索-only 安全窄入口，可显式设置为 `qq.web_search`。 |
| `CLONOTH_QQ_CONFIG_PATH` | 否 | `${CLONOTH_WORKSPACE}/config/qq.yaml` | 热载配置文件位置。文件不存在时全部热载键回落 env / 内置默认 |
| `CLONOTH_QQ_CUSTOM_FACES_PATH` | 否 | `${CLONOTH_WORKSPACE}/config/qq_custom_faces.txt` | AI 可见的 QQ 收藏表情名称文件；一行一个名称，空行和 `#` 注释忽略。 |
| `CLONOTH_QQ_CUSTOM_FACES_METADATA_PATH` | 否 | `${CLONOTH_WORKSPACE}/config/qq_custom_faces.json` | 内部元数据文件（name/md5/resId/emojiId/fileName/url）。AI 不读取，用于稳定匹配与直接发送。 |
| `ONEBOT_CUSTOM_FACE_PROMPT_LIMIT` | 否 | `50` | 每轮注入给 AI 的表情名称上限；设为 `0` 可禁用注入。热载键 `output.face_prompt_limit` |
| `CLONOTH_BQBS_PATH` | 否 | 空 | 旧 `bqbs.txt` 顺序别名文件。默认不使用；只有配置 env 后才按收藏列表顺序补充别名。 |
| `CLONOTH_ADMIN_QQ_USERS` | 是 | 空（= 无人是管理员） | Clonoth 审批管理员 QQ 号，逗号分隔。空 = 审批一律自动拒绝，管理命令全部不可用。热载键 `permissions.admin_users` |
| `CLONOTH_ALLOWED_GROUPS` | 是 | 空（= 不放行任何群） | 允许接入的 QQ 群号，逗号分隔。空是刻意的 fail-closed：漏配时 bot 装死，而不是对所有群开放。热载键 `channels.allowed_groups` |
| `CLONOTH_ALLOWED_PRIVATE_USERS` | 否 | 空（= 不额外放行任何人） | 允许私聊使用 Clonoth 的 QQ 用户，逗号分隔；管理员始终允许。热载键 `channels.allowed_private_users` |
| `ONEBOT_ALLOW_PRIVATE_FRIENDS` | 否 | `true` | 好友是否自动放行私聊。热载键 `channels.allow_private_friends`；只配 env 的老部署按 `CLONOTH_ALLOWED_PRIVATE_USERS` 的文本推导（名单为空、或含"好友"/"friend"即为 true） |

未放行的人私聊时默认一个字都不回：回一句提示就等于向任何陌生人确认这个号是 bot。要提示就填
`channels.private_denied_reply`（只能写 yaml）。

## 部署步骤

### 1. 部署 OneBot 11 实现端

以 NapCat 为例：

1. 安装 NapCat 并登录 QQ 账号。
2. 配置反向 WebSocket 连接地址，指向 NoneBot2 监听端口（默认 `ws://127.0.0.1:8080/onebot/v11/ws`）。
3. 如果使用 Docker 部署 NapCat，创建容器时需要把 Clonoth 附件目录以**相同绝对路径**挂载进容器，便于 `收藏表情` 调用 `add_custom_face` 时让 NapCat 读取本地图片文件。例如 Clonoth 部署在 `/opt/Clonoth`：

   ```bash
   mkdir -p /opt/napcat/config /opt/napcat/qq /opt/Clonoth/data/attachments

   docker run -d \
     --name napcat \
     --restart always \
     -e ACCOUNT=0 \
     -e WSR_ENABLE=true \
     -e NAPCAT_UID=0 \
     -e NAPCAT_GID=0 \
     -e TZ=Asia/Shanghai \
     -v /opt/napcat/config:/app/napcat/config \
     -v /opt/napcat/qq:/app/.config/QQ \
     -v /opt/Clonoth/data/attachments:/opt/Clonoth/data/attachments:ro \
     mlikiowa/napcat-docker:v4.18.5
   ```

   关键点：容器内路径必须仍是 `/opt/Clonoth/data/attachments`，不能改挂到 `/app/...`；因为 Clonoth 传给 NapCat 的 `file` 就是宿主机上的绝对路径。创建容器时指定该挂载后，后续普通 `docker restart napcat` 会保留挂载。
4. 确认连接建立后 NoneBot2 能收到消息事件。

### 2. 配置 NoneBot2 项目

在 NoneBot2 项目中加载本插件。将 `adapters/onebot/` 目录作为 NoneBot2 插件加载：

```python
# bot.py 或 pyproject.toml
nonebot.load_plugin("adapters.onebot")
```

或将目录放入 NoneBot2 插件目录，使其自动发现。

### 3. 配置环境变量

在仓库根的 `.env` 中添加。`bot.py` 会在加载插件之前把它灌进 `os.environ`——本插件读的是
`os.environ`，而 NoneBot 自己的 `.env` 只进它的 Config 对象，两者不是一回事：

```bash
CLONOTH_BASE_URL=http://127.0.0.1:8765
CLONOTH_WORKSPACE=/path/to/clonoth
CLONOTH_ENTRY_NODE=qq.orchestrator
CLONOTH_ADMIN_QQ_USERS=10001,10002
CLONOTH_ALLOWED_GROUPS=123456789
# 留空 = 只放行好友 + 管理员
CLONOTH_ALLOWED_PRIVATE_USERS=
```

白名单与管理员名单同时也是热载键，更推荐直接写 `config/qq.yaml`（改完不用重启）。

### 4. 确保 Supervisor 已启动

本插件通过 Clonoth SDK 与 Supervisor 通信。启动前确认 Supervisor 已在 `CLONOTH_BASE_URL` 指定的地址上运行。

### 5. 启动 NoneBot2

```bash
nb run
```

## 功能说明

### 消息处理

- 群聊默认 @Bot 或被回复时触发，同时携带最近群聊历史作为上下文；只有 `CLONOTH_ALLOWED_GROUPS` 中的群会接入。
- 群聊触发一共七个信号（全量 / @ / 回复 / 名字 / 前缀 / 关键词 / 随机）加双层冷却，另有 LLM 意愿判定兜底，全部是 `config/qq.yaml` 的热载键。判定逻辑集中在 `trigger_policy.py`，配置与坑位见 `docs/TRIGGER-GUIDE.md`。
- 私聊消息直接触发回复，无需 @；默认只允许好友私聊，或通过 `CLONOTH_ALLOWED_PRIVATE_USERS` 指定用户。未放行的人默认得不到任何回应。
- 管理员 QQ 号通过 `CLONOTH_ADMIN_QQ_USERS` 配置；管理员始终允许私聊，用于处理审批命令。每项管理能力还能单独调档（见下）。
- 支持引用消息解析。

### 附件处理

图片附件从 QQ 临时 URL 下载并保存到 `data/attachments/` 目录，与 Discord 适配器使用相同的路径格式。MIME 类型根据 URL 和响应头自动推断。

普通文件附件也会尽量从 OneBot `file` 消息段或群文件上传通知中下载到 `data/attachments/`，用于管理员后续转发；文件下载受 `ONEBOT_ENABLE_FILE_INPUT`、`ONEBOT_FILE_MAX_BYTES`、`ONEBOT_MAX_FILES_PER_TURN` 限制。入站文件按扩展名白名单收取（文档/文本/图片/音视频/压缩包/常见源码），可执行/脚本宿主类型一律拒绝，且拒绝内容为可执行文件的伪装上传；白名单之外的类型可通过 `input.file_extra_allowed_extensions`（`ONEBOT_FILE_EXTRA_ALLOWED_EXTENSIONS`）补充放行，但可执行类型不受此项影响。事件携带的本机路径（`file://` 或绝对路径）只允许读取 Clonoth 工作区内的文件；同机 NapCat 只上报缓存绝对路径的部署需把该缓存目录加入 `ONEBOT_LOCAL_SOURCE_ROOTS`，否则文件入站会被拒绝并给出提示。若当前 OneBot 实现不提供文件 URL，则只能在历史中记录文件名，无法自动转发实际文件。

### 管理员主动发送 / 自然语言转发

管理员（`CLONOTH_ADMIN_QQ_USERS`）可以在 QQ 侧直接让 Bot 发送文本、图片、文件或上文摘要，不进入普通模型上下文，也不会把真实 QQ 号/群号交给模型。

命令式用法仍可用：

- `主动目标` / `主动目标 私聊` / `主动目标 群`：查看可用目标名称。
- `私信 <联系人名> <内容>`、`群发 <群名> <内容>`：主动发文本；同条消息附图会一起发送。
- `发文件 私聊 <联系人名> data/attachments/xxx.zip`：发送工作区内本地文件。
- `合并转发 群 <群名> <内容>`：发送合并转发卡片；引用消息时可省略内容。

自然语言用法示例：

- `帮我把上面聊到的关于预算的消息私发给我`
- `把上面的会议内容和文件合并发送到群项目组`
- `转发这图片给小王`
- 引用某条图片/文件消息后发送：`发给小王` / `转发这张图给小王`

自然语言转发的内容选择顺序：当前消息附件 → 引用消息附件 → 近期图片/文件缓存；涉及“上面/上文/聊到/关于 xxx/会议内容”时，会从最近群历史中筛选文本，并可附带对应或近期文件。目标解析复用主动目标列表中的联系人显示名、好友备注、群名或配置别名；如目标含空格，建议配置无空格别名或使用命令式显式目标。

模型侧走 `qq_forward` 工具：先 `op=list` / `op=recent` 拿候选，返回项带一个 `ref` 标识（形如 `m41`），
再用 `message_refs` / `recent_refs` 挑选。旧的 `message_indices` / `recent_indices` 已被显式拒绝 ——
下标会随新消息进出而漂移，“转发第 3 条”很容易变成转发另一条。`op=list` 与 `op=recent` 同样过鉴权：
无权读这个会话时返回 403，不是空列表。

为降低误触发风险，自然语言转发仅管理员可用；非管理员不会获得目标列表或主动发送能力。

### QQ 收藏表情 / 表情包

适配器支持 NapCat 的 QQ 收藏表情扩展 API：

- 发送前会把模型输出的 `[表情:开心]`、`[emoji:开心]`、`[收藏表情:开心]`、旧格式 `[QQ_EMOJI:开心]` 自动转换成 OneBot `image` segment。
- AI 默认只看到 `CLONOTH_QQ_CUSTOM_FACES_PATH` 指向的名称文件，默认路径为 `config/qq_custom_faces.txt`。文件一行一个名称，空行和 `#` 注释会被忽略，手动修改后无需重启即可生效。
- 无名称收藏表情不会写入该文件，也不会注入给 AI，因此 AI 不会主动使用未命名表情。
- 发送解析优先使用名称文件；`CLONOTH_BQBS_PATH` 默认不使用，只有显式配置 env 时才作为旧 `bqbs.txt` 顺序别名参与兼容匹配。
- 除名称文件外，还会维护内部元数据文件 `config/qq_custom_faces.json`（含 `name/md5/resId/emojiId/fileName/url`）。这些字段不注入给 AI，仅用于把名称稳定映射到具体收藏表情，并在可能时直接用保存的 URL 发送，减少频繁调用 `fetch_custom_face_detail`。
- 序号、md5、resId、emojiId 不写入 AI 可见的名称文件，避免模型误用；它们只保存在元数据文件里。
- 表情详情仅通过 NapCat `fetch_custom_face_detail` 获取；该接口需要较新的 NapCat（建议 `v4.18.5+`）。

QQ 侧可直接管理收藏表情。以下命令全部受 `permissions.capabilities.custom_face` 档位控制，默认只有 `CLONOTH_ADMIN_QQ_USERS` 中的管理员能用；无权限者会被提示，且不会消耗 LLM（档位配成 `off` 时对非管理员完全静默）。命令中的数字参数（如 `50`）表示最多展示的条数，范围 1~100：

| 命令 | 权限 | 说明 |
|---|---|---|
| `表情包帮助` | 仅管理员 | 输出全部表情包管理命令示例 |
| `同步表情列表` | 仅管理员 | 从 NapCat 收藏表情详情同步“已命名表情”到 `config/qq_custom_faces.txt`；未命名表情会被跳过 |
| `收藏表情 开心` | 仅管理员 | 将同一条消息、引用消息或最近一张图片添加到 QQ 收藏，并尽量把描述设置为“开心” |
| `命名表情 3 开心` / `重命名表情 3 开心` | 仅管理员 | 给已有收藏表情设置/修改描述；第一个参数可用序号、md5、resId、文件名或旧描述；成功后会同步名称文件 |
| `删除表情 开心` | 仅管理员 | 按名称/描述/resId/md5/序号删除收藏表情；成功后会同步名称文件 |
| `表情列表` / `表情列表 50` | 仅管理员 | 查看当前名称文件中 AI 可用的表情名；`50` 表示最多展示 50 项（默认 30） |
| `表情详情列表` / `表情详情列表 50` | 仅管理员 | 查看 NapCat 收藏表情详情列表，包含未命名项，便于按序号命名；`50` 表示最多展示 50 项（默认 50） |

注意：NapCat 的 `add_custom_face` 要求 `file` 是 NapCat 运行环境能访问的本地路径。如果 NoneBot 与 NapCat 分容器/分机器部署，需要在**创建 NapCat 容器时**把 `${CLONOTH_WORKSPACE}/data/attachments` 挂载到 NapCat 中的相同绝对路径，例如 `-v /opt/Clonoth/data/attachments:/opt/Clonoth/data/attachments:ro`，否则新增收藏会因 NapCat 容器内找不到文件而失败。已收藏表情的发送不需要本地路径，直接使用 `fetch_custom_face_detail` 取得的 URL/资源信息发送。

### 审批流程

QQ 适配器不会再自动放行 `approval_requested`。当 Clonoth 触发需要审批的内部或外部操作时：

1. Bot 会把审批摘要私聊发送给 `CLONOTH_ADMIN_QQ_USERS` 中的管理员。
2. 管理员通过私聊回复命令处理审批：
   - 同意：`审批 同意 <approval_id>`
   - 拒绝：`审批 拒绝 <approval_id>`
3. `<approval_id>` 可以填写完整 ID，也可以填写唯一前缀。
4. 如果未配置 `CLONOTH_ADMIN_QQ_USERS`，审批请求会被默认拒绝，避免无人确认时误放行。
5. 引用审批卡片但取不到 approval_id 时**不会**回退到"当前唯一待审批"（`ONEBOT_APPROVAL_REPLY_UNIQUE_FALLBACK` 默认关）：这条回退分不清"引用的就是它"和"引用的是另一张已处理的卡片"，是误批的直接成因。
6. 超过 `ONEBOT_PENDING_APPROVAL_TTL_SECONDS`（默认 3600 秒）的条目移出待审批、不再可批，避免快捷审批命中一条早已 auto-deny 的死条目。

### 管理能力档位

八项管理命令各有一个授权档位，写在 `config/qq.yaml` 的 `permissions.capabilities.*`（只能写 yaml，热载）：

| 档位 | 谁能用 |
|---|---|
| `off` | 谁都不能用，管理员名单里的人也一样 |
| `admin` | 只有 `permissions.admin_users`（默认） |
| `owner` | 名单 + 本群群主 |
| `group_admin` | 名单 + 本群群主 + 本群管理员 |

`approval` / `model` / `draw_preset` / `custom_face` / `cross_session` / `send_dead_letter` 六项影响面超出
单个群或会泄露真实号，写 `owner`/`group_admin` 会被夹回 `admin`；`clear_memory` / `proactive` 可以给到
群主与群管，但目标会被夹在他自己那个群里。群角色由 OneBot 上报，只在群消息里成立 —— 群主在私聊里就是
普通用户。

档位配成 `off` 时，非管理员触发对应命令**一个字都不回**：说"这项能力已关闭"就等于公布这项能力存在。

`send_dead_letter` 对应私聊命令 `/发送死信`：无参数列出卡住的投递，`/发送死信 清理 <编号>` 放行一条，
`/发送死信 清理 全部` 全部放行。清单带真实群号/QQ 号，所以最宽只能给到管理员名单。

### 任务状态反馈

默认入口为 `qq.orchestrator`，适合希望 QQ 同时承担联网搜索、调度、重启和取消任务的部署；`qq.web_search` 会继续保留为极简搜索入口，适合只想开放联网搜索能力的部署。

Bot 处理消息时，通过 QQ 表情 Reaction 反馈当前阶段；当检测到联网搜索工具进度时，还会向会话发送低频文本提示，例如“已收到联网搜索请求，正在检索网页资料……”，避免用户误以为 Bot 卡住：

| 阶段 | 表情 ID | 含义 |
|---|---|---|
| submitted | 281 | 已提交到引擎 |
| thinking | 178 | 模型思考中 |
| tool | 97 | 工具调用中 |
| writing | 326 | 生成回复中 |

除上述阶段机外，模型还可在回复里写 `[REACT:ID]` 主动给用户消息贴表情表态。该通路受 `ONEBOT_ENABLE_REACTIONS` 开关与内置白名单双重约束：开关关闭时完全静默，白名单外的 ID 直接丢弃。可用 ID 由入站提示词【QQ表情表态】动态列出，与阶段机 ID 互斥，避免被收尾清理抹掉。

## Docker 注意事项

- NapCat 容器重启后会丢失 QQ 登录 session，需要用手机重新扫码登录。**不要随意重启 NapCat 容器**。
- 如果需要使用 QQ 收藏表情的 `收藏表情` 命令，`CLONOTH_WORKSPACE/data/attachments` 必须在创建 NapCat 容器时以相同绝对路径挂载进容器；已创建容器不能动态增加 volume，只能重建容器。
- `CLONOTH_WORKSPACE` 指向的路径需要在容器内可访问（挂载为卷或与 Supervisor 共享卷）。
- NoneBot2 与 NapCat 之间的 WebSocket 连接需在容器网络中可达。

## 与 Discord 适配器的区别

| 特性 | Discord | OneBot |
|---|---|---|
| 协议 | Discord Gateway (WebSocket) | OneBot 11 (反向 WebSocket) |
| 框架 | discord.py | NoneBot2 |
| 触发方式 | 所有消息 / @Bot | 七信号 + 冷却 + LLM 意愿判定（默认只认 @Bot 与被回复）/ 私聊 |
| 审批按钮 | Discord UI Button | QQ 管理员私聊命令审批（不自动放行） |
| Bridge Server | 有（discord_manage 工具） | 有（qq_forward 工具） |
| 附件上传 | Discord CDN 下载 → 本地保存 | QQ 临时 URL 下载 → 本地保存 |
