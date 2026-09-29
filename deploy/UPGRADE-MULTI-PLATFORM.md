# 旧部署升级到实例隔离与共享配置

本手册适用于从旧 `data/config.yaml` 布局升级到 `defaults/` 加实例 `config/` 的版本。目标是保持现有账号、会话、记忆、附件和模型接入方式可用，同时保留可验证的回退点。

这些步骤是维护窗口的执行方案，文档完成不代表已部署。先完成现场核对和隔离副本演练，再确定目标提交与维护窗口。首次升级采用下面的受控流程，不直接运行 `bootstrap.sh` 或现有 `deploy.sh`。

## 1. 固定升级范围

- 一个实例只运行一个平台、一个账号；默认各自拥有配置与记忆。
- 本次升级不自动建共享组、不切账号、不更换会话哈希密钥、不整理历史路由。需要共享时，在升级验收通过后单独操作；记忆始终不共享。
- 主实例继续使用 `/opt/kazebot`。保留既有实例 id、idx、URL 前缀、端口及附件绝对路径，不搬工作区、不重建或重启 NapCat。
- 代码、Python 环境和前端产物共用，所有依赖该代码目录的实例必须进入同一个维护窗口。不能一边让旧进程运行，一边原地换公共代码。
- 保留线上 model-proxy 的现有入口与策略；代理代码补丁不随本次功能提交发布，需作为独立的本机补丁恢复。

执行时记录三个版本：本地审阅的完整目标 SHA、线上 Git HEAD、线上实际文件树。线上可能存在未提交的历史同步代码，Git HEAD 不能代表真实运行版本。

## 2. 升级前只读核对

保存一份限制访问的现场记录，至少包含：

| 对象 | 要核对的内容 |
|---|---|
| 代码 | HEAD、分支、受跟踪修改/删除、未跟踪文件、ignored 配置和工具；与目标代码逐文件比较 |
| 实例 | `instances.yaml` 每行的 id/旧 uin、idx、platform/account、工作区、前缀和端口；idx=0 必须唯一 |
| 进程 | 实际运行的 supervisor、engine、平台适配器、定时清理任务及其 cgroup |
| systemd | 主单元、模板、drop-in、User、WorkingDirectory、ReadWritePaths、依赖关系和启动状态 |
| 配置 | 旧/新配置同时存在的情况、布局 marker、文件权限、链接本身与真实目标 |
| 身份 | 当前账号文件、匿名映射、会话哈希密钥的来源、有效环境中的路径覆写；只报告是否存在，不输出密钥 |
| 数据 | sessions、会话正文、记忆、人物资料、附件、提醒、定时任务、群协作、执行计划、资料库、发送防重库及共享图库 |
| 环境 | Python/Node/npm 版本、Python 包状态、前端构建条件、磁盘剩余空间、现有代理行为 |

不要把 `migrate_*.py` 的 dry-run 当作只读检查：CLI 会经过 `offline_workspace()`，触发配置布局迁移；会话迁移预览还会取得 SQLite 写锁并执行回滚事务。线上运行中仅用文件读取、纯哈希计算和明确只读的数据库查询。完整迁移预览在隔离副本上执行。

当前支持 Python 3.11+；前端版本要求以目标 `package-lock.json` 为准。先验证现有运行环境，避免把升级 Python、Node、NapCat 或代理软件夹进本次变更。

## 3. 形成保留清单和发布材料

每个线上差异必须归入以下处理方式，写清来源、目标位置和验证办法：

| 差异 | 处理 |
|---|---|
| 已被目标提交包含的代码 | 使用目标提交，不把旧文件整体覆盖回来 |
| 本机代码补丁 | 在目标源码上保留最小差异，单独校验与测试；model-proxy 属此类 |
| 用户改过的节点、runtime | 放回原来的 `config/` 路径，作为覆盖层 |
| 原仓库默认且未自定义的节点、runtime | 使用新版 `defaults/`，不把整份旧默认复制成永久覆盖 |
| 旧 skills、自定义工具和依赖资源 | 全部备份，按第 7 节迁移；不能只依赖 `.py` 自动发现 |
| 本机 unit/drop-in、cloudflared、polkit 配置 | 与新版逐项合并，保留端口、路径、代理和现有安全边界 |
| 旧测试、缓存、无关辅助文件 | 保留在快照中，除非有明确业务用途，不覆盖新版源码 |

准备固定目标提交的 Git 对象或 bundle、前端依赖锁、依赖安装材料、model-proxy 最小 patch、已审阅的单元文件和完整保留清单。未推送的目标可通过 Git bundle 传递；不要让线上跟随会移动的分支名。

本地只导出指定补丁，不能把整个脏工作区打进发布包。可用 Git 自己写文件，避免 PowerShell 重定向改变编码：

```bash
git diff --binary --output=/安全的发布目录/model-proxy.patch -- engine/runner.py
```

此时只从已审阅目标准备新版 `deploy.sh`，尚不覆盖宿主文件。第 5 节完成旧宿主文件快照并验明后、第 6 节换码前，再安装到 `/usr/local/lib/kazebot/deploy.sh`。旧脚本先 reset 再自更新，第一次丢失的本地配置无法靠后面的自更新补救。

新版也不是完整备份工具：它只保留部分受跟踪文件，仍归仓库管理的代码补丁不会自动恢复；未跟踪/ignored 文件、删除意图、外链目标和完整数据不在其保护范围内。它会自动重启主服务、不会完成所有实例和 systemd 的升级。本轮预装后不直接执行它，按下面步骤换码与验收。

## 4. 隔离副本演练

用受保护的副本验证配置迁移、代理补丁、依赖安装、构建和回滚。严禁让演练服务接入真实 NapCat、发送消息、触发定时任务或调用付费模型。

完整副本至少包含工作区全部配置和数据、实例清单、共享图库与共享配置真实目标、相关外置文件。SQLite 副本必须有一致性保证；普通在线文件复制不能当成同一时刻快照，也不能只取主数据库而忽略 WAL。

涉及账号作用域迁移时，使用隔离 Linux 环境并保持 `/opt/kazebot` 等原绝对路径。`workspace_instance()` 根据解析后的完整绝对路径生成身份；直接把快照放 Windows 临时目录运行 CLI，会改变旧功能数据的身份匹配条件。所有绝对链接和环境路径都必须指向隔离副本。

保留不可变原始副本与工作副本两份。先做配置布局迁移，核对文件差异；只有明确需要改变身份时才预览相应脚本。未执行完整演练时，发布记录应标为未验证，不能用单元测试通过替代。

## 5. 停止写入并建立回退点

1. 记录各应用和 timer 原来的启停状态，暂停实际存在的清理/调度入口和其他数据写入者。停外部 timer 不能停止 supervisor 内置调度，应用进程也必须停止。
2. 先停所有相关平台适配器，再停 supervisor，确认对应 cgroup 的 engine 全部退出。NapCat 保持现状。
3. 备份整个代码工作区和数据根，包含 `.git`、`.env`、旧/新配置、未跟踪/ignored 工具、skills、`data/`、`.venv`、前端 dist、实例清单、共享图库。归档保留链接本身，同时另行包含所有外部真实目标。
4. 备份相关 unit/drop-in、root 脚本、polkit 和 cloudflared 配置。给出文件清单、哈希和原属主/权限。
5. 验证归档能列出和解包，核对业务文件数量与数据库完整性，确认空间够存原始快照、失败现场和演练副本。

以下为当前主实例路径的备份示例，只有完成停写及路径核对后执行；有其他实例或外置目录时须补入：

```bash
set -euo pipefail
umask 077
BACKUP="/var/backups/kazebot/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BACKUP"
tar --acls --xattrs --numeric-owner -cpf "$BACKUP/workspaces.tar" \
  -C / opt/kazebot opt/kazebot-data
tar --acls --xattrs --numeric-owner -cpf "$BACKUP/host-config.tar" \
  -C / etc/systemd/system usr/local/lib/kazebot etc/polkit-1/rules.d
sha256sum "$BACKUP/workspaces.tar" "$BACKUP/host-config.tar" > "$BACKUP/SHA256SUMS"
tar -tf "$BACKUP/workspaces.tar" > "$BACKUP/workspaces.files"
sha256sum -c "$BACKUP/SHA256SUMS"
```

本机存在的 cloudflared 配置和其他外置目标另行纳入快照。上例不备份或修改运行中的 NapCat；若额外拷贝它的挂载数据，必须标为在线副本，不宣称一致，也不要在应用回滚时顺手覆盖它。

备份在代码目录之外，权限保持私有，不进入 Git 或共享下载地址。不能拿 `data/deploy-backup/` 或迁移脚本自己的局部备份替代这个回退点。

## 6. 受控换码、恢复补丁与准备运行环境

服务保持停止。确认完整目标 SHA 的 Git 对象已经可用、备份已验明、未跟踪文件与目标路径的碰撞已处理；将已准备的新版部署脚本安装到 root 脚本位置但不运行它，再以 `kazebot` 身份换码：

```bash
ROOT=/opt/kazebot
TARGET='<已审阅的40位提交SHA>'
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || exit 1
sudo -u kazebot git -C "$ROOT" cat-file -e "$TARGET^{commit}"
sudo -u kazebot git -C "$ROOT" reset --hard "$TARGET"
```

这是会覆盖受跟踪修改的步骤，不能提前执行。不要运行 `git clean -fdx`；它会删除忽略的配置、数据和环境。

按保留清单恢复自定义覆盖层与资源，不整份还原旧代码。被 reset 移除的自定义 `config/nodes/*.yaml` 和 `config/runtime.yaml` 要在首次迁移和启动前放回。

对独立 model-proxy patch 先 `git apply --check`，成功后再应用；失败就停止并重新审阅，不整文件恢复旧 runner：

```bash
PATCH=/安全的发布目录/model-proxy.patch
sudo -u kazebot git -C /opt/kazebot apply --check "$PATCH"
sudo -u kazebot git -C /opt/kazebot apply "$PATCH"
sudo -u kazebot /opt/kazebot/.venv/bin/python -m py_compile /opt/kazebot/engine/runner.py
```

patch 必须可由目标用户读取。保留原 supervisor 的 `20-model-proxy.conf` 及真实代理入口，不拿仓库示例端口替换线上值，不装到 QQ 适配器。确认模型 HTTP client 使用 `CLONOTH_LLM_TRUST_ENV` 开关，supervisor 内部 HTTP client 仍然直连。其他实例需要代理时，单独核对各自的 supervisor drop-in。

明确完成 Python 依赖检查与前端构建，不依赖旧部署脚本的“有 node_modules 才构建”分支：

```bash
sudo -u kazebot /opt/kazebot/.venv/bin/python -m pip install -r /opt/kazebot/requirements.txt
sudo -u kazebot /opt/kazebot/.venv/bin/python -m pip check
sudo -u kazebot npm --prefix /opt/kazebot/adapters/web/frontend ci
sudo -u kazebot npm --prefix /opt/kazebot/adapters/web/frontend run build
```

已启用的可选功能按其依赖文件核对，不删除现场已有的额外包。若更新依赖，原 `.venv` 必须可还原；升级完成后不能只退源码却继续使用无法兼容的依赖。

## 7. 配置布局迁移与逐项核对

迁移前检查每个工作区：`config/` 必须是正常目录或可信的共享组链接，不能是断链。主实例在代码目录直接使用新版 `defaults/`；旧附加实例补 `defaults → /opt/kazebot/defaults`，只在原位置完全不存在时创建，遇到同名目录或链接先核对，不覆盖。

所有实例保持原工作区路径。确认 `.env` 中没有将实例错误指回主工作区的 `CLONOTH_WORKSPACE`；systemd 的 `EnvironmentFile` 会覆盖单元 `Environment`。

自动迁移行为如下：

| 旧位置 | 新位置/处理 |
|---|---|
| `data/config.yaml` | 模型部分到 `config/models.yaml`；存在的顶层 `platforms` 段到私有 `data/platform_secrets.yaml` |
| `data/policy.yaml` | `config/policy.yaml` |
| `data/mcp_clients.yaml` | `config/mcp.yaml` |
| `skills/` | `config/skills/` |
| `tools/drawtools/settings.yaml`、`character_tags.yaml`、三份实际 prompt | `config/drawtools/` 的对应位置 |
| Git 识别的未跟踪且未忽略 `.py` 工具 | 老实例的 `config/tools/`，排除 `__init__.py` |
| `config/nodes/`、runtime、QQ 配置、人物资料 | 保持原位置；必须事先保住 Git 切换涉及的自定义项 |

迁移只复制不删除，目标存在就不覆盖。ignored 工具、非 `.py` 依赖资源和已跟踪工具的本机修改不属于自动发现范围，须按保留清单处理。旧工具副本会通过 marker 的 `legacy_tools` 清单避免重复注册。

在停机的工作区可主动运行布局迁移，再逐项验收，避免让服务启动同时承担迁移和业务接入：

```bash
WS=/opt/kazebot
cd /opt/kazebot
sudo -u kazebot /opt/kazebot/.venv/bin/python - "$WS" <<'PY'
import json
import sys
import config_layout

result = config_layout.ensure_layout(sys.argv[1])
print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
PY
```

检查 `data/.config_layout.json` 的 `version`、`copied`、`warnings`、`legacy_tools`，并比对实际文件内容和权限；不能只看到 marker 就宣布成功。`models.yaml` 要按移除 `platforms` 后的语义比对，其他纯复制文件可比字节。

出现 warning、目标新文件早已存在、旧新文件不一致或平台密钥缺失时停止启动并核对。布局迁移不是完整事务：models 写成功而平台 secrets 写失败后，重跑会因 models 已存在跳过该段。恢复维护快照或按明确差异补齐；不要靠删 marker、反复重启试运气。

## 8. 身份与历史数据：默认保持原状

固定保留账号记录、会话哈希密钥、匿名映射、路由表、sessions、全部 memory 目录和功能数据。账号名、密钥、工作区绝对路径任一变化都可能导致旧数据读不到。

以下脚本不是普通版本升级的必跑步骤：

| 脚本 | 什么时候才使用 | 本次默认 |
|---|---|---|
| `migrate_qq_bot_scope.py` | 明确要把旧无作用域/其他账号数据归到目标账号，且冲突处理已审阅 | 不运行；若现场确实首次启用账号作用域，另作迁移方案 |
| `migrate_qq_conversation_hash.py` | 主动改变哈希密钥或从无盐切有盐 | 不运行；它不迁短期 sessions，不能作为无损升级默认操作 |
| `migrate_platform_identity.py` | 明确的跨平台/身份对应关系迁移 | 不运行 |

只有旧部署尚未启用账号 scope 时，新 bot 首次登录才可能把新消息带进不同空间，此时必须在副本中先解决迁移；不能把“有当前账号文件”当作所有历史路由都已归属正确的证明。

混合旧路由、未知来源路由、找不到路由的记忆目录一律完整保留。若当前在线会话已经用目标账号和同一密钥，新旧程序算法一致，可以只升级代码和配置布局；历史整理另做，不为了清空诊断列表强制合并。

如以后决定迁移，报告中的 `blocked`、`unresolved`、`unknown_conv_dirs` 和各存储的 `replaced` 都要逐项审阅，即使退出码为 0。目标已有 active session 时可能被标 reset，部分功能记录可能被替换；这不是自动无损合并。迁移不是跨文件事务，部分失败后不能只重跑命令。

## 9. 更新宿主单元，仍保持服务停止

本次优先逐项安装已审阅的文件，避免安装器过早启动应用。

- root 执行的脚本安装到 `/usr/local/lib/kazebot/`，保持 root 属主。更新 `deploy.sh`、`provision_instance.sh`、`cloudflared_ingress.py`、`qq_cache.py` 及所需模板。
- 主实例 unit 与新模板逐份合并本机配置，不直接覆盖仍在使用的定制。模板既要更新 `$LIB/units/`，也要更新实际 `/etc/systemd/system/` 中的版本。
- 主 supervisor 保留数据根写权限和 `CLONOTH_INSTANCES_FILE`；主 QQ 增加共享目录写权限。模板实例要允许自己的工作区、stickers 和 shared。
- 准备 `/opt/kazebot-data/shared`，属主 `kazebot`、权限 `0700`。继续保留现有图库真实目录与链接。
- 为对应应用配置合法的平台 id，QQ 为 `qq`；保留原账号、端口和路径。主 QQ 同样需要知道数据根，见下方说明。
- 安装所需 polkit、provision/deprovision、cleanup 单元，但维护窗口内不启用会写数据的 timer。
- 合并后执行 `systemctl daemon-reload`，以 `systemctl cat/show` 核对最终生效配置。

`install_provision.sh` 不是“只装文件”：它会覆盖主服务的 `10-instances.conf`、主 QQ 的 `10-stickers.conf`/`20-shared-config.conf`，启用宿主清理 timer，重写主实例登记并直接重启 supervisor；不自动更新现存 `/etc` 多实例模板，也不保证 QQ 适配器恢复运行。若选择运行它，只能放在代码、依赖、配置、补丁和副本验收全部就绪之后，并预先处理上述副作用。

重跑安装器必须沿用清单原 idx=0 实例 id；旧格式只有 `uin` 时按兼容规则沿用它，不能新取名字，否则可能产生两个 idx=0。保留原标签，避免安装器重写标签后误以为换了实例。

共享组的可信链接判断依赖 `CLONOTH_INSTANCES_FILE` 所在的数据根。给会访问共享 `config/` 的主 QQ 服务同样配置这个环境变量，不能只给 supervisor；附加实例模板也要逐个核对。未配置时，本次保持独立 `config/` 可先运行，但不能宣称该实例已具备共享组切换条件。

## 10. 分层启动与验收

启动 supervisor 就越过了可能产生新业务写入和模型调用的边界：它会启动内置 scheduler，现有 schedules、dream、data_cleanup 可能执行，重启协调也会处理旧会话。停 QQ 和外部 timer 不能保证这些行为被暂停。先在副本中核对待执行任务并验证需要的暂停方案；没有经验证的暂停方案时，首次启动必须按正式恢复业务对待，不能称为“只读验收”。恢复服务的授权范围也必须包含这些已说明的启动行为。

1. 先启动原本运行的 supervisor，检查对应 cgroup、worker 状态及本次启动日志。`/v1/health` 只证明 supervisor 应答，不能代替 engine 健康；不要用全局 `pgrep` 判断某个实例的 worker。
2. 核对工作区、配置来源、平台与账号、渠道和凭据存在性、节点覆盖层、工具、技能、MCP、调度与功能数据数量。检查迁移 warning 和权限错误。
3. 核对 model-proxy 的 supervisor/engine 实际环境和模型 client 补丁。保留 localhost 的 `NO_PROXY`；QQ 适配器不继承模型代理。仅“模型列表可访问”不足以证明实际生成链路正常。
4. 确认前端产物来自目标提交；验收登录、读取配置、原文冲突提示、刷新、实例切换，避免用生产配置作随手测试数据。
5. 再启动对应 QQ 适配器，确认 NapCat 仍是原账号。明确启动，不能依赖 `BindsTo` 自动恢复适配器。
6. 在受控会话检查原有短期历史、长期记忆、人物资料和历史附件，核对正常收发、提醒和发送防重。实际发消息或调用付费模型须纳入已批准的验收动作；不能由只读核对自行触发。
7. 验收后按原计划恢复确实被暂停的任务和外部 cleanup timer；没有显式暂停的内置调度从 supervisor 启动时就已经运行。共享组的创建、退出、切换属于后续操作，不混入本次数据迁移验收。

通过条件应包括文件/数据对照、实例边界、页面行为和实际进程状态，不能只以 `systemctl is-active` 或测试数量作为上线成功的依据。

## 11. 回滚

维护窗口内冻结配置编辑、账号切换、实例增删与共享组操作。始终先保存失败现场，再决定恢复范围。

- **尚未开放新写入**：通常指新版 supervisor 尚未启动，或已有经验证的隔离/暂停措施。停止相关应用和 timer；将升级后的目录移到独立失败现场位置，不在上面直接解压旧快照。按原路径恢复完整旧代码、未提交补丁、依赖、dist、配置、数据、链接及宿主文件，再 daemon-reload，按原运行状态启动核验。
- **已经产生新消息或业务写入**：先保存这部分新数据，审查各存储的兼容性和增量合并办法。不能直接覆盖 `data/` 并称无损，也不能保证简单 `git reset` 就能回退。特别是 scope/hash 已变更时，需要恢复配套身份和数据关系。
- 旧配置文件虽保留，升级后对 `config/models.yaml` 等的修改不会自动同步回旧 `data/config.yaml`。回退只换代码可能回到旧配置。
- 恢复本次实际修改的 unit/drop-in、root 脚本、polkit、实例清单与 timer 状态，不整体覆盖无关系统配置。
- NapCat 保持原容器、原挂载和登录状态；不要因应用失败顺手重建容器或还原在线登录副本。

验证回滚不仅看进程启动，还要核对原渠道、账号、会话、记忆、附件和代理行为。保留快照至观察期结束。

## 12. 发布记录

记录审阅目标 SHA、合并方式、旧代码及实际文件树、执行人和时间、备份位置与校验、迁移报告、恢复的本机补丁、依赖/构建结果、每项验收结论、回滚演练结果和仍保留的历史数据问题。日志中的密钥、账号映射和聊天正文不进入仓库。

相关实现：`config_layout.py`、`deploy.sh`、`install_provision.sh`、三份 `migrate_*.py`。常规多实例管理见 [MULTI-ACCOUNT.md](MULTI-ACCOUNT.md)。
