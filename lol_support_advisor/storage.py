from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Iterator

from .history import MatchLpChange, RankSnapshot
from .models import (
    ChampionBuildGuide, GamePrediction, JungleTendencyStat,
    OpggMcpSummonerProfile, OpggSnapshot,
    OpggSynergySnapshot, PersonalStat,
    PlayerBehaviorStat,
)


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._settings_cache_lock = threading.RLock()
        # SQLite commits may wait for another writer for up to 20 seconds.
        # Never hold the tiny reader lock during that wait: the Tk thread reads
        # settings for status labels and must be able to keep using the previous
        # committed snapshot while a background writer finishes.
        self._settings_write_lock = threading.Lock()
        self._settings_cache: dict[str, str] = {}
        # Live-game analysis asks several independent questions about the same
        # locally cached match history.  Decoding every 80KB Match-v5 payload
        # again for each question can consume seconds of GIL time and starve Tk
        # even when the callers run on background threads.  Keep one immutable
        # row collection per Storage instance and rebuild it only after matches
        # are written.  The lock is intentionally held while the cache is built
        # so simultaneous profile workers cannot perform duplicate full scans.
        self._decoded_match_cache_lock = threading.Lock()
        self._decoded_match_cache: tuple[tuple[int, dict[str, Any]], ...] | None = None
        # Header count reads must never wait behind the expensive first decode
        # of every Match-v5 payload.  Keep this tiny revision cache on its own
        # lock so Tk can query it while analysis workers build their cache.
        self._match_revision_lock = threading.Lock()
        self._match_revision_cache: tuple[int, int] | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._load_settings_cache()

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
                CREATE TABLE IF NOT EXISTS opgg_player_profiles (
                    riot_id TEXT PRIMARY KEY COLLATE NOCASE,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opgg_synergy_snapshots (
                    cache_key TEXT PRIMARY KEY COLLATE NOCASE,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS build_guides (
                    cache_key TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    game_creation INTEGER NOT NULL DEFAULT 0,
                    queue_id INTEGER NOT NULL DEFAULT 0,
                    cached_at TEXT NOT NULL DEFAULT '',
                    protected_owner_puuid TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS player_match_pages (
                    riot_id TEXT NOT NULL COLLATE NOCASE,
                    page_start INTEGER NOT NULL,
                    puuid TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    has_more INTEGER NOT NULL DEFAULT 1,
                    match_ids_json TEXT NOT NULL,
                    PRIMARY KEY(riot_id, page_start)
                );
                CREATE INDEX IF NOT EXISTS idx_player_match_pages_updated
                    ON player_match_pages(updated_at);
                CREATE TABLE IF NOT EXISTS game_predictions (
                    prediction_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    match_id TEXT UNIQUE,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_game_predictions_created_at
                    ON game_predictions(created_at DESC);
                CREATE TABLE IF NOT EXISTS rank_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    puuid TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    session_key TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL,
                    division TEXT NOT NULL DEFAULT '',
                    league_points INTEGER NOT NULL,
                    wins INTEGER NOT NULL,
                    losses INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'RIOT_LEAGUE_V4'
                );
                CREATE INDEX IF NOT EXISTS idx_rank_snapshots_player_time
                    ON rank_snapshots(puuid, observed_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rank_snapshots_session_stage
                    ON rank_snapshots(puuid, session_key, stage)
                    WHERE session_key <> '';
                CREATE TABLE IF NOT EXISTS match_lp_changes (
                    match_id TEXT PRIMARY KEY,
                    puuid TEXT NOT NULL,
                    before_snapshot_id INTEGER NOT NULL,
                    after_snapshot_id INTEGER NOT NULL,
                    before_tier TEXT NOT NULL,
                    before_division TEXT NOT NULL DEFAULT '',
                    before_lp INTEGER NOT NULL,
                    after_tier TEXT NOT NULL,
                    after_division TEXT NOT NULL DEFAULT '',
                    after_lp INTEGER NOT NULL,
                    lp_delta INTEGER,
                    confidence TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    UNIQUE(puuid, before_snapshot_id, after_snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_match_lp_changes_player
                    ON match_lp_changes(puuid, resolved_at DESC);
                """
            )
            match_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(matches)").fetchall()
            }
            if "cached_at" not in match_columns:
                connection.execute(
                    "ALTER TABLE matches ADD COLUMN cached_at TEXT NOT NULL DEFAULT ''"
                )
            if "protected_owner_puuid" not in match_columns:
                connection.execute(
                    "ALTER TABLE matches ADD COLUMN protected_owner_puuid "
                    "TEXT NOT NULL DEFAULT ''"
                )
            # Old databases cannot tell when a non-owner match was cached.
            # Give those rows one full retention window instead of guessing
            # from the date the game itself was played.
            connection.execute(
                "UPDATE matches SET cached_at = ? WHERE cached_at = ''",
                (datetime.now().isoformat(timespec="seconds"),),
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

    def _load_settings_cache(self) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        with self._settings_cache_lock:
            self._settings_cache = {
                str(row["key"]): str(row["value"]) for row in rows
            }

    def get_setting(self, key: str, default: str = "") -> str:
        with self._settings_cache_lock:
            return self._settings_cache.get(key, default)

    def get_int_setting(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        """Read one bounded integer preference without trusting stored text."""
        try:
            value = int(self.get_setting(key, str(default)).strip())
        except (TypeError, ValueError):
            return default
        return value if minimum <= value <= maximum else default

    def set_setting(self, key: str, value: str) -> None:
        stored_value = str(value)
        # Serialize the commit and cache publication. Readers either observe the
        # previous committed value or the new committed value, never an update
        # that later fails to persist.
        with self._settings_write_lock:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, stored_value),
                )
            with self._settings_cache_lock:
                self._settings_cache[key] = stored_value

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

    def opgg_cooldown_remaining(
        self, now: datetime | None = None, minutes: int = 60
    ) -> timedelta:
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
        self, now: datetime | None = None, minutes: int = 24 * 60
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

    @staticmethod
    def _cache_job_setting(job_key: str) -> str:
        return f"cache_job_success:{str(job_key).strip().lower()}"

    def cache_job_cooldown_remaining(
        self,
        job_key: str,
        now: datetime | None = None,
        hours: int = 24,
    ) -> timedelta:
        raw = self.get_setting(self._cache_job_setting(job_key))
        if not raw:
            return timedelta(0)
        try:
            completed_at = datetime.fromisoformat(raw)
        except ValueError:
            return timedelta(0)
        remaining = completed_at + timedelta(hours=hours) - (now or datetime.now())
        return max(remaining, timedelta(0))

    def mark_cache_job_success(
        self, job_key: str, when: datetime | None = None
    ) -> None:
        self.set_setting(
            self._cache_job_setting(job_key),
            (when or datetime.now()).isoformat(timespec="seconds"),
        )

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

    def save_opgg_position_catalog(
        self, position: str, patch: str, champion_ids: Iterable[str],
        when: datetime | None = None,
    ) -> None:
        payload = {
            "updated_at": (when or datetime.now()).isoformat(timespec="seconds"),
            "patch": str(patch or "UNKNOWN"),
            "champion_ids": list(dict.fromkeys(
                str(champion_id) for champion_id in champion_ids if champion_id
            )),
        }
        self.set_setting(
            f"opgg_position_catalog:{str(position or 'SUPPORT').upper()}",
            json.dumps(payload, ensure_ascii=False),
        )

    def load_opgg_position_catalog(
        self, position: str, max_age: timedelta | None = timedelta(hours=24),
    ) -> tuple[str, list[str]] | None:
        raw = self.get_setting(
            f"opgg_position_catalog:{str(position or 'SUPPORT').upper()}"
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            updated_at = datetime.fromisoformat(str(payload.get("updated_at") or ""))
            champion_ids = [
                str(champion_id) for champion_id in payload.get("champion_ids") or []
                if champion_id
            ]
        except (ValueError, TypeError, AttributeError):
            return None
        if max_age is not None and datetime.now() - updated_at > max_age:
            return None
        if not champion_ids:
            return None
        return str(payload.get("patch") or "UNKNOWN"), champion_ids

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

    def load_opgg_snapshots_for_position(
        self, position: str = "SUPPORT"
    ) -> dict[str, OpggSnapshot]:
        """Load a position's per-champion snapshots with one database read."""
        normalized_position = str(position or "SUPPORT").upper()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM opgg_snapshots WHERE cache_key LIKE ?",
                (f"{normalized_position}:%",),
            ).fetchall()
        snapshots: dict[str, OpggSnapshot] = {}
        for row in rows:
            try:
                snapshot = OpggSnapshot.from_dict(json.loads(row["payload_json"]))
            except (ValueError, TypeError, KeyError):
                continue
            if snapshot.enemy_support_id:
                snapshots[snapshot.enemy_support_id] = snapshot
        return snapshots

    def save_opgg_synergy_snapshot(self, snapshot: OpggSynergySnapshot) -> None:
        cache_key = (
            f"{snapshot.ally_champion_id}:"
            f"{snapshot.ally_position}:{snapshot.candidate_position}"
        ).upper()
        updated_at = snapshot.fetched_at or datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO opgg_synergy_snapshots(cache_key, updated_at, payload_json) "
                "VALUES(?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (
                    cache_key, updated_at,
                    json.dumps(snapshot.to_dict(), ensure_ascii=False),
                ),
            )

    def load_opgg_synergy_snapshot(
        self,
        ally_champion_id: str,
        ally_position: str = "BOTTOM",
        candidate_position: str = "SUPPORT",
        max_age: timedelta | None = None,
    ) -> OpggSynergySnapshot | None:
        cache_key = (
            f"{ally_champion_id}:{ally_position}:{candidate_position}"
        ).upper()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at, payload_json FROM opgg_synergy_snapshots "
                "WHERE cache_key = ?", (cache_key,),
            ).fetchone()
        if not row:
            return None
        if max_age is not None:
            try:
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
            except ValueError:
                return None
            if datetime.now() - updated_at > max_age:
                return None
        try:
            return OpggSynergySnapshot.from_dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError, KeyError):
            return None

    def save_opgg_player_profile(self, profile: OpggMcpSummonerProfile) -> None:
        updated_at = profile.fetched_at or datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO opgg_player_profiles(riot_id, updated_at, payload_json) "
                "VALUES(?, ?, ?) ON CONFLICT(riot_id) DO UPDATE SET "
                "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (
                    profile.riot_id,
                    updated_at,
                    json.dumps(profile.to_dict(), ensure_ascii=False),
                ),
            )

    def load_opgg_player_profile(
        self,
        riot_id: str,
        max_age: timedelta = timedelta(hours=24),
    ) -> OpggMcpSummonerProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at, payload_json FROM opgg_player_profiles "
                "WHERE riot_id = ? COLLATE NOCASE",
                (riot_id,),
            ).fetchone()
        if not row:
            return None
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
            if datetime.now() - updated_at > max_age:
                return None
            return OpggMcpSummonerProfile.from_dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError, KeyError):
            return None

    def load_opgg_player_profile_any_age(
        self, riot_id: str
    ) -> OpggMcpSummonerProfile | None:
        return self.load_opgg_player_profile(riot_id, max_age=timedelta(days=3650))

    @staticmethod
    def _build_cache_key(champion_id: str, position: str) -> str:
        return f"{str(position or 'SUPPORT').upper()}:{champion_id}"

    def save_build_guide(self, guide: ChampionBuildGuide) -> None:
        cache_key = self._build_cache_key(guide.champion_id, guide.position)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO build_guides(cache_key, updated_at, payload_json) VALUES(?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (
                    cache_key,
                    guide.updated_at,
                    json.dumps(guide.to_dict(), ensure_ascii=False),
                ),
            )

    def load_build_guide(
        self, champion_id: str, position: str = "SUPPORT"
    ) -> ChampionBuildGuide | None:
        cache_key = self._build_cache_key(champion_id, position)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM build_guides WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if not row:
            return None
        try:
            return ChampionBuildGuide.from_dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError, KeyError):
            return None

    def load_build_guides_for_position(
        self, position: str = "SUPPORT"
    ) -> dict[str, ChampionBuildGuide]:
        """Load a position's build guides with one database read for cache UI."""
        normalized_position = str(position or "SUPPORT").upper()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM build_guides WHERE cache_key LIKE ?",
                (f"{normalized_position}:%",),
            ).fetchall()
        guides: dict[str, ChampionBuildGuide] = {}
        for row in rows:
            try:
                guide = ChampionBuildGuide.from_dict(json.loads(row["payload_json"]))
            except (ValueError, TypeError, KeyError):
                continue
            guides[guide.champion_id] = guide
        return guides

    def build_guide_cooldown_remaining(
        self, champion_id: str, position: str = "SUPPORT",
        now: datetime | None = None, hours: int = 24,
    ) -> timedelta:
        cache_key = self._build_cache_key(champion_id, position)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM build_guides WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if not row:
            return timedelta(0)
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
        except ValueError:
            return timedelta(0)
        remaining = updated_at + timedelta(hours=hours) - (now or datetime.now())
        return max(remaining, timedelta(0))

    def save_game_prediction(self, prediction: GamePrediction) -> GamePrediction:
        """Persist the first eligible estimate as the immutable accuracy baseline."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at, match_id, payload_json FROM game_predictions "
                "WHERE prediction_key = ?",
                (prediction.prediction_key,),
            ).fetchone()
            if existing:
                try:
                    stored = GamePrediction.from_dict(
                        json.loads(existing["payload_json"])
                    )
                    stored.captured_at = str(existing["created_at"])
                    return stored
                except (ValueError, TypeError, KeyError):
                    # A corrupt row can be repaired only while it has not yet
                    # been attached to a real match.
                    if existing["match_id"]:
                        return prediction
                    connection.execute(
                        "UPDATE game_predictions SET created_at = ?, updated_at = ?, "
                        "payload_json = ? WHERE prediction_key = ? AND match_id IS NULL",
                        (
                            prediction.captured_at,
                            now,
                            json.dumps(prediction.to_dict(), ensure_ascii=False),
                            prediction.prediction_key,
                        ),
                    )
                    return prediction
            connection.execute(
                "INSERT INTO game_predictions("
                "prediction_key, created_at, updated_at, match_id, payload_json"
                ") VALUES(?, ?, ?, NULL, ?)",
                (
                    prediction.prediction_key,
                    prediction.captured_at,
                    now,
                    json.dumps(prediction.to_dict(), ensure_ascii=False),
                ),
            )
        return prediction

    def load_game_prediction_by_key(
        self, prediction_key: str,
    ) -> GamePrediction | None:
        if not prediction_key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at, payload_json FROM game_predictions "
                "WHERE prediction_key = ?",
                (prediction_key,),
            ).fetchone()
        if not row:
            return None
        try:
            prediction = GamePrediction.from_dict(json.loads(row["payload_json"]))
        except (ValueError, TypeError, KeyError):
            return None
        prediction.captured_at = str(row["created_at"])
        return prediction

    def load_game_predictions(
        self, match_ids: Iterable[str],
    ) -> dict[str, GamePrediction]:
        ids = list(dict.fromkeys(str(match_id) for match_id in match_ids if match_id))
        predictions: dict[str, GamePrediction] = {}
        with self._connect() as connection:
            for offset in range(0, len(ids), 400):
                chunk = ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT match_id, payload_json FROM game_predictions "
                    f"WHERE match_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    try:
                        prediction = GamePrediction.from_dict(
                            json.loads(row["payload_json"])
                        )
                    except (ValueError, TypeError, KeyError):
                        continue
                    predictions[str(row["match_id"])] = prediction
        return predictions

    @staticmethod
    def _prediction_riot_id(participant: dict[str, Any]) -> str:
        game_name = str(
            participant.get("riotIdGameName")
            or participant.get("summonerName") or ""
        ).strip()
        tag_line = str(
            participant.get("riotIdTagline")
            or participant.get("riotIdTagLine") or ""
        ).strip()
        return f"{game_name}#{tag_line}" if game_name and tag_line else game_name

    def resolve_game_predictions(self, matches: Iterable[dict[str, Any]]) -> int:
        """Attach recent pending estimates to Riot matches using time and roster evidence."""
        match_list = list(matches)
        if not match_list:
            return 0
        newest_creation = max(
            (
                int((match.get("info") or {}).get("gameCreation") or 0)
                for match in match_list
            ),
            default=0,
        )
        oldest_allowed = datetime.fromtimestamp(
            max(0, newest_creation) / 1000
        ) - timedelta(hours=12)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT prediction_key, created_at, payload_json "
                "FROM game_predictions WHERE match_id IS NULL AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 30",
                (oldest_allowed.isoformat(timespec="seconds"),),
            ).fetchall()
        pending: list[GamePrediction] = []
        for row in rows:
            try:
                prediction = GamePrediction.from_dict(json.loads(row["payload_json"]))
                prediction.captured_at = str(row["created_at"])
            except (ValueError, TypeError, KeyError):
                continue
            pending.append(prediction)

        resolved = 0
        used_predictions: set[str] = set()
        for match in sorted(
            match_list,
            key=lambda item: int((item.get("info") or {}).get("gameCreation") or 0),
            reverse=True,
        ):
            info = match.get("info") or {}
            participants = info.get("participants") or []
            if int(info.get("queueId") or 0) != 420:
                continue
            match_id = str(
                (match.get("metadata") or {}).get("matchId")
                or info.get("gameId") or ""
            )
            if not match_id or not participants:
                continue
            game_creation = int(info.get("gameCreation") or 0)
            duration = int(info.get("gameDuration") or 0)
            game_start = datetime.fromtimestamp(game_creation / 1000)
            game_end = game_start + timedelta(seconds=max(duration, 60))
            best: tuple[float, GamePrediction, dict[str, Any]] | None = None
            for prediction in pending:
                if prediction.prediction_key in used_predictions:
                    continue
                try:
                    captured = datetime.fromisoformat(prediction.captured_at)
                except ValueError:
                    continue
                if not (
                    game_start - timedelta(minutes=10)
                    <= captured <= game_end + timedelta(hours=2)
                ):
                    continue
                active = next(
                    (
                        participant for participant in participants
                        if self._prediction_riot_id(participant).casefold()
                        == prediction.active_riot_id.casefold()
                    ),
                    None,
                )
                if not active:
                    continue
                active_team = active.get("teamId")
                allies = [
                    participant for participant in participants
                    if participant.get("teamId") == active_team
                ]
                enemies = [
                    participant for participant in participants
                    if participant.get("teamId") != active_team
                ]
                ally_champions = sorted(
                    str(participant.get("championName") or "").casefold()
                    for participant in allies
                )
                enemy_champions = sorted(
                    str(participant.get("championName") or "").casefold()
                    for participant in enemies
                )
                predicted_allies = sorted(
                    champion.casefold() for champion in prediction.ally_champion_ids
                )
                predicted_enemies = sorted(
                    champion.casefold() for champion in prediction.enemy_champion_ids
                )
                score = 5.0
                if str(active.get("championName") or "").casefold() == (
                    prediction.active_champion_id.casefold()
                ):
                    score += 2.0
                score += 5.0 if ally_champions == predicted_allies else 0.0
                score += 5.0 if enemy_champions == predicted_enemies else 0.0
                ally_ids = {
                    self._prediction_riot_id(participant).casefold()
                    for participant in allies if self._prediction_riot_id(participant)
                }
                enemy_ids = {
                    self._prediction_riot_id(participant).casefold()
                    for participant in enemies if self._prediction_riot_id(participant)
                }
                predicted_ally_ids = {
                    riot_id.casefold() for riot_id in prediction.ally_riot_ids
                }
                predicted_enemy_ids = {
                    riot_id.casefold() for riot_id in prediction.enemy_riot_ids
                }
                score += 3.0 * len(ally_ids & predicted_ally_ids) / max(
                    len(predicted_ally_ids), 1
                )
                score += 3.0 * len(enemy_ids & predicted_enemy_ids) / max(
                    len(predicted_enemy_ids), 1
                )
                if best is None or score > best[0]:
                    best = score, prediction, active
            if not best or best[0] < 9.0:
                continue
            _score, prediction, active = best
            prediction.match_id = match_id
            prediction.actual_win = bool(active.get("win"))
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE OR IGNORE game_predictions SET match_id = ?, updated_at = ?, "
                    "payload_json = ? WHERE prediction_key = ? AND match_id IS NULL",
                    (
                        match_id, datetime.now().isoformat(timespec="seconds"),
                        json.dumps(prediction.to_dict(), ensure_ascii=False),
                        prediction.prediction_key,
                    ),
                )
                if cursor.rowcount == 0:
                    # A roster can be sampled under more than one prediction
                    # key while the loading screen settles.  If an earlier
                    # baseline already owns this real match, preserve it and
                    # discard only the still-pending duplicate.  Without this
                    # guard SQLite's UNIQUE(match_id) aborted the entire match
                    # history load.
                    connection.execute(
                        "DELETE FROM game_predictions "
                        "WHERE prediction_key = ? AND match_id IS NULL "
                        "AND EXISTS(SELECT 1 FROM game_predictions "
                        "WHERE match_id = ?)",
                        (prediction.prediction_key, match_id),
                    )
            used_predictions.add(prediction.prediction_key)
            if cursor.rowcount > 0:
                resolved += 1
        return resolved

    @staticmethod
    def _rank_snapshot_from_row(row: sqlite3.Row) -> RankSnapshot:
        return RankSnapshot(
            snapshot_id=int(row["snapshot_id"]),
            puuid=str(row["puuid"]),
            observed_at=str(row["observed_at"]),
            stage=str(row["stage"]),
            session_key=str(row["session_key"]),
            tier=str(row["tier"]),
            division=str(row["division"]),
            league_points=int(row["league_points"]),
            wins=int(row["wins"]),
            losses=int(row["losses"]),
            source=str(row["source"]),
        )

    def save_rank_snapshot(
        self,
        puuid: str,
        solo_entry: dict[str, Any] | None,
        *,
        stage: str,
        session_key: str = "",
        observed_at: datetime | str | None = None,
        source: str = "RIOT_LEAGUE_V4",
    ) -> RankSnapshot | None:
        """Persist one pre/post solo-rank observation.

        A keyed PRE observation is immutable: retrying after a restart must not
        replace the actual baseline with a post-game rank.  Keyed POST rows are
        updated in place so Riot's initially stale league response can settle.
        """
        normalized_puuid = str(puuid or "").strip()
        if not normalized_puuid:
            return None
        entry = dict(solo_entry or {})
        queue_type = str(
            entry.get("queueType") or "RANKED_SOLO_5x5"
        ).upper()
        if queue_type != "RANKED_SOLO_5X5":
            return None
        normalized_stage = str(stage or "OBSERVED").strip().upper()
        normalized_session = str(session_key or "").strip()
        if isinstance(observed_at, datetime):
            stamp = observed_at.isoformat(timespec="seconds")
        elif observed_at is None:
            stamp = datetime.now().isoformat(timespec="seconds")
        else:
            stamp = str(observed_at).strip()
            # Reject malformed timestamps now rather than creating an
            # observation that can never be matched safely.
            datetime.fromisoformat(stamp)
        tier = str(entry.get("tier") or "UNRANKED").strip().upper()
        division = str(entry.get("rank") or "").strip().upper()

        def bounded_integer(key: str) -> int:
            try:
                return max(0, int(entry.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        values = (
            tier, division, bounded_integer("leaguePoints"),
            bounded_integer("wins"), bounded_integer("losses"),
            str(source or "RIOT_LEAGUE_V4").strip() or "RIOT_LEAGUE_V4",
        )
        with self._connect() as connection:
            existing = None
            if normalized_session:
                existing = connection.execute(
                    "SELECT * FROM rank_snapshots WHERE puuid = ? "
                    "AND session_key = ? AND stage = ?",
                    (normalized_puuid, normalized_session, normalized_stage),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT * FROM rank_snapshots WHERE puuid = ? "
                    "AND session_key = '' AND stage = ? AND observed_at = ?",
                    (normalized_puuid, normalized_stage, stamp),
                ).fetchone()
            if existing and normalized_stage == "PRE":
                return self._rank_snapshot_from_row(existing)
            if existing:
                connection.execute(
                    "UPDATE rank_snapshots SET observed_at = ?, tier = ?, "
                    "division = ?, league_points = ?, wins = ?, losses = ?, "
                    "source = ? WHERE snapshot_id = ?",
                    (stamp, *values, int(existing["snapshot_id"])),
                )
                snapshot_id = int(existing["snapshot_id"])
            else:
                cursor = connection.execute(
                    "INSERT INTO rank_snapshots("
                    "puuid, observed_at, stage, session_key, tier, division, "
                    "league_points, wins, losses, source"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized_puuid, stamp, normalized_stage,
                        normalized_session, *values,
                    ),
                )
                snapshot_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM rank_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return self._rank_snapshot_from_row(row)

    def load_rank_snapshots(
        self, puuid: str, *, limit: int = 200,
    ) -> list[RankSnapshot]:
        if not puuid or limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ("
                "SELECT * FROM rank_snapshots WHERE puuid = ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT ?"
                ") ORDER BY observed_at ASC, snapshot_id ASC",
                (puuid, int(limit)),
            ).fetchall()
        return [self._rank_snapshot_from_row(row) for row in rows]

    @staticmethod
    def _match_lp_change_from_row(row: sqlite3.Row) -> MatchLpChange:
        raw_delta = row["lp_delta"]
        return MatchLpChange(
            match_id=str(row["match_id"]),
            puuid=str(row["puuid"]),
            before_snapshot_id=int(row["before_snapshot_id"]),
            after_snapshot_id=int(row["after_snapshot_id"]),
            before_tier=str(row["before_tier"]),
            before_division=str(row["before_division"]),
            before_lp=int(row["before_lp"]),
            after_tier=str(row["after_tier"]),
            after_division=str(row["after_division"]),
            after_lp=int(row["after_lp"]),
            lp_delta=int(raw_delta) if raw_delta is not None else None,
            confidence=str(row["confidence"]),
            resolved_at=str(row["resolved_at"]),
        )

    def load_match_lp_changes(
        self, match_ids: Iterable[str],
    ) -> dict[str, MatchLpChange]:
        ids = list(dict.fromkeys(str(match_id) for match_id in match_ids if match_id))
        result: dict[str, MatchLpChange] = {}
        with self._connect() as connection:
            for offset in range(0, len(ids), 400):
                chunk = ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM match_lp_changes WHERE match_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    change = self._match_lp_change_from_row(row)
                    result[change.match_id] = change
        return result

    @staticmethod
    def _snapshot_epoch(snapshot: RankSnapshot) -> float:
        return datetime.fromisoformat(snapshot.observed_at).timestamp()

    @staticmethod
    def _match_end_epoch(match: dict[str, Any]) -> float:
        info = match.get("info") or {}
        try:
            explicit = int(info.get("gameEndTimestamp") or 0)
        except (TypeError, ValueError):
            explicit = 0
        if explicit:
            return explicit / 1000.0
        try:
            creation = int(info.get("gameCreation") or 0) / 1000.0
        except (TypeError, ValueError):
            creation = 0.0
        try:
            duration = max(0, int(info.get("gameDuration") or 0))
        except (TypeError, ValueError):
            duration = 0
        return creation + duration

    @staticmethod
    def _match_player_result(
        match: dict[str, Any], puuid: str,
    ) -> bool | None:
        participants = (match.get("info") or {}).get("participants") or []
        player = next(
            (participant for participant in participants
             if str(participant.get("puuid") or "") == puuid),
            None,
        )
        return bool(player.get("win")) if player is not None else None

    @staticmethod
    def _rank_transition_delta(
        before: RankSnapshot, after: RankSnapshot, won: bool,
    ) -> tuple[int | None, str]:
        same_rank = (
            before.tier == after.tier
            and before.division == after.division
            and before.tier != "UNRANKED"
        )
        if not same_rank:
            # League-v4 gives states, not the awarded LP.  Across promotion,
            # demotion, placements, or apex tier changes retain the transition
            # but do not invent a numeric 100-LP conversion.
            return None, "TRANSITION"
        delta = after.league_points - before.league_points
        if (won and delta < 0) or (not won and delta > 0):
            return None, "UNKNOWN"
        return delta, "EXACT"

    def resolve_match_lp_changes(
        self, puuid: str, matches: Iterable[dict[str, Any]],
    ) -> int:
        """Link only unambiguous PRE/POST rank observations to solo matches."""
        snapshots = self.load_rank_snapshots(puuid)
        match_list = list(matches)
        if not puuid or len(snapshots) < 2 or not match_list:
            return 0
        with self._connect() as connection:
            existing_rows = connection.execute(
                "SELECT match_id, before_snapshot_id, after_snapshot_id "
                "FROM match_lp_changes WHERE puuid = ?",
                (puuid,),
            ).fetchall()
        used_matches = {str(row["match_id"]) for row in existing_rows}
        used_pairs = {
            (int(row["before_snapshot_id"]), int(row["after_snapshot_id"]))
            for row in existing_rows
        }
        solo_matches: list[tuple[str, float, bool]] = []
        for match in match_list:
            info = match.get("info") or {}
            if int(info.get("queueId") or 0) != 420:
                continue
            match_id = str(
                (match.get("metadata") or {}).get("matchId")
                or info.get("gameId") or ""
            )
            result = self._match_player_result(match, puuid)
            if not match_id or result is None:
                continue
            solo_matches.append((match_id, self._match_end_epoch(match), result))

        resolved = 0
        for before_index, before in enumerate(snapshots[:-1]):
            if before.stage != "PRE":
                continue
            before_time = self._snapshot_epoch(before)
            for after in snapshots[before_index + 1:]:
                if after.stage == "PRE":
                    # A new tracked game started before the former session got
                    # a trustworthy post state. Never span across it.
                    break
                if after.stage not in {"POST", "RECOVERY"}:
                    continue
                if (
                    before.session_key and after.session_key
                    and before.session_key != after.session_key
                ):
                    continue
                pair = (before.snapshot_id, after.snapshot_id)
                if pair in used_pairs:
                    break
                wins_delta = after.wins - before.wins
                losses_delta = after.losses - before.losses
                if wins_delta < 0 or losses_delta < 0:
                    break
                if wins_delta + losses_delta == 0:
                    continue
                if wins_delta + losses_delta != 1:
                    break
                after_time = self._snapshot_epoch(after)
                candidates = [
                    item for item in solo_matches
                    if item[0] not in used_matches
                    and before_time < item[1] <= after_time
                ]
                if len(candidates) != 1:
                    continue
                match_id, _ended_at, won = candidates[0]
                if (wins_delta, losses_delta) != ((1, 0) if won else (0, 1)):
                    continue
                lp_delta, confidence = self._rank_transition_delta(
                    before, after, won,
                )
                resolved_at = datetime.now().isoformat(timespec="seconds")
                with self._connect() as connection:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO match_lp_changes("
                        "match_id, puuid, before_snapshot_id, after_snapshot_id, "
                        "before_tier, before_division, before_lp, after_tier, "
                        "after_division, after_lp, lp_delta, confidence, resolved_at"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            match_id, puuid, before.snapshot_id,
                            after.snapshot_id, before.tier, before.division,
                            before.league_points, after.tier, after.division,
                            after.league_points, lp_delta, confidence, resolved_at,
                        ),
                    )
                if cursor.rowcount:
                    used_matches.add(match_id)
                    used_pairs.add(pair)
                    resolved += 1
                break
        return resolved

    def prune_non_owner_data(
        self,
        owner_puuid: str,
        owner_riot_id: str,
        now: datetime | None = None,
        days: int = 1,
    ) -> dict[str, int]:
        """Delete only stale non-owner cache rows.

        Owner match history is the durable local record and is never aged out.
        Malformed match payloads are also retained because absence of owner
        evidence cannot be proven safely.
        """
        normalized_puuid = str(owner_puuid or "").strip()
        normalized_riot_id = str(owner_riot_id or "").strip()
        if not normalized_puuid or not normalized_riot_id:
            raise ValueError("owner_puuid and owner_riot_id are required")
        current = now or datetime.now()
        cutoff = current - timedelta(days=max(1, int(days)))

        def stale_timestamp(value: object) -> bool:
            try:
                observed = datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return False
            comparable_cutoff = cutoff
            if observed.tzinfo is None and comparable_cutoff.tzinfo is not None:
                observed = observed.replace(tzinfo=comparable_cutoff.tzinfo)
            elif observed.tzinfo is not None and comparable_cutoff.tzinfo is None:
                comparable_cutoff = comparable_cutoff.replace(tzinfo=observed.tzinfo)
            return observed < comparable_cutoff

        deleted_match_rows: list[tuple[str, str]] = []
        deleted = {
            "matches": 0,
            "live_profiles": 0,
            "opgg_player_profiles": 0,
            "player_identities": 0,
            "player_match_pages": 0,
        }
        with self._connect() as connection:
            old_matches = connection.execute(
                "SELECT match_id, payload_json, cached_at FROM matches "
                "WHERE protected_owner_puuid = '' "
                "OR protected_owner_puuid != ?",
                (normalized_puuid,),
            ).fetchall()
            protected_owner_ids: list[str] = []
            for row in old_matches:
                if not stale_timestamp(row["cached_at"]):
                    continue
                try:
                    payload = json.loads(row["payload_json"])
                    participants = (payload.get("info") or {}).get("participants") or []
                except (ValueError, TypeError, AttributeError):
                    continue
                if any(
                    str(participant.get("puuid") or "") == normalized_puuid
                    for participant in participants
                    if isinstance(participant, dict)
                ):
                    protected_owner_ids.append(str(row["match_id"]))
                    continue
                deleted_match_rows.append(
                    (str(row["match_id"]), str(row["cached_at"]))
                )
            for offset in range(0, len(protected_owner_ids), 400):
                chunk = protected_owner_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                connection.execute(
                    f"UPDATE matches SET protected_owner_puuid = ? "
                    f"WHERE match_id IN ({placeholders})",
                    (normalized_puuid, *chunk),
                )
            deleted_match_ids: list[str] = []
            for match_id, cached_at in deleted_match_rows:
                cursor = connection.execute(
                    "DELETE FROM matches WHERE match_id = ? AND cached_at = ? "
                    "AND (protected_owner_puuid = '' OR protected_owner_puuid != ?)",
                    (match_id, cached_at, normalized_puuid),
                )
                if cursor.rowcount:
                    deleted_match_ids.append(match_id)
            deleted["matches"] = len(deleted_match_ids)

            page_rows = connection.execute(
                "SELECT riot_id, page_start, updated_at FROM player_match_pages"
            ).fetchall()
            stale_pages = [
                (
                    str(row["riot_id"]), int(row["page_start"]),
                    str(row["updated_at"]),
                )
                for row in page_rows
                if str(row["riot_id"]).casefold() != normalized_riot_id.casefold()
                and stale_timestamp(row["updated_at"])
            ]
            deleted_pages = 0
            for riot_id, page_start, updated_at in stale_pages:
                cursor = connection.execute(
                    "DELETE FROM player_match_pages "
                    "WHERE riot_id = ? COLLATE NOCASE AND page_start = ? "
                    "AND updated_at = ?",
                    (riot_id, page_start, updated_at),
                )
                deleted_pages += int(bool(cursor.rowcount))
            deleted["player_match_pages"] = deleted_pages

            live_rows = connection.execute(
                "SELECT riot_id, puuid, updated_at FROM live_profiles"
            ).fetchall()
            stale_live_ids = [
                (str(row["riot_id"]), str(row["updated_at"])) for row in live_rows
                if str(row["puuid"]) != normalized_puuid
                and str(row["riot_id"]).casefold() != normalized_riot_id.casefold()
                and stale_timestamp(row["updated_at"])
            ]
            deleted_live = 0
            for riot_id, updated_at in stale_live_ids:
                cursor = connection.execute(
                    "DELETE FROM live_profiles WHERE riot_id = ? AND updated_at = ?",
                    (riot_id, updated_at),
                )
                deleted_live += int(bool(cursor.rowcount))
            deleted["live_profiles"] = deleted_live

            opgg_rows = connection.execute(
                "SELECT riot_id, updated_at FROM opgg_player_profiles"
            ).fetchall()
            stale_opgg_ids = [
                (str(row["riot_id"]), str(row["updated_at"])) for row in opgg_rows
                if str(row["riot_id"]).casefold() != normalized_riot_id.casefold()
                and stale_timestamp(row["updated_at"])
            ]
            deleted_opgg = 0
            for riot_id, updated_at in stale_opgg_ids:
                cursor = connection.execute(
                    "DELETE FROM opgg_player_profiles WHERE riot_id = ? COLLATE NOCASE "
                    "AND updated_at = ?",
                    (riot_id, updated_at),
                )
                deleted_opgg += int(bool(cursor.rowcount))
            deleted["opgg_player_profiles"] = deleted_opgg

            identity_rows = connection.execute(
                "SELECT riot_id, puuid, updated_at FROM player_identities"
            ).fetchall()
            stale_identity_ids = [
                (str(row["riot_id"]), str(row["updated_at"])) for row in identity_rows
                if str(row["puuid"]) != normalized_puuid
                and str(row["riot_id"]).casefold() != normalized_riot_id.casefold()
                and stale_timestamp(row["updated_at"])
            ]
            deleted_identities = 0
            for riot_id, updated_at in stale_identity_ids:
                cursor = connection.execute(
                    "DELETE FROM player_identities WHERE riot_id = ? COLLATE NOCASE "
                    "AND updated_at = ?",
                    (riot_id, updated_at),
                )
                deleted_identities += int(bool(cursor.rowcount))
            deleted["player_identities"] = deleted_identities

        if deleted_match_ids:
            # Match-derived readers share both caches. Invalidating rather than
            # mutating in place avoids stale counts and stale decoded payloads.
            with self._match_revision_lock:
                self._match_revision_cache = None
            with self._decoded_match_cache_lock:
                self._decoded_match_cache = None
        return deleted

    def known_match_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT match_id FROM matches").fetchall()
        return {str(row["match_id"]) for row in rows}

    @staticmethod
    def _cached_match_id(match: dict[str, Any]) -> str:
        return str((match.get("metadata") or {}).get("matchId") or "")

    @staticmethod
    def _cached_game_creation(match: dict[str, Any]) -> int:
        return int((match.get("info") or {}).get("gameCreation") or 0)

    def _merge_decoded_match_cache(
        self, updates: list[tuple[str, int, dict[str, Any]]],
    ) -> None:
        """Apply committed match upserts without forcing another 69MB decode.

        If the cache has not been requested yet there is nothing to maintain.
        If it is being built, this waits for that single build and then replaces
        the matching rows, which keeps concurrent readers accurate without a
        second full database scan.
        """
        if not updates:
            return
        with self._match_revision_lock:
            # Upserts can replace an existing match, so invalidating the small
            # revision tuple is safer than assuming every saved payload adds one.
            self._match_revision_cache = None
        with self._decoded_match_cache_lock:
            if self._decoded_match_cache is None:
                return
            updated_ids = {match_id for match_id, _queue_id, _match in updates}
            merged = [
                row for row in self._decoded_match_cache
                if self._cached_match_id(row[1]) not in updated_ids
            ]
            merged.extend(
                (queue_id, match) for _match_id, queue_id, match in updates
            )
            merged.sort(
                key=lambda row: self._cached_game_creation(row[1]), reverse=True
            )
            self._decoded_match_cache = tuple(merged)

    def _decoded_match_rows(self) -> tuple[tuple[int, dict[str, Any]], ...]:
        """Return newest-first decoded matches shared by all local analyses.

        Match payloads are treated as read-only by storage analysis methods.
        Returning the cached objects avoids both the SQLite full fetch and the
        much more expensive repeated ``json.loads`` work.
        """
        with self._decoded_match_cache_lock:
            if self._decoded_match_cache is not None:
                return self._decoded_match_cache
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT queue_id, payload_json FROM matches "
                    "ORDER BY game_creation DESC"
                ).fetchall()
            decoded: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                try:
                    match = dict(json.loads(row["payload_json"]))
                except (ValueError, TypeError):
                    continue
                decoded.append((int(row["queue_id"]), match))
            self._decoded_match_cache = tuple(decoded)
            return self._decoded_match_cache

    def save_matches(
        self,
        matches: Iterable[dict[str, Any]],
        cached_at: datetime | None = None,
    ) -> int:
        saved = 0
        cache_updates: list[tuple[str, int, dict[str, Any]]] = []
        cache_stamp = (cached_at or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as connection:
            for match in matches:
                metadata = match.get("metadata", {})
                info = match.get("info", {})
                match_id = str(metadata.get("matchId", ""))
                if not match_id:
                    continue
                connection.execute(
                    "INSERT INTO matches(match_id, game_creation, queue_id, cached_at, payload_json) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET "
                    "game_creation = excluded.game_creation, queue_id = excluded.queue_id, "
                    "cached_at = excluded.cached_at, payload_json = excluded.payload_json",
                    (
                        match_id,
                        int(info.get("gameCreation", 0)),
                        int(info.get("queueId", 0)),
                        cache_stamp,
                        json.dumps(match, ensure_ascii=False),
                    ),
                )
                # Identity retention is based on when this program observed the
                # player, not when an old match happened. Otherwise newly
                # downloaded older pages would be deleted immediately.
                identity_stamp = cache_stamp
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
                            "updated_at = CASE "
                            "WHEN player_identities.updated_at >= excluded.updated_at "
                            "THEN player_identities.updated_at ELSE excluded.updated_at END",
                            (f"{game_name}#{tag_line}", participant_puuid, identity_stamp),
                        )
                cache_updates.append((match_id, int(info.get("queueId", 0)), match))
                saved += 1
        self._merge_decoded_match_cache(cache_updates)
        return saved

    def touch_match_cache(
        self, match_ids: Iterable[str], when: datetime | None = None,
    ) -> int:
        """Refresh retention for cached details reused by an explicit page."""
        unique_ids = list(dict.fromkeys(
            str(match_id).strip()
            for match_id in match_ids
            if str(match_id).strip()
        ))
        if not unique_ids:
            return 0
        stamp = (when or datetime.now()).isoformat(timespec="seconds")
        updated = 0
        with self._connect() as connection:
            for offset in range(0, len(unique_ids), 400):
                chunk = unique_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"UPDATE matches SET cached_at = ? "
                    f"WHERE match_id IN ({placeholders})",
                    (stamp, *chunk),
                )
                updated += max(int(cursor.rowcount or 0), 0)
        return updated

    def count_matches(self) -> int:
        return self.match_revision()[0]

    def match_revision(self) -> tuple[int, int]:
        with self._match_revision_lock:
            if self._match_revision_cache is not None:
                return self._match_revision_cache
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS count, "
                    "COALESCE(MAX(game_creation), 0) AS newest FROM matches"
                ).fetchone()
            self._match_revision_cache = (
                int(row["count"]), int(row["newest"])
            )
            return self._match_revision_cache

    def count_player_matches(self, puuid: str, limit: int = 1000) -> int:
        count = 0
        for queue_id, match in self._decoded_match_rows():
            if queue_id != 420:
                continue
            participants = match.get("info", {}).get("participants", [])
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

    def save_player_match_page(
        self,
        riot_id: str,
        puuid: str,
        page_start: int,
        match_ids: Iterable[str],
        has_more: bool,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist one explicit ten-game Riot page as its own cache unit."""
        normalized_id = str(riot_id or "").strip()
        normalized_puuid = str(puuid or "").strip()
        if not normalized_id or not normalized_puuid:
            return
        page = list(dict.fromkeys(
            str(match_id).strip()
            for match_id in match_ids
            if str(match_id).strip()
        ))[:10]
        stamp = (updated_at or datetime.now()).isoformat(timespec="seconds")
        page_start = max(int(page_start), 0)
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT match_ids_json FROM player_match_pages "
                "WHERE riot_id = ? COLLATE NOCASE AND page_start = ?",
                (normalized_id, page_start),
            ).fetchone()
            previous_ids = str(previous["match_ids_json"]) if previous else ""
            page_json = json.dumps(page, ensure_ascii=False)
            connection.execute(
                "INSERT INTO player_match_pages("
                "riot_id, page_start, puuid, updated_at, has_more, match_ids_json"
                ") VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(riot_id, page_start) DO UPDATE SET "
                "puuid = excluded.puuid, updated_at = excluded.updated_at, "
                "has_more = excluded.has_more, match_ids_json = excluded.match_ids_json",
                (
                    normalized_id,
                    page_start,
                    normalized_puuid,
                    stamp,
                    int(bool(has_more)),
                    page_json,
                ),
            )
            # A new match shifts every later offset. Keep deeper cached pages
            # only while the preceding page's exact IDs are unchanged.
            if previous is None or previous_ids != page_json or not has_more:
                connection.execute(
                    "DELETE FROM player_match_pages "
                    "WHERE riot_id = ? COLLATE NOCASE AND page_start > ?",
                    (normalized_id, page_start),
                )

    def load_player_match_page(
        self,
        riot_id: str,
        page_start: int,
        max_age: timedelta | None = None,
    ) -> tuple[str, list[str], bool, str] | None:
        """Load a saved page; ``max_age=None`` deliberately accepts stale data."""
        normalized_id = str(riot_id or "").strip()
        if not normalized_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT puuid, updated_at, has_more, match_ids_json "
                "FROM player_match_pages WHERE riot_id = ? COLLATE NOCASE "
                "AND page_start = ?",
                (normalized_id, max(int(page_start), 0)),
            ).fetchone()
        if not row:
            return None
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
            match_ids = [
                str(match_id) for match_id in json.loads(row["match_ids_json"])
                if str(match_id).strip()
            ][:10]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if max_age is not None and datetime.now() - updated_at > max_age:
            return None
        return (
            str(row["puuid"]),
            match_ids,
            bool(row["has_more"]),
            str(row["updated_at"]),
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
        matches: list[dict[str, Any]] = []
        for stored_queue_id, match in self._decoded_match_rows():
            if stored_queue_id != queue_id:
                continue
            participants = (match.get("info") or {}).get("participants") or []
            if not any(participant.get("puuid") == puuid for participant in participants):
                continue
            matches.append(match)
            if len(matches) >= limit:
                break
        return matches

    def jungle_tendency(
        self, puuid: str, champion_id: str = "", limit: int = 30
    ) -> JungleTendencyStat:
        """Summarize only jungle behavior fields present in cached solo games.

        Lane-specific gank direction requires Riot timeline data, which this
        cache does not store. The method deliberately does not infer top/bot
        preference from end-of-game KDA.
        """
        if not puuid:
            return JungleTendencyStat(message="플레이어 식별 정보 없음")

        samples: list[dict[str, Any]] = []
        champion_samples: list[dict[str, Any]] = []
        for match in self.player_matches(puuid, limit=max(limit * 3, limit)):
            participant = next(
                (
                    item for item in (match.get("info") or {}).get("participants", [])
                    if item.get("puuid") == puuid
                ),
                None,
            )
            if not participant:
                continue
            position = self._normalized_position(
                str(
                    participant.get("teamPosition")
                    or participant.get("individualPosition")
                    or ""
                )
            )
            if position != "JUNGLE":
                continue
            samples.append(participant)
            if str(participant.get("championName") or "") == champion_id:
                champion_samples.append(participant)
            if len(samples) >= limit:
                break

        # Three current-champion games are enough to prefer champion-specific
        # behavior; below that, the player's broader jungle sample is steadier.
        selected = champion_samples if len(champion_samples) >= 3 else samples
        if not selected:
            return JungleTendencyStat(
                puuid=puuid,
                champion_id=champion_id,
                message="저장된 솔로랭크 정글 표본 없음",
            )

        def average_challenge(key: str) -> float | None:
            values: list[float] = []
            for participant in selected:
                challenges = participant.get("challenges") or {}
                if key not in challenges:
                    continue
                try:
                    values.append(float(challenges[key]))
                except (TypeError, ValueError):
                    continue
            return sum(values) / len(values) if values else None

        early_takedowns = average_challenge("takedownsFirstXMinutes")
        early_lane_kills = average_challenge("killsOnLanersEarlyJungleAsJungler")
        jungle_cs_10 = average_challenge("jungleCsBefore10Minutes")
        enemy_jungle_cs = average_challenge("enemyJungleMonsterKills")
        spawn_objectives = average_challenge("epicMonsterKillsWithin30SecondsOfSpawn")

        labels: list[str] = []
        if (
            (early_takedowns is not None and early_takedowns >= 2.0)
            or (early_lane_kills is not None and early_lane_kills >= 0.8)
        ):
            labels.append("초반 개입 적극")
        if jungle_cs_10 is not None and jungle_cs_10 >= 52.0:
            labels.append("10분 성장 우선")
        if enemy_jungle_cs is not None and enemy_jungle_cs >= 10.0:
            labels.append("상대 정글 침투")
        if spawn_objectives is not None and spawn_objectives >= 0.35:
            labels.append("생성 직후 오브젝트")
        available = any(
            value is not None for value in (
                early_takedowns, early_lane_kills, jungle_cs_10,
                enemy_jungle_cs, spawn_objectives,
            )
        )
        if available and not labels:
            labels.append("균형형")
        return JungleTendencyStat(
            puuid=puuid,
            champion_id=champion_id,
            games=len(selected),
            champion_specific=selected is champion_samples,
            early_takedowns=early_takedowns,
            early_lane_kills=early_lane_kills,
            jungle_cs_10=jungle_cs_10,
            enemy_jungle_cs=enemy_jungle_cs,
            spawn_objectives=spawn_objectives,
            labels=labels,
            status="OK" if available else "NO_FIELDS",
            message=(
                "현재 챔피언 표본" if selected is champion_samples
                else "최근 정글 전체 표본"
            ),
        )

    def player_behavior(
        self,
        puuid: str,
        champion_id: str = "",
        position: str = "UNKNOWN",
        limit: int = 20,
    ) -> PlayerBehaviorStat:
        """Analyze recent role behavior while preserving missing-field honesty."""
        normalized_position = self._normalized_position(position)
        if not puuid:
            return PlayerBehaviorStat(message="플레이어 식별 정보 없음")
        samples: list[dict[str, Any]] = []
        champion_samples: list[dict[str, Any]] = []
        for match in self.player_matches(puuid, limit=max(limit * 3, limit)):
            participant = next(
                (
                    item for item in (match.get("info") or {}).get("participants", [])
                    if item.get("puuid") == puuid
                ),
                None,
            )
            if not participant:
                continue
            participant_position = self._normalized_position(
                str(
                    participant.get("teamPosition")
                    or participant.get("individualPosition")
                    or ""
                )
            )
            if participant_position != normalized_position:
                continue
            samples.append(participant)
            if str(participant.get("championName") or "") == champion_id:
                champion_samples.append(participant)
            if len(samples) >= limit:
                break
        selected = champion_samples if len(champion_samples) >= 3 else samples
        if not selected:
            return PlayerBehaviorStat(
                puuid=puuid, champion_id=champion_id,
                position=normalized_position,
                message="저장된 최근 포지션 표본 없음",
            )

        first_blood_kills = sum(bool(item.get("firstBloodKill")) for item in selected)
        first_blood_assists = sum(bool(item.get("firstBloodAssist")) for item in selected)
        first_blood_rate = (
            (first_blood_kills + first_blood_assists) / len(selected) * 100
        )

        def challenge_values(key: str) -> list[float]:
            values: list[float] = []
            for participant in selected:
                challenges = participant.get("challenges") or {}
                if key not in challenges:
                    continue
                try:
                    values.append(float(challenges[key]))
                except (TypeError, ValueError):
                    continue
            return values

        def average(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        early_advantage_values = challenge_values("earlyLaningPhaseGoldExpAdvantage")
        early_advantage_rate = (
            sum(value > 0 for value in early_advantage_values)
            / len(early_advantage_values) * 100
            if early_advantage_values else None
        )
        early_takedowns = average(challenge_values("takedownsFirstXMinutes"))
        kill_participation = average(challenge_values("killParticipation"))
        if kill_participation is not None and kill_participation <= 1.0:
            kill_participation *= 100
        average_deaths = sum(float(item.get("deaths") or 0) for item in selected) / len(selected)
        vision_per_minute = average(challenge_values("visionScorePerMinute"))
        control_ward_values: list[float] = []
        for participant in selected:
            raw = participant.get("detectorWardsPlaced")
            if raw is None:
                continue
            try:
                control_ward_values.append(float(raw))
            except (TypeError, ValueError):
                continue
        control_wards = average(control_ward_values)

        labels: list[str] = []
        if first_blood_rate >= 25.0:
            labels.append("선취점 관여 잦음")
        if early_advantage_rate is not None and early_advantage_rate >= 60.0:
            labels.append("초반 라인 우위")
        if kill_participation is not None and kill_participation >= 65.0:
            labels.append("합류 적극")
        if average_deaths >= 6.0:
            labels.append("고위험 진입")
        if vision_per_minute is not None and vision_per_minute >= 1.7:
            labels.append("시야 투자 높음")
        if not labels:
            labels.append("균형형")
        return PlayerBehaviorStat(
            puuid=puuid,
            champion_id=champion_id,
            position=normalized_position,
            games=len(selected),
            champion_specific=selected is champion_samples,
            first_blood_kills=first_blood_kills,
            first_blood_assists=first_blood_assists,
            first_blood_rate=first_blood_rate,
            early_advantage_rate=early_advantage_rate,
            early_takedowns=early_takedowns,
            kill_participation=kill_participation,
            average_deaths=average_deaths,
            vision_per_minute=vision_per_minute,
            control_wards=control_wards,
            labels=labels,
            status="OK",
            message=(
                "현재 챔피언 최근 표본" if selected is champion_samples
                else "현재 포지션 최근 표본"
            ),
        )

    def load_live_profile(
        self, riot_id: str, max_age: timedelta = timedelta(hours=24)
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

    def find_riot_id_by_puuid(self, puuid: str) -> str:
        """Return the newest locally observed Riot ID for a PUUID."""
        normalized = str(puuid or "").strip()
        if not normalized:
            return ""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT riot_id FROM player_identities WHERE puuid = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (normalized,),
            ).fetchone()
            return str(row["riot_id"]) if row else ""

    def recent_riot_ids(self, my_puuid: str, limit: int = 9) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for _queue_id, match in self._decoded_match_rows():
            participants = match.get("info", {}).get("participants", [])
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
        for _queue_id, match in self._decoded_match_rows():
            participants = match.get("info", {}).get("participants", [])
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
        inspected = 0
        for _queue_id, match in self._decoded_match_rows():
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

    def player_champion_record_for_matches(
        self, puuid: str, champion_id: str, match_ids: Iterable[str]
    ) -> tuple[int, int, int]:
        """Count one champion only inside an explicit Riot match-ID sample."""
        inspected = games = wins = 0
        for match_id in match_ids:
            match = self.load_match(str(match_id))
            if not match:
                continue
            participant = next(
                (
                    item for item in (match.get("info") or {}).get("participants", [])
                    if item.get("puuid") == puuid
                ),
                None,
            )
            if not participant:
                continue
            inspected += 1
            if participant.get("championName") == champion_id:
                games += 1
                wins += int(bool(participant.get("win")))
        return inspected, games, wins

    def latest_player_match(self, puuid: str) -> dict[str, Any] | None:
        for queue_id, match in self._decoded_match_rows():
            if queue_id != 420:
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
        inspected_my_games = 0
        for _queue_id, match in self._decoded_match_rows():
            participants = match.get("info", {}).get("participants", [])
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
        ally_adc_id: str | None = None,
        limit: int = 1000,
        position: str = "SUPPORT",
    ) -> PersonalStat:
        return self.personal_stats(
            puuid, [champion_id], enemy_support_id, ally_adc_id,
            limit=limit, position=position,
        )[champion_id]

    @staticmethod
    def _normalized_position(position: str | None) -> str:
        value = str(position or "SUPPORT").upper()
        return {
            "UTILITY": "SUPPORT",
            "SUP": "SUPPORT",
            "ADC": "BOTTOM",
            "MID": "MIDDLE",
            "JGL": "JUNGLE",
        }.get(value, value)

    def personal_stats(
        self,
        puuid: str,
        champion_ids: Iterable[str],
        enemy_support_id: str | None,
        ally_adc_id: str | None = None,
        limit: int = 1000,
        position: str = "SUPPORT",
    ) -> dict[str, PersonalStat]:
        unique_ids = list(dict.fromkeys(champion_id for champion_id in champion_ids if champion_id))
        stats = {champion_id: PersonalStat() for champion_id in unique_ids}
        totals = {
            champion_id: {"kills": 0.0, "deaths": 0.0, "assists": 0.0, "vision": 0.0}
            for champion_id in unique_ids
        }
        if not stats or not puuid:
            return stats

        inspected = 0
        requested_position = self._normalized_position(position)
        for queue_id, match in self._decoded_match_rows():
            if queue_id != 420:
                continue
            participants = match.get("info", {}).get("participants", [])
            mine = next((p for p in participants if p.get("puuid") == puuid), None)
            if not mine:
                continue
            inspected += 1
            champion_id = str(mine.get("championName") or "")
            stat = stats.get(champion_id)
            mine_position = self._normalized_position(
                str(mine.get("teamPosition") or mine.get("individualPosition") or "")
            )
            if stat is not None and mine_position == requested_position:
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
                            and self._normalized_position(
                                str(
                                    p.get("teamPosition")
                                    or p.get("individualPosition") or ""
                                )
                            ) == requested_position
                        ),
                        None,
                    )
                    if enemy and enemy.get("championName") == enemy_support_id:
                        stat.matchup_games += 1
                        stat.matchup_wins += int(won)
                        stat.matchup_losses += int(not won)
                if ally_adc_id and requested_position == "SUPPORT":
                    ally_adc = next(
                        (
                            p for p in participants
                            if p.get("teamId") == mine.get("teamId")
                            and p.get("puuid") != puuid
                            and str(
                                p.get("teamPosition")
                                or p.get("individualPosition") or ""
                            ).upper() == "BOTTOM"
                        ),
                        None,
                    )
                    if ally_adc and ally_adc.get("championName") == ally_adc_id:
                        stat.ally_adc_games += 1
                        stat.ally_adc_wins += int(won)
                        stat.ally_adc_losses += int(not won)
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
            if stat.ally_adc_games:
                stat.ally_adc_win_rate = (
                    stat.ally_adc_wins / stat.ally_adc_games * 100
                )
        return stats
