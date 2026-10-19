"""One watcher pass: scan new listings, analyze candidates, and re-check the watchlist near auction end."""
from __future__ import annotations

import argparse
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import notify, state as state_mod
from .analyze import Analysis, Analyzer, build_context
from .market import BUNDLE_RE, Comparables, refresh_market
from .prefilter import effective_price, screen
from .yahoo import ItemDetail, Listing, YahooClient, YahooError

log = logging.getLogger("watcher")


def now() -> datetime:
    return datetime.now(timezone.utc)


def hours_left(end: datetime) -> float:
    return (end - now()).total_seconds() / 3600


def all_in(price: int, shipping: int | None, free: bool) -> int:
    return price + (0 if free else (shipping or 0))


class Watcher:
    def __init__(self, cfg: dict, dry_run: bool = False, no_llm: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.no_llm = no_llm
        self.state = state_mod.load()
        self.yahoo = YahooClient(delay=cfg["search"]["request_delay_seconds"])
        self.analyzer = None if no_llm else Analyzer(cfg, self.yahoo)
        self.families = cfg["families"]
        self.market: dict[str, int | None] = {}
        self.stats = {"listings": 0, "new": 0, "candidates": 0, "triaged": 0, "verified": 0,
                      "alerts": 0, "watchlisted": 0, "deferred": 0}
        self.alerts: list[notify.Alert] = []
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- scanning
    def run(self) -> None:
        self.market = refresh_market(self.yahoo, self.cfg, self.state)
        self.comps = Comparables(self.yahoo, self.state, self.cfg["search"].get("category"))
        self.check_watchlist()
        candidates = self.scan()
        self.analyze_candidates(candidates)
        state_mod.prune(self.state)
        self.state["last_run"] = now().isoformat()
        if not self.dry_run:
            state_mod.save(self.state)
            write_watchlist_md(self.state, self.families)
        self.report()

    def scan(self) -> list[tuple[Listing, str]]:
        seen = self.state["seen"]
        pages = self.cfg["search"]["pages_first_run" if not seen else "pages_per_query"]
        queries = list(dict.fromkeys(q for f in self.families.values() for q in f.get("queries") or []))
        listings: dict[str, Listing] = {}
        for q in queries:
            for page in range(1, pages + 1):
                try:
                    batch = self.yahoo.search(q, page=page, category=self.cfg["search"].get("category"))
                except YahooError as e:
                    log.error("search %r p%d failed: %s", q, page, e)
                    break
                for l in batch:
                    listings.setdefault(l.auction_id, l)
                if len(batch) < 100:
                    break
        self.stats["listings"] = len(listings)

        candidates = []
        for l in listings.values():
            if l.end_time <= now():
                continue
            prev = seen.get(l.auction_id)
            if prev:
                self.recheck_seen_fixed_price(l, prev)
                continue
            self.stats["new"] += 1
            s = screen(l, self.families, self.market, self.cfg)
            if not s.passed:
                seen[l.auction_id] = {"end_time": l.end_time.isoformat(), "status": "screened",
                                      "reason": s.reason, "family": s.family}
                continue
            candidates.append((l, s.family))
        # Most urgent first: auctions ending soonest, then cheapest relative to market.
        candidates.sort(key=lambda c: (hours_left(c[0].end_time) > self.cfg["schedule"]["alert_window_hours"],
                                       effective_price(c[0]) / self.market[c[1]] if self.market.get(c[1]) else 0.5))
        self.stats["candidates"] = len(candidates)
        return candidates

    def recheck_seen_fixed_price(self, l: Listing, prev: dict) -> None:
        """Buy-it-now listings that were analyzed but too expensive: alert if the seller cuts the price."""
        ceiling = prev.get("ceiling")
        if not ceiling or prev.get("alerted") or not l.buy_now_price:
            return
        price = all_in(l.buy_now_price, l.shipping_fee, l.is_free_shipping)
        if price < prev.get("price", price) and price <= ceiling:
            prev["price"] = price
            self.alert("price_drop", l.auction_id, l.url, l.title, price, l.end_time, prev)
        else:
            prev["price"] = min(price, prev.get("price", price))

    # ---------------------------------------------------------------- analysis
    def analyze_candidates(self, candidates: list[tuple[Listing, str]]) -> None:
        if self.no_llm:
            for listing, family in candidates:
                log.info("candidate %s %s ¥%s %s", listing.auction_id, family, effective_price(listing), listing.title)
            return
        budget = self.cfg["llm"]["max_triage_per_run"]
        batch, deferred = candidates[:budget], candidates[budget:]
        self.stats["deferred"] += len(deferred)  # not marked seen, so they are picked up next run
        with ThreadPoolExecutor(max_workers=self.cfg["llm"].get("parallel", 4)) as pool:
            list(pool.map(self._analyze_safe, batch))

    def _analyze_safe(self, c: tuple[Listing, str]) -> None:
        listing, family = c
        try:
            self.analyze_one(listing, family)
        except YahooError as e:
            log.warning("item %s: %s", listing.auction_id, e)
            with self.lock:
                self.state["seen"][listing.auction_id] = {"end_time": listing.end_time.isoformat(),
                                                          "status": "error", "reason": str(e)}
        except Exception:
            log.exception("analysis failed for %s; will retry next run", listing.auction_id)

    def analyze_one(self, listing: Listing, family: str) -> None:
        comps = self.comps.for_listing(family, listing.title)
        if family == "epyc" and not BUNDLE_RE.search(listing.title):
            med = Comparables.epyc_cpu_median(comps)
            if med and effective_price(listing) > med * self.cfg["filters"]["max_price_to_market"]:
                with self.lock:
                    self.state["seen"][listing.auction_id] = {
                        "end_time": listing.end_time.isoformat(), "status": "screened", "family": family,
                        "reason": f"EPYC price {effective_price(listing) / med:.0%} of sold median"}
                return
        detail = self.yahoo.item(listing.auction_id)
        if detail.status != "open":
            return
        llm = self.cfg["llm"]
        bin_price = detail.buy_now_price or listing.buy_now_price
        price_now = all_in(bin_price if listing.is_fixed_price and bin_price else detail.price,
                           listing.shipping_fee, listing.is_free_shipping)
        market = self.market.get(family)
        ctx = build_context(listing, detail, self.families[family]["name"], market, price_now, comps)

        first = self.analyzer.triage(ctx, detail.images)
        with self.lock:
            self.stats["triaged"] += 1
        final: Analysis | None = first
        needs_verify = (
            first is None
            or first.verdict == "uncertain"
            or (first.verdict in ("interesting", "strong_deal") and
                (first.confidence < llm["verify_below_confidence"] or llm["verify_all_alerts"]))
        )
        if needs_verify and self.analyzer.reserve_verify():
            second = self.analyzer.verify(ctx, detail.images, first)
            with self.lock:
                self.stats["verified"] += 1
            if second is not None:
                final = second
        elif needs_verify and (first is None or first.verdict == "uncertain"):
            with self.lock:
                self.stats["deferred"] += 1  # out of Sonnet budget: retry the whole thing next run
            return
        if final is None:
            log.warning("no usable analysis for %s", listing.auction_id)
            return

        d = final.data
        models = [first.model] if first else []
        if final is not first:
            models.append(final.model)
        entry = {
            "end_time": detail.end_time.isoformat(), "status": "analyzed", "family": family,
            "title": detail.title, "url": listing.url, "verdict": final.verdict,
            "confidence": final.confidence, "summary": d["summary"], "hardware": d["identified_hardware"],
            "red_flags": d["red_flags"], "positives": d["positive_signals"], "reasoning": d["reasoning"],
            "market": d["estimated_market_value_jpy"] or market, "models": models,
            "shipping": listing.shipping_fee, "free_shipping": listing.is_free_shipping,
            "fixed_price": listing.is_fixed_price, "price": price_now,
            "image": detail.images[0] if detail.images else listing.image_url,
        }
        with self.lock:
            self.state["seen"][listing.auction_id] = entry
        log.info("%-11s %.2f %s ¥%s %s [%s]", final.verdict, final.confidence, listing.auction_id,
                 f"{price_now:,}", detail.title[:60], "+".join(models))
        if final.verdict == "pass":
            return

        ceiling = self.ceiling(d)
        entry["ceiling"] = ceiling
        hl = hours_left(detail.end_time)
        window = self.cfg["schedule"]["alert_window_hours"]
        if bin_price and all_in(bin_price, listing.shipping_fee, listing.is_free_shipping) <= ceiling:
            self.alert("buy_now", listing.auction_id, listing.url, detail.title,
                       all_in(bin_price, listing.shipping_fee, listing.is_free_shipping), detail.end_time, entry)
        elif not listing.is_fixed_price and price_now <= ceiling:
            if hl <= window:
                self.alert("ending_soon", listing.auction_id, listing.url, detail.title, price_now, detail.end_time, entry)
            else:
                with self.lock:
                    self.state["watchlist"][listing.auction_id] = entry
                    self.stats["watchlisted"] += 1

    def ceiling(self, d: dict) -> int:
        """LLM's ceiling, capped at deal_ratio x its own market estimate so 'deal' always means well below market."""
        mkt = d.get("estimated_market_value_jpy") or 0
        cap = int(mkt * self.cfg["deal"]["deal_ratio"]) if mkt else None
        c = d.get("deal_ceiling_jpy") or 0
        if cap:
            c = min(c, cap) if c > 0 else cap
        return int(c)

    # ---------------------------------------------------------------- watchlist
    def check_watchlist(self) -> None:
        sched = self.cfg["schedule"]
        for aid, e in list(self.state["watchlist"].items()):
            end = datetime.fromisoformat(e["end_time"])
            hl = hours_left(end)
            if hl <= 0:
                self.state["watchlist"].pop(aid)
                continue
            due = None
            if hl <= sched["alert_window_hours"] and not e.get("alerted"):
                due = "ending_soon"
            elif sched.get("final_call_hours") and hl <= sched["final_call_hours"] and not e.get("final_called"):
                due = "final_call"
            if not due:
                continue
            try:
                detail: ItemDetail = self.yahoo.item(aid)
            except YahooError as ex:
                log.warning("watchlist %s: %s", aid, ex)
                if "404" in str(ex):
                    self.state["watchlist"].pop(aid)
                continue
            if detail.status != "open":
                self.state["watchlist"].pop(aid)
                continue
            e["end_time"] = detail.end_time.isoformat()  # auto-extension can move it
            price = all_in(detail.price, e.get("shipping"), e.get("free_shipping"))
            e["price"] = price
            if price <= e["ceiling"]:
                if due == "final_call":
                    e["final_called"] = True
                self.alert(due, aid, e["url"], detail.title, price, detail.end_time, e)
            else:
                log.info("watchlist %s priced out: ¥%s > ceiling ¥%s", aid, f"{price:,}", f"{e['ceiling']:,}")
                self.state["watchlist"].pop(aid)

    # ---------------------------------------------------------------- output
    def alert(self, kind: str, aid: str, url: str, title: str, price: int, end: datetime, e: dict) -> None:
        a = notify.Alert(
            kind=kind, auction_id=aid, url=url, title=title, price=price, hours_left=hours_left(end),
            market=e.get("market"), ceiling=e.get("ceiling", 0), verdict=e.get("verdict", "?"),
            confidence=e.get("confidence", 0), summary=e.get("summary", ""), hardware=e.get("hardware", ""),
            red_flags=e.get("red_flags", []), positives=e.get("positives", []), reasoning=e.get("reasoning", ""),
            models=e.get("models", []), image=e.get("image"),
        )
        channels = [] if self.dry_run else notify.send(a)
        log.warning("ALERT %s %s via %s", kind, a.headline(), channels or "dry-run")
        with self.lock:
            e["alerted"] = True
            self.alerts.append(a)
            self.stats["alerts"] += 1
            self.state["alerts"].append({"at": now().isoformat(), "kind": kind, "id": aid, "price": price,
                                         "title": title, "channels": channels})

    def report(self) -> None:
        lines = ["## Yahoo! Auctions watcher run", "",
                 " · ".join(f"{k}: **{v}**" for k, v in self.stats.items()), "",
                 "| family | market (median sold) |", "|---|---|"]
        lines += [f"| {self.families[k]['name']} | {'¥{:,}'.format(v) if v else '-'} |" for k, v in self.market.items()]
        if self.alerts:
            lines += ["", "### Alerts", *[f"- [{a.headline()}]({a.url})" for a in self.alerts]]
        text = "\n".join(lines)
        log.info("stats %s", self.stats)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(text + "\n")


def write_watchlist_md(state: dict, families: dict, path: str = "WATCHLIST.md") -> None:
    rows = sorted(state["watchlist"].items(), key=lambda kv: kv[1]["end_time"])
    lines = ["# Watchlist", "",
             "Auctions that look genuine and are currently under their deal ceiling. You'll get an alert "
             "about 24h before each one ends if the price is still under the ceiling.", "",
             f"_Updated {now().strftime('%Y-%m-%d %H:%M UTC')}_", "",
             "| Ends (JST) | Item | Now | Ceiling | Market | Verdict |", "|---|---|---|---|---|---|"]
    for aid, e in rows:
        end = datetime.fromisoformat(e["end_time"]).strftime("%m-%d %H:%M")
        title = e["title"].replace("|", "/")[:70]
        lines.append(f"| {end} | [{title}]({e['url']}) | ¥{e['price']:,} | ¥{e['ceiling']:,} | "
                     f"{'¥{:,}'.format(e['market']) if e.get('market') else '-'} | {e['verdict']} |")
    if not rows:
        lines.append("| | _nothing on the watchlist right now_ | | | | |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="analyze but don't notify or save state")
    ap.add_argument("--no-llm", action="store_true", help="scrape + prefilter only; list candidates")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    Watcher(cfg, dry_run=args.dry_run, no_llm=args.no_llm).run()


if __name__ == "__main__":
    main()
