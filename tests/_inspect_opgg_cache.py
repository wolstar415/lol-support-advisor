import json
import sqlite3


database = sqlite3.connect("data/advisor.db")
database.row_factory = sqlite3.Row
targets = {"Twitch", "Heimerdinger", "Hecarim", "MonkeyKing"}
print("CATALOGS")
for row in database.execute(
    "SELECT key, value FROM settings WHERE key LIKE 'opgg_position_catalog:%'"
):
    payload = json.loads(row["value"])
    found = [
        champion_id for champion_id in payload.get("champion_ids", [])
        if champion_id in targets
    ]
    print(row["key"], found)
print("SNAPSHOTS")
for row in database.execute(
    "SELECT cache_key, updated_at, payload_json FROM opgg_snapshots "
    "WHERE cache_key LIKE '%:Twitch' OR cache_key LIKE '%:Heimerdinger' "
    "OR cache_key LIKE '%:Hecarim' ORDER BY cache_key"
):
    payload = json.loads(row["payload_json"])
    print(
        row["cache_key"], row["updated_at"],
        len(payload.get("counters", [])), len(payload.get("weak_picks", [])),
        payload.get("source_url", ""),
    )
print("WUKONG BUILDS")
for row in database.execute(
    "SELECT cache_key, updated_at, payload_json FROM build_guides "
    "WHERE cache_key LIKE '%:MonkeyKing' ORDER BY cache_key"
):
    payload = json.loads(row["payload_json"])
    print(
        row["cache_key"], row["updated_at"], payload.get("patch", ""),
        len(payload.get("rune_builds", [])), len(payload.get("item_groups", [])),
        payload.get("source_url", ""),
    )
database.close()
