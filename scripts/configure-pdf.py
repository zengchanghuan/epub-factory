#!/usr/bin/env python3
"""Configure PDF pricing and MinerU privately; never enable, restart or submit OCR."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from dotenv import dotenv_values, set_key


class ConfigurationError(ValueError):
    pass


def read_private(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ConfigurationError("配置必须是私有普通文件（权限 0600），不能是符号链接。")
    return dotenv_values(path, interpolate=False)


def valid_key(value):
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,8192}", value):
        raise ConfigurationError("MinerU Key 为空或格式不适合安全保存；未输出输入内容。")
    return value


def money(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= 10000 or amount != amount.quantize(Decimal('.01')):
            raise ValueError()
        return format(amount, '.2f')
    except (InvalidOperation, ValueError):
        raise ConfigurationError("价格须为正数且最多两位小数。") from None


def save(path, key, rate='0.20', minimum='3.99', priority_pages=None):
    desired = {'MINERU_API_KEY': valid_key(key),
               'PDF_TRANSLATION_PRICE_PER_PAGE_CNY': money(rate),
               'PDF_TRANSLATION_MIN_PRICE_CNY': money(minimum)}
    if priority_pages is not None:
        if not 0 <= priority_pages <= 1000:
            raise ConfigurationError("本站优先额度必须在 0–1000 页之间。")
        desired['MINERU_LOCAL_PRIORITY_PAGES'] = str(priority_pages)
    read_private(path)
    project = path.parent.parent if path.parent.name == 'backend' else path.parent
    with (project / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = read_private(path)
        original = path.read_bytes()
        stat = path.stat()
        for name, default in [('PDF_TRANSLATION_ENABLED', '0'),
                              ('MINERU_LOCAL_PRIORITY_PAGES', '0'),
                              ('MINERU_QUOTA_ACCOUNT', 'default-account')]:
            if name not in before:
                desired.setdefault(name, default)
        fd, name = tempfile.mkstemp(prefix='.pdf-env-', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(original)
            for k, v in desired.items():
                set_key(str(temporary), k, v)
            temporary.chmod(0o600)
            if (stat.st_uid, stat.st_gid) != (os.geteuid(), os.getegid()):
                os.chown(temporary, stat.st_uid, stat.st_gid)
            after = dotenv_values(temporary, interpolate=False)
            if any(after.get(k) != v for k, v in desired.items()) or any(
                    after.get(k) != v for k, v in before.items() if k not in desired):
                raise ConfigurationError("配置保存校验失败，原文件未修改。")
            backup_dir = path.parent / '.config-backups'
            if backup_dir.is_symlink():
                raise ConfigurationError("配置备份目录不能是符号链接。")
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup_dir.chmod(0o700)
            with (backup_dir / ('before-pdf-' + uuid.uuid4().hex + '.env')).open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != original:
                raise ConfigurationError("配置已被其他进程修改，请重新运行。")
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def check_auth(key):
    # A documented GET against a random nonexistent task performs no OCR.
    # Compare invalid credentials to avoid mistaking an unauthenticated 404 for success.
    import httpx
    url = 'https://mineru.net/api/v4/extract/task/' + str(uuid.uuid4())
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        codes = []
        for token in ('invalid-token-for-auth-check', valid_key(key)):
            with client.stream('GET', url, headers={'Authorization': 'Bearer ' + token}) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ConfigurationError("鉴权探测响应过大，未确认通过。")
                if response.is_redirect:
                    raise ConfigurationError("鉴权探测拒绝重定向，未确认通过。")
                try:
                    code = str(json.loads(body).get('code'))
                except (ValueError, AttributeError):
                    code = None
                codes.append((response.status_code, code))
        control, configured = codes
        if (control[0] == 401 or control[1] == 'A0202') and configured == (200, '-60012'):
            return
        if configured[0] == 401 or configured[1] in {'A0202', 'A0211'}:
            raise ConfigurationError("MinerU Token 无效或已过期，请在官方控制台更新。")
        raise ConfigurationError("只读鉴权响应未满足校验条件；未创建 OCR 任务，不能认定已通过。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--key-env-file', type=Path, help='私有 dotenv 文件，接受 MINERU_API_TOKEN / MINERU_API_KEY')
    mode.add_argument('--stdin-key', action='store_true', help='从管道读取 Key；不要把 Key 写进命令参数')
    mode.add_argument('--check', action='store_true', help='仅检查已保存配置，不联网')
    mode.add_argument('--check-mineru', action='store_true', help='仅 GET 验证鉴权，不上传或创建任务')
    parser.add_argument('--price-per-page', default='0.20')
    parser.add_argument('--minimum-price', default='3.99')
    parser.add_argument('--priority-pages', type=int, default=None, help='保留已配置额度；缺省为 0')
    args = parser.parse_args(argv)
    try:
        if args.check or args.check_mineru:
            values = read_private(args.env)
            valid_key(values.get('MINERU_API_KEY'))
            if args.check_mineru:
                check_auth(values['MINERU_API_KEY'])
                print('MinerU 只读鉴权通过；未上传文件、未创建 OCR 任务，尚未验证真实 OCR。')
            else:
                print(json.dumps({'mineru_key_configured': True,
                                  'price_per_page_cny': money(values.get('PDF_TRANSLATION_PRICE_PER_PAGE_CNY', '0')),
                                  'minimum_price_cny': money(values.get('PDF_TRANSLATION_MIN_PRICE_CNY', '0')),
                                  'pdf_enabled': values.get('PDF_TRANSLATION_ENABLED', '0') in {'1', 'true', 'yes'},
                                  'priority_pages': int(values.get('MINERU_LOCAL_PRIORITY_PAGES', '0')),
                                  'mode': oct(args.env.stat().st_mode & 0o777)}))
        else:
            if args.key_env_file:
                source = read_private(args.key_env_file)
                key = source.get('MINERU_API_KEY') or source.get('MINERU_API_TOKEN')
            else:
                key = sys.stdin.read(8194)
            save(args.env.absolute(), key, args.price_per_page, args.minimum_price, args.priority_pages)
            print('PDF 价格及 MinerU Key 已安全保存（0600），其他配置保持不变，原配置已私密备份。')
            print('未开启 PDF、未重启服务、未上传文件或调用 OCR。应用重新加载后才使用新配置。')
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # SDK, network and filesystem exceptions may contain sensitive request data.
        print('配置或只读检查未完成；请检查权限、依赖和网络。详细异常未输出以保护凭据。', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
