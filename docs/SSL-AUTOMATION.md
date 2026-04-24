---
title: "EPUB Factory SSL 证书自动化指南"
date: 2026-04-21
tags: ["运维", "SSL", "HTTPS", "自动化", "Nginx", "Caddy"]
status: "active"
---

# EPUB Factory SSL 证书自动化指南

## 一、 背景与痛点

目前国内云厂商（如腾讯云、阿里云）提供的免费 SSL 证书有效期已全部缩短至 **3 个月（90天）**。如果依赖云控制台手动申请、下载、上传至服务器并重启 Web 服务器（如 Nginx），将带来极高的运维成本，且极易因遗忘导致网站 HTTPS 过期阻断访问。

遵循“工程自动化与免维护”的原则，我们必须摒弃云厂商的手动免费证书，转向**完全自动化的 SSL 签发与续期方案**。

---

## 二、 方案 A：`acme.sh` + DNSPod API 自动续期（推荐现有 Nginx 架构使用）

此方案使用 Let's Encrypt 签发证书，通过腾讯云（DNSPod）的 API 自动完成域名归属权 DNS 验证。证书到期前（通常是 60 天），脚本会自动续签并无缝重载 Nginx，实现真正的“一劳永逸”。

### 1. 获取腾讯云 API 密钥
1. 登录腾讯云控制台，进入 **API 密钥管理（CAM）**。
2. 新建并获取一对 `SecretId` 和 `SecretKey`。
3. （可选且推荐）为了安全，建议创建一个子账号，仅赋予 `QcloudDNSPodFullAccess`（DNSPod 完全访问权限），并使用该子账号的 API 密钥。

### 2. 安装 `acme.sh`
在服务器终端执行以下命令（将邮箱替换为你的管理员邮箱，用于接收极少情况下的证书过期预警）：

```bash
curl https://get.acme.sh | sh -s email=your-email@example.com
```

安装完成后，建议重新加载一下 bash 环境变量（或重新连接 SSH）：
```bash
source ~/.bashrc
```

### 3. 配置环境变量并申请证书
将获取到的腾讯云 API 密钥导入环境变量（`acme.sh` 会自动将它们保存到配置文件中供后续自动续期使用）：

```bash
export Tencent_SecretId="你的SecretId"
export Tencent_SecretKey="你的SecretKey"
```

使用 DNS 方式申请证书（以 `fixepub.com` 为例，支持泛域名）：
```bash
acme.sh --issue --dns dns_tencent -d fixepub.com -d www.fixepub.com
```

*说明：此过程大约需要等待 2 分钟，等待 DNS 记录生效并验证通过。*

### 4. 安装证书并配置 Nginx 自动重载
申请成功后，千万**不要**直接把 Nginx 配置指向 `~/.acme.sh/` 目录下的文件。必须使用 `--install-cert` 命令将证书安装到你的 Nginx 目录（例如 `/etc/nginx/ssl/`）。

首先确保目标目录存在：
```bash
mkdir -p /etc/nginx/ssl/
```

执行安装并设置自动 reload 钩子：
```bash
acme.sh --install-cert -d fixepub.com \
--key-file       /etc/nginx/ssl/fixepub.com.key  \
--fullchain-file /etc/nginx/ssl/fixepub.com.pem \
--reloadcmd     "systemctl reload nginx"
```

### 5. Nginx 配置示例
修改你的 Nginx 配置文件（如 `/etc/nginx/sites-available/fixepub`），指向刚刚安装的证书路径：

```nginx
server {
    listen 443 ssl http2;
    server_name fixepub.com www.fixepub.com;

    ssl_certificate /etc/nginx/ssl/fixepub.com.pem;
    ssl_certificate_key /etc/nginx/ssl/fixepub.com.key;
    
    # 推荐的 SSL 安全配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # ...你的前端和代理配置...
}
```

### 6. 验证自动续期
`acme.sh` 安装时已经自动在系统中添加了 Cron 定时任务。你可以通过以下命令查看：
```bash
crontab -l
```
它会在每天凌晨自动检查证书有效期，并在距离过期还有 30 天左右时自动执行申请、安装并执行 `--reloadcmd` 重启 Nginx。**后续无需任何人工干预。**

---

## 三、 方案 B：使用 Caddy 替代 Nginx（最极客、零配置）

如果你准备部署新的海外节点，或者觉得 Nginx 配置过于繁琐，强烈建议使用 **Caddy** 作为现代化的 Web 网关。

**核心优势**：Caddy 原生内置了完全自动化的 HTTPS 申请与续期能力。**你不需要装任何脚本，不需要配 API 密钥，甚至不需要指定证书路径。**

### 1. 安装 Caddy
参考 [Caddy 官方文档](https://caddyserver.com/docs/install#debian-ubuntu) 安装（以 Ubuntu/Debian 为例）：
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

### 2. 编写 Caddyfile
编辑 `/etc/caddy/Caddyfile`，清空默认内容，写入以下极简配置：

```caddyfile
# 你的域名，Caddy 会自动为它们申请和续期 HTTPS 证书
fixepub.com, www.fixepub.com {
    # 1. 托管前端静态页面（假设存放在 /var/www/epub-factory/frontend）
    root * /var/www/epub-factory/frontend
    file_server

    # 2. 将 /api 开头的请求反向代理到后端 FastAPI
    handle /api/* {
        reverse_proxy localhost:8000
    }
    
    # 3. (可选) 开启 gzip/zstd 压缩
    encode gzip zstd
}
```

### 3. 启动并生效
```bash
sudo systemctl restart caddy
```
Caddy 启动时，会自动向 Let's Encrypt 或 ZeroSSL 申请证书（只要你的域名已经解析到这台服务器的 IP），并自动配置好完美的 HTTPS/HTTP2 设置。

---

## 四、 总结与选型建议

1. **如果你已经在现有服务器上稳定运行了 Nginx**，为了不影响线上业务，请直接花 15 分钟实施 **方案 A (`acme.sh`)**。
2. **如果你在配置全新的出海节点（如 AWS EC2 等）**，强烈推荐尝试 **方案 B (Caddy)**。它的极致简单和内置的现代化特性非常适合小型团队和独立开发者。
3. **避坑指南**：坚决不要购买云厂商控制台中“SSL自动续期”的付费增值服务，这属于不必要的运维税。
