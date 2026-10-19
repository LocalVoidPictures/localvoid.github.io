"""Learn current market value per family from recently sold (closed) auctions."""
from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from datetime import datetime, timezone

from .prefilter import ACCESSORY_RE, JUNK_RE, LAPTOP_RE, PC_RE, detect_family
from .yahoo import Listing, YahooClient, YahooError

log = logging.getLogger(__name__)
GPU_CATEGORY = "グラフィックカード"


def sold_prices(client: YahooClient, families: dict, category: int | None) -> dict[str, list[Listing]]:
    """Run every family's queries against closed auctions and bucket clean sold listings by family."""
    queries = sorted({q for fam in families.values() for q in fam.get("queries") or []})
    seen: set[str] = set()
    buckets: dict[str, list[Listing]] = {}
    for q in queries:
        try:
            listings = client.search(q, closed=True, category=category)
        except YahooError as e:
            log.warning("closed search %r failed: %s", q, e)
            continue
        for l in listings:
            if l.auction_id in seen:
                continue
            seen.add(l.auction_id)
            key = detect_family(l.title, families)
            if key is None or key == "epyc":
                continue
            t = unicodedata.normalize("NFKC", l.title)
            if ACCESSORY_RE.search(t) or JUNK_RE.search(t) or LAPTOP_RE.search(t) or PC_RE.search(t):
                continue
            if not any(GPU_CATEGORY in c for c in l.category_path):
                continue
            if l.bid_count == 0 and not l.is_fixed_price:
                continue
            buckets.setdefault(key, []).append(l)
    return buckets


def robust_median(prices: list[int], fallback: int | None) -> int | None:
    if fallback:
        # Drop obvious parts/boxes and fat-finger listings relative to the configured ballpark.
        prices = [p for p in prices if 0.35 * fallback <= p <= 2.5 * fallback]
    if len(prices) < 5:
        return None
    prices.sort()
    q1, q3 = prices[len(prices) // 4], prices[(3 * len(prices)) // 4]
    iqr = q3 - q1
    trimmed = [p for p in prices if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]
    return int(statistics.median(trimmed))


def refresh_market(client: YahooClient, cfg: dict, state: dict, max_age_hours: float = 20) -> dict[str, int | None]:
    """Return {family: market_jpy}; re-learns from closed auctions at most once per `max_age_hours`."""
    families = cfg["families"]
    cache = state.setdefault("market", {})
    now = datetime.now(timezone.utc)
    updated = cache.get("_updated")
    if not updated or (now - datetime.fromisoformat(updated)).total_seconds() > max_age_hours * 3600:
        buckets = sold_prices(client, families, cfg["search"].get("category"))
        for key, fam in families.items():
            sold = buckets.get(key, [])
            med = robust_median([l.price for l in sold], fam.get("fallback_market_jpy"))
            recent = sorted(sold, key=lambda l: l.end_time, reverse=True)[:12]
            cache[key] = {"median": med, "samples": len(sold), "comps": [_comp(l) for l in recent]}
            prices = sold
            log.info("market %-15s median=%s from %d sold", key, med, len(prices))
        cache["_updated"] = now.isoformat()
    return {key: (cache.get(key) or {}).get("median") or fam.get("fallback_market_jpy")
            for key, fam in families.items()}


def _comp(l: Listing) -> dict:
    return {"price": l.price, "bids": l.bid_count, "fixed": l.is_fixed_price,
            "date": l.end_time.date().isoformat(), "title": l.title[:90]}


EPYC_MODEL_RE = re.compile(r"EPYC\s*(?:GENOA|MILAN|ROME|NAPLES|TURIN|BERGAMO|SIENA)?[\s-]*(?:X\s*)?(\d[0-9A-Z]{3}[A-Z]?\d?)", re.I)
BUNDLE_RE = re.compile(r"マザー|M/B|\bMB\b|セット|BUNDLE|サーバー|SERVER|ワークステーション|H1[1-3]SS|MZ[0-9]{2}|ROMED|KRPA|GENOAD|TURIND|メモリ|RDIMM|ベアボーン", re.I)


class Comparables:
    """Recent sold listings to ground the LLM's price judgement."""

    def __init__(self, client: YahooClient, state: dict, category: int | None):
        self.client = client
        self.cache = state.get("market", {})
        self.category = category
        self._epyc: dict[str, list[dict]] = {}

    def for_listing(self, family: str, title: str) -> list[dict]:
        if family != "epyc":
            return (self.cache.get(family) or {}).get("comps", [])
        m = EPYC_MODEL_RE.search(unicodedata.normalize("NFKC", title))
        if not m:
            return []
        model = m.group(1).upper()
        if model not in self._epyc:
            try:
                sold = self.client.search(f"EPYC {model}", closed=True, category=self.category)
            except YahooError as e:
                log.warning("EPYC comps %s: %s", model, e)
                sold = []
            sold = [l for l in sold if model in unicodedata.normalize("NFKC", l.title).upper()]
            self._epyc[model] = [_comp(l) for l in sorted(sold, key=lambda l: l.end_time, reverse=True)[:15]]
        return self._epyc[model]

    @staticmethod
    def epyc_cpu_median(comps: list[dict]) -> int | None:
        """Median sold price of CPU-only listings, or None if there are too few."""
        prices = [c["price"] for c in comps
                  if not BUNDLE_RE.search(c["title"]) and not JUNK_RE.search(c["title"]) and (c["bids"] or c["fixed"])]
        return int(statistics.median(prices)) if len(prices) >= 3 else None
