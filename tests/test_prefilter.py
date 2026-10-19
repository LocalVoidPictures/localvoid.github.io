from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from watcher.market import robust_median
from watcher.prefilter import detect_family, screen
from watcher.yahoo import Listing

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
FAM = CFG["families"]


@pytest.mark.parametrize("title,family", [
    ("ZOTAC GAMING GeForce RTX 4090 Trinity 24GB", "rtx4090"),
    ("ZOTAC RTX 4090 D TRINITY", "rtx4090d"),
    ("MSI GeForce RTX 3090 Ti SUPRIM X 24G", "rtx3090ti"),
    ("GeForce RTX 3090 Founders Edition", "rtx3090"),
    ("玄人志向 RTX4080S 16GB", "rtx4080super"),
    ("RTX 4080 SUPER 16GB", "rtx4080super"),
    ("ASUS TUF RTX4080 16GB", "rtx4080"),
    ("ASUS ROG Astral GeForce RTX 5090 32GB", "rtx5090"),
    ("MSI RTX 5070 Ti 16G VENTUS", "rtx5070ti"),
    ("NVIDIA RTX A6000 48GB", "rtxa6000"),
    ("RTX 6000 Ada Generation 48GB", "rtx6000ada"),
    ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB", "rtxpro6000"),
    ("AMD EPYC 7763 64コア", "epyc"),
    # not targets
    ("DELL OptiPlex 5090 SFF Core i5", None),
    ("◎(D4090) HITACHI DVDマルチドライブ", None),
    ("New Core i7 14700K/RTX4070（RTX3090同等&省エネ）/32GB", None),
    ("GeForce GTX 1080Ti 検 RTX 1070 2070 2080 3070 3080 3090 4070 4080 4090 5070 5080 5090", None),
])
def test_detect_family(title, family):
    assert detect_family(title, FAM) == family


def _listing(title, price, fixed=False):
    now = datetime.now(timezone.utc)
    return Listing(
        auction_id="x1", title=title, price=price, buy_now_price=price if fixed else None, bid_count=0,
        start_time=now, end_time=now + timedelta(days=3), is_fixed_price=fixed, condition=None,
        category_path=["オークション", "コンピュータ"], seller_id=None, seller_rating="99%", seller_is_store=False,
        shipping_fee=None, is_free_shipping=True, watch_count=0, image_url=None,
    )


MARKET = {k: f.get("fallback_market_jpy") for k, f in FAM.items()}


@pytest.mark.parametrize("title,price,fixed,passed", [
    ("MSI GeForce RTX 4090 SUPRIM X 24G", 1, False, True),                 # 1円 start auction: judge item, not price
    ("MSI GeForce RTX 4090 SUPRIM X 24G", 250000, True, True),             # ~58% of market buy-now
    ("MSI GeForce RTX 4090 SUPRIM X 24G", 420000, True, False),            # market price: skip
    ("MSI GeForce RTX 4090 SUPRIM X 24G", 9000, True, False),              # bait-level buy-now
    ("RTX 4090 ジャンク 映像出力なし", 30000, False, False),
    ("ZOTAC RTX5090クーラーと箱 部品交換用", 5000, True, False),
    ("RTX 3090 3080 Suprim X用 水冷ブロック", 20000, True, False),
    ("ASUS ROG Zephyrus 16インチ RTX 4090 Laptop", 300000, False, False),
    ("田中様専用 MSI RTX 5090 VENTUS", 900000, True, False),
    ("ゲーミングPC i9 13900KF RTX4090 64GB", 500000, True, True),          # whole PC can exceed bare GPU price
])
def test_screen(title, price, fixed, passed):
    assert screen(_listing(title, price, fixed), FAM, MARKET, CFG).passed is passed


def test_robust_median_ignores_parts_and_outliers():
    prices = [2000, 5000, 7250, 400000, 420000, 430000, 435000, 440000, 450000, 3_000_000]
    assert 420000 <= robust_median(prices, 430000) <= 440000
    assert robust_median([400000, 410000], 430000) is None
