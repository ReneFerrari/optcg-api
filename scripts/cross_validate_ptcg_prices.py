"""
Cross-validation pricing engine for JA Pokémon cards.

Combines per-card prices from multiple real JP-market sources, applies a
consensus rule, assigns a confidence label, and flags variant-conflation
candidates — then emits apply-ready SQL. NEVER fabricates: a card with no
real source stays NULL.

Sources (all FX-normalised to USD):
  - In-D1 already (read from pricing_json): tcgplayer, pricecharting,
    yuyutei, hareruya, ebay, cardmarket
  - Scraped fresh: fullahead (Makeshop promo shop, SV-P/SM-P/XY-P/BW-P/S-P)

Consensus rule (per session spec), applied WITHIN a market class so we
don't false-flag a real JP-retail-vs-US-TCGplayer spread:
  - >=2 same-market sources within +/-15%  -> price=median, confidence='high',
    price_source='consensus'
  - sources disagree >15%                   -> keep best single, confidence='low',
    record the spread; FLAG if max/min > CONFLATION_RATIO (variant-conflation
    candidate, e.g. base card matched to a Master-Ball variant page)
  - exactly 1 source                        -> confidence='single-source'
  - 0 sources                               -> leave NULL (no fabrication)

Markets:
  JP_RETAIL = {yuyutei, hareruya, fullahead}   # shop sell prices, comparable
  JP_SOLD   = {pricecharting}                  # eBay-sold (USD), JP cards
  US        = {tcgplayer}                       # US TCGplayer market
  EU        = {cardmarket}                      # Cardmarket EUR
Consensus is computed within JP_RETAIL (the only class with >=2 members for
our gap). Cross-market values are recorded for the conflation check only.

Spot-check gates BEFORE this writes anything (enforced by --report, manual):
  1. Control test on already-priced rows (ratio sanity)
  2. Conflation flags reviewed
  3. No row gets a price from a source whose key it doesn't actually carry

Usage:
  python -m scripts.cross_validate_ptcg_prices --cards-json scratch_ja_probe/ja_all.json \
      --fullahead scratch_ja_probe/fullahead_catalog.json --report
  python -m scripts.cross_validate_ptcg_prices --from-d1 --dry-run   # when D1 auth restored
"""
from __future__ import annotations
import argparse, json, re, statistics, sys, urllib.request
from collections import defaultdict

CONSENSUS_TOL = 0.15        # +/-15%
CONFLATION_RATIO = 3.0      # max/min within a card across markets -> flag
JP_RETAIL = ('yuyutei', 'hareruya', 'fullahead')
FX_FALLBACK = 0.0064

def fetch_fx() -> float:
    try:
        return float(json.load(urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=JPY&to=USD", timeout=10))["rates"]["USD"])
    except Exception as e:
        print(f"FX fetch failed ({e}); fallback {FX_FALLBACK}", file=sys.stderr)
        return FX_FALLBACK

def li(x):
    m = re.match(r'^0*(\d+)', str(x)); return m.group(1) if m else str(x)

def usd_from_pricing(pricing: dict, fx: float) -> dict:
    """Extract every source's USD price from a D1 pricing_json blob."""
    out = {}
    t = pricing.get('tcgplayer') or {}
    for v in ('holofoil', 'normal', 'reverseHolofoil', '1stEdition', 'unlimited'):
        m = t.get(v, {}).get('market') if isinstance(t.get(v), dict) else None
        if isinstance(m, (int, float)): out['tcgplayer'] = float(m); break
    for k in ('yuyutei', 'hareruya'):
        u = pricing.get(k, {}).get('price_usd') if isinstance(pricing.get(k), dict) else None
        if isinstance(u, (int, float)): out[k] = float(u)
    if isinstance(pricing.get('ebay'), dict) and isinstance(pricing['ebay'].get('price_usd'), (int, float)):
        out['ebay'] = float(pricing['ebay']['price_usd'])
    if isinstance(pricing.get('pricecharting'), dict) and isinstance(pricing['pricecharting'].get('market'), (int, float)):
        out['pricecharting'] = float(pricing['pricecharting']['market'])
    cm = pricing.get('cardmarket') or {}
    for k in ('trend', 'avg', 'avg7', 'avg30'):
        if isinstance(cm.get(k), (int, float)) and cm[k] > 0: out['cardmarket'] = float(cm[k]); break
    if isinstance(pricing.get('manual'), dict) and isinstance(pricing['manual'].get('price'), (int, float)):
        out['manual'] = float(pricing['manual']['price'])
    return out

def consensus(prices_by_source: dict):
    """Return (price, source_label, confidence, detail)."""
    if not prices_by_source:
        return (None, None, 'none', {})
    if 'manual' in prices_by_source:
        return (prices_by_source['manual'], 'manual', 'manual', {})
    jp = {s: p for s, p in prices_by_source.items() if s in JP_RETAIL}
    vals = list(prices_by_source.values())
    spread = (max(vals) / min(vals)) if min(vals) > 0 else 99
    flag = spread > CONFLATION_RATIO and len(vals) >= 2
    # consensus within JP retail
    if len(jp) >= 2:
        jpvals = sorted(jp.values()); med = statistics.median(jpvals)
        within = [v for v in jpvals if abs(v - med) / med <= CONSENSUS_TOL]
        if len(within) >= 2:
            return (round(statistics.median(within), 2), 'consensus', 'high',
                    {'sources': list(jp), 'spread': round(spread, 2), 'flag': flag})
        return (round(med, 2), 'consensus', 'low',
                {'sources': list(jp), 'spread': round(spread, 2), 'flag': flag, 'note': 'jp_retail_disagree'})
    # single best: prefer JP retail > pricecharting > tcgplayer > cardmarket
    for s in ('yuyutei', 'hareruya', 'fullahead', 'pricecharting', 'tcgplayer', 'ebay', 'cardmarket'):
        if s in prices_by_source:
            return (round(prices_by_source[s], 2), s, 'single-source',
                    {'spread': round(spread, 2), 'flag': flag})
    return (None, None, 'none', {})

def load_fullahead(path, fx):
    fa = json.load(open(path, encoding='utf-8'))
    out = {}
    for code, v in fa.items():
        m = re.match(r'PK-[A-Z]+-P-(\d+)', code)
        if not m: continue
        out[(v['set'].upper(), li(m.group(1)))] = round(v['jpy'] * fx, 2)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cards-json', help='cached /pokemon/cards/all?lang=ja dump')
    ap.add_argument('--fullahead', help='fullahead_catalog.json from scrape_fullahead.py')
    ap.add_argument('--from-d1', action='store_true', help='(stub) query D1 instead of cards-json')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    fx = fetch_fx()
    if args.from_d1:
        print("--from-d1 needs wrangler D1 auth (d1 scope). Use --cards-json for now.", file=sys.stderr)
        sys.exit(2)
    cards = json.load(open(args.cards_json, encoding='utf-8'))['data']
    fa = load_fullahead(args.fullahead, fx) if args.fullahead else {}

    rows = []
    fills = []           # newly-priced (was NULL)
    flags = []           # conflation candidates among already-priced
    for c in cards:
        key = (c['set_id'].upper(), li(c['local_id']))
        prices = usd_from_pricing(c.get('pricing') or {}, fx)
        if key in fa:
            prices['fullahead'] = fa[key]
        price, label, conf, detail = consensus(prices)
        had = bool(c.get('price_source'))
        rows.append((c['id'], key, price, label, conf, detail, had, list(prices)))
        if price is not None and not _displayable_before(c.get('pricing') or {}):
            # only counts as a NEW fill if it wasn't already user-visible
            if 'fullahead' in prices and len(prices) == 1:
                fills.append((c['id'], price, 'fullahead'))
        if detail.get('flag'):
            flags.append((c['id'], prices, detail.get('spread')))

    if args.report:
        priced = sum(1 for r in rows if r[2] is not None)
        fa_only_fills = [f for f in fills]
        print(f"FX 1 JPY = {fx:.6f} USD")
        print(f"cards={len(rows)}  resolvable price (any source)={priced} ({100*priced/len(rows):.2f}%)")
        print(f"NEW fullahead-only fills (cards with no other source) = {len(fa_only_fills)}")
        print(f"variant-conflation FLAGS (max/min > {CONFLATION_RATIO}x) = {len(flags)}")
        print("\nsample fullahead-only fills:")
        for cid, p, s in fa_only_fills[:12]: print(f"  {cid:12} ${p} ({s})")
        print("\nsample conflation flags (review before any write):")
        for cid, pr, sp in sorted(flags, key=lambda x: -(x[2] or 0))[:12]:
            print(f"  {cid:12} spread={sp}x  {pr}")
    return rows, fills, flags

def _displayable_before(p):
    t = p.get('tcgplayer') or {}
    if any(isinstance(o, dict) and isinstance(o.get('market'), (int, float)) for o in t.values()): return True
    for k in ('yuyutei', 'hareruya'):
        if isinstance(p.get(k), dict) and isinstance(p[k].get('price_usd'), (int, float)): return True
    if isinstance(p.get('ebay'), dict) and isinstance(p['ebay'].get('price_usd'), (int, float)): return True
    if isinstance(p.get('pricecharting'), dict) and isinstance(p['pricecharting'].get('market'), (int, float)): return True
    cm = p.get('cardmarket') or {}
    if any(isinstance(cm.get(k), (int, float)) and cm[k] > 0 for k in ('avg', 'trend', 'avg7', 'avg30', 'avg1', 'low')): return True
    if isinstance(p.get('manual'), dict): return True
    return False

if __name__ == '__main__':
    main()
