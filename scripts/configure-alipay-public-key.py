#!/usr/bin/env python3
"""Validate or replace only the Alipay verification public key; never charge."""
from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
import warnings

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values, set_key


class ConfigurationError(ValueError):
    """Fixed safe messages; user input and SDK exception strings stay private."""


def public_key(value: str):
    raw = value.strip().replace("\\n", "\n").encode()
    if b"PRIVATE KEY" in raw:
        raise ConfigurationError("需要支付宝公钥，不能使用私钥。")
    try:
        key = (serialization.load_pem_public_key(raw) if b"-----BEGIN" in raw
               else serialization.load_der_public_key(base64.b64decode(re.sub(rb"\s", b"", raw), validate=True)))
    except Exception:
        raise ConfigurationError("不是有效的支付宝 RSA 公钥；请从对应应用获取支付宝公钥，不要填应用私钥或证书。") from None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ConfigurationError("需要至少 2048 位的 RSA 公钥。")
    return key


def validate_key(value: str, existing: dict) -> str:
    key = public_key(value)
    raw = (existing.get("ALIPAY_PRIVATE_KEY") or "").strip().replace("\\n", "\n").encode()
    if raw:
        try:
            private = (serialization.load_pem_private_key(raw, password=None) if b"-----BEGIN" in raw
                       else serialization.load_der_private_key(base64.b64decode(re.sub(rb"\s", b"", raw), validate=True), password=None))
        except Exception:
            raise ConfigurationError("应用私钥格式无效，无法核对公钥身份；原配置未修改。") from None
        if key.public_numbers() == private.public_key().public_numbers():
            raise ConfigurationError("这是应用公钥，需要填写支付宝公钥；原配置未修改。")
    return base64.b64encode(key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def save_key(path: Path, value: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("配置文件不存在或是符号链接，未修改。")
    project = path.parent.parent if path.parent.name == "backend" else path.parent
    with (project / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = path.read_bytes()
        before = dotenv_values(path, interpolate=False)
        normalized = validate_key(value, before)
        fd, temporary_name = tempfile.mkstemp(prefix=".alipay-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(original)
            set_key(str(temporary), "ALIPAY_PUBLIC_KEY", normalized)
            temporary.chmod(0o600)
            after = dotenv_values(temporary, interpolate=False)
            if (after.get("ALIPAY_PUBLIC_KEY") != normalized or any(
                    after.get(k) != v for k, v in before.items() if k != "ALIPAY_PUBLIC_KEY")):
                raise ConfigurationError("配置保存校验失败，原文件未修改。")
            if path.read_bytes() != original:
                raise ConfigurationError("配置已被其他进程修改，请重新运行。")
            backups = path.parent / ".config-backups"
            backups.mkdir(mode=0o700, exist_ok=True)
            if backups.is_symlink():
                raise ConfigurationError("配置备份目录不能是符号链接。")
            backups.chmod(0o700)
            backup = backups / ("before-alipay-" + uuid.uuid4().hex + ".env")
            with backup.open("xb") as stream:
                backup.chmod(0o600)
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全配置支付宝公钥，不下单、不扣款、不重启。")
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / "backend/.env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="仅检查现有配置格式，不联网、不修改")
    mode.add_argument("--public-key-file", type=Path, help="读取支付宝公钥文件，不在命令行传入密钥正文")
    args = parser.parse_args(argv)
    try:
        if args.check:
            if not args.env.is_file():
                raise ConfigurationError("配置文件不存在。")
            existing = dotenv_values(args.env, interpolate=False)
            validate_key(existing.get("ALIPAY_PUBLIC_KEY") or "", existing)
            print("支付宝公钥格式检查通过；尚未联网验证其与当前应用的对应关系。")
        else:
            if args.public_key_file:
                value = args.public_key_file.read_text()
            else:
                if not sys.stdin.isatty():
                    raise ConfigurationError("请在交互终端输入支付宝公钥，或使用 --public-key-file。")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass("粘贴支付宝公钥（单行，不显示输入）：")
            save_key(args.env.absolute(), value)
            print("支付宝公钥已安全保存（0600），原配置已私密备份；未输出密钥、未联网或重启。")
            print("应用重新加载后生效；仍需验证查单验签，格式通过不代表真实收款验收。")
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
    except BlockingIOError:
        print("发布或配置操作正在进行，请稍后重试。", file=sys.stderr)
    except (OSError, getpass.GetPassWarning):
        print("无法安全读取或保存配置；请检查文件权限和交互终端。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
