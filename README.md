<div align="center">

**一个能在群里长期待着的 AI 机器人。**

[![License](https://img.shields.io/badge/License-GPL--3.0-1f6feb?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-1f6feb?style=flat-square)](#快速开始)
[![OneBot](https://img.shields.io/badge/OneBot-11-1f6feb?style=flat-square)](#功能)
[![Tests](https://img.shields.io/badge/tests-pytest%20%2B%20Vitest-1f6feb?style=flat-square)](#开发)

<a href="README.md"><img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-1f6feb?style=for-the-badge" alt="简体中文"></a>
<a href="README.en.md"><img src="https://img.shields.io/badge/English-57606a?style=for-the-badge" alt="English"></a>

</div>

---

它看得懂群里发的图片和文件，能把合并转发整棵展开，会自己攒表情包、写工具、排定时任务、记住跟谁聊过什么。

群友能让它搜索、生图、算东西，聊天里用得上的都能用；执行服务器管理命令、改文件这些事情，只有管理员能让它做。

配置基本不用碰 yaml —— 自带一个 Web 控制台，模型、人格和消息收发设置支持热更新，页面会显示 QQ 适配器有没有读到新配置。

## 目录

- [功能](#功能)
- [架构](#架构)
- [权限与安全](#权限与安全)
- [快速开始](#快速开始)
- [配置](#配置)
- [部署](#部署)
- [开发](#开发)
- [致谢](#致谢)
- [许可](#许可)

## 功能

### 群里的表现

**什么时候开口** —— 默认只在被 @ 的时候说话。也可以让它认自己的名字、认关键词、按概率随机插话，或者干脆每条都回。这些都不满足时，还能让一个便宜的小模型判断「这句需要接话吗」。担心它太吵就加冷却，按群或按人都行。

控制台里可以**试听**：喂几条假消息进去，看它到底会不会回、是被哪条规则挡下的。用的就是线上那份判定逻辑，不是另写一套模拟。

**看得懂什么** —— 图片、文件、合并转发。合并转发是整棵展开的，嵌套的卡片、卡片里夹的图和文件都会一起读进来；有人转了一大段聊天记录进来问「这什么意思」，它是真的看得到内容。引用回复也能还原。QQ 经常把「一段话 + 一张图」拆成两条发过来，它会等两秒把这两条合回去。

**怎么说话** —— 想分几条发就在中间写个记号，会拆开依次发出去。回复时引用你那条消息。干活的时候在你消息上贴表情表示进度（收到 → 在想 → 在查 → 在写），回完自动摘掉。想给谁点个赞、想 @ 谁，它写个名字就行，出站时自动换成真的 @。

**表情包** —— 自动从群里攒。收进来的图先进待审池，调一个看图模型给它打标签，你在控制台点通过才入库。之后每轮聊天按概率把库里的图连标签一起摆给模型看，让它自己决定发不发、发哪张。半小时内发过的不会重复出现。几个 bot 实例共用一个库。

**还有** —— 复读跟风（纯规则，不花模型的钱）、管理员让它主动往某个群发消息、模型自己把聊天记录转发给你、按人记档案（@ 到谁就把那个人的记忆全量捞出来）、二十多类斜杠命令（在进模型之前就拦下来，不消耗 token）。

### 它自己能干的

| | |
|---|---|
| **上网** | 搜索、抓网页、看图。优先用模型自带的联网能力，不行再退到 Exa |
| **画图** | NovelAI（带画师串预设，群里可以直接切）、Gemini、GPT Image |
| **算东西** | LaTeX 输入的符号计算、股票行情与 K 线 |
| **文件与命令** | 读写文件、改 diff、全文搜索、执行 shell 命令 |
| **定时任务** | 五段 cron。到点发一句话给自己，或者跑个脚本再把结果发给自己 |
| **记忆** | 自己存、自己查、自己删。每天夜里整理一次，合并重复的、清掉过期的 |
| **自己长本事** | 写一个新工具然后立刻加载、从模板派生一个专职的子智能体、接 MCP 服务器 |

上下文快满的时候会自己压缩，保留最近的原文，旧的折成摘要。任务太大就拆成子任务派给别的节点做。

### Web 控制台

React 写的，跟着 supervisor 一起跑，浏览器打开就用。

**对话页**能直接跟它聊，看得到它每一步在调什么工具、工具返回了什么、有没有在等审批；派出去的子任务可以点进去看。

**设置页**按用途分组：

| 入口 | 管什么 |
|---|---|
| **通用 / 客户端** | 浏览器连接控制台、管理员访问凭据；客户端偏好在独立页面设置 |
| **机器人** | 账号与连接、人格、全局默认、记忆、表情包图库、群协作、会话与接话、提醒 |
| **模型与渠道** | 模型服务商、密钥、备选链、看图渠道和模型参数 |
| **引擎** | 运行诊断、审批、节点、工具与权限、绘图、技能、MCP、自动化等 |
| **平台 → QQ·NapCat** | 信道白名单、触发时机、权限、输入输出、互动和链路状态 |

**账号与连接**是「机器人」分组下的独立页面，与「人格」同级。在这里管理多开实例、共享配置组，以及当前实例的 QQ 登录和换号。

### 多开

**一个实例只连接一个平台、登录一个账号。** 当前部署和建实例流程使用 QQ + NapCat，一台机器最多九个实例（含主实例）。开启多开后，在「机器人 → 账号与连接」里创建和切换实例。删实例时，工作区会改名归档。

各实例默认有自己的配置和数据，也可以加入**共享配置组**：同组实例共用整个 `config/`，包括人格、模型渠道与密钥、技能、工具、MCP、权限策略和平台配置；改一处会影响全组。会话、记忆等 `data/` 数据始终按实例独立，`.env` 和 `data/platform_secrets.yaml` 中的端口、路径、平台连接令牌也保留在各实例。表情包图库由同一台机器上的实例共用。

加入共享组时，原配置会归档到本实例的 `data/config.archived-*`。离开时复制组内的当前配置，成为自己的独立配置，不会自动恢复加入前的版本。

同一个号也能换：换了之后记忆自动隔离，换回来又自动接上。

## 架构

三层，各跑各的进程：**Supervisor** 管会话、任务、策略、审批、事件；**Engine** 跑推理循环和工具；**Adapter** 接平台。

```
                       QQ 服务器
                            │
                  ┌─────────▼──────────┐
                  │ NapCat (Docker)    │  OneBot 11 实现
                  └─────────┬──────────┘
                   反向 WebSocket
                            │
┌───────────────────────────▼──────────────────────────┐
│ bot.py  — NoneBot2 + adapters/onebot        :8080    │
│   白名单 → 触发判定 → 匿名化 → 落盘 → 提交            │
│   消费事件流，回调驱动发消息 / 贴表情 / 发审批卡片     │
└──┬───────────────────────────────────────────────────┘
   │ HTTP 提交消息 ·  WebSocket 收事件
┌──▼───────────────────────────────────────────────────┐
│ supervisor (FastAPI)                        :8765    │
│   会话 · 任务 · 路由 · 策略 · 审批 · 事件日志 · 调度   │
│   Web 控制台挂在 /web ───────────────────────────────┼→ 浏览器
└──┬───────────────────────────────────────────────────┘
   │ engine 主动来拉活
┌──▼──────────────────────────────────┐
│ engine worker ×2                    │
│   拿任务 → 组提示词 → 调模型         │
│   → 调工具 → 回写 → 再来一轮 → 交差  │
└──┬──────────────────────────────────┘
   ├─→ tools/*.py     每次调用起一个子进程
   └─→ MCP 服务器
```

一条群消息的完整路径：过白名单 → 判断该不该回 → 把 QQ 号换成 `UserA` 这样的代号 → 图片文件存下来 → 交给 supervisor → 建任务 → engine 领走 → 注入记忆和技能 → 模型和工具来回几轮 → 出结果 → 净化格式 → 发回群里。

主要目录：

| 目录 | 干什么的 |
|---|---|
| `supervisor/` | 会话、任务、策略、审批、事件日志、调度、进程管理 |
| `engine/` | 推理循环、Hook、记忆、上下文压缩、工具执行 |
| `adapters/onebot/` | QQ / OneBot 消息收发、登录和平台配置 |
| `adapters/_shared/` | 适配器共用的配置加载、触发判定等逻辑 |
| `adapters/web/` | 控制台前端 |
| `platforms/` | 平台描述、能力和运行钩子登记 |
| `providers/` | 模型后端。OpenAI / Responses / Anthropic / Gemini / DeepSeek |
| `toolbox/` `tools/` | 工具运行时与外部工具脚本 |
| `stickers/` | 表情包库 |
| `defaults/` | 随仓库更新的节点、运行参数和配置模板 |
| `config/` | 实例配置与默认值覆盖；可由共享配置组共用，不进仓库 |
| `data/` | 实例自己的会话、记忆、身份信息和其他运行数据，不进仓库 |
| `deploy/` | systemd 单元与部署脚本 |

加一家模型后端就是加一个 `providers/*.py`，会被自动发现。仓库自带工具放在 `tools/`，实例自己的工具放在 `config/tools/`，同路径文件会覆盖仓库版本；工具声明中指定它需要过哪道权限闸。

## 权限与安全

**普通聊天与服务器操作分开授权。** 联网搜索、生图、算数、看图、发表情包这些，群友都能用。一般的服务器文件读写、命令执行和进程重启对非管理员直接拒绝；严格校验过的公网只读 `curl` 请求有受限通道。他们发来的图和文件走附件通道处理。

**管理员的高危操作会弹卡片。** 需要放行时，bot 私聊发一张审批卡过来，引用它回一句「同意」就行。管理员名单为空的时候，所有审批一律自动拒绝，不会默默放行。

**具体放行规则由策略决定。** `defaults/policy.example.yaml` 已对密钥、节点定义和配置文件设置拒绝或审批规则；管理员也不能越过明确拒绝的规则。在「引擎 → 工具与权限」中查看和调整实例的规则。

**系统权限由部署单元约束。** 生产服务使用专用用户，并启用 systemd 的 `ProtectSystem=strict`、`ProtectHome` 和 `NoNewPrivileges`。实际可写范围由单元及其附加配置中的 `ReadWritePaths` 决定；主实例默认允许写 `/opt/kazebot`，其中也包含源码，不能把这套配置当作源码只读保护。

另外几件默认就做了的事：密钥不进仓库（`.env`、整个 `data/` 和 `config/` 都在 `.gitignore` 里，仓库只带 `defaults/` 下的默认和模板）；模型看到的群号和 QQ 号都是 `GroupA`、`UserB` 这样的代号，真实号码存在本地对照表里；日志出去之前会把 key 打码；`.env`、配置文件、私钥这些东西不允许通过 QQ 发出去，管理员也不行。

## 快速开始

需要 Python 3.11+、Node.js 18+，还有一个 OneBot 11 实现（推荐 NapCat）。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

mkdir -p config
cp defaults/models.example.yaml config/models.yaml   # 用哪家模型
cp defaults/policy.example.yaml config/policy.yaml   # 权限策略
cp .env.example .env                                 # 密钥
cp defaults/qq.example.yaml config/qq.yaml           # QQ 配置

cd adapters/web/frontend && npm ci && npm run build && cd -

python main.py     # supervisor + engine
python bot.py      # QQ 适配器，另开一个终端
```

第一次启动时，supervisor 会在日志里打一条带 token 的控制台链接，点进去就能配剩下的。

**群白名单和管理员名单默认为空。** 在 `config/qq.yaml` 中填写 `channels.allowed_groups`，它才会在指定群里响应；`permissions.admin_users` 决定谁能管理它和处理审批，留空时审批会自动拒绝。

## 配置

| 文件 | 管什么 | 改完 |
|---|---|---|
| `config/models.yaml` | 用哪家模型、备选链、看图和压缩分别走哪个渠道 | 下次读取配置时生效 |
| `config/policy.yaml` | 权限策略 | 下次权限判定时生效 |
| `config/features.yaml` | 通用功能默认值：触发、审批、复读、收图收文件、队列、表情包 | 后续消息或适配器轮询加载，页面显示生效状态 |
| `config/qq.yaml` | QQ 配置，也能覆盖通用功能的同名项 | 后续消息或适配器轮询加载，页面显示生效状态 |
| `config/runtime.yaml` | 引擎调参、压缩阈值、记忆预算、数据清理；只写要改的键，其余照 `defaults/runtime.yaml` | 按读取生效；并发数、轮询间隔、HTTP 超时等需重启引擎 |
| `config/nodes/*.yaml` | 节点定义；只放改过的和新加的，其余用 `defaults/nodes/` 里的默认 | 新任务加载节点时生效 |
| `config/skills/`、`config/drawtools/` | 技能和绘图配置 | 后续加载或调用使用新配置 |
| `config/tools/` | 自己加的脚本工具 | 检测到变化后，在后续引擎轮询中重载 |
| `config/mcp.yaml` | MCP 客户端 | 工具列表需显式重载 MCP 工具或重启引擎 |
| `config/secrets.env` | 可随共享配置组共用的模型和工具密钥 | 后续密钥读取使用新值；`.env` 同名项优先 |
| `data/platform_secrets.yaml` | 平台连接令牌这类实例身份，也可以写在 `.env` | **要重启** |
| `.env` | 端口、密钥、workspace 位置 | **要重启** |

仓库自带的节点和 runtime 在 `defaults/` 下直接生效，升级时跟着代码更新。`config/nodes/` 中的同名文件整份覆盖默认节点；`config/runtime.yaml` 按键覆盖，没写的键继续使用仓库默认值。控制台修改写入 `config/`，也可以恢复默认；加入共享配置组后，这些修改会影响组内其他实例。
改过仓库自带的脚本工具也一样，改动存进 `config/tools/` 的同一路径。

平台连接令牌优先读取 `data/platform_secrets.yaml` 中对应平台的值，再回退到其环境变量。模型和工具的共享密钥可放在 `config/secrets.env`，但 `.env` 中的同名值优先；端口、路径和账号身份信息始终保留在实例自身。

从旧版本升上来时，`data/config.yaml`、`data/policy.yaml`、`data/mcp_clients.yaml`、`skills/` 和绘图配置在第一次启动时自动复制进 `config/`，
`data/config.yaml` 里的 `platforms` 段拆入 `data/platform_secrets.yaml`；已有目标文件不会被覆盖。旧文件会保留，但不会持续同步后续改动，不能直接拿来回滚线上数据。首次升级和回滚步骤见[数据保全升级说明](deploy/UPGRADE-MULTI-PLATFORM.md)。

`config/features.yaml` 不用整份复制模板，照 `defaults/features.example.yaml` 只抄要改的那几项：它比 `.env` 优先，整份抄过去 `.env` 里的同名设置就全失效了。

`config/features.yaml` 和 `config/qq.yaml` 解析失败时，适配器会继续用上一份有效配置，同时记录错误。控制台会显示当前生效值、来源和加载状态，也可以用管理员凭据查询 `GET /v1/platforms/qq/config/state`。

全新部署或新建实例时，**机器人 → 会话与接话**默认使用以下配置：

| 策略 | 默认值 |
|---|---|
| 统一选择忽略、表态、短答和办事 | 关闭 |
| 按引用和参与者接续话题 | 开启 |
| 同人短句等待 | 8 秒 |
| 连续追加最长等待 | 10 秒 |
| 每分钟自主回应上限 | 6 次 |

8/10 秒用于等待同一个人继续输入并合并请求，不是两条回复之间的间隔。自主回应上限在统一接话策略开启时使用。

已有实例升级时会保留原来的有效默认值和单个会话覆盖，不会强行套用新配置。各实例独立保存；恢复会话继承后使用该实例的默认值。

## 部署

### 开始之前

准备三样东西：

- 一台全新的 Debian 12 / Ubuntu 22.04 以上的服务器，1 核 2G 起步，能连 root
- 一个准备给 bot 用的 QQ 号（会用手机扫码登录，建议别用你自己的主号）
- 一个模型 API key，官方或中转站都行

### 装

```bash
git clone <仓库地址> /opt/kazebot
sudo /opt/kazebot/deploy/bootstrap.sh
```

路径必须是 `/opt/kazebot` —— systemd 单元里写死了这个位置，放别处脚本会直接拒绝跑。

它不会问你任何问题，一路装到底：系统依赖和 Docker、专用用户、venv、配置模板、编译前端、拉起 NapCat 容器、装 systemd 单元、启动，最后确认 engine 真的活着。五到十分钟，取决于网速。

### 去控制台把剩下的填了

装完会打印一条带 token 的地址。控制台只听本机，所以从自己电脑上先开个隧道：

```bash
ssh -L 8765:127.0.0.1:8765 <你的用户>@<服务器IP>
```

然后浏览器打开脚本给你的那个 `http://127.0.0.1:8765/web/?token=...`，按顺序填四项：

| | 页面 | 填什么 | 不填会怎样 |
|---|---|---|---|
| 1 | **模型与渠道 → 渠道** | 模型 base_url、API key、模型名 | 它不会说话 |
| 2 | **机器人 → 账号与连接** | 手机扫码登录 bot 的 QQ 号 | 连不上 QQ |
| 3 | **平台 → QQ·NapCat → 权限** | 你自己的 QQ 号，当管理员 | 审批自动拒绝，没有管理员权限 |
| 4 | **平台 → QQ·NapCat → 信道** | 要让它待的群号 | 它不在任何群开口 |

配置保存或应用后会热更新，QQ 页的状态条会显示是否生效。换号操作会重启 NapCat，期间 QQ 连接暂时不可用。填完去群里 @ 它试试。

这些东西之所以不在命令行里问，是因为控制台那边有校验、能看到当前实际生效的值、填错了还能改回来 —— API key 敲进 shell 里还会留在 history 里。

### 装完之后

```bash
systemctl status kazebot kazebot-qq     # 两个都该是 running
journalctl -u kazebot -f                # 实时日志
docker logs --tail 50 napcat            # QQ 那边的日志
```

群里 @ 它没反应，先检查「平台 → QQ·NapCat → 信道」里的群白名单，再查看链路和配置生效状态。

日常更新脚本是 `deploy/deploy.sh`：默认拉取并 `reset --hard origin/master`，构建前端、重启主服务，最多等待 90 秒检查是否出现 engine 进程。有 `node_modules` 时每次都构建前端，锁文件变化才额外执行 `npm ci`；缺少依赖目录时会报警并跳过构建。脚本的进程检查不能替代对每个实例、QQ 连接和实际收发消息的验收。

该脚本不安装 Python 依赖、不更新 systemd 单元，也不负责重启所有附加实例；这些有变化时需要按对应部署说明处理。

脚本会备份已修改的受跟踪文件，但新版本仍跟踪的文件不会自动恢复本地补丁；这份备份也不包含完整运行数据。**从旧目录布局首次升级、保留线上定制或更新多实例部署时，先按[数据保全升级说明](deploy/UPGRADE-MULTI-PLATFORM.md)操作**：备份代码、配置、身份信息和各实例数据，包含共享配置的实际目录，固定目标提交后再迁移和验收。不要用旧文件覆盖升级后产生的新数据。

想再多开几个实例（一台机器最多 9 个，一个实例登一个号）：

```bash
sudo /opt/kazebot/deploy/install_provision.sh <主实例名> <主实例QQ号>
```

主实例就是现在这一套，名字自己起；只给一个 QQ 号的老写法照旧能用，实例名就是那个号。跑完在「机器人 → 账号与连接」中管理多开实例和共享配置组。

### 装完的样子

```
/opt/kazebot/                 代码，同时也是主实例的工作区
/opt/kazebot-data/
  ├ instances.yaml            实例登记表
  ├ stickers/                 几个实例共用的表情包库
  ├ shared/<共享组>/config/    共享配置组的实际配置目录
  └ <实例名>/                 其余实例各自一个工作区
      ├ config/               自有配置，或指向共享组的链接
      └ data/                 本实例的会话、记忆和运行数据
```

生产用 systemd，Docker 只拿来跑 NapCat。端口 `8765`（控制台）/ `8080`（NoneBot）/ `8769`（转发桥）/ `6099`（NapCat）全部只听本机，**防火墙只放 22 就够**。

排障清单、非交互部署、不用脚本手工装的步骤，都在 [deploy/INSTALL.md](deploy/INSTALL.md)。

多开时端口按实例序号 `N` 推导：supervisor `8765+10N`、NoneBot `8080+10N`、转发桥 `8769+10N`，NapCat WebUI 为 `6099+100N`；主实例的 `N` 为 0。

supervisor 默认只绑本机。要从外网开控制台，走隧道并在前面加一层认证，别把端口直接暴露出去。

## 开发

```bash
python -m pytest
cd adapters/web/frontend
npm run test:run
npm run build                                  # TypeScript 检查与生产构建
```

QQ 那边的判定逻辑（什么时候该说话、谁有什么权限、什么文件能收、什么文件不能外传）都刻意没有依赖 NoneBot，可以脱开平台直接单测。

## 致谢

- [HCPTangHY/Clonoth](https://github.com/HCPTangHY/Clonoth) —— 本项目的框架
- [qianzhuowo/Clonoth](https://github.com/qianzhuowo/Clonoth) —— 感谢 qianzhuowo 的修改版中提供的功能
- 秋雨·月落 —— 感谢提供 OneBot 适配器
- [astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) —— 表情包部分有借鉴

## 许可

[GPL-3.0](LICENSE)
