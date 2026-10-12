# 公司资料静态页面部署记录

> **历史归档（2026-10-12 整理）**：下文保留记录日期当时的实现、配置、测试和发布状态；“当前”“已上线”“未提交”等均指当时，不代表今天的状态。本次仅整理留存资料，未重新执行历史测试、生产操作或外部调用。历史命令不是当前操作指引；阅读入口见[历史记录索引](HISTORICAL-RECORDS.md)，最近已验证的生产发布见[2026-10-12 发布记录](PRODUCTION-RELEASE-2026-10-12-MANUAL-REVIEW.md)。

2026-10-10 16:21（北京时间），按用户授权更新 fixepub.com 四个静态页面：index.html、about.html、contact.html、contact-en.html。

首页公司业务简介、深圳实际办公地址和联系电话集中放在最底部页脚；新增公司介绍使用“双语对照服务”，去掉“生成”措辞。地址及电话由用户确认准确且可以公开。

工作区包含其他未提交改动，因此以生产页面快照为基准应用本次文案修改，未执行全量 deploy.sh 或 push.sh，未提交或推送 Git。线上原有业务脚本、首页其他内容和 about.html 原有算法声明保留，后端服务未重启。

部署时持有 /home/ubuntu/epub-factory/.deploy.lock，校验四个原文件 SHA-256 后备份，再逐文件原子替换；备份位置：/home/ubuntu/epub-factory/backups/company-info-20261010T082108Z。备份含原始页面及部署前后哈希 manifest.json，可用于恢复。

公网四个页面 HTTP 200 且 SHA-256 与部署包一致；HTML Cache-Control 为 no-cache, must-revalidate；/api/healthz 返回 status=ok。已通过浏览器确认线上首页页脚显示公司简介、实际办公地址和联系电话。

本次完成网站修改上线，未重新提交备案申请。
