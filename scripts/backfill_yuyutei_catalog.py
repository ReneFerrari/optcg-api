"""
Yuyutei catalog ingest — INSERT new ptcg_cards rows for cards that exist
in Yuyutei's per-set listings but are missing from D1 entirely.

Sibling to scripts/backfill_yuyutei_jp.py (which is the UPDATE consumer).
Both consumers share scripts/lib/yuyutei_scraper.py.

The 2026-05-18 Yuyutei audit found 4,118 priced Yuyutei products mapping
to TCGdex set IDs but only 1,058 of those have a corresponding D1 row —
TCGdex's import has never picked up the remaining ~2,454 (mostly modern
JA sets where TCGdex hasn't ingested the era's promos yet). This script
closes that gap.

INSERT shape carries everything Yuyutei gives us:
  card_id, lang='ja', set_id, local_id (unpadded), name (JA from <h4>),
  image_high, image_low (both = Yuyutei 100x140 thumb),
  pricing_json ({"yuyutei": {...}}), price_source ('yuyutei' or NULL).

Other denormalized fields (category/rarity/hp/types_csv/stage) stay NULL
— TCGdex eventual UPSERT fills them when (if) TCGdex catalogs the card.

Per-apply rollback: the list of card_ids INSERTed lands in
data/backfill/yuyutei_catalog_inserted_<YYYYMMDD-HHMMSS>.txt before
wrangler runs. Exact rollback: DELETE WHERE lang='ja' AND card_id IN (
the list).

Usage:
    python -m scripts.backfill_yuyutei_catalog --dry-run
    python -m scripts.backfill_yuyutei_catalog --set=SV10 --dry-run
    python -m scripts.backfill_yuyutei_catalog --limit=3 --dry-run
    python -m scripts.backfill_yuyutei_catalog --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from scripts.lib.yuyutei_scraper import (
    IMAGE_HOST,
    REQ_INTERVAL_S,
    USER_AGENT,
    build_card_id_candidates,
    get_jpy_to_usd_rate,
    load_mapping,
    scrape_set_listing,
)
from scripts.wrangler_retry import WRANGLER_MAX_ATTEMPTS, run_wrangler


DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
OUT_DIR = Path("scripts/insert_promo_rows")
ROLLBACK_DIR = Path("data/backfill")
BATCH_SIZE = 100  # rows per multi-statement SQL file
TARGET_LANG = "ja"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", help="Only run this TCGdex set id (e.g. SV10)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of TCGdex sets processed (smoke tests)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + diff + write SQL. Don't touch D1.")
    g.add_argument("--apply", action="store_true",
                   help="Fetch + parse + diff + write SQL AND run wrangler.")
    args = ap.parse_args()

    # Body fills in subsequent tasks.
    print(f"args: dry_run={args.dry_run} apply={args.apply} "
          f"set={args.set!r} limit={args.limit}")


if __name__ == "__main__":
    main()
