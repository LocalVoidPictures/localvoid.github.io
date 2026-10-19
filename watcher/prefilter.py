"""Cheap rule-based screening so the LLMs only see plausible candidates."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .yahoo import Listing

ACCESSORY_RE = re.compile(
    "|".join([
        r"ケーブル", r"ライザー", r"変換", r"延長", r"ブラケット", r"ステー", r"支え", r"サポート(ステイ|スタンド)",
        r"バックプレート", r"ブロックのみ", r"クーラーのみ", r"ファンのみ", r"冷却ファン", r"ヒートシンクのみ",
        r"シュラウド", r"箱のみ", r"空箱", r"外箱のみ", r"化粧箱", r"確認用", r"ステッカー", r"マウスパッド",
        r"12VHPWR", r"12V-?2X6", r"電源ユニット", r"ATX電源", r"PCケース", r"\d{4}(シリーズ)?対応", r"対応(電源|ケース|モデル)",
        r"水枕", r"水冷ブロック(?!付|装着|取付|搭載)", r"EISBLOCK", r"PACIFIC", r"冷却用", r"ヒートシンク(?!付)",
        r"(固定)?トレー", r"キャリア(フレーム)?", r"ドライバー", r"ソケットカバー", r"リテンション", r"CPUクーラー(?!付)",
        r"グリス", r"サーマルパッド", r"説明書", r"レプリカ", r"ダミー", r"\bCABLE\b", r"\bRISER\b",
    ]),
    re.I,
)
# Shops advertising build-to-order PCs, and keyword-stuffed titles.
AD_RE = re.compile(r"ご相談|お見積|見積り|オーダーメイド|受付中|カスタム(ゲーミング)?PC|ご予算|検\s?(索|用)?\s?RTX|\b検\b", re.I)
# "RTX4070 (RTX3090同等)" style comparisons mention a model that isn't the one being sold.
COMPARISON_RE = re.compile(r"[（(][^（）()]*(同等|相当|並み|近い性能|より上|超え|比較|性能)[^（）()]*[）)]")
RESERVED_RE = re.compile(r"様専用|専用出品|様\s?[12]/2|様分|取り置き|予約品")
# A GeForce/Quadro family match only counts when the title also looks like a graphics card,
# so "OptiPlex 5090", "HDD-3090" or part number "D4090" don't match.
GPU_CONTEXT_RE = re.compile(
    r"RTX|GEFORCE|NVIDIA|QUADRO|TITAN|グラボ|グラフィック|ビデオカード|ZOTAC|MSI|ASUS|GIGABYTE|AORUS|PALIT|"
    r"INNO3D|GAINWARD|GALAX|玄人志向|PNY|ELSA|FOUNDERS|\bROG\b|\bTUF\b|LEADTEK",
    re.I,
)
NOT_GPU_RE = re.compile(r"OPTIPLEX|PRECISION\s?[357]\d90|ELITEDESK|PRODESK|THINKCENTRE|VOSTRO", re.I)
JUNK_RE = re.compile(r"ジャンク|動作未確認|通電(のみ|未確認)|現状品|部品取り|故障|訳あり|JUNK|コア抜き|不動", re.I)
LAPTOP_RE = re.compile(
    r"ノート|LAPTOP|ゼフィルス|ZEPHYRUS|STRIX\s?SCAR|LEGION|ALIENWARE\s?M|OMEN\s?(16|17)|RAZER\s?BLADE|"
    r"AORUS\s?1[5-7]|TITAN\s?18|RAIDER|STEALTH|VECTOR|CROSSHAIR|PRECISION\s?[357][5-7]\d0|ZBOOK|LAPTOP\s?GPU|モバイル|"
    r"ALIENWARE\s?X1[5-7]|ALIENWARE\s?M1[5-8]|\b1[3-8](\.\d)?\s?(インチ|INCH|型)|THINKPAD|DYNABOOK|NEXTGEAR\s?J6",
    re.I,
)


def normalize(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).upper()
    return re.sub(r"[\s　_\-・/]+", "", t)


def detect_family(title: str, families: dict) -> str | None:
    title = COMPARISON_RE.sub(" ", unicodedata.normalize("NFKC", title))
    norm = normalize(title)
    if len(set(re.findall(r"(?<!\d)[1-5]0[5-9]0(?!\d)", title))) >= 4:
        return None  # lists many GPU generations: keyword stuffing or a shop ad
    gpu_ok = bool(GPU_CONTEXT_RE.search(norm)) and not NOT_GPU_RE.search(unicodedata.normalize("NFKC", title))
    # More specific families are listed first in config where it matters (e.g. 3090 Ti vs 3090),
    # and their patterns use lookaheads, so the first match wins.
    for key, fam in families.items():
        if fam.get("gpu", True) and not gpu_ok:
            continue
        for pat in fam.get("patterns", []):
            if re.search(pat, norm):
                return key
    return None


@dataclass
class Screen:
    passed: bool
    family: str | None
    reason: str
    effective_price: int
    is_junk: bool = False


def effective_price(listing: Listing) -> int:
    """What it costs right now: buy-now for fixed-price listings, else current bid, plus known shipping."""
    base = listing.buy_now_price if listing.is_fixed_price and listing.buy_now_price else listing.price
    ship = 0 if listing.is_free_shipping else (listing.shipping_fee or 0)
    return base + ship


def screen(listing: Listing, families: dict, market: dict[str, int | None], cfg: dict) -> Screen:
    f = cfg["filters"]
    title = listing.title
    price = effective_price(listing)
    family = detect_family(title, families)
    if family is None:
        return Screen(False, None, "no target model in title", price)
    nt = unicodedata.normalize("NFKC", title)
    if ACCESSORY_RE.search(nt):
        return Screen(False, family, "accessory/parts keyword", price)
    if AD_RE.search(nt):
        return Screen(False, family, "shop ad / keyword stuffing", price)
    if RESERVED_RE.search(nt):
        return Screen(False, family, "reserved for another buyer", price)
    junk = bool(JUNK_RE.search(nt))
    if junk and not f.get("include_junk"):
        return Screen(False, family, "junk/untested", price, junk)
    if family != "epyc" and not f.get("include_laptops") and LAPTOP_RE.search(nt):
        return Screen(False, family, "laptop", price, junk)
    if listing.buy_now_price and listing.buy_now_price >= 9_000_000:
        return Screen(False, family, "placeholder price", price, junk)

    mkt = market.get(family)
    if mkt and not families[family].get("always_llm"):
        ratio = price / mkt
        if ratio > f["max_price_to_market"]:
            # Whole PCs can legitimately be priced above the bare GPU's market value.
            if not _looks_like_pc(nt):
                return Screen(False, family, f"price {ratio:.0%} of market", price, junk)
            if ratio > f["max_price_to_market"] * 2.5:
                return Screen(False, family, f"PC price {ratio:.0%} of GPU market", price, junk)
        if listing.is_fixed_price and ratio < f["min_price_to_market"]:
            return Screen(False, family, f"suspiciously cheap fixed price ({ratio:.0%})", price, junk)
    return Screen(True, family, "ok", price, junk)


PC_RE = re.compile(r"ゲーミングPC|デスクトップ|自作PC|PC本体|BTO|ワークステーション|WORKSTATION|CORE\s?I[579]|ULTRA\s?[579]|RYZEN|XEON|THREADRIPPER|メモリ\d+GB|SSD", re.I)


def _looks_like_pc(title: str) -> bool:
    return bool(PC_RE.search(title))
