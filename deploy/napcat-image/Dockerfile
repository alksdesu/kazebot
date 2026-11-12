# NapCat Shell + Linux QQ。QQ 来自构建上下文里已解包的 qq/{opt,usr}，由 build.sh 准备。
FROM ubuntu:22.04
ARG MACHINE_ID=
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y \
      libnss3 libnotify4 libsecret-1-0 libgbm1 xvfb libasound2 fonts-wqy-zenhei gnutls-bin \
      libglib2.0-dev libdbus-1-3 libgtk-3-0 libxss1 libxtst6 libatspi2.0-0 libx11-xcb1 \
      ffmpeg unzip dbus-user-session curl jq gosu tzdata \
    && echo "${TZ}" > /etc/timezone && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN useradd --no-log-init -d /app napcat
WORKDIR /app
COPY NapCat.Shell.zip entrypoint.sh /app/

COPY qq/opt /opt
COPY qq/usr /usr
# 等价于 deb 的 postinst；COPY 不保留 setuid 位
RUN ln -sf /opt/QQ/qq /usr/bin/qq && chmod 4755 /opt/QQ/chrome-sandbox \
    && chmod +x /app/entrypoint.sh \
    && echo "(async () => {await import('file:///app/napcat/napcat.mjs');})();" > /opt/QQ/resources/app/loadNapCat.js \
    && sed -i 's|"main": "[^"]*"|"main": "./loadNapCat.js"|' /opt/QQ/resources/app/package.json \
    && if [ -n "$MACHINE_ID" ]; then echo "$MACHINE_ID" > /etc/machine-id; fi

VOLUME ["/app/napcat/config", "/app/.config/QQ"]
ENTRYPOINT ["bash", "entrypoint.sh"]
