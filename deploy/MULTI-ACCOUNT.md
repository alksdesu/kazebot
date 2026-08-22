# 一台机器跑多个 QQ 号

一个 adapter 进程只服务一个号（账号作用域是单值，群历史等缓存按裸群号索引），所以多开的形态是**几套独立进程共用一份代码**。

```
/opt/kazebot/                 代码，git 更新一次，全部实例生效
/opt/kazebot-data/
  ├ instances.yaml            控制台切换器读的共享清单
  ├ 1000000001/               序号 0：端口与路径都和单实例时一样
  │   ├ data/ config/ .env
  │   └ adapters engine plugins tools   → 软链回代码目录
  └ 1000000002/               序号 1
      └ …                     :8775 :8090 :8779，NapCat WebUI :6199
```

软链那四个目录不能省：`workspace_root` 既用来找 `data/`，也用来找 `tools/`、`engine/system_nodes/` 和前端 `dist`。

## 已有单实例部署：第一个号留在原地

不必把现役的号搬进 `kazebot-data/`。序号 0 的端口和 URL 前缀本来就等同于单实例部署，所以让它继续拿 `/opt/kazebot` 当工作区即可，加号时只要在它的 `.env` 里补一行：

```bash
CLONOTH_INSTANCES_FILE=/opt/kazebot-data/instances.yaml
```

两个号读同一份清单，切换器就认得彼此。

搬反而有代价：NapCat 容器 bind mount 了 `/opt/kazebot/data/attachments`，而 docker 不支持给已存在的容器改挂载。目录搬走后容器暂时还能用（`mv` 不换 inode），但它下次重启就会挂在一个不存在的路径上，届时只能重建容器。

## 端口

按序号推导，序号 0 就是单实例部署原有的那一组。

| | supervisor | NoneBot | bridge | NapCat WebUI |
|---|---|---|---|---|
| 序号 0 | 8765 | 8080 | 8769 | 6099 |
| 序号 N | 8765+10N | 8080+10N | 8769+10N | 6099+100N |

## 建实例

```bash
/opt/kazebot/deploy/new_instance.sh <uin> <序号> [标签]
```

脚本幂等，只建目录骨架、软链、`.env` 和清单条目。NapCat 容器、systemd、入口这三步需要 root，脚本会把命令打印出来。已有的 `.env` 和真实目录它不会覆盖。

## 容易踩的点

- **序号 0 的 NapCat 容器不要重建。** 现有容器跑 `--network host`，第二个 host 容器会抢 6099 以及 QQ NT 自己占的 4001/4301。新号一律用 bridge 网络加 `-p 127.0.0.1:<端口>:6099`。
- **附件目录必须挂成同一个绝对路径**（NapCat 靠路径发图），所以每个实例挂自己那份 `data/attachments`。
- bridge 网络下 NoneBot 不能只监听 `127.0.0.1`，且必须配 `ONEBOT_ACCESS_TOKEN`。
- 几个号共用一个域名，靠 `CLONOTH_URL_PREFIX` 分流。cloudflared 只需把 `/i/<号>/` 指到对应端口，**不需要重写路径**——supervisor 自己就挂在那个前缀下。
- 控制台左轨的账号切换器只在清单里有两个及以上条目时出现，单实例看不到任何变化。
- 前端 `dist` 只有一份，build 一次即可。

## 隔离到什么程度

工作区之间不共享任何文件：会话上下文、长期记忆、人物画像、渠道密钥、人格、群白名单、附件全部各自一份。

共享的只有代码和 `.venv`，也就是说 git 更新会同时作用于所有号，做不到逐个灰度。

## 相关环境变量

| 变量 | 作用 |
|---|---|
| `CLONOTH_WORKSPACE` | 工作区根。不设时回落到代码目录，行为与改造前一致 |
| `CLONOTH_URL_PREFIX` | 控制台与 API 挂载的路径前缀，留空为根 |
| `CLONOTH_INSTANCES_FILE` | 共享清单位置，默认 `<工作区>/config/instances.yaml` |
| `NAPCAT_WEBUI_URL` | 本实例对应的 NapCat WebUI，默认 `http://127.0.0.1:6099` |
| `CLONOTH_PORT` / `PORT` / `ONEBOT_FORWARD_BRIDGE_PORT` | 三个监听端口 |
