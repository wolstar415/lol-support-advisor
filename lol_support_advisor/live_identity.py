from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from .models import LiveGameSnapshot, LivePlayer


LIVE_IDENTITY_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
LIVE_IDENTITY_CACHE_VERSION = 1


def gameflow_puuid_by_champion(session: dict[str, Any]) -> dict[int, str]:
    """Extract per-game player UUIDs from an in-game LCU session.

    Privacy mode can blank Riot IDs while ``playerChampionSelections`` still
    carries UUID-shaped values. They are useful as local cache keys but are
    not guaranteed to be valid Riot Account-v1 PUUIDs. Normal ranked draft
    does not allow duplicate champions, so champion id is a stable join key
    for the Live Client roster. Ambiguous/invalid rows are discarded.
    """
    game_data = session.get("gameData") if isinstance(session, dict) else None
    rows = (
        game_data.get("playerChampionSelections")
        if isinstance(game_data, dict) else None
    )
    values: dict[int, str] = {}
    duplicates: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            champion_id = int(row.get("championId") or 0)
        except (TypeError, ValueError):
            continue
        puuid = str(row.get("puuid") or "").strip()
        if champion_id <= 0 or not puuid:
            continue
        if champion_id in values and values[champion_id] != puuid:
            duplicates.add(champion_id)
        else:
            values[champion_id] = puuid
    for champion_id in duplicates:
        values.pop(champion_id, None)
    return values


def gameflow_summoner_id_by_champion(session: dict[str, Any]) -> dict[int, str]:
    """Extract local summoner ids from both in-game teams.

    In privacy mode ``playerChampionSelections[].puuid`` is a short-lived
    36-character privacy identifier, not the Riot Account-v1 PUUID.  The team
    rows still contain a local ``summonerId`` which the League Client can
    resolve through ``/lol-summoner/v1/summoners/{id}`` without an external
    Riot API key.
    """
    game_data = session.get("gameData") if isinstance(session, dict) else None
    if not isinstance(game_data, dict):
        return {}
    values: dict[int, str] = {}
    duplicates: set[int] = set()
    for team_key in ("teamOne", "teamTwo"):
        rows = game_data.get(team_key)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                champion_id = int(row.get("championId") or 0)
            except (TypeError, ValueError):
                continue
            summoner_id = str(row.get("summonerId") or "").strip()
            if champion_id <= 0 or not summoner_id:
                continue
            if champion_id in values and values[champion_id] != summoner_id:
                duplicates.add(champion_id)
            else:
                values[champion_id] = summoner_id
    for champion_id in duplicates:
        values.pop(champion_id, None)
    return values


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
