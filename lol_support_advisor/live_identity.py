from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from .models import LiveGameSnapshot, LivePlayer


LIVE_IDENTITY_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
LIVE_IDENTITY_CACHE_VERSION = 1


def live_identity_available(player: LivePlayer) -> bool:
    """Return whether a player has a complete Riot ID worth retaining."""
    game_name = player.riot_game_name.strip()
    tag_line = player.riot_tag_line.strip()
    lowered = game_name.casefold()
    return bool(
        game_name and tag_line
        and not lowered.startswith("비공개 ")
        and lowered not in {"알 수 없음", "unknown", "private"}
    )


def live_roster_fingerprint(snapshot: LiveGameSnapshot) -> str:
    """Identify one game without using names that privacy mode can redact."""
    roster = tuple(sorted(
        (player.team.upper(), player.champion_id)
        for player in snapshot.players
        if player.champion_id
    ))
    return repr((len(snapshot.players), roster))


def live_identity_count(snapshot: LiveGameSnapshot) -> int:
    return sum(live_identity_available(player) for player in snapshot.players)


def _entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (
        str(entry.get("team") or "").upper(),
        str(entry.get("champion_id") or ""),
    )


def update_live_identity_payload(
    snapshot: LiveGameSnapshot,
    previous: dict[str, Any] | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Add identities observed in a raw Live Client roster to local memory.

    A different ten-champion/team fingerprint starts a new game record. An
    entirely redacted snapshot never replaces a useful record from the current
    game.
    """
    fingerprint = live_roster_fingerprint(snapshot)
    observed = {
        (player.team.upper(), player.champion_id): {
            "team": player.team.upper(),
            "champion_id": player.champion_id,
            "position": player.position.upper(),
            "game_name": player.riot_game_name.strip(),
            "tag_line": player.riot_tag_line.strip(),
        }
        for player in snapshot.players
        if live_identity_available(player)
    }
    previous = previous if isinstance(previous, dict) else None
    same_game = bool(
        previous and str(previous.get("fingerprint") or "") == fingerprint
    )
    existing = {
        _entry_key(entry): dict(entry)
        for entry in ((previous or {}).get("players") or [])
        if same_game and isinstance(entry, dict)
        and str(entry.get("game_name") or "").strip()
        and str(entry.get("tag_line") or "").strip()
    }
    if not observed and not existing:
        return previous
    combined = {**existing, **observed}
    if same_game and combined == existing:
        return previous
    return {
        "version": LIVE_IDENTITY_CACHE_VERSION,
        "captured_at": float(now if now is not None else time.time()),
        "fingerprint": fingerprint,
        "game_mode": snapshot.game_mode,
        "players": [combined[key] for key in sorted(combined)],
    }


def merge_live_roster_identities(
    snapshot: LiveGameSnapshot,
    payload: dict[str, Any] | None,
    *,
    now: float | None = None,
    max_age_seconds: float = LIVE_IDENTITY_CACHE_MAX_AGE_SECONDS,
) -> LiveGameSnapshot:
    """Restore briefly exposed Riot IDs into a later redacted roster."""
    if not isinstance(payload, dict):
        return snapshot
    try:
        captured_at = float(payload.get("captured_at") or 0.0)
    except (TypeError, ValueError):
        return snapshot
    current_time = float(now if now is not None else time.time())
    age = current_time - captured_at
    if age < -300 or age > max_age_seconds:
        return snapshot
    if str(payload.get("fingerprint") or "") != live_roster_fingerprint(snapshot):
        return snapshot

    remembered = {
        _entry_key(entry): entry
        for entry in (payload.get("players") or [])
        if isinstance(entry, dict)
        and str(entry.get("game_name") or "").strip()
        and str(entry.get("tag_line") or "").strip()
    }
    if not remembered:
        return snapshot
    players: list[LivePlayer] = []
    changed = False
    active_riot_id = snapshot.active_riot_id
    for player in snapshot.players:
        if live_identity_available(player):
            restored = player
        else:
            entry = remembered.get((player.team.upper(), player.champion_id))
            if entry:
                restored = replace(
                    player,
                    riot_game_name=str(entry.get("game_name") or "").strip(),
                    riot_tag_line=str(entry.get("tag_line") or "").strip(),
                )
                changed = changed or restored != player
            else:
                restored = player
        if restored.is_active_player and live_identity_available(restored):
            active_riot_id = restored.riot_id
        players.append(restored)
    if not changed and active_riot_id == snapshot.active_riot_id:
        return snapshot
    return replace(snapshot, players=players, active_riot_id=active_riot_id)
