"""Optional offline coarse geolocation. Never persist, log or transmit an IP."""
import ipaddress
import os
from functools import lru_cache
from pathlib import Path

LANGUAGES = ('en', 'zh-CN', 'zh-TW', 'ja', 'ko')


def database_path():
    return Path(os.environ.get('FUNNEL_GEO_DB') or Path(__file__).resolve().parents[1] / 'geoip' / 'dbip-city-lite.mmdb')


@lru_cache(maxsize=2)
def _reader(filename, mtime):
    import maxminddb
    return maxminddb.open_database(filename)


def reader():
    p = database_path()
    return _reader(str(p), p.stat().st_mtime_ns)


def names(value):
    if not isinstance(value, dict):
        return {}
    return {key: text[:120] for key, text in value.items()
            if key in LANGUAGES and isinstance(text, str) and text.strip()}


def lookup(address):
    try:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return {}
        row = reader().get(str(ip)) or {}
        if not row:
            return {}
        country = row.get('country') or {}
        subdivisions = row.get('subdivisions') or []
        region = subdivisions[-1] if isinstance(subdivisions, list) and subdivisions else {}
        code = country.get('iso_code', '')
        return {'country_code': code if isinstance(code, str) and len(code) == 2 and code.isalpha() else '',
                'country': names(country.get('names')), 'region': names(region.get('names')),
                'city': names((row.get('city') or {}).get('names'))}
    except Exception:
        return {}


def status():
    try:
        return {'available': True, 'build_epoch': reader().metadata().build_epoch, 'provider': 'DB-IP Lite'}
    except Exception:
        return {'available': False, 'build_epoch': None, 'provider': 'DB-IP Lite'}
