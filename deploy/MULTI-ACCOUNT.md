# 一台机器跑多个 QQ 号

一个 adapter 进程只服务一个号（账号作用域是单值，群历史等缓存按裸群号索引），所以多开的形态是**几套独立进程共用一份代码**。

```
/opt/kazebot/                 代码，git 更新一次，全部实例生效
/usr/local/lib/kazebot/       root 侧建号组件，kazebot 写不到
/opt/kazebot-data/
  ├ instances.yaml            控制台切换器读的共享清单
  ├ provision-<uin>.log       建号进度，控制台轮询它
  ├ 1000000001/               序号 0：端口与路径都和单实例时一样
  │   ├ data/ config/ .env
  │   └ adapters engine plugins tools   → 软链回代码目录
  └ 1000000002/               序号 1
      └ …                     :8775 :8090 :8779，NapCat WebUI :6199
```

软链那四个目录不能省：`workspace_root` 既用来找 `data/`，也用来找 `tools/`、`engine/system_nodes/` 和前端 `dist`。

## 启用

一次性，需要 root：

```bash
sudo /opt/kazebot/deploy/install_provision.sh <主实例QQ号>
```

它会装好 root 侧组件与 polkit 规则、建 `/opt/kazebot-data`、用 drop-in 放开主实例对它的写权限，并把主实例登记进清单。装完控制台的「账号 → 多开实例」里就能直接加号。

**改过 `provision_instance.sh`、`cloudflared_ingress.py` 或 `deploy/systemd/` 里的模板之后要重跑一次** —— git 更新的是 `/opt/kazebot` 里的源，不会自动同步到 `/usr/local/lib`。

## 加号

控制台「账号 → 多开实例 → 加号」，填 QQ 号点「建实例」，进度就在下面滚。约一两分钟，完成后去它自己的控制台扫码。

命令行等价物（控制台不可用时）：

```bash
sudo /opt/kazebot/deploy/new_instance.sh <uin> [标签]
tail -f /opt/kazebot-data/provision-<uin>.log
```

两条路都走同一份脚手架逻辑（`supervisor/provisioning.py`）和同一个 root 侧脚本。

## 删号

控制台里点「删掉」，手打 QQ 号确认。**数据不会真删**：工作区和 NapCat 目录改名成 `.deleted-<时间戳>`，确认无误后自己清理。序号 0 和当前正在看的实例删不掉。

## 为什么 root 侧要单独一套

supervisor 跑在 `kazebot` 下，`ProtectSystem=strict` + `NoNewPrivileges=yes`，碰不到 docker、systemd 和 `/etc`。而 agent 能以 kazebot 身份执行任意命令 —— 边界靠内核而不是黑名单，这是 `kazebot.service` 的既定设计。

所以建号被拆成两半：无特权的（工作区、软链、`.env`、清单）由 supervisor 做，其余交给 root 的 oneshot 单元，polkit 只放行 `kazebot-provision@<数字>` 和 `kazebot-deprovision@<数字>` 的 `start`。

由此推出几条**改这套东西时不能破的规矩**：

- **root 脚本必须在 `/usr/local/lib/kazebot/`。** 放 `/opt/kazebot/deploy/` 下等于没有 polkit 白名单 —— 那里 kazebot 可写，改写脚本内容再触发就是 root。
- **不 `source` 工作区的 `.env`。** 那是 kazebot 可写的，当输入看待：grep 取值，逐个按形状校验。
- **不用 `/opt/kazebot/.venv` 的解释器。** 同理，用 `/usr/bin/python3`。
- **脚本里不出现递归删除。** 删号只 `mv` 归档。
- **uin 正则三处必须一致**：`supervisor/provisioning.py`、`provision_instance.sh`、polkit 规则。宽的那一处说了算。

`tests/test_provisioning.py` 会静态检查上面每一条。

## 端口

按序号推导，序号 0 就是单实例部署原有的那一组。唯一真源是 `supervisor/instances.py` 的 `ports_for()`。

| | supervisor | NoneBot | bridge | NapCat WebUI |
|---|---|---|---|---|
| 序号 0 | 8765 | 8080 | 8769 | 6099 |
| 序号 N | 8765+10N | 8080+10N | 8769+10N | 6099+100N |

序号上限 8。分配时除了避开清单里已登记的，还会实测端口 —— 清单被手改漏记也不会撞上正在跑的实例。

## 已有单实例部署：第一个号留在原地

不必把现役的号搬进 `kazebot-data/`。序号 0 的端口和 URL 前缀本来就等同于单实例部署，`install_provision.sh` 用 drop-in 给它补上清单路径即可。

搬反而有代价：NapCat 容器 bind mount 了 `/opt/kazebot/data/attachments`，而 docker 不支持给已存在的容器改挂载。目录搬走后容器暂时还能用（`mv` 不换 inode），但它下次重启就会挂在一个不存在的路径上，届时只能重建容器。

## 容易踩的点

- **序号 0 的 NapCat 容器不要重建。** 现有容器跑 `--network host`，第二个 host 容器会抢 6099 以及 QQ NT 自己占的 4001/4301。新号一律用 bridge 网络加 `-p 127.0.0.1:<端口>:6099`。
- bridge 网络下 NoneBot 不能只监听 `127.0.0.1`，且必须配 `ONEBOT_ACCESS_TOKEN` —— 这两条 `provisioning.py` 会按序号自动写进 `.env`。
- **附件目录必须挂成同一个绝对路径**（NapCat 靠路径发图），所以每个实例挂自己那份 `data/attachments`。
- 几个号共用一个域名，靠 `CLONOTH_URL_PREFIX` 分流。cloudflared 那条 path 规则由建号脚本自动加，**它会先备份再 `ingress validate`，不过就回滚** —— 同一条 tunnel 上还挂着别的站点。
- 前端 `dist` 只有一份，build 一次即可。

## 隔离到什么程度

会话上下文、长期记忆、人物画像、渠道密钥、人格、群白名单、附件全部各自一份。

**表情包库是唯一共享的数据**：图片和索引都在 `/opt/kazebot-data/stickers/`，各工作区的 `data/stickers` 是指向它的软链。一个号在群里攒到的表情包，其它号立刻能用；谁丢弃的图，谁都不会再收第二次。用法见 [STICKERS.md](STICKERS.md)。

之所以做成软链而不是配一个绝对路径：附件白名单（`engine/attachments.py`）只认工作区内的 `data/` 前缀，库放在工作区外就发不出去。

索引是 SQLite，几个实例同时读写靠 WAL，不需要额外协调。「最近发过」的去重记录按会话键分开存，所以共享图库不会让 A 群发过的图在 B 群被跳过。

代码和 `.venv` 也是共享的，git 更新会同时作用于所有号，做不到逐个灰度。

## 相关环境变量

| 变量 | 作用 |
|---|---|
| `CLONOTH_WORKSPACE` | 工作区根。不设时回落到代码目录，行为与改造前一致 |
| `CLONOTH_URL_PREFIX` | 控制台与 API 挂载的路径前缀，留空为根 |
| `CLONOTH_INSTANCES_FILE` | 共享清单位置，默认 `<工作区>/config/instances.yaml` |
| `NAPCAT_WEBUI_URL` | 本实例对应的 NapCat WebUI，默认 `http://127.0.0.1:6099` |
| `CLONOTH_PORT` / `PORT` / `ONEBOT_FORWARD_BRIDGE_PORT` | 三个监听端口 |
