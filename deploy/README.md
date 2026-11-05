# 云端部署与验收指南

## 配置边界

本目录提供 Nginx、FRP、防火墙与证书续期模板，需结合实际云主机配置审核后安装。
公网入口格式为 `https://<公网IPv4>:18443/`。文中的 `203.0.113.10` 仅为示例，
请使用实际公网地址；管理地址、SSH 凭据、FRP token 和证书私钥均只保存在私有位置。

在本机运行 `python scripts/render_cloud.py --public-ip <公网IPv4>`，
生成 `~/.local/state/web-desktop/cloud-bundle/`。其中认证哈希也属于凭据，不得提交仓库。

## 部署顺序

1. 登录正确云主机，只读核对主机身份、Nginx include 目录、frps 配置/版本、
   18097/18443 端口占用、主机防火墙管理方式，以及现有代理的业务基线。
2. 将本次涉及的配置和防火墙状态备份到带时间戳的私有目录。不得全量替换现有 Nginx
   配置或 FRP 配置，也不改变 frps 全局 `proxyBindAddr`。
3. 先通过安全组与主机防火墙阻断非回环来源访问 18097，覆盖 IPv4/IPv6。
   `nft-web-desktop.nft` 是使用 nftables 时的规则草案，须合入实际系统的持久化机制。
   若云端使用 UFW/firewalld/iptables，应通过对应管理工具实现同样限制。
   18443 需允许浏览器访问，原 FRP 控制端口和代理规则保留。
4. 在现有端口 80 的 Nginx 虚拟主机中增加 ACME 验证路径，先验证公网可读，
   再申请受信任的 IP 地址证书。不另建冲突的默认 80 虚拟主机。
5. 安装独立网页认证文件，部署 `nginx-web-desktop.conf`，执行 `nginx -t` 后重载。
6. 读取 frps 实际证书及信任链，在 frpc 顶层显式设置 TLS、信任 CA 和服务端名称。
   确认 token 认证已启用，实际 token 不能放进仓库。使用 `frpc verify -c ...` 检查，结合现有版本的
   热重载机制或可控重启启用 `frpc-desktop.toml.example` 中的代理，保留原有代理。
7. 设置续期定时器，执行续期演练，再做下文的公网验收。

FRP 的顶层 TLS 配置示意如下，CA 必须来自核实后的真实 frps，不能把此占位路径直接上线：

```toml
transport.tls.enable = true
transport.tls.trustedCaFile = "/path/to/frps-ca.pem"
transport.tls.serverName = "frps.example.com"
```

`transport.useEncryption = true` 不能替代服务端证书校验。涉及共享 frps 证书变更时，
须先核对其他 frpc 的兼容性与重载方式，不能直接重启而跳过已有业务回归。

## HTTPS 与续期

ACME 验证路径示例，需要加到已经服务公网 IP 的现有 Nginx 80 端口 server 内：

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/lib/letsencrypt;
    auth_basic off;
    try_files $uri =404;
}
```

按照 [Let's Encrypt 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)，
IP 证书的 webroot 验证需 Certbot 5.4 或以上版本，证书有效期为六天。
在云端独立虚拟环境 `/opt/web-desktop-certbot` 安装 Certbot。
确认 HTTP-01 验证文件从外部可读取后，以实际 ACME 账户参数申请：

```bash
/opt/web-desktop-certbot/bin/certbot certonly --webroot \
  -w /var/lib/letsencrypt --ip-address 203.0.113.10 \
  --cert-name web-desktop-ip --preferred-profile shortlived
```

交互或自动化部署时使用实际公网 IP、账户邮箱与服务条款参数。
先添加 `--staging` 演练，再从生产 ACME 服务申请受信任证书。
需要持续自动续期。将 `50-web-desktop-nginx` 以可执行权限放入
`/etc/letsencrypt/renewal-hooks/deploy/`，将提供的两个 systemd 文件安装到
`/etc/systemd/system/`，并检查：

```bash
systemctl daemon-reload
systemctl enable --now web-desktop-renew.timer
systemctl list-timers web-desktop-renew.timer
/opt/web-desktop-certbot/bin/certbot renew --cert-name web-desktop-ip --dry-run --run-deploy-hooks
openssl x509 -in /etc/letsencrypt/live/web-desktop-ip/fullchain.pem -noout -dates -ext subjectAltName
journalctl -u web-desktop-renew.service --since today
```

安装认证文件时应使用 `root` 所有、Nginx worker 所在组可读的 `0640` 权限，
不能直接复制本机的 `0600` 权限导致 Nginx worker 无法读取。
Nginx 配置中 `/websockify` 与静态资源继承同一认证；只有指定来源可以升级 WebSocket。
本机密码哈希文件位于 `~/.local/state/web-desktop/cloud-bundle/web-desktop.htpasswd`。

## 验收要求

| 检查 | 通过条件 |
| --- | --- |
| 本地 Nginx 模板演练 | 无认证/错误认证返回 401；错误 WS 来源返回 403；正确认证收到 RFB |
| 公网可信 HTTPS | 正常验证证书，无忽略证书参数 |
| 网页认证 | `/`、`/vnc.html` 和资源未认证均为 401 |
| WebSocket 认证 | 未认证/错误认证为 401；正确认证升级为 101 |
| 回源端口隔离 | 外网不能连接 18097，本机 5900/6080 不向外监听 |
| 证书续期 | dry-run 和重载 hook 成功，timer 可见 |
| 原 FRP 服务 | 已有代理访问与部署前一致 |
| Model Y 停车验收 | 点击、右键、滚动、中文、组合键、缩放、锁屏恢复、断线恢复、30 分钟使用 |

本机 Nginx 演练使用仅在回环地址有效的临时自签名证书，客户端显式信任该测试证书。
它只验证配置逻辑，不能代替公网证书、FRP链路或实车验收：

```bash
PYTHONNOUSERSITE=1 python scripts/render_cloud.py --public-ip 203.0.113.10
PYTHONNOUSERSITE=1 python scripts/verify_gateway.py --nginx /path/to/nginx
```

## 回退顺序

1. 移除或禁用本次桌面 FRP 代理，保留原代理，回归原有业务。
2. 停用 18443 的独立 Nginx 配置，`nginx -t` 成功后重载。
3. 停止并禁用 `web-desktop-renew.timer`，移除本次续期 hook。
   验证路径若被其他证书使用则保留。
4. 代理监听消失后，移除本次 18097 专用防火墙规则和 18443 安全组放行。
5. 按需停止本机两个 `web-desktop-*` 用户服务。

## 官方资料

- noVNC：<https://github.com/novnc/noVNC>
- TigerVNC 1.16.2：<https://github.com/TigerVNC/tigervnc/releases/tag/v1.16.2>
- Certbot 参数：<https://eff-certbot.readthedocs.io/en/stable/using.html>
- IP 证书：<https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html>
- FRP TLS：<https://gofrp.org/zh-cn/docs/features/common/network/network-tls/>
