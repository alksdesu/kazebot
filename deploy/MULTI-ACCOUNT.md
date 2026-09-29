# 一台机器跑多个 bot

一个实例就是一个 bot：只跑一个平台（在 `.env` 的 `CLONOTH_PLATFORM` 里声明，没写的是 QQ）、登一个号，人格、记忆、渠道、模型各自一份。一个 adapter 进程只服务一个号（账号作用域是单值，群历史等缓存按裸群号索引），所以多开的形态是**几套独立进程共用一份代码**。同一个平台想登第二个号、或者想接另一个平台，都是再开一个实例。

```
/opt/kazebot/                 代码，git 更新一次，全部实例生效；序号 0 的实例就住在这里，端口与路径和单实例时一样
/usr/local/lib/kazebot/       root 侧组件（建实例、宿主清理），kazebot 写不到
/opt/kazebot-data/
  ├ instances.yaml            控制台切换器读的共享清单
  ├ provision-<实例名>.log     建实例进度，控制台轮询它
  ├ stickers/                 几个实例共用的表情包库
  ├ shared/<组名>/config/      共享配置组，加入的实例 config/ 链到这里
  └ xiaoyu/                   序号 1，:8775 :8090 :8779，NapCat WebUI :6199
      ├ data/ config/ .env
      └ adapters engine plugins tools defaults   → 软链回代码目录
```

软链那几个目录不能省：`workspace_root` 既用来找 `data/`，也用来找 `tools/`、`engine/system_nodes/` 和前端 `dist`。`defaults` 是后加的，之前建的实例补一条：`ln -s /opt/kazebot/defaults /opt/kazebot-data/<实例名>/defaults`，否则 agent 读 `defaults/nodes/...` 时找不到文件。

每个实例的 `config/` 默认只属于它自己，里面只放它改过的配置；没改过的节点和 runtime 直接用代码目录 `defaults/` 下的默认，代码更新一次全部实例跟着变。想让几个实例用同一套配置，见下面的「共享配置」。

## 实例名

实例名会拼进 systemd 单元名、工作区目录、NapCat 容器名和 URL 前缀（`/i/<实例名>/`），所以收得很紧：小写字母开头，只含小写字母、数字和下划线，2-32 位；`stickers` 和 `shared` 被共享表情包库、共享配置组占着，不能用。

改成按 bot 开之前，实例是按 QQ 号开的，实例名就是那个号。这类老实例照旧认（5-11 位、不以 0 开头的数字），目录、单元、容器、前缀都不用动；清单里老条目的 `uin` 读出来就是实例名，同时也是它的 QQ 账号。

## 清单

`instances.yaml` 每条记一个实例：

| 字段 | 含义 |
|---|---|
| `id` | 实例名 |
| `label` | 控制台上显示的备注 |
| `path` | URL 前缀，序号 0 为空 |
| `idx` | 序号，决定端口 |
| `platform` | 这个实例跑的平台 id，比如 `qq` |
| `account` | 登录的账号，比如 `"1000000002"`；还没登录过是空串 |
| `uin` | QQ 实例另记的一份账号，只给退回改造前的版本时读 |

`account` 建实例时可以先填；之后在控制台快速切换、扫码登录成功，本实例的 supervisor 会把新号记进自己那一条。别的实例据此拦着同一个号双登 —— 同一个号两边登，会互相把对方踢下线，数据也会分成两份。前端置灰之外，supervisor 的切号接口也会拒绝。比的是平台加账号：同机的 QQ 实例和别的平台的实例，号长得一样也互不相干。

改造前的两种写法照样认：只有 `uin` 的老条目，和一个实例挂几个平台时的 `accounts` 映射（认其中 QQ 那一项）。下次改写清单时会换成上面的写法。

## 启用

一次性，需要 root：

```bash
sudo /opt/kazebot/deploy/install_provision.sh <主实例名> <主实例QQ号>
```

它会装好 root 侧组件与 polkit 规则、建 `/opt/kazebot-data`、用 drop-in 放开主实例对它的写权限，并把主实例登记进清单；宿主清理的定时任务也在这一步装上，见下面「宿主清理」。装完控制台的「账号 → 多开实例」里就能直接建实例。老写法 `install_provision.sh <主实例QQ号>` 照旧能用：实例名和 QQ 号都是那个号。

**改过 `provision_instance.sh`、`cloudflared_ingress.py`、polkit 规则或 `deploy/systemd/` 里的模板之后要重跑一次** —— git 更新的是 `/opt/kazebot` 里的源，不会自动同步到 `/usr/local/lib` 和 `/etc`。实例改按 bot 命名这一次就要重跑：老 polkit 规则只放行纯数字的实例名，老的删实例脚本也只按 `uin` 摘清单。

## 建实例

控制台「账号 → 多开实例」，填实例名、备注和要登录的 QQ 号（可留空）点「建实例」，进度就在下面滚。约一两分钟，完成后去它自己的控制台扫码。

命令行等价物（控制台不可用时）：

```bash
sudo /opt/kazebot/deploy/new_instance.sh <实例名> [标签] [QQ号]
tail -f /opt/kazebot-data/provision-<实例名>.log
```

两条路都走同一份脚手架逻辑（`supervisor/provisioning.py`）和同一个 root 侧脚本。

## 删实例

控制台里点「停用并归档」，手打实例名确认。**数据不会真删**：工作区和 NapCat 目录改名成 `.deleted-<时间戳>`，确认无误后自己清理。序号 0 和当前正在看的实例删不掉。

## 为什么 root 侧要单独一套

supervisor 跑在 `kazebot` 下，`ProtectSystem=strict` + `NoNewPrivileges=yes`，碰不到 docker、systemd 和 `/etc`。而 agent 能以 kazebot 身份执行任意命令 —— 边界靠内核而不是黑名单，这是 `kazebot.service` 的既定设计。

所以建实例被拆成两半：无特权的（工作区、软链、`.env`、清单）由 supervisor 做，其余交给 root 的 oneshot 单元，polkit 只放行 `kazebot-provision@<实例名>` 和 `kazebot-deprovision@<实例名>` 的 `start`。

由此推出几条**改这套东西时不能破的规矩**：

- **root 脚本必须在 `/usr/local/lib/kazebot/`。** 放 `/opt/kazebot/deploy/` 下等于没有 polkit 白名单 —— 那里 kazebot 可写，改写脚本内容再触发就是 root。
- **不 `source` 工作区的 `.env`。** 那是 kazebot 可写的，当输入看待：grep 取值，逐个按形状校验。
- **不用 `/opt/kazebot/.venv` 的解释器。** 同理，用 `/usr/bin/python3`。
- **脚本里不出现递归删除。** 删实例只 `mv` 归档。
- **宿主清理只跑自己那一份。** `qq_cache.py` 只用标准库；它一 import 仓库里的模块，root 拿到的就又是 kazebot 可写的代码。
- **实例名的正则几处必须一致**：`supervisor/provisioning.py`、`provision_instance.sh`、`cloudflared_ingress.py`、polkit 规则，外加两个手动入口 `new_instance.sh`、`install_provision.sh`。宽的那一处说了算。

`tests/test_provisioning.py` 会静态检查上面每一条。

## 宿主清理

各实例 NapCat 容器里的 NTQQ 图片、视频和缓存归 root。实例自己的定期清理以 kazebot 跑、带沙箱，删不动它们，所以整台机器由一个 root 定时任务统一清：`kazebot-host-cleanup.timer` 每天跑一次 `kazebot-host-cleanup.service`，默认删七天没动过的。

- 跑的是 `/usr/local/lib/kazebot/qq_cache.py`，解释器是 `/usr/bin/python3 -I`。这个文件是 `adapters/onebot/qq_cache.py` 的副本，`bootstrap.sh` 和 `install_provision.sh` 负责装，`deploy.sh` 每次更新顺带同步；单元文件改了照上面的规矩重跑 `install_provision.sh`。
- 不读任何工作区的 `.env`。改保留天数、加缓存根用 `sudo systemctl edit kazebot-host-cleanup.service`，在 `[Service]` 下写 `Environment=CLONOTH_QQ_CACHE_MAX_AGE_SECONDS=...` 或 `Environment=CLONOTH_QQ_CACHE_ROOTS=...`。
- 除了经 `/proc/<pid>/root` 找正在跑的容器，还认部署脚本挂出来的 `/opt/napcat/qq` 和 `/opt/napcat-<实例名>/qq`，容器停着也清得到；删实例归档的 `.deleted-*` 目录不碰。以 root 跑时只跟 root 的进程，别的用户起一个命令行带 napcat 的进程引不走它。
- 实例的清理以 `--scope workspace` 跑，不再碰这一项：`kazebot-cleanup@` 写在 `ExecStart` 里，engine 自己定时拉起的那份靠单元里的 `CLONOTH_MAINTENANCE_SCOPE=workspace`。

先演练，再看排期和日志：

```bash
sudo /usr/bin/python3 -I /usr/local/lib/kazebot/qq_cache.py --dry-run
systemctl list-timers kazebot-host-cleanup.timer
journalctl -u kazebot-host-cleanup -n 100 --no-pager
```

## 端口

按序号推导，序号 0 就是单实例部署原有的那一组。唯一真源是 `supervisor/instances.py` 的 `ports_for()`：supervisor 的端口由核心定，平台进程要的端口由平台描述的 `instance_ports` 声明（QQ 声明了 NoneBot、转发桥、NapCat WebUI 三个）。名字重复、两个序号落到同一个端口，`ports_for()` 直接报错。新实例 `.env` 里平台那几行由平台的 `instance_env` 钩子写，QQ 的在 `adapters/onebot/provisioning.py`。

| | supervisor | NoneBot | bridge | NapCat WebUI |
|---|---|---|---|---|
| 序号 0 | 8765 | 8080 | 8769 | 6099 |
| 序号 N | 8765+10N | 8080+10N | 8769+10N | 6099+100N |

序号上限 8。分配时除了避开清单里已登记的，还会实测端口 —— 清单被手改漏记也不会撞上正在跑的实例。

## 已有单实例部署：第一个实例留在原地

不必把现役的实例搬进 `kazebot-data/`。序号 0 的端口和 URL 前缀本来就等同于单实例部署，`install_provision.sh` 用 drop-in 给它补上清单路径即可。

搬反而有代价：NapCat 容器 bind mount 了 `/opt/kazebot/data/attachments`，而 docker 不支持给已存在的容器改挂载。目录搬走后容器暂时还能用（`mv` 不换 inode），但它下次重启就会挂在一个不存在的路径上，届时只能重建容器。

## 容易踩的点

- **序号 0 的 NapCat 容器不要重建。** 现有容器跑 `--network host`，第二个 host 容器会抢 6099 以及 QQ NT 自己占的 4001/4301。新实例一律用 bridge 网络加 `-p 127.0.0.1:<端口>:6099`。
- bridge 网络下 NoneBot 不能只监听 `127.0.0.1`，且必须配 `ONEBOT_ACCESS_TOKEN` —— 这两条 QQ 的 `instance_env` 会按序号自动写进 `.env`。
- **附件目录必须挂成同一个绝对路径**（NapCat 靠路径发图），所以每个实例挂自己那份 `data/attachments`。
- 几个实例共用一个域名，靠 `CLONOTH_URL_PREFIX` 分流。cloudflared 那条 path 规则由建实例脚本自动加，**它会先备份再 `ingress validate`，不过就回滚** —— 同一条 tunnel 上还挂着别的站点。
- 前端 `dist` 只有一份，build 一次即可。

## 隔离到什么程度

会话上下文、长期记忆、人物画像、渠道密钥、人格、群白名单、附件全部各自一份。

**表情包库是唯一共享的数据**：图片和索引都在 `/opt/kazebot-data/stickers/`，各工作区的 `data/stickers` 是指向它的软链。一个实例在群里攒到的表情包，其它实例立刻能用；谁丢弃的图，谁都不会再收第二次。用法见 [STICKERS.md](STICKERS.md)。

之所以做成软链而不是配一个绝对路径：附件白名单（`engine/attachments.py`）只认工作区内的 `data/` 前缀，库放在工作区外就发不出去。

索引是 SQLite，几个实例同时读写靠 WAL，不需要额外协调。打标例外：各实例的 supervisor 每批开打前抢库旁的 `stickers.sqlite3.tagging.lock`，同一时刻只有一个在调看图渠道，免得几份一起把频率限制打满。抢到的实例用自己配的看图渠道，没配好的实例抢到了也只是歇着，别的实例接着打。「最近发过」的去重记录按会话键分开存，所以共享图库不会让 A 群发过的图在 B 群被跳过。

代码和 `.venv` 也是共享的，git 更新会同时作用于所有实例，做不到逐个灰度。

## 共享配置

默认每个实例一套配置。控制台「账号」页的「共享配置」里可以建组：几个实例共用整套 `config/`——节点与人格、技能、工具、MCP、模型渠道与密钥、policy、全局设置、平台配置、绘图、runtime 都在里面，同平台、跨平台都行。**记忆在 `data/` 里，永远不共享。**

- **建组**：把本实例现在的 `config/` 整份抄到 `/opt/kazebot-data/shared/<组名>/config/`，本实例随即加入。本实例 `.env` 里能共享的键（API key 这类）一并抄进组的 `config/secrets.env`，端口、监听地址、平台令牌、路径不抄。
- **加入**：本实例的 `config/` 换成指向组目录的链接，原来那份改名归档到 `data/config.archived-<时间>/`，不删。
- **退出**：拷一份组里当时的配置换成本实例自己的 `config/`，之后两边各改各的。
- **删组**：组里没有成员才能删；组目录改名成 `<组名>.deleted-<时间>` 留着，不做递归删除。

切换之后控制台会重启本实例的 engine，工具和节点换成新配置；模型渠道、policy、runtime、平台配置和按人资料按文件的修改时间、大小和 inode 自己重读，组里别的实例改了这边不用重启。成员身份只看 `config/` 链到哪，没有另记一份名单。

组里几个实例的控制台改的是同一份文件。整份写回的编辑器（原文编辑器、人设正文、服务端策略的规则表）记着读到的是哪一版，保存时发现中途被别处改过就报冲突、不盖掉，按提示点「刷新」「加载」或「丢弃」读入最新内容再改；只改几个键的表单（模型参数、带图路由、平台设置、工具权限）保存时现读现合并，渠道和系统槽位只提交改过的项，别处改的其他项都留着。

密钥分两处放：`config/secrets.env` 跟着组走，放能共享的 API key；`.env` 是实例身份。两边有同名项时 `.env` 的算数。`manage_secret` 按键名自动分流。共享密钥不灌进进程环境，渠道配置里写 `${EXA_API_KEY}` 这种引用、以及搜索绘图这类工具，下次取值就读到新值；直接读进程环境的地方（比如节点声明了别家 provider 却没配密钥时的兜底）看不到它，这种情况把密钥写进渠道配置。

单元里给整个 `/opt/kazebot-data/shared` 放开了写权限：建组要在那下面新建目录，没法只放开某一个组。所以在内核层面，任何一个实例都写得到所有组的配置，连不在组里的实例也一样；组与组之间、组和组外实例之间的边界靠控制台和 policy（agent 改工作区外的路径要审批），不靠 systemd。

### 已有实例启用共享配置前

重跑 `install_provision.sh` 会准备共享目录和主实例的 drop-in，但只把多实例模板更新到 `/usr/local/lib/kazebot/units/`，不会替换已经装在 `/etc/systemd/system/` 的模板，也不会重启主实例的 QQ 适配器。

升级时还需要完成以下步骤，再在控制台建组或加入：

1. 备份并核对现有单元的本机修改，把新版 `kazebot@.service`、`kazebot-qq@.service` 安装到 `/etc/systemd/system/`；保留本机所需的 drop-in。
2. 执行 `systemctl daemon-reload`，在维护窗口重启主实例 `kazebot-qq.service`，以及本次升级的各实例 `kazebot@<实例名>.service`、`kazebot-qq@<实例名>.service`。只重启 engine 不会重建 supervisor 或 bot 的 systemd 文件系统沙箱。
3. 用 `systemctl show <单元名> -p ReadWritePaths` 核对共享目录权限，并检查服务状态与日志。主 supervisor 的权限来自 `10-instances.conf`，主 QQ 的权限来自 `20-shared-config.conf`，多实例的权限来自新版模板。
4. 给旧实例工作区补上指向 `/opt/kazebot/defaults` 的 `defaults` 软链；若同名位置已有内容，先核对并保留，不能直接覆盖。

## 相关环境变量

| 变量 | 作用 |
|---|---|
| `CLONOTH_WORKSPACE` | 工作区根。不设时回落到代码目录，行为与改造前一致。实例的工作区由 systemd 单元给，实例 `.env` 里别写这一行：`EnvironmentFile=` 的值总是盖过单元里的 `Environment=`，整份抄主实例的 `.env` 过来，这个实例就落到了主实例的目录 |
| `CLONOTH_URL_PREFIX` | 控制台与 API 挂载的路径前缀，留空为根 |
| `CLONOTH_MAINTENANCE_SCOPE` | 定期清理管到哪：`workspace` 只清本工作区，不设就是 `all`，连宿主级的一起。装了宿主清理的机器，单元里已经写成 `workspace` |
| `CLONOTH_INSTANCES_FILE` | 共享清单位置，默认 `<工作区>/config/instances.yaml` |
| `NAPCAT_WEBUI_URL` | 本实例对应的 NapCat WebUI，默认 `http://127.0.0.1:6099` |
| `CLONOTH_PORT` / `PORT` / `ONEBOT_FORWARD_BRIDGE_PORT` | 三个监听端口 |
