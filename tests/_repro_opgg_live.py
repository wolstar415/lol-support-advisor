from pathlib import Path

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.opgg import OpggClient


registry = ChampionRegistry(Path("data/champions_ko.json"))
client = OpggClient(registry, timeout=20.0)
for champion_id, position in (
    ("Twitch", "BOTTOM"),
    ("Heimerdinger", "TOP"),
    ("Hecarim", "JUNGLE"),
):
    try:
        snapshot = client.refresh_matchup(champion_id, position)
        print(
            "MATCHUP OK", champion_id, position,
            len(snapshot.counters), len(snapshot.weak_picks), snapshot.source_url,
        )
    except Exception as exc:
        print("MATCHUP FAIL", champion_id, position, type(exc).__name__, repr(str(exc)))

for position in ("TOP", "JUNGLE"):
    try:
        guide = client.refresh_build("MonkeyKing", position)
        print(
            "BUILD OK", position, len(guide.rune_builds),
            len(guide.item_groups), guide.source_url,
        )
    except Exception as exc:
        print("BUILD FAIL", position, type(exc).__name__, repr(str(exc)))

original_slug = registry.slug
registry.slug = lambda champion_id: (
    "monkeyking" if champion_id == "MonkeyKing" else original_slug(champion_id)
)
for position in ("TOP", "JUNGLE"):
    try:
        guide = client.refresh_build("MonkeyKing", position)
        print(
            "CANONICAL BUILD OK", position, len(guide.rune_builds),
            len(guide.item_groups), guide.source_url,
        )
    except Exception as exc:
        print(
            "CANONICAL BUILD FAIL", position,
            type(exc).__name__, repr(str(exc)),
        )
