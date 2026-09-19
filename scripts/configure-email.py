#!/usr/bin/env python3
"""Configure customer-result and owner-payment email without exposing secrets.

Default action only writes the selected .env. Network access requires --check or
--test-to; neither network action modifies the file.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from email.message import EmailMessage
import getpass
import ipaddress
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import sys
import tempfile
from urllib.parse import urlsplit
import warnings


DEFAULT_ENV = Path(__file__).resolve().parents[1] / "backend/.env"
TRUE = {"1", "true", "yes"}
FALSE = {"0", "false", "no"}
DEFAULT_OWNER_EMAIL = "249998620@qq.com"


class ConfigurationError(ValueError):
    """Safe, fixed error messages only: never include input or credentials."""


def parse_env(text: str):
    """Read quoted/unquoted dotenv assignments without expanding variables.

    Keep source spans so unrelated assignments, comments and multiline values
    can be copied verbatim. This script needs only the Python standard library.
    """
    lines = text.splitlines(keepends=True)
    result, entries = {}, []
    index, offset = 0, 0
    while index < len(lines):
        start = offset
        line = lines[index]
        offset += len(line)
        index += 1
        match = re.match(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$", line, re.S)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).lstrip(" \t")
        if raw.startswith(("'", '"')):
            quote = raw[0]
            position = 1
            while True:
                while position < len(raw):
                    if raw[position] == "\\":
                        position += 2
                        continue
                    if raw[position] == quote:
                        break
                    position += 1
                if position < len(raw):
                    break
                if index >= len(lines):
                    raise ConfigurationError("已有配置含未闭合引号，未修改文件；请先检查 dotenv 格式。")
                raw += lines[index]
                offset += len(lines[index])
                index += 1
            remainder = raw[position + 1:].strip()
            if remainder and not remainder.startswith("#"):
                raise ConfigurationError("已有配置引号后含无效内容，未修改文件；请先检查 dotenv 格式。")
            value = raw[1:position]
            escapes = {"\\": "\\", quote: quote}
            if quote == '"':
                escapes.update(n="\n", r="\r", t="\t", b="\b", f="\f", v="\v", a="\a")
            value = re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), value)
        else:
            value = re.split(r"[ \t]+#", raw, maxsplit=1)[0].strip()
        result[key] = value
        entries.append((start, offset, key))
    return result, entries


def replace_env_values(original: str, desired: dict[str, str]) -> str:
    _, entries = parse_env(original)
    pieces, written, cursor = [], set(), 0

    def assignment(key):
        escaped = desired[key].replace("\\", "\\\\").replace("'", "\\'")
        return key + "='" + escaped + "'\n"

    for start, end, key in entries:
        if key not in desired:
            continue
        pieces.append(original[cursor:start])
        pieces.append(assignment(key))
        written.add(key)
        cursor = end
    pieces.append(original[cursor:])
    combined = "".join(pieces)
    if combined and not combined.endswith("\n"):
        combined += "\n"
    combined += "".join(assignment(key) for key in desired if key not in written)
    parsed, _ = parse_env(combined)
    before, _ = parse_env(original)
    if any(parsed.get(key) != value for key, value in desired.items()) or any(
            parsed.get(key) != value for key, value in before.items() if key not in desired):
        raise ConfigurationError("配置无法安全写入 dotenv 格式，原文件未修改。")
    return combined


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    security: str
    user: str = field(repr=False)
    password: str = field(repr=False)
    sender: str = field(repr=False)
    base_url: str
    enabled: bool
    timeout: int = 15
    owner_enabled: bool = True
    owner_to: str = field(default=DEFAULT_OWNER_EMAIL, repr=False)

    def env_values(self) -> dict[str, str]:
        return {
            "NOTIFY_EMAIL_ENABLED": "1" if self.enabled else "0",
            "OWNER_PAYMENT_EMAIL_ENABLED": "1" if self.owner_enabled else "0",
            "OWNER_PAYMENT_EMAIL_TO": self.owner_to,
            "SMTP_HOST": self.host,
            "SMTP_PORT": str(self.port),
            "SMTP_SECURITY": self.security,
            "SMTP_USER": self.user,
            "SMTP_PASSWORD": self.password,
            "SMTP_FROM": self.sender,
            "SITE_BASE_URL": self.base_url,
            "SMTP_TIMEOUT_SECONDS": str(self.timeout),
        }


def email_address(value: str) -> str:
    value = value.strip()
    if (len(value) > 254 or not value.isascii()
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
            or ".." in value or value.startswith(".") or ".@" in value
            or len(value.rsplit("@", 1)[0]) > 64):
        raise ConfigurationError("邮箱格式无效；请填写普通邮箱地址，不包含显示名称或换行。")
    return value


def public_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ConfigurationError("网站地址必须是有效的公网 HTTPS 地址。") from None
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or port == 0
            or not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value)
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise ConfigurationError("网站地址必须是公网 HTTPS 站点根地址，不含账号、查询参数或片段。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or ".." in host:
            raise ConfigurationError("网站地址必须使用有效的公网域名。") from None
    else:
        if not address.is_global:
            raise ConfigurationError("网站地址不能使用本机或私有网络地址。")
    return value.rstrip("/")


def validated(values: dict[str, str | None]) -> Settings:
    def required(key: str) -> str:
        value = str(values.get(key) or "").strip()
        if not value:
            raise ConfigurationError(f"缺少 {key}；请显式设置或使用交互配置。")
        return value

    host = required("SMTP_HOST")
    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        raise ConfigurationError("SMTP_HOST 只接受主机名或 IP 地址，不包含协议和端口。")
    security = required("SMTP_SECURITY").lower()
    if security not in {"ssl", "starttls"}:
        raise ConfigurationError("SMTP_SECURITY 只能为 ssl 或 starttls，禁止明文连接。")
    try:
        port = int(required("SMTP_PORT"))
        timeout = int(values.get("SMTP_TIMEOUT_SECONDS") or "15")
    except ValueError:
        raise ConfigurationError("SMTP 端口和超时时间必须为整数。") from None
    if not 1 <= port <= 65535 or not 1 <= timeout <= 60:
        raise ConfigurationError("SMTP 端口须在 1–65535，超时时间须在 1–60 秒。")
    if (port == 465 and security != "ssl") or (port == 587 and security != "starttls"):
        raise ConfigurationError("端口 465 必须配合 ssl，端口 587 必须配合 starttls。")
    user = required("SMTP_USER")
    if len(user) > 320 or any(ord(c) < 32 for c in user):
        raise ConfigurationError("SMTP_USER 无效。")
    password = str(values.get("SMTP_PASSWORD") or "")
    if not password or len(password) > 4096 or any(ord(c) < 32 for c in password):
        raise ConfigurationError("SMTP 授权码不能为空或包含控制字符。")
    if password.endswith("\\"):
        raise ConfigurationError("授权码末尾不能为反斜杠；当前 dotenv 格式不能稳定保存该值。")
    # python-dotenv interpolates ${...} even inside single quotes. Refuse to
    # silently change a credential when the application loads its environment.
    if any("${" in value for value in (host, user, password)):
        raise ConfigurationError("SMTP 配置不能含有 ${ 插值表达式；请改用不含该表达式的授权码。")
    enabled = required("NOTIFY_EMAIL_ENABLED").lower()
    if enabled not in TRUE | FALSE:
        raise ConfigurationError("NOTIFY_EMAIL_ENABLED 必须为 1 或 0。")
    sender = email_address(required("SMTP_FROM"))
    if "${" in sender:
        raise ConfigurationError("发件邮箱不能含有 dotenv 插值表达式。")
    owner_enabled = required("OWNER_PAYMENT_EMAIL_ENABLED").lower()
    if owner_enabled not in TRUE | FALSE:
        raise ConfigurationError("OWNER_PAYMENT_EMAIL_ENABLED 必须为 1 或 0。")
    owner_to = email_address(required("OWNER_PAYMENT_EMAIL_TO"))
    if "${" in owner_to:
        raise ConfigurationError("商户收件邮箱不能含有 dotenv 插值表达式。")
    return Settings(host, port, security, user, password, sender,
                    public_base_url(required("SITE_BASE_URL")), enabled in TRUE, timeout,
                    owner_enabled in TRUE, owner_to)


def atomic_save(path: Path, config: Settings) -> None:
    """Preserve unrelated dotenv entries/comments; replace atomically as 0600."""
    if path.is_symlink():
        raise ConfigurationError("为避免替换符号链接，请用 --env 指定配置文件的真实路径。")
    if not path.parent.is_dir():
        raise ConfigurationError("配置目录不存在，请先创建 backend 配置目录。")
    previous_stat = path.stat() if path.exists() else None
    original = path.read_bytes().decode("utf-8") if path.exists() else ""
    content = replace_env_values(original, config.env_values()).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".email-env-", dir=path.parent)
    temporary = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if previous_stat and (previous_stat.st_uid != os.geteuid() or previous_stat.st_gid != os.getegid()):
            os.chown(temporary, previous_stat.st_uid, previous_stat.st_gid)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def contact_smtp(config: Settings, test_to: str | None = None) -> None:
    """Only called following an explicit --check or --test-to argument."""
    recipient = email_address(test_to) if test_to else None
    context = ssl.create_default_context()
    if config.security == "ssl":
        connection = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context)
    else:
        connection = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
    with connection as smtp:
        smtp.ehlo()
        if config.security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config.user, config.password)
        if recipient:
            message = EmailMessage()
            message["Subject"] = "FixEpub 邮件通道测试"
            message["From"] = config.sender
            message["To"] = recipient
            message.set_content("这是一封管理员主动发起的邮件通道测试信，不包含用户订单或书籍内容。\n")
            if smtp.send_message(message):
                raise ConfigurationError("SMTP 服务拒绝了测试收件人，邮件未被确认接受。")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全配置可选邮件通知；默认只保存配置，不连接或发信。")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help=".env 路径，默认工程 backend/.env")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="只验证配置并连接 SMTP 认证，不保存、不发信")
    action.add_argument("--test-to", metavar="EMAIL", help="显式向此邮箱发送一封测试信，不保存配置")
    parser.add_argument("--qq", action="store_true", help="采用 QQ SMTP SSL 465 设置，交互输入授权码")
    parser.add_argument("--address", help="--qq 使用的邮箱，默认本项目已选定的 QQ 客服邮箱")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--security", choices=("ssl", "starttls"))
    parser.add_argument("--user")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--base-url", help="邮件下载链接的公网 HTTPS 站点根地址")
    parser.add_argument("--timeout", type=int)
    enable = parser.add_mutually_exclusive_group()
    enable.add_argument("--enable", dest="enabled", action="store_true", default=None,
                        help="开启顾客任务结果邮件，不改变商户通知开关")
    enable.add_argument("--disable", dest="enabled", action="store_false",
                        help="关闭顾客任务结果邮件，不改变商户通知开关")
    parser.add_argument("--owner-to", metavar="EMAIL", help="商户支付通知收件邮箱；未指定则保留已有地址")
    owner_enable = parser.add_mutually_exclusive_group()
    owner_enable.add_argument("--enable-owner-notifications", dest="owner_enabled", action="store_true", default=None,
                              help="开启商户已验证支付通知，不改变顾客通知开关")
    owner_enable.add_argument("--disable-owner-notifications", dest="owner_enabled", action="store_false",
                              help="关闭商户支付通知，不改变顾客通知开关")
    parser.add_argument("--password-env", metavar="VARIABLE", help="从此进程环境变量读取授权码；不要把授权码写进命令参数")
    parser.add_argument("--non-interactive", action="store_true", help="只使用显式参数或已有配置，缺项则退出")
    return parser


def collect_settings(args: argparse.Namespace) -> Settings:
    values = parse_env(args.env.read_bytes().decode("utf-8"))[0] if args.env.is_file() else {}
    original_identity = (values.get("SMTP_HOST"), values.get("SMTP_USER"))
    values.setdefault("OWNER_PAYMENT_EMAIL_ENABLED", "1")
    values.setdefault("OWNER_PAYMENT_EMAIL_TO", DEFAULT_OWNER_EMAIL)
    if args.owner_to is not None:
        values["OWNER_PAYMENT_EMAIL_TO"] = args.owner_to
    if args.owner_enabled is not None:
        values["OWNER_PAYMENT_EMAIL_ENABLED"] = "1" if args.owner_enabled else "0"
    if args.address and not args.qq:
        raise ConfigurationError("--address 仅与 --qq 一起使用；通用服务请用 --user 和 --from。")
    if args.qq:
        address = email_address(args.address or "249998620@qq.com")
        if address.rsplit("@", 1)[1].lower() != "qq.com":
            raise ConfigurationError("QQ 预设只适用于 qq.com 地址；其他服务请显式设置 SMTP 参数。")
        values.update(SMTP_HOST="smtp.qq.com", SMTP_PORT="465", SMTP_SECURITY="ssl",
                      SMTP_USER=address, SMTP_FROM=address)
        values.setdefault("SITE_BASE_URL", "https://fixepub.com")
        values.setdefault("NOTIFY_EMAIL_ENABLED", "1")
    for attribute, key in (("host", "SMTP_HOST"), ("port", "SMTP_PORT"),
                           ("security", "SMTP_SECURITY"), ("user", "SMTP_USER"),
                           ("sender", "SMTP_FROM"), ("base_url", "SITE_BASE_URL"),
                           ("timeout", "SMTP_TIMEOUT_SECONDS")):
        value = getattr(args, attribute)
        if value is not None:
            values[key] = str(value)
    if args.enabled is not None:
        values["NOTIFY_EMAIL_ENABLED"] = "1" if args.enabled else "0"
    if not values.get("SITE_BASE_URL") and values.get("PUBLIC_BASE_URL"):
        values["SITE_BASE_URL"] = values["PUBLIC_BASE_URL"]
    if original_identity != (values.get("SMTP_HOST"), values.get("SMTP_USER")):
        values.pop("SMTP_PASSWORD", None)
    if args.password_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.password_env):
            raise ConfigurationError("授权码环境变量名称无效。")
        password = os.environ.get(args.password_env)
        if not password:
            raise ConfigurationError("指定的授权码环境变量未设置或为空。")
        values["SMTP_PASSWORD"] = password
    interactive = not args.non_interactive and not args.check and not args.test_to and sys.stdin.isatty()
    if interactive:
        for key, prompt in (("SMTP_HOST", "SMTP 主机"), ("SMTP_SECURITY", "安全模式 ssl 或 starttls"),
                            ("SMTP_PORT", "SMTP 端口"), ("SMTP_USER", "SMTP 用户名"),
                            ("SMTP_FROM", "发件邮箱"), ("SITE_BASE_URL", "公网 HTTPS 网站根地址"),
                            ("NOTIFY_EMAIL_ENABLED", "启用顾客任务结果邮件？填写 1 或 0")):
            if not values.get(key):
                values[key] = input(prompt + ": ").strip()
        if not args.password_env:
            suffix = "（留空保留已配置授权码）" if values.get("SMTP_PASSWORD") else ""
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    password = getpass.getpass("SMTP 授权码" + suffix + ": ")
            except getpass.GetPassWarning:
                raise ConfigurationError("此终端不能隐藏输入，已取消；请在普通交互终端重新执行。") from None
            if password:
                values["SMTP_PASSWORD"] = password
    return validated(values)


def main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        config = collect_settings(args)
        if args.check or args.test_to:
            contact_smtp(config, args.test_to)
            print("测试邮件已由 SMTP 服务接受，请到收件箱验证；未修改配置。" if args.test_to
                  else "SMTP TLS 连接与认证通过；未发送邮件，未修改配置。")
        else:
            atomic_save(args.env.absolute(), config)
            print("邮件配置已安全保存（权限 0600），授权码未输出；未连接 SMTP 或发送邮件。")
            print("配置将在应用重新加载后生效。请择无正在处理订单时按项目发布流程重启，随后可运行 --check。")
            if not config.enabled:
                print("顾客任务结果邮件当前关闭；启用时需显式加 --enable 后重新保存。")
            if not config.owner_enabled:
                print("商户支付邮件当前关闭；启用时需显式加 --enable-owner-notifications 后重新保存。")
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
    except smtplib.SMTPAuthenticationError:
        print("SMTP 认证失败，请检查授权码和邮箱 SMTP 开通状态；服务端返回内容已隐藏。", file=sys.stderr)
    except ssl.SSLError:
        print("SMTP TLS 验证失败，请检查主机名、端口和证书；连接未降级。", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("输入已取消，配置未修改。", file=sys.stderr)
    except (smtplib.SMTPException, socket.timeout, OSError, UnicodeError, ValueError):
        print("邮件配置或连接未完成，请检查本地文件权限、SMTP 参数和网络；错误细节已隐藏以保护凭据。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
