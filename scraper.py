#!/usr/bin/env python3
"""
Fetch current Cardmarket + TCGPlayer prices for every One Piece card via
cardmarket-api-tcg (RapidAPI), looping over all episode (set) IDs and all
pages within each episode, then upsert into Supabase.

Designed to run as a GitHub Action step. Reads:
    API_KEY              - RapidAPI key (repo secret)
    SUPABASE_URL          - your project URL (repo secret)
    SUPABASE_SERVICE_KEY  - service role key, needed to bypass RLS on write
                            (repo secret; NEVER use the anon key here)

Usage:
    API_KEY=xxx SUPABASE_URL=xxx SUPABASE_SERVICE_KEY=xxx python fetch_one_piece_prices.py

Requires:
    pip install requests supabase
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

# --- Config -----------------------------------------------------------------

API_HOST = "cardmarket-api-tcg.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}/one-piece/episodes/{{episode_id}}/cards"
PER_PAGE = 100

# 30 req/minute limit on Basic plan -> stay comfortably under it.
REQUEST_DELAY_SECONDS = 2.1

# Hardcoded expected request count as a sanity guard. Update this if new
# One Piece sets are released and you add their episode IDs.
EXPECTED_MAX_REQUESTS = 80

# Supabase table + upsert conflict target (must match the unique constraint).
TABLE_NAME = "price_snapshots"
ON_CONFLICT = "card_number,episode_id,snapshot_date"
UPSERT_BATCH_SIZE = 500

# Local backup of the raw fetch, in case the Supabase step fails and you
# want to re-run just the upsert without re-hitting the API.
OUTPUT_PATH = "one_piece_prices.json"

# All current One Piece episode (set) IDs.
EPISODE_IDS = [
    348, 349, 350, 351, 352, 353, 354, 355, 356, 357, 358, 359, 360, 361,
    362, 363, 364, 365, 366, 367, 368, 369, 370, 371, 372, 373, 374, 375,
    376, 377, 378, 379, 380, 381, 382, 383, 384, 385, 386, 387, 388, 389,
    390, 391, 392, 393, 394, 395, 404, 416, 417, 418, 430,
]


# --- Fetching -----------------------------------------------------------------

def get_headers() -> dict:
    api_key = os.environ.get("API_KEY")
    if not api_key:
        print("ERROR: API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return {
        "Content-Type": "application/json",
        "x-rapidapi-host": API_HOST,
        "x-rapidapi-key": api_key,
    }


def fetch_episode_cards(episode_id: int, headers: dict, request_count: list) -> list:
    """Fetch all pages of cards for a single episode, return list of
    {episode_id, episode_code, card_number, prices} dicts."""
    cards = []
    page = 1
    total_pages = 1  # updated after first response

    while page <= total_pages:
        url = BASE_URL.format(episode_id=episode_id)
        params = {
            "per_page": PER_PAGE,
            "page": page,
            "sort": "price_highest",
        }

        response = None
        for attempt in range(3):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=20)
                request_count[0] += 1
                if response.status_code == 200:
                    break
                if response.status_code == 429:
                    print(f"  [episode {episode_id}] rate limited, backing off...")
                    time.sleep(5)
                    continue
                print(f"  [episode {episode_id}] HTTP {response.status_code}, retrying...")
                time.sleep(2)
            except requests.RequestException as exc:
                print(f"  [episode {episode_id}] request error: {exc}, retrying...")
                time.sleep(2)
        else:
            print(f"  [episode {episode_id}] page {page} failed after retries, skipping.")
            break

        if response is None or response.status_code != 200:
            break

        payload = response.json()
        data = payload.get("data", [])
        paging = payload.get("paging", {})
        total_pages = paging.get("total", 1)

        for card in data:
            episode = card.get("episode") or {}
            cards.append({
                "episode_id": episode_id,
                "episode_code": episode.get("code"),
                "card_number": card.get("card_number"),
                "prices": card.get("prices", {}),
            })

        print(f"  [episode {episode_id}] page {page}/{total_pages} -> {len(data)} cards")

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return cards


# --- Transform -----------------------------------------------------------------

def extract_market_prices(prices: dict) -> dict:
    """Pull out the flat current-price fields from the raw prices block.
    Handles both 'tcgplayer' and 'tcg_player' key variants seen across
    different games on this API."""
    cardmarket = prices.get("cardmarket") or {}
    tcgplayer = prices.get("tcgplayer") or prices.get("tcg_player") or {}

    return {
        "cardmarket_price": cardmarket.get("lowest_near_mint"),
        "cardmarket_currency": cardmarket.get("currency"),
        "tcgplayer_price": tcgplayer.get("market_price"),
        "tcgplayer_currency": tcgplayer.get("currency"),
    }


def build_rows(all_cards: list, snapshot_date: str) -> list:
    rows = []
    for card in all_cards:
        if not card.get("card_number"):
            continue  # skip anything without a card number, nothing to key on
        market = extract_market_prices(card["prices"])
        rows.append({
            "card_number": card["card_number"],
            "episode_id": card["episode_id"],
            "episode_code": card.get("episode_code"),
            "raw_prices": card["prices"],
            "snapshot_date": snapshot_date,
            **market,
        })
    return rows


# --- Supabase -----------------------------------------------------------------

def get_supabase_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not set.", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


def upsert_rows(client, rows: list):
    total = len(rows)
    for i in range(0, total, UPSERT_BATCH_SIZE):
        batch = rows[i:i + UPSERT_BATCH_SIZE]
        client.table(TABLE_NAME).upsert(batch, on_conflict=ON_CONFLICT).execute()
        print(f"  upserted rows {i + 1}-{i + len(batch)} of {total}")


# --- Main -----------------------------------------------------------------

def main():
    headers = get_headers()
    request_count = [0]  # mutable int, shared across calls
    all_cards = []

    print(f"Fetching prices for {len(EPISODE_IDS)} episodes...")

    for episode_id in EPISODE_IDS:
        cards = fetch_episode_cards(episode_id, headers, request_count)
        all_cards.extend(cards)

    print(f"\nTotal API requests made: {request_count[0]}")
    if request_count[0] > EXPECTED_MAX_REQUESTS:
        print(
            f"WARNING: request count ({request_count[0]}) exceeded expected "
            f"guard ({EXPECTED_MAX_REQUESTS}). Check for a runaway loop or "
            f"new sets before this becomes a billing surprise.",
            file=sys.stderr,
        )

    # Local backup, useful for debugging / re-running the upsert alone.
    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_date": snapshot_date,
            "card_count": len(all_cards),
            "cards": all_cards,
        }, f, indent=2)
    print(f"Wrote raw backup ({len(all_cards)} cards) to {OUTPUT_PATH}")

    rows = build_rows(all_cards, snapshot_date)
    print(f"Prepared {len(rows)} rows for upsert (snapshot_date={snapshot_date})")

    client = get_supabase_client()
    upsert_rows(client, rows)

    print("Done.")


if __name__ == "__main__":
    main()
