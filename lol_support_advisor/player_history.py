from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import OpggMcpRecentMatch


OTHER_PLAYER_PAGE_SIZE = 10
OPGG_MCP_MATCH_LIMIT = 20


@dataclass(slots=True)
class OtherPlayerHistoryPager:
    """Ten-at-a-time Riot history cursor kept independently for each UI tab."""

    start: int = 0
    match_ids: list[str] = field(default_factory=list)
    has_more: bool = True

    def next_request(self) -> tuple[int, int] | None:
        if not self.has_more:
            return None
        return max(self.start, 0), OTHER_PLAYER_PAGE_SIZE

    def accept_page(self, match_ids: Iterable[str], has_more: bool) -> list[str]:
        page = [str(match_id).strip() for match_id in match_ids if str(match_id).strip()]
        self.start += len(page)
        known = set(self.match_ids)
        added: list[str] = []
        for match_id in page:
            if match_id in known:
                continue
            known.add(match_id)
            self.match_ids.append(match_id)
            added.append(match_id)
        self.has_more = bool(has_more and page)
        return added


def normalize_riot_id(value: str) -> str:
    """Return a stable key without changing the Riot ID shown to the user."""
    game_name, separator, tag_line = str(value or "").strip().partition("#")
    if not separator:
        return ""
    game_name = " ".join(game_name.split())
    tag_line = " ".join(tag_line.split())
    if not game_name or not tag_line:
        return ""
    return f"{game_name.casefold()}#{tag_line.casefold()}"


def split_riot_id(value: str) -> tuple[str, str] | None:
    game_name, separator, tag_line = str(value or "").strip().partition("#")
    game_name = " ".join(game_name.split())
    tag_line = " ".join(tag_line.split())
    if not separator or not game_name or not tag_line:
        return None
    return game_name, tag_line


def completed_solo_matches(
    matches: Iterable[OpggMcpRecentMatch],
    *,
    limit: int | None = None,
) -> list[OpggMcpRecentMatch]:
    """Keep only completed solo-ranked games, newest first and de-duplicated."""
    unique: dict[str, OpggMcpRecentMatch] = {}
    anonymous = 0
    for match in matches:
        if str(match.game_type or "").upper() != "SOLORANKED":
            continue
        if str(match.result or "").upper() not in {"WIN", "LOSE"}:
            continue
        key = str(match.match_id or "").strip()
        if not key:
            anonymous += 1
            key = f"__anonymous__{anonymous}:{match.created_at}:{match.champion_key}"
        unique.setdefault(key, match)
    ordered = sorted(
        unique.values(),
        key=lambda item: (str(item.created_at or ""), str(item.match_id or "")),
        reverse=True,
    )
    return ordered[:max(int(limit), 0)] if limit is not None else ordered


def merge_solo_match_pages(
    existing: Iterable[OpggMcpRecentMatch],
    incoming: Iterable[OpggMcpRecentMatch],
) -> list[OpggMcpRecentMatch]:
    return completed_solo_matches([*existing, *incoming])


def next_opgg_match_limit(loaded_count: int) -> int | None:
    """OP.GG MCP has no cursor and caps this tool at twenty matches."""
    loaded = max(int(loaded_count), 0)
    if loaded < OTHER_PLAYER_PAGE_SIZE:
        return OTHER_PLAYER_PAGE_SIZE
    if loaded < OPGG_MCP_MATCH_LIMIT:
        return OPGG_MCP_MATCH_LIMIT
    return None
