# 自建 NapCat 镜像

NapCat 上游的镜像仓库和源码仓库已经没了，腾讯也撤掉了旧版 Linux QQ 的下载。起容器用的镜像由这里构建，标签写在 `image.env`，`bootstrap.sh`、`provision_instance.sh` 和 `build.sh` 都读它。

要自备两个文件：

| 文件 | 来源 | 校验 |
|---|---|---|
| Linux QQ 3.2.34-53644 amd64 | 原 deb，或 `dpkg -x` 解出的目录 / 其 tar 包 | 解包目录 tar.zst 的 sha256 `436dc22dc7286fad9635671d43b319b4f2975e6446a218242577645dffea6304` |
| `NapCat.Shell.zip` 4.18.42 | NapCat 最后一次发布的 Shell 包 | zip 里 `napcat.mjs` 含 `"4.18.42"` |

```bash
sudo /opt/kazebot/deploy/napcat-image/build.sh --qq linuxqq_3.2.34-53644_amd64.deb --shell NapCat.Shell.zip
```

`--qq` 也接受已解包目录或它的 tar 包。不传 `--machine-id` 时沿用机器上在跑的 `napcat*` 容器里的值，重建容器后 QQ 看到的仍是同一台机器；全新机器不传就用基础镜像自带的。换标签用 `--tag`，同时改 `image.env`。

镜像里 QQ 的 `machine-id`、容器的 hostname（`bootstrap.sh` 的 `KAZEBOT_NAPCAT_HOSTNAME` / 实例 `.env` 的 `NAPCAT_HOSTNAME`）和挂进去的 `/app/.config/QQ` 卷一起构成 QQ 眼里的设备；升级镜像时保住这三样，登录态和设备 GUID 就能沿用，不用重新扫码。
