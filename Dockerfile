# 与生产对齐的后端镜像（本地开发 + CI 一致性用；生产仍走 systemd 部署）
# - Python 3.10.12：与生产同解释器
# - requirements.lock：与生产同依赖快照
# - epubcheck + JRE：从官方 release 安装，和代码/git 解耦，保证本地/CI 镜像一致
# - calibre：体积大，默认不装；需要 mobi/azw3 等格式转换时 --build-arg INSTALL_CALIBRE=1
FROM python:3.10.12-slim

ARG INSTALL_CALIBRE=0
ARG EPUBCHECK_VERSION=5.1.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend \
    EPUBCHECK_JAR=/opt/epubcheck-${EPUBCHECK_VERSION}/epubcheck.jar

# 系统依赖：JRE(epubcheck)、构建工具(pycrypto/lxml 编译)、curl/unzip(取 epubcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless \
        build-essential \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# 安装 epubcheck（与生产同版本 5.1.0）
RUN curl -fsSL -o /tmp/epubcheck.zip \
        "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" \
    && unzip -q /tmp/epubcheck.zip -d /opt \
    && rm /tmp/epubcheck.zip

# 可选：calibre（ebook-convert，用于非 EPUB 输入格式转换）
RUN if [ "$INSTALL_CALIBRE" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends calibre \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# 先装依赖（利用层缓存：lock 不变则不重装）
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --upgrade pip && pip install -r /app/backend/requirements.lock

# 再拷代码
COPY . /app

WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
