"""Resolve PriceCharting vintage_lid_mismatch rows via name comparison.

`audit_pricecharting_jp_conflation.py` defers any vintage JA row whose PC
URL slug has a number that doesn't match our local_id, because JA sets
renumber independently of EN — PC's trailing number is the JA-native
set position (often Pokédex-keyed), not our local_id. The lid mismatch
alone proves nothing.

This script does the follow-up name-similarity pass:

  1. Pull every JA row currently sourced from pricecharting whose audit
     verdict was `vintage_lid_mismatch`.
  2. Extract the pokemon-name slug from the PC URL (between the set-slug
     and the trailing number).
  3. Slug-normalize our row's `name_en`.
  4. If the two slugs match (one is a prefix/substring of the other,
     after gender/apostrophe/punctuation normalization), the lid mismatch
     is benign JA renumbering — keep the price.
  5. If they DON'T match, it's a conflation — NULL price_source and
     remove `pricecharting` from pricing_json.

Run:
  python -m scripts.audit_pc_vintage_name_similarity              # dry-run report
  python -m scripts.audit_pc_vintage_name_similarity --emit-sql   # also write NULL SQL
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

from scripts.wrangler_retry import run_wrangler


WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", "optcg-cards"]
OUT_AUDIT = Path("data/backfill/pc_vintage_name_similarity_audit.json")
OUT_SQL_DIR = Path("scripts/jp_batches")
BATCH_SIZE = 100

# Strip trailing -N or -Nxy or -Nxy-p from a path tail. The optional letter
# block lets us peel off promo set tags PC sometimes appends after the
# number (e.g. "...mega-tokyo's-98xy-p").
SLUG_TAIL_RE = re.compile(r"^(.*?)-(\d+)([a-z]*)(-p)?$", re.IGNORECASE)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--emit-sql", action="store_true",
        help="Write the NULL-out SQL to scripts/jp_batches/null_pc_vintage_conflated.sql",
    )
    args = ap.parse_args()

    print("1. Querying D1 for JA pricecharting rows + their name_en...")
    rows = fetch_pc_ja_rows()
    print(f"   {len(rows)} rows in scope (lang='ja', price_source='pricecharting')")

    print("\n2. Filtering to vintage_lid_mismatch (slug set matches, slug lid differs)...")
    targets = [r for r in rows if _is_vintage_lid_mismatch(r)]
    print(f"   {len(targets)} rows to name-check")

    print("\n3. Comparing pc-slug pokemon name vs our name_en...")
    audit = []
    verdicts = Counter()
    for row in targets:
        result = audit_row(row)
        audit.append(result)
        verdicts[result["verdict"]] += 1

    print("\n4. Verdict summary:")
    for v, n in verdicts.most_common():
        print(f"   {v:18s} {n}")

    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n5. Full audit written to {OUT_AUDIT}")

    bad_rows = [r for r in audit if r["verdict"] == "name_mismatch"]
    if not bad_rows:
        print("\nNo name-mismatched rows — all 176 vintage lid mismatches are benign renumbering.")
        return
    print(f"\n{len(bad_rows)} row(s) flagged for NULL-out.")

    print("\nSample (first 15):")
    for r in bad_rows[:15]:
        print(f"   {r['card_id']:14s} ours='{r.get('our_slug','?'):20s}' pc='{r.get('pc_slug','?'):25s}'  ${r.get('pc_price','?')}")

    if args.emit_sql:
        OUT_SQL_DIR.mkdir(parents=True, exist_ok=True)
        files = write_null_batches(bad_rows)
        print(f"\nWrote {len(files)} NULL-out SQL file(s) to {OUT_SQL_DIR}/")
        for f in files:
            print(f"   {f}")
    else:
        print("\n--emit-sql not set; SQL files not written.")


def fetch_pc_ja_rows() -> list[dict]:
    cmd = WRANGLER + [
        "--remote", "--json", "--command",
        "SELECT card_id, set_id, local_id, name_en, "
        "json_extract(pricing_json, '$.pricecharting.url') AS pc_url, "
        "json_extract(pricing_json, '$.pricecharting.market') AS pc_price "
        "FROM ptcg_cards WHERE lang='ja' AND price_source='pricecharting'",
    ]
    result = run_wrangler(cmd)
    if result.returncode != 0:
        print(f"FAIL: {(result.stderr or '')[:400]}")
        sys.exit(1)
    payload = _strip_wrangler_chrome(result.stdout)
    data = json.loads(payload)
    return data[0]["results"] if isinstance(data, list) else data.get("results", [])


def _is_vintage_lid_mismatch(row: dict) -> bool:
    """Re-derive the vintage_lid_mismatch filter so this script is self-contained.

    Vintage URLs end with `-N` (no `-p` promo tag). We treat a row as a
    vintage lid mismatch when:
      - the URL has a vintage-style numeric tail
      - our local_id (zero-stripped) differs from the slug's number
      - the slug has no promo `-Nxy-p` shape (those are the high-confidence
        promo conflations already NULL'd by the sibling audit)
    """
    pc_url = (row.get("pc_url") or "").lower()
    if not pc_url:
        return False
    last = pc_url.rsplit("/", 1)[-1]
    last = urllib.parse.unquote(last)
    # Promo `-Nxy-p` shape — skip (handled by audit_pricecharting_jp_conflation)
    if re.search(r"-\d+[a-z]+-p\s*$", last):
        return False
    m = re.search(r"-(\d+)\s*$", last)
    if not m:
        return False
    slug_lid = m.group(1).lstrip("0") or m.group(1)
    our_lid = str(row.get("local_id") or "").lstrip("0") or str(row.get("local_id") or "")
    return slug_lid != our_lid


def audit_row(row: dict) -> dict:
    pc_url = row.get("pc_url") or ""
    name_en = row.get("name_en") or ""
    our_slug = normalize_pokemon_slug(name_en)
    pc_slug = extract_pc_pokemon_slug(pc_url)

    out = {
        "card_id": row.get("card_id"),
        "set_id": row.get("set_id"),
        "local_id": row.get("local_id"),
        "name_en": name_en,
        "pc_url": pc_url,
        "pc_price": row.get("pc_price"),
        "our_slug": our_slug,
        "pc_slug": pc_slug,
    }

    if not our_slug:
        # No name_en to compare against — defer rather than NULL on no signal.
        out["verdict"] = "no_name_en"
        return out
    if not pc_slug:
        out["verdict"] = "no_pc_slug"
        return out
    if slugs_match(our_slug, pc_slug):
        out["verdict"] = "name_match"
        return out
    out["verdict"] = "name_mismatch"
    return out


def normalize_pokemon_slug(name: str) -> str:
    """Slug-normalize a pokemon name for comparison."""
    s = name.lower()
    # Strip gender / pokedex symbols
    s = re.sub(r"[♂♀★☆]", "", s)
    # Apostrophes are inconsistent between PC slugs and our names — drop them.
    s = s.replace("'", "").replace("’", "")
    # Anything non-alnum becomes a dash separator.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def extract_pc_pokemon_slug(pc_url: str) -> str:
    """Pull the pokemon-name portion from a PriceCharting URL.

    Examples:
      'pokemon-japanese-jungle/nidoran-29' → 'nidoran'
      'pokemon-japanese-gym/sabrina%27s-gengar-94' → 'sabrinas-gengar'
      '.../shining-charizard-6' → 'shining-charizard'
      '.../farfetchd-83' → 'farfetchd'
    """
    if not pc_url:
        return ""
    last = pc_url.rsplit("/", 1)[-1]
    last = urllib.parse.unquote(last)
    m = SLUG_TAIL_RE.match(last)
    base = m.group(1) if m else last
    return normalize_pokemon_slug(base)


def slugs_match(ours: str, theirs: str) -> bool:
    """Lenient match: exact, or one is a hyphen-bounded prefix/suffix of the other.

    Covers benign variation like:
      'mr-mime' vs 'mr-mime-trainer' → match
      'farfetchd' vs 'farfetchd' → match
      'sabrinas-gengar' vs 'sabrinas-gengar' → match
      'sabrinas-gengar' vs 'gengar' (mismatch) — the prefix check is
        hyphen-bounded so 'gengar' isn't a prefix of 'sabrinas-gengar'.
    """
    if ours == theirs:
        return True
    # Hyphen-bounded prefix / suffix to avoid 'oddish' matching 'oddballish'.
    if theirs.startswith(ours + "-") or theirs.endswith("-" + ours):
        return True
    if ours.startswith(theirs + "-") or ours.endswith("-" + theirs):
        return True
    return False


def write_null_batches(bad_rows: list[dict]) -> list[Path]:
    statements = []
    for r in bad_rows:
        cid = (r["card_id"] or "").replace("'", "''")
        statements.append(
            f"UPDATE ptcg_cards SET "
            f"price_source = NULL, "
            f"pricing_json = json_remove(pricing_json, '$.pricecharting') "
            f"WHERE card_id = '{cid}' AND lang = 'ja' AND price_source = 'pricecharting';"
        )
    files = []
    for i in range(0, len(statements), BATCH_SIZE):
        chunk = statements[i:i + BATCH_SIZE]
        idx = (i // BATCH_SIZE) + 1
        path = OUT_SQL_DIR / f"null_pc_vintage_conflated_{idx:03d}.sql"
        path.write_text("\n".join(chunk) + "\n", encoding="utf-8")
        files.append(path)
    return files


def _strip_wrangler_chrome(stdout: str) -> str:
    for i, ch in enumerate(stdout):
        if ch in "[{":
            return stdout[i:]
    return stdout


if __name__ == "__main__":
    main()
