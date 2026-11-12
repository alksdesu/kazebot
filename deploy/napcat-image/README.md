# 自建 NapCat 镜像

NapCat 上游的镜像仓库和源码仓库已经没了，起容器用的镜像由这里构建。`build.sh` 按实际版本打 tag（`kazebot/napcat:<NapCat 版本>-qq<QQ 版本>`），再把 `image.env` 里的别名 `kazebot/napcat:current` 指到它；`bootstrap.sh` 和 `provision_instance.sh` 起容器认的是别名，已有实例跟着在跑的容器走。

要自备两个文件：

| 文件 | 来源 | 说明 |
|---|---|---|
| Linux QQ amd64 | [官网](https://im.qq.com/linuxqq/index.shtml)当前版本的 deb（写这份文档时是 `QQ_3.2.33_260902_amd64_01.deb`），或 `dpkg -x` 解出的目录 / 其 tar 包 | 下载 CDN 只对大陆 IP 开放，境外机器拿不到就在本地下好传上去。NapCat 4.18.42 认 3.2.33-52892 与 3.2.34-53644 |
| `NapCat.Shell.zip` | NapCat 最后一次发布（4.18.42）的 Shell 包 | zip 里 `napcat.mjs` 含 `"4.18.42"` |

```bash
sudo /opt/kazebot/deploy/napcat-image/build.sh --qq QQ_3.2.33_260902_amd64_01.deb --shell NapCat.Shell.zip
```

不传 `--machine-id` 时沿用机器上在跑的 `napcat*` 容器里的值，重建容器后 QQ 看到的仍是同一台机器；全新机器不传就用基础镜像自带的。`--tag` 可以换版本 tag，别名始终按 `image.env`。

镜像里 QQ 的 `machine-id`、容器的 hostname（`bootstrap.sh` 的 `KAZEBOT_NAPCAT_HOSTNAME` / 实例 `.env` 的 `NAPCAT_HOSTNAME`）和挂进去的 `/app/.config/QQ` 卷一起构成 QQ 眼里的设备；升级镜像时保住这三样，登录态和设备 GUID 就能沿用，不用重新扫码。
