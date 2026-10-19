"""Scraper for auctions.yahoo.co.jp.

Search and item pages are Next.js apps that embed their full state as JSON in
<script id="__NEXT_DATA__">, so we parse that instead of the rendered HTML.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

BASE = "https://auctions.yahoo.co.jp"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class YahooError(RuntimeError):
    pass


@dataclass
class Listing:
    auction_id: str
    title: str
    price: int  # current price, tax included
    buy_now_price: int | None
    bid_count: int
    start_time: datetime
    end_time: datetime
    is_fixed_price: bool
    condition: str | None
    category_path: list[str]
    seller_id: str | None
    seller_rating: str | None
    seller_is_store: bool
    shipping_fee: int | None
    is_free_shipping: bool
    watch_count: int
    image_url: str | None

    @property
    def url(self) -> str:
        return f"{BASE}/jp/auction/{self.auction_id}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["start_time"] = self.start_time.isoformat()
        d["end_time"] = self.end_time.isoformat()
        d["url"] = self.url
        return d


@dataclass
class ItemDetail:
    auction_id: str
    title: str
    price: int
    buy_now_price: int | None
    bids: int
    end_time: datetime
    status: str
    condition_name: str | None
    description: str
    images: list[str] = field(default_factory=list)
    seller_name: str | None = None
    seller_is_store: bool = False
    returnable: bool | None = None
    returnable_comment: str | None = None
    is_early_closing: bool | None = None
    unanswered_qa: int = 0


class YahooClient:
    def __init__(self, delay: float = 1.5, timeout: float = 30.0):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
        self._last = 0.0
        self._lock = threading.Lock()  # one Yahoo request at a time, even from worker threads

    def _get(self, url: str) -> str:
        with self._lock:
            return self._get_locked(url)

    def _get_locked(self, url: str) -> str:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        for attempt in range(4):
            try:
                r = self.session.get(url, timeout=self.timeout)
                self._last = time.monotonic()
                if r.status_code == 404:
                    raise YahooError(f"404 {url}")
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code}")
                r.raise_for_status()
                return r.text
            except YahooError:
                raise
            except requests.RequestException as e:
                backoff = 2 ** (attempt + 1)
                log.warning("GET %s failed (%s); retry in %ss", url, e, backoff)
                time.sleep(backoff)
        raise YahooError(f"giving up on {url}")

    @staticmethod
    def _next_data(html: str) -> dict:
        m = NEXT_DATA_RE.search(html)
        if not m:
            raise YahooError("no __NEXT_DATA__ on page (layout changed or blocked?)")
        return json.loads(m.group(1))

    def search(self, query: str, *, closed: bool = False, page: int = 1, per_page: int = 100,
               category: int | None = None, sort_new: bool = True) -> list[Listing]:
        params = {"p": query, "va": query, "b": 1 + (page - 1) * per_page, "n": per_page}
        if category:
            params["auccat"] = category
        if sort_new and not closed:
            params.update({"s1": "new", "o1": "d"})
        path = "/closedsearch/closedsearch" if closed else "/search/search"
        data = self._next_data(self._get(f"{BASE}{path}?{urlencode(params)}"))
        try:
            items = data["props"]["pageProps"]["initialState"]["search"]["items"]["listing"].get("items") or []
        except (KeyError, TypeError) as e:
            raise YahooError(f"unexpected search JSON shape: {e}") from e
        return [_parse_listing(it) for it in items]

    def item(self, auction_id: str) -> ItemDetail:
        data = self._next_data(self._get(f"{BASE}/jp/auction/{auction_id}"))
        try:
            it = data["props"]["pageProps"]["initialState"]["item"]["detail"]["item"]
        except (KeyError, TypeError) as e:
            raise YahooError(f"unexpected item JSON shape: {e}") from e
        desc = it.get("description") or []
        if isinstance(desc, list):
            desc = "\n".join(str(x) for x in desc)
        if not desc.strip() and it.get("descriptionHtml"):
            desc = _strip_html(it["descriptionHtml"])
        seller = it.get("seller") or {}
        ret = it.get("itemReturnable") or {}
        return ItemDetail(
            auction_id=it["auctionId"],
            title=it.get("title", ""),
            price=int(it.get("taxinPrice") or it.get("price") or 0),
            buy_now_price=_int(it.get("taxinBidorbuy") or it.get("bidorbuy")),
            bids=int(it.get("bids") or 0),
            end_time=datetime.fromisoformat(it["endTime"]),
            status=it.get("status", "open"),
            condition_name=it.get("conditionName"),
            description=desc.strip(),
            images=[i["image"] for i in it.get("img") or [] if i.get("image")],
            seller_name=seller.get("displayName"),
            seller_is_store=bool(seller.get("isStore")),
            returnable=ret.get("allowed"),
            returnable_comment=ret.get("comment"),
            is_early_closing=it.get("isEarlyClosing"),
            unanswered_qa=int(it.get("unAnsweredQAndANum") or 0),
        )

    def image_bytes(self, url: str, max_bytes: int = 4_500_000) -> tuple[bytes, str] | None:
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            log.info("image fetch failed %s: %s", url, e)
            return None
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp") or len(r.content) > max_bytes:
            return None
        return r.content, ctype


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


def _strip_html(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t　]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text)


def _parse_listing(it: dict) -> Listing:
    seller = it.get("seller") or {}
    return Listing(
        auction_id=it["auctionId"],
        title=it.get("title", ""),
        price=int(it.get("price") or 0),
        buy_now_price=_int(it.get("buyNowPrice")),
        bid_count=int(it.get("bidCount") or 0),
        start_time=datetime.fromisoformat(it["startTime"]),
        end_time=datetime.fromisoformat(it["endTime"]),
        is_fixed_price=bool(it.get("isFixedPrice")),
        condition=it.get("itemCondition"),
        category_path=[c.get("name", "") for c in it.get("categoryPath") or []],
        seller_id=seller.get("userId"),
        seller_rating=seller.get("goodRating"),
        seller_is_store=bool(seller.get("isStore")),
        shipping_fee=_int(it.get("currentPriceShippingFee")),
        is_free_shipping=bool(it.get("isFreeShipping")),
        watch_count=int(it.get("watchCount") or 0),
        image_url=it.get("imageUrl"),
    )
