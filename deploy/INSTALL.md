# 部署

一台全新的 Debian / Ubuntu 服务器，到控制台能打开。

```bash
git clone <仓库地址> /opt/kazebot
sudo /opt/kazebot/deploy/bootstrap.sh
```

路径必须是 `/opt/kazebot` —— systemd 单元和建号脚本里都写死了这个位置，脚本检测到别的地方会直接拒绝跑。

**不会问你任何问题。** 模型、管理员、群白名单、扫码登录全在控制台里填，那边有校验、即时生效、填错能改回来。

## 它做了什么

```
1  装系统依赖        python3 / git / curl / docker / nodejs
2  建用户与目录      kazebot 系统用户（nologin），/opt/kazebot 与 /opt/kazebot-data 归它
3  建 venv           pip install -r requirements.txt
4  写 .env           只放机器相关的：端口、随机生成的 Bridge token 和会话摘要盐
5  铺配置模板        data/config.yaml、data/policy.yaml、config/qq.yaml，都是空的等你填
6  编前端            npm ci && npm run build，控制台就是配置入口
7  起 NapCat         docker 容器 + 反向 WS 指向 127.0.0.1:8080
8  装 root 侧组件    deploy.sh、宿主清理脚本放进 /usr/local/lib/kazebot，归 root
9  装 systemd        kazebot.service、kazebot-qq.service、宿主清理的 timer
10 启动并验证        health → engine → 8080 监听 → NapCat 通道
```

端口固定是 `8765`（控制台）/ `8080`（NoneBot）/ `8769`（转发 Bridge）/ `6099`（NapCat WebUI），全部只听本机。这四个数字不能改，以后多开时新号的端口是在它们基础上推出来的。

## 装完：去控制台填四项

脚本最后会打出一条带 token 的地址。不在服务器本机的话先开隧道：

```bash
ssh -L 8765:127.0.0.1:8765 <你的用户>@<服务器>
```

浏览器打开那个地址，按顺序填：

| | 页面 | 填什么 | 不填会怎样 |
|---|---|---|---|
| 1 | **渠道** | 模型 base_url、API key、模型名 | 它不会说话 |
| 2 | **账号** | 手机扫码登录 bot 的 QQ 号 | 连不上 QQ |
| 3 | **权限** | 你自己的 QQ 号，当管理员 | 所有审批自动拒绝，管理命令全废 |
| 4 | **信道** | 要让它待的群号 | 它不在任何群开口 |

都是即时生效，不用重启。填完去群里 @ 它。

## 重跑

脚本可以重复跑，每一步会跳过已经做完的部分，**现有配置一律不覆盖**。中途失败（网络断了、npm 挂了）修完直接再跑一次。

但**已经在跑的机器它会拒绝** —— 更新代码请用：

```bash
sudo /usr/local/lib/kazebot/deploy.sh
```

唯一能调的是 NapCat 镜像版本：

```bash
sudo KAZEBOT_NAPCAT_IMAGE=mlikiowa/napcat-docker:v4.18.5 /opt/kazebot/deploy/bootstrap.sh
```

## 出问题了

```bash
systemctl status kazebot kazebot-qq
journalctl -u kazebot -n 100
journalctl -u kazebot-qq -n 100
docker logs --tail 100 napcat
```

| 现象 | 多半是 |
|---|---|
| 控制台打不开 / 404 | 前端没编出来。`ls /opt/kazebot/adapters/web/frontend/dist`，空的就手动 `npm run build` |
| 控制台能开，账号页是空的 | `.env` 里 `NAPCAT_WEBUI_TOKEN` 没回填，或容器没起来 |
| 扫完码群里 @ 它没反应 | 群号没进白名单，控制台「信道」页加 |
| 它收到了但不回话 | 模型渠道没配或 key 不对，控制台「渠道」页看 |
| 服务显示 running 但完全不干活 | **engine 没起来**。`pgrep -af "python -m engine"` 应该有 2 个进程，没有就看 `data/logs/engine-*.log` |
| 启动即退出，日志说端口被占 | 退出码 98。`ss -ltnp \| grep 8765` 看谁占了 |
| NapCat 连不上 | 按顺序查：8080 在听吗 → 容器是不是 `--network host` → 路径是不是 `/onebot/v11/ws` |

「服务 running 但不干活」这条值得单独记住：systemd 只看得到 supervisor，engine 是它派生出来的子进程，engine 全死了 systemd 照样显示 `active (running)`，从外面完全看不出异常。脚本收尾会专门验这一条。

## 几个坑

**容器重启会丢 QQ 登录态**，得重新扫码。调试时优先重启 `kazebot` / `kazebot-qq`，别顺手 `docker restart napcat`。

**`config/qq.yaml` 的优先级高于 `.env`。** 控制台写的就是这份 yaml，所以脚本生成的 `.env` 里刻意不放任何业务配置 —— 两处都写会变成「在控制台改了不生效」。

**防火墙只放 22。** 8765 / 8080 / 8769 / 6099 一律不要对外，控制台走 SSH 隧道或者带认证的隧道。supervisor 端口后面挂着的不只是控制台页面。

**别用 root 跑 git。** root 执行过一次 `git reset --hard` 之后，被改动的文件属主会变成 root，engine 启动时写不了 `tools/__init__.py` 就起不来 —— 而 supervisor 照常活着，表面看不出任何问题。`deploy.sh` 里有属主兜底，手工操作时自己注意。

## 之后

**多开**（一台机器最多 9 个实例，一个实例登一个号）：

```bash
sudo /opt/kazebot/deploy/install_provision.sh <主实例名> <主实例QQ号>
```

主实例就是现在这一套，名字自己起：小写字母开头，只含小写字母、数字和下划线，2-32 位。只给一个 QQ 号的老写法照旧能用，实例名就是那个号。跑完之后控制台「账号」页就有「多开实例」了，点两下加一个实例。细节见 [MULTI-ACCOUNT.md](MULTI-ACCOUNT.md)。

**表情包库**的配置和排障见 [STICKERS.md](STICKERS.md)。

**备份**：值得留一份的是 `/opt/kazebot/.env`、`data/config.yaml`、`data/policy.yaml`、`config/qq.yaml`，以及 `data/memory/` 和 `data/stickers/`。前四个含密钥，注意存放位置。

## 模型与工具的出站代理

主模型客户端默认忽略系统代理。只有将 `CLONOTH_LLM_TRUST_ENV=1` 注入 `kazebot.service`，它才读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`。留空或设为 `0` 保持原来的直连行为。

需要统一代理时，使用 `systemd/kazebot-model-proxy.conf` 作为主服务的 drop-in。先确认本机代理可用，再将其中的 `127.0.0.1:7893` 换成实际入口；不要把文件装到 `kazebot-qq.service.d`。示例通过 mihomo 的独立 listener 选择出口，不改变其他应用已有的节点选择：

```yaml
listeners:
  - name: qqbot-models
    type: mixed
    listen: 127.0.0.1
    port: 7893
    proxy: QQbot模型出口
```

`QQbot模型出口` 必须是 mihomo 配置中已存在的节点或策略组名称，按实际情况填写。入口及 mihomo 控制 API 都应只监听回环地址。校验配置后热加载 mihomo，避免重启其他代理应用。

```bash
sudo install -d /etc/systemd/system/kazebot.service.d
```

```bash
sudo install -m 644 deploy/systemd/kazebot-model-proxy.conf /etc/systemd/system/kazebot.service.d/20-model-proxy.conf
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl restart kazebot
```

代理覆盖主模型的普通请求与流式回复、备用模型、系统节点、控制台模型列表，以及继承环境的读图、绘图、联网搜索等工具。代理不可用时请求失败，不会自动偷偷改成直连。QQ 收发适配器与 NapCat 不继承此 drop-in。若适配器单元已有 `BindsTo=kazebot.service`，重启主服务会按原有依赖使它短暂重连；不要修改这项依赖或重启 NapCat。engine 到 supervisor 的客户端仍强制直连，其他本机调用由 `NO_PROXY` 保持直连。若另有本地模型域名或内网服务，应将实际主机名或地址同时加入两种大小写的 `NO_PROXY`。

检查主服务与两个 engine 子进程均继承代理环境，并通过 mihomo 连接记录确认实际出口。公开模型列表成功只能证明网络连通；生成权限、余额及地区限制仍需单独验证，验证时使用不含聊天记录的最小请求。

回滚时移除本次新增的 drop-in、重载 systemd 并重启 `kazebot`；如改了 mihomo 配置，恢复备份并热加载。不要删其他已有 drop-in，也不要重启 NapCat。

## 不想用脚本

手工装的顺序是：系统依赖 → 建 kazebot 用户 → clone 到 `/opt/kazebot` 并 chown → venv 和 pip → 拷三份配置模板 → 编前端 → 起 NapCat 容器并把 WebUI token 填回 `.env` → 装 `deploy/systemd/` 里那两个单元 → `systemctl enable --now kazebot kazebot-qq`。

每一步的具体命令直接读 `bootstrap.sh`，它就是按这个顺序写的，函数名和上面一一对应。
