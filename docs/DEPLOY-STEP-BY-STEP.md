# fixepub.com 一步步部署到 AWS

按顺序做，每一步做完再做下一步。主站域名：**fixepub.com**（腾讯云）。  
**本机环境**：Mac；密钥路径 `fixepub-key.pem` 放在项目目录下（见第二步免密连接）。

---

## 第一步：登录 AWS 并开一台 EC2

1. 打开 [aws.amazon.com](https://aws.amazon.com)，登录你的账号（没有就注册）。
2. 右上角选一个**区域**（例如 **新加坡 ap-southeast-1** 或 **香港 ap-east-1**），后续资源都在这区。
3. 在搜索框输入 **EC2**，进入 **EC2 控制台**。
4. 点 **启动实例 (Launch instance)**：
   - **名称**：填 `fixepub` 或任意。
   - **镜像 (AMI)**：选 **Ubuntu Server 22.04 LTS**。
   - **实例类型**：选 **t3.small**（2 vCPU，2 GiB 内存）或 **t2.small**（2 GiB 内存）。
   - **密钥对**：点「创建新密钥对」，名称填 `fixepub-key`，类型 RSA，格式 `.pem`，下载后保存到 Mac 项目目录（例如 `.../epub-factory/fixepub-key.pem`），**弄丢无法再下载**。
   - **网络设置**：点「编辑」，**创建安全组**，安全组名填 `fixepub-sg`；**允许 SSH** 来源选「我的 IP」；点「添加安全组规则」：类型 **HTTP**，来源 **0.0.0.0/0**；再添加一条：类型 **HTTPS**，来源 **0.0.0.0/0**。
   - **存储**：默认 8 GiB 即可，建议改成 **20 GiB**。
5. 点 **启动实例**。等状态变成「运行中」。
6. 在实例列表里点该实例，在下方 **详情** 里记下 **公有 IPv4 地址**（例如 `54.xxx.xxx.xxx`），后面叫「服务器 IP」。

---

## 第二步：用 SSH 连上服务器（Mac 免密一键连接）

**密钥路径**（本机 Mac）：`/Users/zengchanghuan/Desktop/workspace/epub-factory/fixepub-key.pem`

### 2.1 首次：给密钥设权限

在 Mac 终端执行一次：

```bash
chmod 400 /Users/zengchanghuan/Desktop/workspace/epub-factory/fixepub-key.pem
```

### 2.2 配置 SSH 免密一键连接（推荐）

在 Mac 上编辑 SSH 配置（没有就新建）：

```bash
mkdir -p ~/.ssh
nano ~/.ssh/config
```

在文件末尾追加（把 `你的服务器IP` 换成第一步记下的 EC2 公网 IP，例如 `54.123.45.67`）：

```
Host fixepub
    HostName 你的服务器IP
    User ubuntu
    IdentityFile /Users/zengchanghuan/Desktop/workspace/epub-factory/fixepub-key.pem
```

保存：`Ctrl+O` 回车，`Ctrl+X` 退出。

以后在终端**一键连接**，只需输入：

```bash
ssh fixepub
```

第一次会问 `Are you sure you want to continue connecting?` 输入 `yes` 回车。看到 `ubuntu@ip-xxx:~$` 即表示已连上。

### 2.3 若不想改 config，每次手动连

```bash
ssh -i /Users/zengchanghuan/Desktop/workspace/epub-factory/fixepub-key.pem ubuntu@你的服务器IP
```

---

## 第三步：在服务器上安装 Python、Git 和项目依赖

在 SSH 里**一条条**执行（复制整段也可以）：

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git nginx
```

然后克隆你的项目（把 `你的仓库地址` 换成真实地址，例如 `https://github.com/你的用户名/epub-factory.git`）：

```bash
cd ~
git clone 你的仓库地址 epub-factory
cd epub-factory/backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

没有 Git 仓库的话，可以本机先打包再上传（Mac 上在项目根目录执行）：

- `zip -r epub-factory.zip . -x "*.git*" -x "*__pycache__*" -x "*.venv*" -x "*.env"`，再用：`scp -i /Users/zengchanghuan/Desktop/workspace/epub-factory/fixepub-key.pem epub-factory.zip ubuntu@你的服务器IP:~`（或配置好 SSH 后：`scp epub-factory.zip fixepub:~`）
- 在服务器上：`cd ~ && unzip epub-factory.zip -d epub-factory && cd epub-factory/backend && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`

---

## 第四步：在服务器上创建 .env

在 SSH 里执行：

```bash
cd ~/epub-factory/backend
nano .env
```

在打开的编辑器里写入（按你实际情况改）：

```env
OPENAI_API_KEY=sk-你的DeepSeek或OpenAI的key
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
DATABASE_URL=sqlite:///./epub_jobs.db
```

保存并退出：`Ctrl+O` 回车，再 `Ctrl+X`。

收紧权限：

```bash
chmod 600 .env
```

---

## 第五步：先前台跑一次，确认能访问

在 SSH 里执行：

```bash
cd ~/epub-factory/backend
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

不要关这个窗口。在你**本机浏览器**打开：`http://你的服务器IP:8000`，能看到 EPUB Factory 页面就对了。

确认无误后，在跑 uvicorn 的那个 SSH 窗口按 `Ctrl+C` 停掉。

---

## 第六步：用 Nginx 反代，只开放 80 端口

1. 在服务器上新建 Nginx 配置：
   ```bash
   sudo nano /etc/nginx/sites-available/fixepub
   ```
2. 写入下面内容（`你的服务器IP` 先不用改，后面绑域名再改；若暂时用 IP 访问，用 `server_name _;` 或 `server_name 你的服务器IP;`）：
   ```nginx
   server {
       listen 80;
       server_name fixepub.com www.fixepub.com;
       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```
   若暂时还没有域名，把 `server_name fixepub.com www.fixepub.com;` 改成 `server_name _;`。
3. 保存退出后执行：
   ```bash
   sudo ln -sf /etc/nginx/sites-available/fixepub /etc/nginx/sites-enabled/
   sudo nginx -t
   sudo systemctl reload nginx
   ```
4. **重要**：到 AWS EC2 控制台 → 你的实例 → 安全组 → 入站规则里**删掉**对 8000 端口的开放（如果有），只保留 22、80、443。

---

## 第七步：用 systemd 让 uvicorn 开机自启

1. 创建服务文件：
   ```bash
   sudo nano /etc/systemd/system/epub-factory.service
   ```
2. 写入（路径假设是 `/home/ubuntu/epub-factory`，若你用的不是 `ubuntu` 用户请改）：
   ```ini
   [Unit]
   Description=EPUB Factory (fixepub.com)
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/epub-factory/backend
   EnvironmentFile=/home/ubuntu/epub-factory/backend/.env
   ExecStart=/home/ubuntu/epub-factory/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
3. 保存退出后执行：
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable epub-factory
   sudo systemctl start epub-factory
   sudo systemctl status epub-factory
   ```
   看到 `active (running)` 即可。以后重启服务器也会自动拉起。

---

## 第八步：用 IP 访问一次

在本机浏览器打开：`http://你的服务器IP`（注意是 80 端口，不要加 `:8000`）。  
应能看到和第五步一样的页面，说明 Nginx 反代和 systemd 都正常。

---

## 第九步：把 fixepub.com 解析到这台服务器（腾讯云 DNS）

1. 登录 [腾讯云控制台](https://console.cloud.tencent.com)，进入 **域名与网站 → DNS 解析 DNSPod**（或你购买 fixepub.com 时用的解析服务）。
2. 找到 **fixepub.com**，点「解析」。
3. 添加两条记录：
   - **主机记录** `@`，**记录类型** A，**记录值** 填你的 **EC2 公有 IP**，TTL 600。
   - **主机记录** `www`，**记录类型** A，**记录值** 填同一 **EC2 公有 IP**，TTL 600。
4. 保存。等几分钟到几十分钟生效后，在浏览器访问 `http://fixepub.com` 和 `http://www.fixepub.com`，应都能打开站点。

---

## 第十步：给 fixepub.com 上 HTTPS（可选但推荐）

1. 在服务器上安装 certbot：
   ```bash
   sudo apt install -y certbot python3-certbot-nginx
   ```
2. 执行（把邮箱换成你的）：
   ```bash
   sudo certbot --nginx -d fixepub.com -d www.fixepub.com --email 你的邮箱 --agree-tos --no-eff-email
   ```
3. 按提示选即可。完成后用 `https://fixepub.com` 访问。证书到期前 certbot 会自动续期。

---

## 以后更新代码怎么部署

Mac 上先一键连上服务器：`ssh fixepub`，然后执行：

```bash
cd ~/epub-factory
git pull
cd backend
.venv/bin/pip install -r requirements.txt
sudo systemctl restart epub-factory
```

若没用 Git，就本机重新打包上传（可用 `scp epub-factory.zip fixepub:~`），在服务器上解压覆盖后再执行上面最后两行。

---

## 常见问题

- **SSH 连不上**：检查安全组是否开放 22；检查本机 IP 是否变了（若安全组只允许「我的 IP」）。
- **8000 能访问、80 不能**：检查 Nginx 是否 `sudo systemctl status nginx` 为 active；检查安全组是否开放 80。
- **域名打不开**：等 DNS 生效（最多 48 小时，通常几分钟）；用 `ping fixepub.com` 看是否已是你的 EC2 IP。
- **翻译失败**：检查 `.env` 里 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 是否正确；看日志 `sudo journalctl -u epub-factory -f`。
