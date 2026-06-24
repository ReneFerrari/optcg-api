import json
import os
import io
from supabase import create_client
from PIL import Image
import httpx

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]
supabase = create_client(url, key)

BUCKET       = "card-images"
STORAGE_BASE = f"{url}/storage/v1/object/public/{BUCKET}"

def already_uploaded(card_id: str) -> bool:
    try:
        files = supabase.storage.from_(BUCKET).list(prefix=card_id)
        return any(f["name"] == f"{card_id}.webp" for f in files)
    except:
        return False

def make_and_upload_thumbnail(card_id: str, image_url: str) -> str | None:
    try:
        response = httpx.get(image_url, timeout=10, follow_redirects=True)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content))
        img = img.convert("RGB")
        img.thumbnail((200, 200), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=80)
        supabase.storage.from_(BUCKET).upload(
            f"{card_id}.webp",
            buf.getvalue(),
            {"content-type": "image/webp", "upsert": "true"}
        )
        return f"{STORAGE_BASE}/{card_id}.webp"
    except Exception as e:
        print(f"  ⚠ thumb failed {card_id}: {e}")
        return None

with open("data/cards.json", encoding="utf-8") as f:
    cards = json.load(f)

# deduplicate by (id, set_id)
seen = {}
for card in cards:
    seen[(card["id"], card["set_id"])] = card
cards = list(seen.values())
print(f"{len(cards)} unique cards after dedup")

# process thumbnails
uploaded = skipped = failed = 0
for i, card in enumerate(cards):
    card_id = card["id"]
    print(f"[{i+1}/{len(cards)}] {card_id}", end=" ")
    if already_uploaded(card_id):
        card["thumb"] = f"{STORAGE_BASE}/{card_id}.webp"
        print("⏭ skipped")
        skipped += 1
    elif card.get("image_url"):
        thumb_url = make_and_upload_thumbnail(card_id, card["image_url"])
        card["thumb"] = thumb_url
        if thumb_url:
            print("✅ uploaded")
            uploaded += 1
        else:
            card["thumb"] = None
            print("❌ failed")
            failed += 1
    else:
        card["thumb"] = None
        print("⚠ no image_url")
        failed += 1

print(f"\nThumbs done — {uploaded} uploaded, {skipped} skipped, {failed} failed\n")

# upsert in batches of 500
batch_size = 500
for i in range(0, len(cards), batch_size):
    batch = cards[i:i+batch_size]
    supabase.table("cards").upsert(batch, on_conflict="id,set_id").execute()
    print(f"Upserted {min(i+batch_size, len(cards))}/{len(cards)}")

print("Done.")
