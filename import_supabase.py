import json
import os
from supabase import create_client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]
supabase = create_client(url, key)

with open("data/cards.json", encoding="utf-8") as f:
    cards = json.load(f)

# upsert in batches of 500
batch_size = 500
for i in range(0, len(cards), batch_size):
    batch = cards[i:i+batch_size]
    supabase.table("cards").upsert(batch, on_conflict="id,set_id").execute()
    print(f"Upserted {min(i+batch_size, len(cards))}/{len(cards)}")

print("Done.")
