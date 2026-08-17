from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from .models import OpggSnapshot, PersonalStat


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        backfill_payloads: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opgg_snapshots (
                    cache_key TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    game_creation INTEGER NOT NULL DEFAULT 0,
                    queue_id INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_matches_game_creation
                    ON matches(game_creation DESC);
                CREATE TABLE IF NOT EXISTS live_profiles (
                    riot_id TEXT PRIMARY KEY,
                    puuid TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_identities (
                    riot_id TEXT PRIMARY KEY COLLATE NOCASE,
                    puuid TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            identity_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM player_identities").fetchone()["count"]
            )
            if identity_count == 0:
                for row in connection.execute("SELECT payload_json FROM matches").fetchall():
                    try:
                        backfill_payloads.append(json.loads(row["payload_json"]))
                    except (ValueError, TypeError):
                        continue
        if backfill_payloads:
            self.save_matches(backfill_payloads)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def set_riot_api_key(self, api_key: str, when: datetime | None = None) -> None:
        """Store a development key locally without ever putting it in source files."""
        self.set_setting("riot_api_key", api_key.strip())
        self.set_setting(
            "riot_api_key_saved_at", (when or datetime.now()).isoformat(timespec="seconds")
        )
        self.set_setting("riot_api_key_invalid", "0")

    def mark_riot_api_key_invalid(self) -> None:
        self.set_setting("riot_api_key_invalid", "1")

    def riot_api_key_refresh_remaining(
        self, now: datetime | None = None, hours: int = 24
    ) -> timedelta:
        if not self.get_setting("riot_api_key"):
            return timedelta(0)
        if self.get_setting("riot_api_key_invalid") == "1":
            return timedelta(0)
        raw = self.get_setting("riot_api_key_saved_at")
        if not raw:
            return timedelta(0)
        try:
            saved_at = datetime.fromisoformat(raw)
        except ValueError:
            return timedelta(0)
        remaining = saved_at + timedelta(hours=hours) - (now or datetime.now())
        return max(remaining, timedelta(0))

    def riot_api_key_needs_refresh(self, now: datetime | None = None) -> bool:
        return bool(self.get_setting("riot_api_key")) and (
            self.riot_api_key_refresh_remaining(now).total_seconds() <= 0
        )

    def opgg_cooldown_remaining(self, now: datetime | None = None, minutes: int = 60) -> timedelta:
        raw = self.get_setting("opgg_last_success")
        if not raw:
            return timedelta(0)
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return timedelta(0)
        remaining = last + timedelta(minutes=minutes) - (now or datetime.now())
        return max(remaining, timedelta(0))

    def mark_opgg_success(self, when: datetime | None = None) -> None:
        self.set_setting("opgg_last_success", (when or datetime.now()).isoformat(timespec="seconds"))

    def riot_sync_cooldown_remaining(
        self, now: datetime | None = None, minutes: int = 10
    ) -> timedelta:
        raw = self.get_setting("riot_last_sync")
        if not raw:
            return timedelta(0)
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return timedelta(0)
        remaining = last + timedelta(minutes=minutes) - (now or datetime.now())
        return max(remaining, timedelta(0))

    def mark_riot_sync(self, when: datetime | None = None) -> None:
        self.set_setting("riot_last_sync", (when or datetime.now()).isoformat(timespec="seconds"))

    def save_opgg_snapshot(self, snapshot: OpggSnapshot) -> None:
        position = str(snapshot.position or "SUPPORT").upper()
        cache_key = f"{position}:{snapshot.enemy_support_id or '__overall__'}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO opgg_snapshots(cache_key, updated_at, payload_json) VALUES(?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (cache_key, snapshot.updated_at, json.dumps(snapshot.to_dict(), ensure_ascii=False)),
            )

    def load_opgg_snapshot(
        self, enemy_support_id: str | None, position: str = "SUPPORT"
    ) -> OpggSnapshot | None:
        normalized_position = str(position or "SUPPORT").upper()
        cache_key = f"{normalized_position}:{enemy_support_id or '__overall__'}"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM opgg_snapshots WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            # Read caches created before position-aware keys were introduced.
            if not row and normalized_position == "SUPPORT":
                legacy_key = enemy_support_id or "__overall__"
                row = connection.execute(
                    "SELECT payload_json FROM opgg_snapshots WHERE cache_key = ?", (legacy_key,)
                ).fetchone()
        if not row:
            return None
        try:
            return OpggSnapshot.from_dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError, KeyError):
            return None

    def known_match_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT match_id FROM matches").fetchall()
        return {str(row["match_id"]) for row in rows}

    def save_matches(self, matches: Iterable[dict[str, Any]]) -> int:
        saved = 0
        with self._connect() as connection:
            for match in matches:
                metadata = match.get("metadata", {})
                info = match.get("info", {})
                match_id = str(metadata.get("matchId", ""))
                if not match_id:
                    continue
                connection.execute(
                    "INSERT INTO matches(match_id, game_creation, queue_id, payload_json) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET "
                    "game_creation = excluded.game_creation, queue_id = excluded.queue_id, "
                    "payload_json = excluded.payload_json",
                    (
                        match_id,
                        int(info.get("gameCreation", 0)),
                        int(info.get("queueId", 0)),
                        json.dumps(match, ensure_ascii=False),
                    ),
                )
                identity_stamp = datetime.fromtimestamp(
                    int(info.get("gameCreation", 0)) / 1000
                ).isoformat(timespec="seconds") if int(info.get("gameCreation", 0)) else datetime.now().isoformat(timespec="seconds")
                for participant in info.get("participants", []):
                    game_name = str(participant.get("riotIdGameName") or "").strip()
                    tag_line = str(
                        participant.get("riotIdTagline")
                        or participant.get("riotIdTagLine")
                        or ""
                    ).strip()
                    participant_puuid = str(participant.get("puuid") or "").strip()
                    if game_name and tag_line and participant_puuid:
                        connection.execute(
                            "INSERT INTO player_identities(riot_id, puuid, updated_at) VALUES(?, ?, ?) "
                            "ON CONFLICT(riot_id) DO UPDATE SET puuid = excluded.puuid, "
                            "updated_at = excluded.updated_at",
                            (f"{game_name}#{tag_line}", participant_puuid, identity_stamp),
                        )
                saved += 1
        return saved

    def count_matches(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM matches").fetchone()
        return int(row["count"])

    def match_revision(self) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(MAX(game_creation), 0) AS newest FROM matches"
            ).fetchone()
        return int(row["count"]), int(row["newest"])

    def count_player_matches(self, puuid: str, limit: int = 1000) -> int:
        count = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches WHERE queue_id = 420 "
                "ORDER BY game_creation DESC"
            ).fetchall()
        for row in rows:
            try:
                participants = json.loads(row["payload_json"]).get("info", {}).get(
                    "participants", []
                )
            except (ValueError, TypeError):
                continue
            if any(item.get("puuid") == puuid for item in participants):
                count += 1
                if count >= limit:
                    break
        return count

    def save_live_profile(self, riot_id: str, puuid: str, payload: dict[str, Any]) -> None:
        updated_at = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO live_profiles(riot_id, puuid, updated_at, payload_json) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(riot_id) DO UPDATE SET puuid = excluded.puuid, "
                "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (riot_id, puuid, updated_at, json.dumps(payload, ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO player_identities(riot_id, puuid, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(riot_id) DO UPDATE SET puuid = excluded.puuid, "
                "updated_at = excluded.updated_at",
                (riot_id, puuid, updated_at),
            )

    def save_player_identity(self, riot_id: str, puuid: str) -> None:
        updated_at = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO player_identities(riot_id, puuid, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(riot_id) DO UPDATE SET puuid = excluded.puuid, "
                "updated_at = excluded.updated_at",
                (riot_id, puuid, updated_at),
            )

    def load_match(self, match_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError):
            return None

    def player_matches(
        self, puuid: str, limit: int = 1000, queue_id: int = 420
    ) -> list[dict[str, Any]]:
        """Return newest locally cached matches containing the requested player."""
        if not puuid or limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches WHERE queue_id = ? "
                "ORDER BY game_creation DESC",
                (queue_id,),
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            try:
                match = dict(json.loads(row["payload_json"]))
            except (ValueError, TypeError):
                continue
            participants = (match.get("info") or {}).get("participants") or []
            if not any(participant.get("puuid") == puuid for participant in participants):
                continue
            matches.append(match)
            if len(matches) >= limit:
                break
        return matches

    def load_live_profile(
        self, riot_id: str, max_age: timedelta = timedelta(hours=1)
    ) -> tuple[str, dict[str, Any], str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT puuid, updated_at, payload_json FROM live_profiles WHERE riot_id = ?",
                (riot_id,),
            ).fetchone()
        if not row:
            return None
        try:
            updated_at = datetime.fromisoformat(row["updated_at"])
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            return None
        if datetime.now() - updated_at > max_age:
            return None
        return str(row["puuid"]), payload, str(row["updated_at"])

    def load_live_profile_any_age(self, riot_id: str) -> tuple[str, dict[str, Any], str] | None:
        return self.load_live_profile(riot_id, max_age=timedelta(days=3650))

    def find_puuid_by_riot_id(self, riot_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT puuid FROM player_identities WHERE riot_id = ? COLLATE NOCASE", (riot_id,)
            ).fetchone()
        return str(row["puuid"]) if row else ""

    def recent_riot_ids(self, my_puuid: str, limit: int = 9) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches ORDER BY game_creation DESC"
            ).fetchall()
        for row in rows:
            try:
                participants = json.loads(row["payload_json"]).get("info", {}).get("participants", [])
            except (ValueError, TypeError):
                continue
            if not any(item.get("puuid") == my_puuid for item in participants):
                continue
            for participant in participants:
                puuid = str(participant.get("puuid") or "")
                if not puuid or puuid == my_puuid or puuid in seen:
                    continue
                game_name = str(participant.get("riotIdGameName") or "").strip()
                tag_line = str(
                    participant.get("riotIdTagline") or participant.get("riotIdTagLine") or ""
                ).strip()
                if game_name and tag_line:
                    result.append((game_name, tag_line))
                    seen.add(puuid)
                if len(result) >= limit:
                    return result
        return result

    def pair_same_team_games(self, first_puuid: str, second_puuid: str, limit: int = 30) -> int:
        games = inspected = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches ORDER BY game_creation DESC"
            ).fetchall()
        for row in rows:
            try:
                participants = json.loads(row["payload_json"]).get("info", {}).get("participants", [])
            except (ValueError, TypeError):
                continue
            first = next((item for item in participants if item.get("puuid") == first_puuid), None)
            second = next((item for item in participants if item.get("puuid") == second_puuid), None)
            if not first or not second:
                continue
            inspected += 1
            if first.get("teamId") == second.get("teamId"):
                games += 1
            if inspected >= limit:
                break
        return games

    def player_champion_record(
        self, puuid: str, champion_id: str, limit: int = 50
    ) -> tuple[int, int]:
        games = wins = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches ORDER BY game_creation DESC"
            ).fetchall()
        inspected = 0
        for row in rows:
            try:
                match = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            participant = next(
                (item for item in match.get("info", {}).get("participants", []) if item.get("puuid") == puuid),
                None,
            )
            if not participant:
                continue
            inspected += 1
            if participant.get("championName") == champion_id:
                games += 1
                wins += int(bool(participant.get("win")))
            if inspected >= limit:
                break
        return games, wins

    def latest_player_match(self, puuid: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches WHERE queue_id = 420 "
                "ORDER BY game_creation DESC"
            ).fetchall()
        for row in rows:
            try:
                match = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            participant = next(
                (
                    item for item in match.get("info", {}).get("participants", [])
                    if item.get("puuid") == puuid
                ),
                None,
            )
            if not participant:
                continue
            return {
                "champion_id": str(participant.get("championName") or ""),
                "position": str(
                    participant.get("teamPosition")
                    or participant.get("individualPosition")
                    or "UNKNOWN"
                ).upper(),
                "kills": int(participant.get("kills") or 0),
                "deaths": int(participant.get("deaths") or 0),
                "assists": int(participant.get("assists") or 0),
                "won": bool(participant.get("win")),
            }
        return None

    def relationship_record(
        self, my_puuid: str, other_puuid: str, limit: int = 100
    ) -> tuple[int, int, int, int]:
        summary = self.relationship_summary(my_puuid, other_puuid, limit=limit)
        return (
            int(summary["together_games"]),
            int(summary["together_wins"]),
            int(summary["against_games"]),
            int(summary["against_my_wins"]),
        )

    def relationship_summary(
        self, my_puuid: str, other_puuid: str, limit: int = 1000
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "together_games": 0,
            "together_wins": 0,
            "against_games": 0,
            "against_my_wins": 0,
            "recent_10_together_games": 0,
            "recent_10_against_games": 0,
            "last_met_game_number": 0,
            "last_met_same_team": None,
            "last_met_my_win": None,
            "last_met_my_champion_id": "",
            "last_met_other_champion_id": "",
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches ORDER BY game_creation DESC"
            ).fetchall()
        inspected_my_games = 0
        for row in rows:
            try:
                participants = json.loads(row["payload_json"]).get("info", {}).get("participants", [])
            except (ValueError, TypeError):
                continue
            mine = next((item for item in participants if item.get("puuid") == my_puuid), None)
            if not mine:
                continue
            inspected_my_games += 1
            other = next((item for item in participants if item.get("puuid") == other_puuid), None)
            if other:
                won = bool(mine.get("win"))
                same_team = mine.get("teamId") == other.get("teamId")
                if same_team:
                    result["together_games"] += 1
                    result["together_wins"] += int(won)
                    if inspected_my_games <= 10:
                        result["recent_10_together_games"] += 1
                else:
                    result["against_games"] += 1
                    result["against_my_wins"] += int(won)
                    if inspected_my_games <= 10:
                        result["recent_10_against_games"] += 1
                if not result["last_met_game_number"]:
                    result["last_met_game_number"] = inspected_my_games
                    result["last_met_same_team"] = same_team
                    result["last_met_my_win"] = won
                    result["last_met_my_champion_id"] = str(mine.get("championName") or "")
                    result["last_met_other_champion_id"] = str(other.get("championName") or "")
            if inspected_my_games >= limit:
                break
        return result

    def personal_stat(
        self,
        puuid: str,
        champion_id: str,
        enemy_support_id: str | None,
        limit: int = 1000,
    ) -> PersonalStat:
        return self.personal_stats(
            puuid, [champion_id], enemy_support_id, limit=limit
        )[champion_id]

    def personal_stats(
        self,
        puuid: str,
        champion_ids: Iterable[str],
        enemy_support_id: str | None,
        limit: int = 1000,
    ) -> dict[str, PersonalStat]:
        unique_ids = list(dict.fromkeys(champion_id for champion_id in champion_ids if champion_id))
        stats = {champion_id: PersonalStat() for champion_id in unique_ids}
        totals = {
            champion_id: {"kills": 0.0, "deaths": 0.0, "assists": 0.0, "vision": 0.0}
            for champion_id in unique_ids
        }
        if not stats or not puuid:
            return stats
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM matches WHERE queue_id = 420 ORDER BY game_creation DESC"
            ).fetchall()

        inspected = 0
        for row in rows:
            try:
                match = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            participants = match.get("info", {}).get("participants", [])
            mine = next((p for p in participants if p.get("puuid") == puuid), None)
            if not mine:
                continue
            inspected += 1
            champion_id = str(mine.get("championName") or "")
            stat = stats.get(champion_id)
            position = str(mine.get("teamPosition") or mine.get("individualPosition") or "").upper()
            if stat is not None and position in {"UTILITY", "SUPPORT"}:
                stat.games += 1
                won = bool(mine.get("win"))
                stat.wins += int(won)
                stat.losses += int(not won)
                totals[champion_id]["kills"] += float(mine.get("kills", 0))
                totals[champion_id]["deaths"] += float(mine.get("deaths", 0))
                totals[champion_id]["assists"] += float(mine.get("assists", 0))
                totals[champion_id]["vision"] += float(mine.get("visionScore", 0))

                if enemy_support_id:
                    enemy = next(
                        (
                            p for p in participants
                            if p.get("teamId") != mine.get("teamId")
                            and str(p.get("teamPosition") or p.get("individualPosition") or "").upper()
                            in {"UTILITY", "SUPPORT"}
                        ),
                        None,
                    )
                    if enemy and enemy.get("championName") == enemy_support_id:
                        stat.matchup_games += 1
                        stat.matchup_wins += int(won)
                        stat.matchup_losses += int(not won)
            if inspected >= limit:
                break

        for champion_id, stat in stats.items():
            if stat.games:
                stat.win_rate = stat.wins / stat.games * 100
                stat.kda = (
                    totals[champion_id]["kills"] + totals[champion_id]["assists"]
                ) / max(totals[champion_id]["deaths"], 1.0)
                stat.vision_score = totals[champion_id]["vision"] / stat.games
            if stat.matchup_games:
                stat.matchup_win_rate = stat.matchup_wins / stat.matchup_games * 100
        return stats
