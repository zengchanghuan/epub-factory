# 线上部署

生产主站 `fixepub.com` 部署在腾讯云服务器上。以后发布线上服务时，统一使用项目根目录的 `deploy.sh`。

```bash
bash deploy.sh
```

该脚本会完成以下步骤：

- 打包当前项目代码，排除 `.env`、密钥、数据库、上传文件、输出文件和 EPUB 测试文件等本地/敏感资产。
- 通过 SSH 主机别名 `fixepub` 上传到线上服务器。
- 在服务器上更新后端依赖。
- 重启 `epub-factory`、`epub-factory-worker`、`epub-factory-beat` 服务。

部署前请确认本机 SSH 配置中存在 `fixepub` 主机别名，并指向当前腾讯云生产服务器。当前脚本使用的密钥文件是项目根目录的 `fix_epub.pem`。

部署完成后，用以下接口验证线上服务：

```bash
curl -I https://fixepub.com/
curl https://fixepub.com/healthz
curl -I https://fixepub.com/api/v2/jobs
```

不要使用已废弃的 AWS/SSM 部署流程；仓库中应只保留 `deploy.sh` 作为线上发布入口。
