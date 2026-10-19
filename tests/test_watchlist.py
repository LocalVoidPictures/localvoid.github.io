from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import watcher.main as wm
from watcher.yahoo import ItemDetail

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))


def _watcher(tmp_path, monkeypatch, price, hours_left):
    monkeypatch.setattr(wm.state_mod, "STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(wm.notify, "send", lambda a: sent.append(a) or ["test"])
    w = wm.Watcher(CFG, no_llm=True)
    end = datetime.now(timezone.utc) + timedelta(hours=hours_left)
    w.state["watchlist"]["a1"] = {
        "end_time": end.isoformat(), "url": "u", "title": "RTX 4090", "ceiling": 300000, "market": 430000,
        "verdict": "interesting", "confidence": 0.9, "shipping": 1500, "free_shipping": False,
    }
    detail = ItemDetail(auction_id="a1", title="RTX 4090", price=price, buy_now_price=None, bids=3,
                        end_time=end, status="open", condition_name=None, description="")
    monkeypatch.setattr(w.yahoo, "item", lambda aid: detail)
    return w, sent


def test_alerts_when_still_cheap_near_end(tmp_path, monkeypatch):
    w, sent = _watcher(tmp_path, monkeypatch, price=200000, hours_left=20)
    w.check_watchlist()
    assert [a.kind for a in sent] == ["ending_soon"] and sent[0].price == 201500
    w.check_watchlist()  # no duplicate alert on the next run
    assert len(sent) == 1


def test_drops_when_priced_out(tmp_path, monkeypatch):
    w, sent = _watcher(tmp_path, monkeypatch, price=350000, hours_left=20)
    w.check_watchlist()
    assert not sent and "a1" not in w.state["watchlist"]


def test_waits_until_window(tmp_path, monkeypatch):
    w, sent = _watcher(tmp_path, monkeypatch, price=1000, hours_left=60)
    w.check_watchlist()
    assert not sent and "a1" in w.state["watchlist"]


def test_ceiling_capped_at_deal_ratio(tmp_path, monkeypatch):
    w, _ = _watcher(tmp_path, monkeypatch, price=1, hours_left=60)
    assert w.ceiling({"estimated_market_value_jpy": 400000, "deal_ceiling_jpy": 380000}) == 300000
    assert w.ceiling({"estimated_market_value_jpy": 400000, "deal_ceiling_jpy": 200000}) == 200000
    assert w.ceiling({"estimated_market_value_jpy": 400000, "deal_ceiling_jpy": 0}) == 300000
