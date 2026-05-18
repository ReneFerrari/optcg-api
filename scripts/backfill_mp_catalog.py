"""
Catch up the M-P (MEGA Evolution era) Japanese promo set in
ptcg_cards. Bulbapedia's master page "M-P Promotional cards (TCG)" is
the canonical enumeration source — currently lists 100 setlist entries
distributed via Pokémon Card Gym Promo Card Packs across 2025–2026.
Our D1 typically lags because the MEGA era is still rolling out and
TCGdex doesn't always have the latest entries by Monday's refresh.

This script INSERTs placeholder rows for any (set_id=MP, local_id=N)
that exists on Bulbapedia but not in D1. Placeholders carry:

  card_id = "MP-{N}"     (matches existing convention, unpadded)
  lang    = "ja"
  set_id  = "MP"
  local_id = str(N)
  name    = <English name from Bulbapedia>   (placeholder, see note)
  name_en = <English name from Bulbapedia>

NULL for everything else — TCGdex's eventual import is UPSERT-shaped
(ON CONFLICT(card_id,lang) DO UPDATE SET ...) so when MEGA cards land
upstream the placeholder gets overwritten with the proper JA name +
types_csv + hp + image_high. INSERT OR IGNORE here makes that future
overwrite a no-op for our placeholders that have already been
upgraded.

Why the English name in the JA `name` column: `name` is NOT NULL and
Bulbapedia doesn't ship a free machine-readable JA name on the master
page (the JA form lives only in the per-card page wikitext, 100x extra
API calls). Placeholder-with-EN is the pragmatic floor — better than
"Unknown" because OPBindr's search still hits, and TCGdex's UPSERT
corrects it within a refresh cycle.

The MEP false-positive guard: Bulbapedia's broader Category:M-P
Promotional cards is contaminated with `(MEP Promo NNN)` rows (a
different promo set) and main-set canonical-page reprints — only 5 of
57 category members are real M-P pages. We sidestep the contamination
entirely by using the master setlist page, which is set-clean by
construction (every {{Setlist/entry|NNN/M-P|...}} is an M-P row).

Output: scripts/insert_promo_rows/mp_catchup_<NNN>.sql

Usage:
    python -m scripts.backfill_mp_catalog --dry-run
    python -m scripts.backfill_mp_catalog --apply
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("scripts/insert_promo_rows")
BATCH_SIZE = 100  # rows per multi-statement file
WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
BULBAPEDIA_API = "https://bulbapedia.bulbagarden.net/w/api.php"
USER_AGENT = "OPBindr-Bot/1.0 (contact: arjun@neuroplexlabs.com)"
RATE_LIMIT_SECONDS = 1.1
MASTER_PAGE = "M-P Promotional cards (TCG)"
TARGET_SET_ID = "MP"
TARGET_LANG = "ja"

# Setlist row leader. We require /M-P| immediately after the LID so a
# stray {{Setlist/entry|...|other-set|...}} entry (Bulbapedia sometimes
# embeds cross-references) can't pollute the M-P enumeration. The
# trailing | guarantees we stop on the M-P token boundary and don't
# accidentally match a longer token like M-PROMO.
_SETLIST_RE = re.compile(r"^\{\{Setlist/(?:entry|nmentry)\|(\d+)/M-P\|")

# Card name from {{TCG ID|<setname>|<cardname>|<num>...}}.
# Group 1 captures the cardname (3rd template arg).
_TCG_ID_RE = re.compile(r"\{\{TCG ID\|[^|]+\|([^|}]+)")

# Wikilink fallback for entries shaped [[<name> (M-P Promo N)|<display>]]{{ex}}.
# Group 1 captures the page title before " (M-P Promo".
_WIKILINK_NAME_RE = re.compile(r"\[\[([^|\]]+?)\s*\(M-P\s+Promo")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + diff + write SQL. Don't touch D1.")
    g.add_argument("--apply", action="store_true",
                   help="Fetch + parse + diff + write SQL AND run wrangler.")
    args = ap.parse_args()

    print(f"1. Fetching Bulbapedia master page {MASTER_PAGE!r}...")
    wt = _fetch_page_wikitext(MASTER_PAGE)
    print(f"   wikitext length: {len(wt)} chars")

    print("2. Parsing setlist entries...")
    parsed = _parse_setlist(wt)
    print(f"   parsed {len(parsed)} M-P entries "
          f"(LID range {min(p[0] for p in parsed) if parsed else '-'}.."
          f"{max(p[0] for p in parsed) if parsed else '-'})")
    if not parsed:
        print("Nothing parsed — abort.")
        sys.exit(1)

    print("3. Querying D1 for existing MP/ja LIDs...")
    existing = _fetch_existing_mp_lids()
    print(f"   D1 has {len(existing)} MP/ja rows already")

    missing = [(lid, name) for lid, name in parsed if lid not in existing]
    print(f"   {len(missing)} new row(s) to INSERT")
    if not missing:
        print("Caught up. Nothing to do.")
        return

    print("4. Writing INSERT OR IGNORE batches...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = _write_batches(missing)
    print(f"   wrote {len(files)} batch file(s) to {OUT_DIR}/")

    if args.dry_run:
        print("\nDry run done. Inspect scripts/insert_promo_rows/*.sql, "
              "then re-run with --apply.")
        return

    print("5. Applying batches against remote D1...")
    for i, f in enumerate(files, 1):
        print(f"   [{i}/{len(files)}] executing {f.name}...")
        result = subprocess.run(
            WRANGLER + [f"--file={f}", "--remote"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"   FAIL: {(result.stderr or '')[:400]}")
            sys.exit(1)
    print("Done.")


def _fetch_page_wikitext(page: str) -> str:
    data = _api_get({
        "action": "parse",
        "page": page,
        "prop": "wikitext",
        "format": "json",
    })
    return data.get("parse", {}).get("wikitext", {}).get("*", "")


def _parse_setlist(wikitext: str) -> list[tuple[int, str]]:
    """Pull (local_id, English card name) pairs out of every
    {{Setlist/entry|NNN/M-P|...}} line. Skip lines that don't carry
    a recognizable card name template.
    """
    out: list[tuple[int, str]] = []
    skipped_no_name = 0
    for raw_line in wikitext.split("\n"):
        line = raw_line.strip()
        m = _SETLIST_RE.match(line)
        if not m:
            continue
        try:
            lid = int(m.group(1))
        except ValueError:
            continue
        name = _extract_name(line)
        if not name:
            skipped_no_name += 1
            continue
        out.append((lid, name))
    if skipped_no_name:
        print(f"     warn: {skipped_no_name} setlist row(s) had no "
              f"parseable card name — skipped")
    return out


def _extract_name(setlist_line: str) -> str:
    m = _TCG_ID_RE.search(setlist_line)
    if m:
        return m.group(1).strip()
    m = _WIKILINK_NAME_RE.search(setlist_line)
    if m:
        return m.group(1).strip()
    return ""


def _fetch_existing_mp_lids() -> set[int]:
    """Return the set of LIDs (as ints) we already have for MP/ja."""
    cmd = WRANGLER + [
        "--remote",
        "--json",
        "--command",
        f"SELECT CAST(local_id AS INTEGER) AS lid FROM ptcg_cards "
        f"WHERE UPPER(set_id) = '{TARGET_SET_ID}' AND lang = '{TARGET_LANG}'",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(f"   FAIL fetching existing LIDs: "
              f"{(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        print(f"   FAIL parsing wrangler JSON: {e}\n--- payload (head) ---\n"
              f"{payload[:400]}")
        sys.exit(1)
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return {int(r["lid"]) for r in rows if r.get("lid") is not None}


def _strip_wrangler_chrome(stdout: str) -> str:
    """Wrangler prints a config-warning banner before the JSON. Find the
    first '[' or '{' and slice from there."""
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


def _write_batches(rows: list[tuple[int, str]]) -> list[Path]:
    files: list[Path] = []
    cols = ("card_id", "lang", "set_id", "local_id", "name", "name_en")
    values: list[str] = []
    for lid, name in rows:
        card_id = f"{TARGET_SET_ID}-{lid}"
        local_id_s = str(lid)
        values.append(
            "(" + ", ".join([
                _esc(card_id),
                _esc(TARGET_LANG),
                _esc(TARGET_SET_ID),
                _esc(local_id_s),
                _esc(name),
                _esc(name),
            ]) + ")"
        )
    for i in range(0, len(values), BATCH_SIZE):
        chunk = values[i:i + BATCH_SIZE]
        idx = (i // BATCH_SIZE) + 1
        path = OUT_DIR / f"mp_catchup_{idx:03d}.sql"
        path.write_text(
            f"INSERT OR IGNORE INTO ptcg_cards "
            f"({', '.join(cols)}) VALUES\n"
            + ",\n".join(chunk) + ";\n",
            encoding="utf-8",
        )
        files.append(path)
    return files


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _api_get(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BULBAPEDIA_API}?{qs}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"   HTTP {e.code} from Bulbapedia: {e.reason}")
        sys.exit(1)
    finally:
        time.sleep(RATE_LIMIT_SECONDS)


if __name__ == "__main__":
    main()
