#!/usr/bin/env python3
"""Set a dedicated administrator password via hidden terminal input; never print it."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import secrets
import tempfile


def main():
    parser = argparse.ArgumentParser(description="设置网站独立管理员密码（隐藏输入）")
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / "backend/.env")
    parser.add_argument("--username", default="tristan")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", args.username):
        raise SystemExit("账号只允许 3–64 位字母、数字、点、下划线和连字符")
    if not os.isatty(0):
        raise SystemExit("请在交互终端运行，密码不会显示或写入命令历史")
    password = getpass.getpass("管理员密码（至少 6 位，无需特殊符号）: ")
    if len(password) < 6 or len(password) > 512:
        raise SystemExit("密码长度须为 6–512 位")
    if password != getpass.getpass("再次输入密码: "):
        raise SystemExit("两次密码不一致，未修改配置")
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    encoded = f"pbkdf2_sha256$600000${salt}${value}"
    path = args.env.resolve()
    if not path.is_file():
        raise SystemExit("找不到已有 .env 文件；请先完成部署环境配置")
    lines = path.read_text().splitlines()
    lines = [line for line in lines if not re.match(r"^\s*(?:export\s+)?ADMIN_(USERNAME|PASSWORD_HASH)\s*=", line)]
    lines.extend([f"ADMIN_USERNAME={args.username}", f"ADMIN_PASSWORD_HASH='{encoded}'"])
    fd, tmp = tempfile.mkstemp(prefix=".admin-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"管理员 {args.username} 已设置，密码未输出。请重启 API 服务使新配置生效。")


if __name__ == "__main__":
    main()
