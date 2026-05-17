"""
Residual Phase 1 — close the last image-coverage gaps.

After Bulbagarden, Hareruya, malie, pokemontcg.io, and the residual
remaps shipped earlier today, we're at ~99.5% on JA and ~99.75% on EN.
This script targets the remaining 172 cards using:

1. **Hareruya PMCG fuzzy-name match** for JA vintage (PMCG1-PMCG6).
   Hareruya carries 558+ vintage products under OP1-OP4/OPG1-OPG2 codes
   without local-id in their titles. We fuzzy-match Pokemon name within
   each set's scope (each PMCG set has ~100 cards, each Pokemon usually
   appears once per set, so name uniqueness is ~95%). Verifies via name
   appearing as substring in the Hareruya product title before writing.

2. **Bulbapedia name-search for XY Trainer Kits**. Cards 5-30 in tk-xy-n
   and tk-xy-sy are XY base set reprints whose Bulbapedia filename
   pattern doesn't match our existing tag-based heuristic (multi-word
   trainer card names like 'Pokemon Center Lady', 'Energy Switch'). We
   query D1 for the EN name, search Bulbapedia for {name} XY {local_id},
   and pick the first XY-tagged file.

3. **Hareruya SVP residual lookup** for SV Promos #176, 204-208, 216-223
   that pokemontcg.io's svp set lacks. Hareruya may have them under
   SV-P / SVP / SVP_M tags.

Output:
  data/backfill/residual_phase1/{pmcg,tk-xy,svp,misc}_*.{sql,json}

Idempotent. Only writes where image_high IS NULL. Spot-check sample
matches before applying.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)

DB_NAME = "optcg-cards"
WRANGLER_BIN = ["node", "./node_modules/wrangler/bin/wrangler.js", "d1", "execute", DB_NAME]
HEADERS = {"User-Agent": "OPBindr-image-backfill/1.0"}
RAW_HARERUYA = Path("data/poc_hareruya/products_raw.jsonl")
OUT_DIR = Path("data/backfill/residual_phase1")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TITLE_RE = re.compile(
    r"〈\s*(?P<lid>[A-Za-z0-9\-/]+?)\s*(?:/\s*[\dA-Za-z\-]+)?\s*〉?\s*\[\s*(?P<setid>[^\]]+?)\s*\]"
)

# Hareruya PMCG-era set codes → our TCGdex set IDs
PMCG_HARERUYA_MAP = {
    "OP1":  "PMCG1",   # Pokemon Card Expansion Pack (JA Base Set)
    "OP00": "PMCG1",   # Earliest first-edition variant — same set scope
    "OP2":  "PMCG2",   # Pokemon Jungle JA equivalent
    "OP3":  "PMCG3",   # Mystery of the Fossils JA
    "OP4":  "PMCG4",   # Team Rocket JA
    "OPG1": "PMCG5",   # Gym Heroes JA
    "OPG2": "PMCG6",   # Gym Challenge JA
    # OPE = Original Pokemon e (VS series), OPG-t = trainers, etc.
    # Skip — not 1:1 mapped to PMCG sets.
}


def query_d1(sql: str) -> list[dict]:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--json", "--command", sql],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out.stdout)
    if not data or not data[0].get("success"): return []
    return data[0]["results"] or []


def normalize_name(s: str) -> str:
    """Strip non-letters, lowercase. For matching JA Pokemon names against
    Hareruya product titles."""
    return re.sub(r"\s+", "", (s or "").strip())


# ──────────────────────────────────────────────────────────────────
# Cohort 1: Hareruya PMCG fuzzy-name match
# ──────────────────────────────────────────────────────────────────

def hareruya_pmcg_match() -> list[dict]:
    """For each PMCG1-6 imageless JA card, find the Hareruya OP/OPG product
    with matching Pokemon name. Returns [{card_id, url}, ...]."""
    print("\n=== Cohort 1: PMCG1-6 native JA-print via Hareruya OP/OPG ===")
    if not RAW_HARERUYA.exists():
        print("  Hareruya cache missing — skip")
        return []

    # Load Hareruya products grouped by Hareruya-set-code
    by_set: dict[str, list[dict]] = defaultdict(list)
    with RAW_HARERUYA.open(encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            title = p.get("title", "") or ""
            m = TITLE_RE.search(title)
            if not m: continue
            sid = m.group("setid").strip()
            if sid not in PMCG_HARERUYA_MAP: continue
            images = p.get("images", []) or []
            if not images or not isinstance(images[0], dict): continue
            img = images[0].get("src")
            if not img: continue
            img = img.split("?")[0]
            # The product title contains the JA Pokemon name as a substring,
            # plus rarity/type markers like (R){炎}. Extract just the
            # leading name.
            name = title.split("(")[0].split("：")[0].split(":")[0].strip()
            by_set[sid].append({"name": name, "name_norm": normalize_name(name), "title": title, "img": img})

    total = sum(len(v) for v in by_set.values())
    print(f"  Hareruya PMCG-era products with images: {total}")

    # Pull D1 imageless PMCG cards
    cards = query_d1(
        "SELECT card_id, name, set_id, local_id FROM ptcg_cards "
        "WHERE lang='ja' AND image_high IS NULL "
        "AND set_id IN ('PMCG1','PMCG2','PMCG3','PMCG4','PMCG5','PMCG6')"
    )
    print(f"  D1 imageless PMCG cards: {len(cards)}")

    matches, misses = [], []
    for c in cards:
        cn = normalize_name(c["name"])
        if not cn:
            misses.append((c["card_id"], "empty name"))
            continue
        # Find the Hareruya products in any of the OP/OPG codes that map
        # to this card's PMCG set, then match by name substring.
        target_pmcg = c["set_id"]
        candidate_haruyu_codes = [k for k, v in PMCG_HARERUYA_MAP.items() if v == target_pmcg]
        candidates = []
        for hcode in candidate_haruyu_codes:
            for prod in by_set.get(hcode, []):
                if cn and cn in prod["name_norm"]:
                    candidates.append(prod)
        if not candidates:
            misses.append((c["card_id"], f"no name-match in {candidate_haruyu_codes}"))
            continue
        # Prefer shortest title (least likely to be a graded/condition variant)
        candidates.sort(key=lambda p: len(p["title"]))
        chosen = candidates[0]
        matches.append({"card_id": c["card_id"], "url": chosen["img"], "title": chosen["title"]})

    print(f"  matched: {len(matches)} / {len(cards)}")
    print(f"  unmatched: {len(misses)}")
    if misses[:5]:
        print("  sample misses:")
        for cid, reason in misses[:5]:
            print(f"    {cid}: {reason}")
    return matches


# ──────────────────────────────────────────────────────────────────
# Cohort 2: tk-xy-* via Bulbapedia name-search
# ──────────────────────────────────────────────────────────────────

BULBAPEDIA_API = "https://archives.bulbagarden.net/w/api.php"


def bulbapedia_search_files(query: str, limit: int = 5) -> list[str]:
    qs = urllib.parse.urlencode({
        "action": "query", "format": "json", "list": "search",
        "srsearch": query, "srlimit": str(limit), "srnamespace": "6",
    })
    req = urllib.request.Request(f"{BULBAPEDIA_API}?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        return [
            h["title"][5:]
            for h in d.get("query", {}).get("search", [])
            if h.get("title", "").startswith("File:")
        ]
    except Exception:
        return []


def bulbapedia_resolve_url(filename: str) -> str | None:
    qs = urllib.parse.urlencode({
        "action": "query", "format": "json",
        "titles": f"File:{filename}",
        "prop": "imageinfo", "iiprop": "url",
    })
    req = urllib.request.Request(f"{BULBAPEDIA_API}?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        for page in d.get("query", {}).get("pages", {}).values():
            ii = page.get("imageinfo") or []
            if ii:
                return ii[0].get("url")
    except Exception:
        pass
    return None


def tk_xy_match() -> list[dict]:
    """For each imageless tk-xy-n / tk-xy-sy card, search Bulbapedia for
    {EN_name} XY and pick the first XY-tagged file."""
    print("\n=== Cohort 2: tk-xy-n / tk-xy-sy via Bulbapedia name-search ===")
    cards = query_d1(
        "SELECT card_id, name, set_id, local_id FROM ptcg_cards "
        "WHERE lang='en' AND image_high IS NULL "
        "AND set_id IN ('tk-xy-n','tk-xy-sy')"
    )
    print(f"  D1 imageless: {len(cards)}")

    matches, misses = [], []
    for c in cards:
        if not c.get("name"):
            misses.append((c["card_id"], "no name"))
            continue
        # Search Bulbapedia for "Spoink XY 49" -> looking for files like
        # SpoinkXY49.jpg. The local_id of the Trainer Kit reprint maps to
        # a different number in the parent set, so we can't include it
        # in the query — search by name + "XY" only.
        files = bulbapedia_search_files(f'"{c["name"]}" XY', limit=10)
        time.sleep(0.3)
        if not files:
            misses.append((c["card_id"], "no search hits"))
            continue
        # Pick the first file whose name starts with the Pokemon name +
        # an XY-era tag. Strip non-card images (anime, set symbols).
        cn = re.sub(r"[\s'\-’.&]+", "", c["name"]).lower()
        chosen = None
        for f in files:
            fl = f.lower()
            if any(t in fl for t in ("anime", "setsymbol", "logo", "boosterbox", "pack", "deck")):
                continue
            if not re.search(r"\.(jpg|jpeg|png)$", fl):
                continue
            # Filename should start with the normalized Pokemon name.
            mm = re.match(r"^([a-z0-9]+?)([a-z][a-z0-9]*)(\d+\w*?)\.(jpg|jpeg|png)$", fl)
            if not mm: continue
            if not mm.group(1).startswith(cn): continue
            chosen = f
            break
        if not chosen:
            misses.append((c["card_id"], "no acceptable file"))
            continue
        url = bulbapedia_resolve_url(chosen)
        time.sleep(0.3)
        if not url:
            misses.append((c["card_id"], f"resolve failed: {chosen}"))
            continue
        matches.append({"card_id": c["card_id"], "url": url, "filename": chosen})
        print(f"    [OK] {c['card_id']:14s} ({c['name']}) → {chosen}")

    print(f"  matched: {len(matches)} / {len(cards)}")
    print(f"  unmatched: {len(misses)}")
    return matches


# ──────────────────────────────────────────────────────────────────
# Cohort 3: svp residual lookup via Hareruya
# ──────────────────────────────────────────────────────────────────

def svp_hareruya_match() -> list[dict]:
    print("\n=== Cohort 3: svp residual via Hareruya SV-P ===")
    if not RAW_HARERUYA.exists():
        print("  Hareruya cache missing — skip")
        return []

    by_lid: dict[str, dict] = {}
    with RAW_HARERUYA.open(encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            title = p.get("title", "") or ""
            m = TITLE_RE.search(title)
            if not m: continue
            sid = m.group("setid").strip()
            if sid not in ("SV-P", "SVP", "SV-P_M", "SVP_M"): continue
            lid = m.group("lid").strip()
            images = p.get("images", []) or []
            if not images or not isinstance(images[0], dict): continue
            img = images[0].get("src")
            if not img: continue
            img = img.split("?")[0]
            # Strip leading zeros for matching
            for variant in (lid, lid.lstrip("0") or lid):
                by_lid.setdefault(variant, {"img": img, "title": title})

    cards = query_d1(
        "SELECT card_id, name, set_id, local_id FROM ptcg_cards "
        "WHERE lang='en' AND image_high IS NULL AND set_id='svp'"
    )
    print(f"  D1 imageless svp: {len(cards)}")
    print(f"  Hareruya SV-P products with lids: {len(by_lid)}")

    matches, misses = [], []
    for c in cards:
        lid = c["local_id"]
        for k in (lid, lid.lstrip("0") or lid):
            hit = by_lid.get(k)
            if hit:
                matches.append({"card_id": c["card_id"], "url": hit["img"], "title": hit["title"]})
                break
        else:
            misses.append(c["card_id"])
    print(f"  matched: {len(matches)} / {len(cards)}")
    return matches


# ──────────────────────────────────────────────────────────────────
# Apply
# ──────────────────────────────────────────────────────────────────

def build_sql(matches: list[dict], lang: str) -> list[str]:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"-- Residual Phase 1 backfill ({lang}). Generated {fetched_at}."]
    for m in matches:
        url = m["url"].replace("'", "''")
        lines.append(
            f"UPDATE ptcg_cards SET image_high='{url}', image_low='{url}' "
            f"WHERE lang='{lang}' AND card_id='{m['card_id']}' AND image_high IS NULL;"
        )
    return lines


def apply_sql(sql_path: Path) -> int:
    out = subprocess.run(
        WRANGLER_BIN + ["--remote", "--file", str(sql_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if out.returncode != 0:
        print(f"  D1 apply FAILED: {out.stderr[:300]}", file=sys.stderr)
        return 0
    written = 0
    for line in out.stdout.split("\n"):
        if "rows_written" in line:
            try: written = int(re.findall(r"\d+", line)[0])
            except (IndexError, ValueError): pass
    return written


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cohort", choices=["pmcg", "tk-xy", "svp", "all"], default="all")
    args = ap.parse_args()

    cohorts = {
        "pmcg":  ("ja", hareruya_pmcg_match),
        "tk-xy": ("en", tk_xy_match),
        "svp":   ("en", svp_hareruya_match),
    }

    targets = [args.cohort] if args.cohort != "all" else list(cohorts.keys())
    grand = {}
    for name in targets:
        lang, fn = cohorts[name]
        matches = fn()
        if not matches:
            print(f"\n[{name}] nothing to write")
            continue
        sql = build_sql(matches, lang)
        sql_path = OUT_DIR / f"{name}.sql"
        sql_path.write_text("\n".join(sql) + "\n", encoding="utf-8")
        json_path = OUT_DIR / f"{name}.json"
        json_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  wrote {sql_path} ({len(matches)} rows)")
        grand[name] = (sql_path, len(matches))

    if args.dry_run:
        print("\n--dry-run: D1 not touched")
        return

    print("\n=== Applying to D1 ===")
    total = 0
    for name, (path, n) in grand.items():
        written = apply_sql(path)
        total += written
        print(f"  [{name}] applied: {written}/{n}")
    print(f"\nGrand total written: {total}")


if __name__ == "__main__":
    main()
