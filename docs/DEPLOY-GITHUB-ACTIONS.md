# AWS SSM + GitHub Actions 自动化部署指南

本文档记录了 EPUB Factory 如何绕过 SSH 干扰，利用 AWS Systems Manager (SSM) 和 GitHub Actions 实现一键/全自动部署的方案。

---

## 1. 核心架构
由于直连 AWS 22 端口 (SSH) 经常受到网络干扰，我们采用了以下链路：
- **代码托管**：GitHub
- **持续集成**：GitHub Actions
- **中转存储**：AWS S3 (用于存放构建包)
- **指令通道**：AWS SSM (用于远程指挥服务器更新，走 HTTPS 443 端口)

---

## 2. 自动化部署流程 (GitHub Actions)

每次执行 `git push origin main` 时，会自动触发以下流程：

1. **打包**：GitHub 运行器将项目打包为 `epub-factory.zip`。
2. **上传**：将 zip 包上传到指定的 S3 存储桶 (`epub-factory-deploy-326709068290`)。
3. **下令**：通过 AWS SSM 向 EC2 实例 (`i-0bc1b7632e9cda9b4`) 发送部署指令。
4. **执行** (服务器端)：
   - 从 S3 下载最新的 zip 包。
   - 解压并更新文件。
   - 安装 Python 依赖 (`pip install`)。
   - 重启 systemd 服务 (`epub-factory.service`)。
5. **健康检查**：自动访问 `https://fixepub.com` 确保服务在线。

### 需要维护的 GitHub Secrets:
在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中配置：
- `AWS_ACCESS_KEY_ID`: 你的 AWS 访问密钥 ID。
- `AWS_SECRET_ACCESS_KEY`: 你的 AWS 私密访问密钥。

---

## 3. 手动一键部署 (本地脚本)

如果你想在本地不通过 GitHub 直接部署，可以使用脚本：

```bash
chmod +x deploy-ssm.sh
./deploy-ssm.sh
```
该脚本逻辑与 GitHub Actions 一致，不依赖 SSH 22 端口。

---

## 4. 交互式运维 (AWS Session Manager)

如果你需要进入服务器终端手动操作（替代 `ssh` 命令），**不要使用 `ssh fixepub`**，请使用以下方式：

### 安装插件 (仅需一次)
```bash
brew install --cask session-manager-plugin
```

### 连接服务器
```bash
aws ssm start-session --target i-0bc1b7632e9cda9b4 --region ap-southeast-1
```
*注：这种方式走的是 443 端口，非常稳定且不需要 SSH 密钥。*

---

## 5. 服务器关键信息备忘
- **实例 ID**: `i-0bc1b7632e9cda9b4`
- **公网 IP**: `54.169.182.124`
- **域名**: `https://fixepub.com`
- **项目路径**: `/home/ubuntu/epub-factory`
- **Systemd 服务名**: `epub-factory.service`
- **查看实时日志**: `aws ssm start-session` 连入后执行 `sudo journalctl -u epub-factory -f`

---

## 6. 常见故障排查
- **部署超时**：检查服务器 SSM Agent 是否在线 (`aws ssm describe-instance-information`)。
- **404 错误**：如果通过 IP 访问返回 404 是正常的，因为 Nginx 配置了域名校验。请通过 `https://fixepub.com` 访问。
- **权限问题**：如果 S3 下载失败，检查 EC2 的 IAM 角色 `fixepub-ssm-role` 是否包含 `AmazonS3ReadOnlyAccess` 策略。
