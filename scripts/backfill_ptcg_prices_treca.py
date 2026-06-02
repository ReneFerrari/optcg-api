"""Treca Sunrise (tcgsunrise.com) vintage JA price ingester — e-Card/PCG/ADV/DP
structured subset only (the safe win; WOTC 旧裏 is unstructured and deferred).

Pipeline:
  1. read data/backfill/treca_catalog.json (from measure_treca_overlap.py crawl)
  2. parse each product name `【printing?】【rarity?】【SET】【NUM/TOTAL】NAME【状態X】`
  3. EXCLUDE graded (PSA/鑑定) and unstructured 旧裏 (no set+number)
  4. map Treca SET token (+ TOTAL) -> our D1 set_id
  5. strict match to our unpriced gap card by (set_id, local_id) + name verify
  6. emit price_source='treca' SQL (guarded: only NULL/cardmarket rows)

Run:
  python -m scripts.backfill_ptcg_prices_treca --selftest   # parser unit tests
  python -m scripts.backfill_ptcg_prices_treca --analyze     # SET-token landscape
  python -m scripts.backfill_ptcg_prices_treca --build-sql   # match + emit SQL (no D1 write)
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CATALOG = Path("data/backfill/treca_catalog.json")

BRACKET = re.compile(r'【([^】]*)】')
NUM_TOTAL = re.compile(r'^(\d{1,3})\s*/\s*(\d{1,3})$')          # 077/080
PRINTING = {'1st', '初版', '1ED', '1ED.', 'マスボ', 'ミラー'}      # printing/parallel markers
GRADED = re.compile(r'PSA|BGS|CGC|鑑定|ARS|PSA10|PSA9')
RARITY = {'U', 'C', 'R', 'RR', 'RRR', 'SR', 'SSR', 'HR', 'UR', 'AR', 'SAR', 'CHR', 'CSR',
          'TR', 'PR', 'A', 'K', 'H', 'S', 'P', 'N', 'PROMO', 'プロモ', 'SA', 'MC', 'TD', 'ACE'}
SALE = {'特価', 'セール', 'NEW', '新入荷', '在庫処分', 'お買い得'}


def parse_treca_name(name: str) -> dict:
    """Parse a Treca product name into structured fields.

    Returns dict with: set_token, number(int|None), total(int|None),
    printing(str|None), name(str), graded(bool), oldback(bool), tokens(list).
    'name' is the bare card name (brackets + trailing 状態 stripped).
    """
    tokens = BRACKET.findall(name)
    graded = any(GRADED.search(t) for t in tokens) or bool(GRADED.search(name))
    oldback = any('旧裏' in t for t in tokens)
    set_token = None
    number = total = None
    printing = None
    for t in tokens:
        ts = t.strip()
        if ts in SALE:
            continue
        m = NUM_TOTAL.match(ts)
        if m:
            number, total = int(m.group(1)), int(m.group(2))
            continue
        if ts in PRINTING:
            printing = ts
            continue
        if ts in RARITY:
            continue
        if '状態' in ts or ts.startswith('状態'):
            continue
        if '旧裏' in ts:
            continue
        # first remaining non-classified token = the SET token (e.g. M2, PCG, E1)
        if set_token is None and ts and not GRADED.search(ts):
            set_token = ts
    # bare name = everything after the last 】 in the leading bracket run,
    # minus a trailing 【状態X】. Strip ALL bracket groups then clean.
    bare = BRACKET.sub('', name)
    bare = re.sub(r'\s+', ' ', bare).strip()
    return {
        "set_token": set_token, "number": number, "total": total,
        "printing": printing, "name": bare, "graded": graded,
        "oldback": oldback, "tokens": tokens,
    }


def _selftest():
    cases = [
        ("【特価】【M2】【U】【077/080】ヒカリ【状態A-】",
         dict(set_token="M2", number=77, total=80, name="ヒカリ", graded=False, oldback=False)),
        ("【PCG】【004/052】フシギバナex【状態B+】",
         dict(set_token="PCG", number=4, total=52, name="フシギバナex", graded=False)),
        ("【1st】【PCG】【022/068】ボーマンダex δ-デルタ種【状態B】",
         dict(set_token="PCG", number=22, total=68, printing="1st", graded=False)),
        ("【PSA10】【旧裏】ブラッキー",
         dict(graded=True, oldback=True)),
        ("メタモンLV．15【旧裏】【状態C】",
         dict(set_token=None, number=None, oldback=True)),
        ("【VS】レインボーエネルギー【状態B+】",
         dict(set_token="VS", number=None, name="レインボーエネルギー")),
    ]
    ok = True
    for raw, exp in cases:
        got = parse_treca_name(raw)
        for k, v in exp.items():
            if got.get(k) != v:
                ok = False
                print(f"FAIL {raw!r}\n   {k}: got {got.get(k)!r} expected {v!r}")
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


def _analyze():
    if not CATALOG.exists():
        print(f"no catalog at {CATALOG} — run measure_treca_overlap first"); return
    prods = json.loads(CATALOG.read_text(encoding="utf-8"))
    print(f"catalog: {len(prods)} products")
    parsed = [parse_treca_name(p["name"]) for p in prods]
    graded = sum(1 for p in parsed if p["graded"])
    oldback = sum(1 for p in parsed if p["oldback"] and not p["number"])
    structured = sum(1 for p in parsed if p["set_token"] and p["number"] and not p["graded"])
    print(f"graded (exclude): {graded} | unstructured 旧裏 (defer): {oldback} | "
          f"structured set+number: {structured}")
    print("\ntop SET tokens among structured (token -> count, sample totals):")
    by_tok = Counter(p["set_token"] for p in parsed if p["set_token"] and p["number"] and not p["graded"])
    totals_by_tok = {}
    for p in parsed:
        if p["set_token"] and p["number"] and not p["graded"]:
            totals_by_tok.setdefault(p["set_token"], Counter())[p["total"]] += 1
    for t, n in by_tok.most_common(40):
        tot = ",".join(f"{tt}({cc})" for tt, cc in totals_by_tok[t].most_common(4))
        print(f"   {t:10} {n:4}   totals: {tot}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--build-sql", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if args.analyze:
        _analyze(); return
    if args.build_sql:
        _build_sql()
        return
    ap.print_help()


CARDS_JSON = Path("scratch_ja_probe/ja_index_live_check.json")
# Treca generic family token -> our set_id prefix. Mapped ONLY when the printed
# TOTAL uniquely identifies one of our sets (else skip — Treca totals don't
# reliably equal our counts: eカード 87/88 vs our E2-E5 90-92; PCG 86 = PCG5 AND
# PCG6). Identity tokens (M2/SV9/CP4/XY6/Pt2/L2...) bypass this entirely.
FAMILY = {"eカード": "E", "PCG": "PCG"}


def _norm(s):
    return re.sub(r"[\s　・,]+", "", s or "")


def _build_sql():
    cat = json.loads(CATALOG.read_text(encoding="utf-8"))
    d = json.load(open(CARDS_JSON, encoding="utf-8"))["data"]
    # our model: counts per set, (set_id,num)->card, unpriced gap set
    from collections import defaultdict
    count = Counter(c["set_id"] for c in d)
    by_key = {}
    for c in d:
        try: by_key[(c["set_id"], int(re.sub(r"\D", "", c["local_id"] or "") or -1))] = c
        except Exception: pass
    our_sets = set(count)
    TCGV = ['holofoil','normal','reverseHolofoil','1stEdition','unlimited']
    def vis(c):
        p=c.get('pricing') or {}; src=c.get('price_source'); t=p.get('tcgplayer') or {}
        if any(isinstance(t.get(v),dict) and isinstance(t[v].get('market'),(int,float)) for v in TCGV): return True
        if src in ('yuyutei','hareruya','fullahead') and isinstance((p.get(src) or {}).get('price_usd'),(int,float)): return True
        if src in ('ebay_jp','ebay_us') and isinstance((p.get('ebay') or {}).get('price_usd'),(int,float)): return True
        if src=='yahoo_sold' and isinstance((p.get('yahoo_sold') or {}).get('price_usd'),(int,float)): return True
        if src=='pricecharting' and isinstance((p.get('pricecharting') or {}).get('market'),(int,float)): return True
        cm=p.get('cardmarket') or {}
        return any(isinstance(cm.get(k),(int,float)) and cm[k]>0 for k in ('avg','trend','avg7','avg30','avg1','low'))

    # family total -> unique set_id (only if exactly one set has that count)
    def family_set(fam_prefix, total):
        cands = [s for s, n in count.items()
                 if s.upper().startswith(fam_prefix) and s.upper()[len(fam_prefix):len(fam_prefix)+1].isdigit()
                 and n == total]
        return cands[0] if len(cands) == 1 else None

    matches, skipped = [], Counter()
    for p in cat:
        if not p.get("price_jpy"): skipped["no_price"] += 1; continue
        info = parse_treca_name(p["name"])
        if info["graded"]: skipped["graded"] += 1; continue
        if not (info["set_token"] and info["number"]): skipped["unstructured"] += 1; continue
        tok = info["set_token"]
        if tok in our_sets:
            set_id = tok
        elif tok in FAMILY and info["total"]:
            set_id = family_set(FAMILY[tok], info["total"])
            if not set_id: skipped["family_ambiguous"] += 1; continue
        else:
            skipped["unmapped_token"] += 1; continue
        card = by_key.get((set_id, info["number"]))
        if not card: skipped["no_card"] += 1; continue
        if vis(card): skipped["already_priced"] += 1; continue
        # name verify (conflation guard): our JP name must be a substring of
        # Treca's bare name (Treca names append δ/ex/forms; our name is the core).
        ours = _norm(card.get("name") or "")
        treca = _norm(info["name"])
        if not ours or ours not in treca:
            skipped["name_mismatch"] += 1; continue
        matches.append({"card_id": card["id"], "set_id": set_id, "local_id": card["local_id"],
                        "price_jpy": p["price_jpy"], "treca_name": p["name"], "our_name": card.get("name"),
                        "printing": info["printing"]})

    # dedupe to one price/card (lowest non-zero — raw NM floor; note collisions)
    best = {}
    for m in matches:
        k = m["card_id"]
        if k not in best or m["price_jpy"] < best[k]["price_jpy"]:
            best[k] = m
    rows = list(best.values())
    print(f"structured non-graded matched to UNPRICED gap cards: {len(rows)} "
          f"(from {len(matches)} raw matches)")
    print("skip reasons:", dict(skipped.most_common()))
    print("\nsample matches (spot-check these against the live card):")
    for m in rows[:20]:
        print(f"   {m['card_id']:10} ¥{m['price_jpy']:>7}  ours={m['our_name']!r}  treca={m['treca_name'][:46]!r}")
    Path("data/backfill/treca_matches.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(rows)} matches -> data/backfill/treca_matches.json (review before any apply)")

    # FX + SQL (price_source='treca', guarded so it never clobbers a real source)
    import urllib.request
    from datetime import datetime, timezone
    try:
        rate = float(json.load(urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD", timeout=10))["rates"]["USD"])
    except Exception:
        rate = 0.0064
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql = [f"-- Treca Sunrise vintage/secret-rare backfill ({now}) — {len(rows)} rows, FX 1JPY={rate:.6f}USD"]
    for m in rows:
        usd = round(m["price_jpy"] * rate, 2)
        obj = json.dumps({"price_jpy": m["price_jpy"], "price_usd": usd, "source": "treca",
                          "printing": m.get("printing"), "fetched_at": now}, ensure_ascii=False).replace("'", "''")
        cid = m["card_id"].replace("'", "''")
        sql.append("UPDATE ptcg_cards SET "
                   f"pricing_json=json_patch(COALESCE(pricing_json,'{{}}'),json_object('treca',json('{obj}'))), "
                   "price_source='treca' "
                   f"WHERE lang='ja' AND card_id='{cid}' AND (price_source IS NULL OR price_source='cardmarket');")
    Path("data/backfill/treca_prices.sql").write_text("\n".join(sql) + "\n", encoding="utf-8")
    print(f"SQL -> data/backfill/treca_prices.sql (FX 1 JPY = {rate:.6f} USD)")


if __name__ == "__main__":
    main()
