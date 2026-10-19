"""LLM analysis: Claude Haiku triages every candidate, Claude Sonnet double-checks the uncertain ones."""
from __future__ import annotations

import base64
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from .yahoo import ItemDetail, Listing, YahooClient

log = logging.getLogger(__name__)

VERDICTS = ["strong_deal", "interesting", "uncertain", "pass"]

SCHEMA = {
    "type": "object",
    "properties": {
        "item_type": {
            "type": "string",
            "enum": ["gpu", "multiple_gpus", "complete_pc", "workstation_or_server", "epyc_cpu",
                     "epyc_bundle", "parts_or_accessory", "not_relevant"],
        },
        "identified_hardware": {"type": "string", "description": "What is actually being sold, concretely."},
        "gpu_models": {"type": "array", "items": {"type": "string"}},
        "condition_assessment": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "positive_signals": {"type": "array", "items": {"type": "string"}},
        "estimated_market_value_jpy": {"type": "integer", "description": "Typical sold price for this exact item in working order, JPY."},
        "deal_ceiling_jpy": {"type": "integer", "description": "Highest total price (incl. shipping) that is still a clearly good deal, JPY. 0 if pass."},
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number", "description": "0.0-1.0 confidence in the verdict."},
        "summary": {"type": "string", "description": "One English sentence for the alert."},
        "reasoning": {"type": "string"},
    },
    "required": ["item_type", "identified_hardware", "gpu_models", "condition_assessment", "red_flags",
                 "positive_signals", "estimated_market_value_jpy", "deal_ceiling_jpy", "verdict",
                 "confidence", "summary", "reasoning"],
    "additionalProperties": False,
}

SYSTEM = """You are an expert buyer of used computer hardware on Yahoo! Auctions Japan (ヤフオク), hunting for
genuinely underpriced hardware for a 3D studio's render farm (Houdini Karma XPU, OctaneRender, Redshift).

What the buyer wants:
- NVIDIA GPUs good for GPU rendering: RTX 3090 / 3090 Ti, 4080 / 4080 SUPER, 4090, 5080, 5090, and other
  CUDA/OptiX cards with lots of VRAM (RTX A5000/A6000, RTX 5000/6000 Ada, RTX PRO 5000/6000 Blackwell,
  TITAN RTX, Quadro RTX 8000, 4070 Ti SUPER / 5070 Ti 16GB). Karma XPU and Octane need NVIDIA OptiX/CUDA;
  VRAM capacity matters as much as raw speed. AMD/Intel GPUs are not useful.
- Complete PCs/workstations containing such GPUs, valued as the sum of usable parts.
- AMD EPYC CPUs, EPYC + motherboard (+RAM) bundles, and EPYC servers/workstations.

The buyer only wants deals WELL BELOW market (roughly 75% of market or less, all-in). Good deals are rare;
a false alarm wastes their time and a missed scam costs real money, so be skeptical and precise.

Known traps on ヤフオク - check the description and photos for all of these:
- Bait listings: generic titles ending in a code (e.g. "GeForce RTX 5090 グラフィックボード AK04"), stock photos,
  junk disclosed only deep in the description (ジャンク, 動作未確認, 動作できないと思います, 分解の痕跡).
- Parts sold under a GPU title: cooler/shroud/box only, PCB無し, GPUコア/メモリ欠品 (core-removed boards),
  backplates, water blocks, cables, "確認用" listings, empty boxes, photos of a card that "isn't included".
- Laptop GPUs (mobile RTX 4090/5090, A5000 laptop) are far weaker than desktop cards of the same name.
- RTX 4090 D / 5090 D are Chinese-market cut-down variants (slightly lower value). Converted/modded cards
  (Tesla V100 / A100 SXM on PCIe adapters, "5090/4090 cooler" swaps, 48GB-modded 4090) need extra scrutiny.
- Ex-mining cards, burnt 12VHPWR connectors, missing/water-damaged parts, reflowed boards.
- EPYC: the buyer wants many-core parts useful for CPU rendering/simulation (roughly 32+ cores: Rome 7002,
  Milan 7003, Genoa/Bergamo 9004, Turin 9005) and complete CPU+board(+RAM) bundles. Low-core or first-gen
  Naples (7001) chips are rarely worth alerting on unless nearly free as part of a useful bundle.
  Vendor-locked CPUs (AMD PSB locked to Dell/Lenovo/HPE - "Dell lock", "ベンダーロック") only work in that
  vendor's boards and are worth much less; ES/QS engineering samples; bent socket pins (SP3/SP5); missing
  torque screwdriver/carrier frame; RAM type (RDIMM DDR4 vs DDR5) and generation (Naples/Rome/Milan/Genoa/Turin).
- Reserved listings ("○○様専用", "1/2" split payments), "代理出品", overseas sellers with poor Japanese and
  new accounts, low seller ratings (< ~97% is a warning), no returns + vague description.
Positive signals: detailed description by an individual with benchmark/stress-test results, original box and
receipt (納品書), remaining manufacturer warranty, store sellers with returns, many real photos of the actual item.

Pricing rules:
- You get the current price, buy-it-now price, shipping, time left, and a market reference computed from
  recently SOLD clean listings of the detected model (median). Trust it as a baseline but use your judgement,
  e.g. a 5090 with a premium cooler may be worth more, a 4090 D less. Prices are JPY and include tax.
- Auctions on ヤフオク commonly start at 1円 and jump in the final hours. For an auction that is NOT ending soon,
  do not treat the low current bid as a deal by itself: judge whether the ITEM is genuine, complete and
  desirable, and set deal_ceiling_jpy so the buyer is alerted if the price is still under it near the end.
- For buy-it-now / fixed-price listings, or auctions ending within a day, judge the price as it stands.
- deal_ceiling_jpy = the most the buyer should pay all-in for this to still be a clearly good deal for THIS item
  (usually <= 75% of its working-order market value, lower when there is condition risk). 0 when verdict is pass.

Verdicts:
- strong_deal: genuine, desirable, low-risk, and the current all-in price is already far below market (~60% or less).
- interesting: genuine and desirable; worth watching or buying if the price stays under the ceiling.
- uncertain: could be a great deal but key facts are unclear (authenticity, completeness, condition) - a more
  careful reviewer will look at it.
- pass: not a target item, parts/accessory, junk/bait, too risky, or the price can never be a good deal.
Reply with only the JSON object."""


@dataclass
class Analysis:
    model: str
    data: dict

    @property
    def verdict(self) -> str:
        return self.data["verdict"]

    @property
    def confidence(self) -> float:
        return float(self.data.get("confidence") or 0)


def _fmt_jpy(v: int | None) -> str:
    return f"¥{v:,}" if v else "-"


def build_context(listing: Listing, detail: ItemDetail, family_name: str | None, market: int | None,
                  total_price: int, comps: list[dict] | None = None, max_desc_chars: int = 8000) -> str:
    now = datetime.now(timezone.utc)
    hours_left = (detail.end_time - now).total_seconds() / 3600
    desc = detail.description
    if len(desc) > max_desc_chars:
        desc = desc[:max_desc_chars] + "\n...[description truncated]"
    lines = [
        f"Title: {detail.title}",
        f"URL: {listing.url}",
        f"Category: {' > '.join(listing.category_path[1:])}",
        f"Listing type: {'fixed price / buy-it-now only' if listing.is_fixed_price else 'auction'}",
        f"Current price: {_fmt_jpy(detail.price)} ({detail.bids} bids)",
        f"Buy-it-now price: {_fmt_jpy(detail.buy_now_price or listing.buy_now_price)}",
        f"Shipping: {'free' if listing.is_free_shipping else _fmt_jpy(listing.shipping_fee) if listing.shipping_fee else 'unknown/buyer pays'}",
        f"All-in price right now: {_fmt_jpy(total_price)}",
        f"Time left: {hours_left:.1f} hours (ends {detail.end_time.isoformat()})",
        f"Watchers: {listing.watch_count}",
        f"Condition field: {detail.condition_name or listing.condition}",
        f"Seller: {detail.seller_name or '?'} | {'store' if detail.seller_is_store else 'individual'} | "
        f"rating {listing.seller_rating or '?'} | returns {'allowed' if detail.returnable else 'not allowed'}"
        + (f" ({detail.returnable_comment})" if detail.returnable_comment else ""),
        f"Unanswered questions: {detail.unanswered_qa}",
        f"Detected model family: {family_name or 'unknown'}",
        f"Market reference (median recent sold price, clean listings): {_fmt_jpy(market)}",
        f"Photos: {len(detail.images)}",
        "",
        "Recent sold comparables on ヤフオク (price | bids | ended | title):",
        *([f"- ¥{c['price']:,} | {'buy-now' if c['fixed'] else str(c['bids']) + ' bids'} | {c['date']} | {c['title']}"
           for c in comps] if comps else ["- (none found)"]),
        "",
        "Description:",
        desc or "(empty)",
    ]
    return "\n".join(lines)


def _image_blocks(client: YahooClient, urls: list[str], limit: int) -> list[dict]:
    blocks = []
    for url in urls[:limit]:
        got = client.image_bytes(url)
        if not got:
            continue
        raw, ctype = got
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": ctype,
                                                     "data": base64.b64encode(raw).decode()}})
    return blocks


class Analyzer:
    def __init__(self, cfg: dict, yahoo: YahooClient):
        self.cfg = cfg["llm"]
        self.yahoo = yahoo
        self.client = anthropic.Anthropic(max_retries=4)
        self.verify_count = 0
        self._lock = threading.Lock()

    def reserve_verify(self) -> bool:
        with self._lock:
            if self.verify_count >= self.cfg["max_verify_per_run"]:
                return False
            self.verify_count += 1
            return True

    def triage(self, context: str, images: list[str]) -> Analysis | None:
        content = _image_blocks(self.yahoo, images, self.cfg["triage_images"])
        content.append({"type": "text", "text": "Analyze this ヤフオク listing.\n\n" + context})
        resp = self.client.messages.create(
            model=self.cfg["triage_model"],
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": self.cfg["triage_effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
        return self._parse(resp, self.cfg["triage_model"])

    def verify(self, context: str, images: list[str], first: Analysis | None) -> Analysis | None:
        content = _image_blocks(self.yahoo, images, self.cfg["verify_images"])
        prior = (
            "A first-pass reviewer (fast model) produced this assessment. It may be wrong in either direction - "
            "verify every claim against the description and photos yourself:\n"
            + json.dumps(first.data, ensure_ascii=False, indent=1)
            if first else "The first-pass reviewer could not assess this listing."
        )
        content.append({"type": "text", "text": (
            "Carefully re-examine this ヤフオク listing. Read the full description and every photo; look for "
            "anything that contradicts the title. Decide whether it is really a good buy.\n\n"
            f"{context}\n\n{prior}"
        )})
        resp = self.client.beta.messages.create(
            model=self.cfg["verify_model"],
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": self.cfg["verify_effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        return self._parse(resp, self.cfg["verify_model"])

    @staticmethod
    def _parse(resp, model: str) -> Analysis | None:
        if resp.stop_reason == "refusal":
            log.warning("%s refused: %s", model, getattr(resp, "stop_details", None))
            return None
        if resp.stop_reason == "max_tokens":
            log.warning("%s hit max_tokens", model)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("%s returned invalid JSON", model)
            return None
        return Analysis(model=model, data=data)
