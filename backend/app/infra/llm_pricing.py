"""Versioned token tariffs. Currency amounts are decimal strings, not floats.

These are documented tariff calculations, never a provider payment receipt.
Unknown providers/models and historical periods deliberately have no fallback.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import os
from urllib.parse import urlsplit


PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
ALIASES = {"deepseek-v4-flash": "deepseek-flash",
           "deepseek-v4-flash-vision-exp": "deepseek-flash"}
BEIJING = timezone(timedelta(hours=8))


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def provider_host(base_url):
    # Never persist credentials, path, query parameters or an API key.
    return (urlsplit(base_url).hostname or "unknown").lower()[:253]


def canonical_model(model):
    return ALIASES.get(model, model)


def peak(at):
    local = utc(at).astimezone(BEIJING)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and (540 <= minute < 720 or 840 <= minute < 1080)


def crosses_band(start, end):
    # Same band at the endpoints is insufficient for a long request crossing
    # lunch, night or a weekend. Inspect every intervening tariff boundary.
    start, end = utc(start), utc(end)
    day = start.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    while utc(day) <= end:
        if day.weekday() < 5:
            for hour in (9, 12, 14, 18):
                boundary = utc(day.replace(hour=hour))
                if start < boundary <= end:
                    return True
        day += timedelta(days=1)
    return False


def catalog():
    rows = []
    for model, hit, miss, output in (
        ("deepseek-flash", "0.02", "1", "4"),
        ("deepseek-v4-pro", "0.15", "4.5", "13.5"),
    ):
        rows.append(dict(provider="api.deepseek.com", model=model, currency="CNY",
                         version="deepseek-official-2026-09-18", valid_from="2026-09-18T00:00:00+08:00",
                         valid_until=None, cache_hit=hit, cache_miss=miss, output=output,
                         peak_multiplier="2", time_policy="deepseek_beijing", source=PRICE_SOURCE,
                         checked_at="2026-09-18"))
    path = os.environ.get("LLM_PRICING_FILE", "").strip()
    if path:
        with open(path, encoding="utf-8") as stream:
            additions = json.load(stream)
        if not isinstance(additions, list):
            raise ValueError("price catalog must be a list")
        rows.extend(additions)
    for row in rows:
        for field in ("provider", "model", "version", "source", "checked_at", "valid_from", "currency"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError("invalid price catalog field: " + field)
        if len(row["currency"]) != 3 or not row["currency"].isalpha() or not row["currency"].isupper():
            raise ValueError("invalid price currency")
        if row.get("time_policy", "fixed") not in {"fixed", "deepseek_beijing"}:
            raise ValueError("unsupported tariff time policy")
        for field in ("cache_hit", "cache_miss", "output", "peak_multiplier"):
            raw = row.get(field, "1" if field == "peak_multiplier" else None)
            if not isinstance(raw, str):
                raise ValueError("token tariff must be a decimal string")
            value = Decimal(raw)
            if not value.is_finite() or value < 0:
                raise ValueError("invalid token tariff")
            if len(str(value)) > 80 or not -12 <= value.as_tuple().exponent <= 12:
                raise ValueError("unsupported tariff precision")
        for field in ("valid_from", "valid_until"):
            if row.get(field):
                value = datetime.fromisoformat(row[field])
                if value.tzinfo is None:
                    raise ValueError("tariff dates require timezone")
        if row.get("valid_until") and datetime.fromisoformat(row["valid_until"]) <= datetime.fromisoformat(row["valid_from"]):
            raise ValueError("invalid tariff interval")
    return rows


def price_usage(provider, requested_model, response_model, usage, start, end, rows):
    result = dict(price_status="unknown_rate", currency=None, calculated_cost=None,
                  rate_version=None, rate_snapshot=None, time_band=None)
    if usage["usage_status"] != "complete":
        result["price_status"] = "usage_" + usage["usage_status"]
        return result
    canonical = canonical_model if provider == "api.deepseek.com" else lambda value: value
    model = canonical(response_model or requested_model)
    if response_model and model != canonical(requested_model):
        # Never guess the pricing of a silently substituted model version.
        result["price_status"] = "model_mismatch"
        return result
    candidates = [r for r in rows if r["provider"] == provider and r["model"] == model]
    applicable = [r for r in candidates
                  if utc(datetime.fromisoformat(r["valid_from"])) <= utc(start)
                  and (not r.get("valid_until") or utc(end) < utc(datetime.fromisoformat(r["valid_until"])))]
    if not applicable:
        result["price_status"] = "historical_rate_unknown" if candidates else "unknown_rate"
        return result
    # Explicit overrides must have a unique latest effective date.
    applicable.sort(key=lambda r: utc(datetime.fromisoformat(r["valid_from"])), reverse=True)
    rate = applicable[0]
    if len(applicable) > 1 and datetime.fromisoformat(applicable[1]["valid_from"]) == datetime.fromisoformat(rate["valid_from"]):
        result["price_status"] = "ambiguous_rate"
        return result
    result.update(currency=rate["currency"], rate_version=rate["version"],
                  rate_snapshot=json.dumps(rate, ensure_ascii=False, sort_keys=True))
    if any(utc(start) < utc(datetime.fromisoformat(r["valid_from"])) <= utc(end) for r in candidates):
        result["price_status"] = "rate_boundary_ambiguous"
        return result
    split_known = usage["cache_hit_tokens"] is not None and usage["cache_miss_tokens"] is not None
    flat_input = Decimal(str(rate["cache_hit"])) == Decimal(str(rate["cache_miss"]))
    if not split_known and not flat_input:
        result["price_status"] = "cache_split_missing"
        return result
    timed = rate.get("time_policy") == "deepseek_beijing"
    if timed and crosses_band(start, end):
        result["price_status"] = "time_band_ambiguous"
        return result
    is_peak = timed and peak(start)
    multiplier = Decimal(str(rate.get("peak_multiplier", "1"))) if is_peak else Decimal(1)
    with localcontext() as context:
        context.prec = 100
        input_amount = (usage["cache_hit_tokens"] * Decimal(str(rate["cache_hit"]))
                        + usage["cache_miss_tokens"] * Decimal(str(rate["cache_miss"]))) if split_known else (
                            usage["prompt_tokens"] * Decimal(str(rate["cache_miss"])))
        amount = (input_amount + usage["completion_tokens"] * Decimal(str(rate["output"]))) * multiplier / Decimal(1000000)
    result.update(price_status="priced", calculated_cost=format(amount, "f"),
                  time_band="peak" if is_peak else "off_peak" if timed else "fixed")
    return result
