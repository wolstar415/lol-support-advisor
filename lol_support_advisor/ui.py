from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, TypeVar
import webbrowser

from .champions import ChampionRegistry
from .history import HistoryOverview, MatchHistoryEntry, analyze_history
from .icons import ChampionIconCache, ItemIconCache
from .lcu import LcuClient, LcuUnavailable, parse_lcu_session
from .live_client import LiveClient, LiveClientUnavailable
from .models import (
    DraftMember, DraftSnapshot, LiveGameSnapshot, LivePlayer, OpggCounter, OpggSnapshot,
    PersonalStat, PlayerProfileStat, Recommendation,
)
from .opgg import OpggClient, OpggError
from .prompting import ResponseError, StaleResponseError, build_prompt, parse_response
from .riot_api import RiotApiClient, RiotApiError
from .storage import Storage


T = TypeVar("T")

COLORS = {
    "bg": "#070b13",
    "panel": "#0f1726",
    "panel_2": "#151f32",
    "border": "#263754",
    "text": "#e8eefc",
    "muted": "#94a3bd",
    "gold": "#e6bd61",
    "blue": "#55b3ff",
    "green": "#48dda0",
    "purple": "#be8cff",
    "red": "#ff6b7c",
    "orange": "#f5a95e",
    "chip": "#1b2941",
    "surface": "#101a2b",
}

ROLE_LABELS = {
    "TOP": "TOP", "JUNGLE": "JGL", "MIDDLE": "MID", "BOTTOM": "ADC",
    "SUPPORT": "SUP", "UTILITY": "SUP", "UNKNOWN": "?",
}

POSITION_NAMES = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜",
    "SUPPORT": "서포터", "UTILITY": "서포터", "UNKNOWN": "포지션 미정",
}

SUPPORT_ARCHETYPES = {
    "UTILITY": {
        "Janna", "Karma", "Lulu", "Milio", "Nami", "Renata", "Senna",
        "Seraphine", "Sona", "Soraka", "Taric", "Yuumi", "Zilean",
    },
    "ENGAGE": {
        "Alistar", "Blitzcrank", "Braum", "Leona", "Maokai", "Nautilus",
        "Poppy", "Pyke", "Rakan", "Rell", "TahmKench", "Thresh",
    },
    "POKE": {
        "Ashe", "Brand", "Heimerdinger", "Lux", "Morgana", "Neeko",
        "Senna", "Shaco", "Velkoz", "Xerath", "Zyra",
    },
}

SUPPORT_FILTER_LABELS = {
    "ALL": "전체",
    "UTILITY": "유틸·안정",
    "ENGAGE": "이니시",
    "POKE": "견제·딜",
}

MANUAL_UNKNOWN_SUPPORT = "__MANUAL_UNKNOWN_SUPPORT__"


def _fmt_rate(value: float | None) -> str:
    return "데이터 없음" if value is None else f"{value:.1f}%"


def _fmt_games(value: int) -> str:
    return "표본 미제공" if not value else f"{value:,}게임"


def support_archetype(champion_id: str) -> str:
    """Return the primary UI archetype for a support champion."""
    for archetype in ("UTILITY", "ENGAGE", "POKE"):
        if champion_id in SUPPORT_ARCHETYPES[archetype]:
            return archetype
    return "OTHER"


def position_name(position: str) -> str:
    return POSITION_NAMES.get(str(position or "SUPPORT").upper(), "서포터")


def team_objective_counts(team_payload: dict) -> dict[str, int]:
    objectives = team_payload.get("objectives") or {}
    return {
        key: int((objectives.get(source) or {}).get("kills") or 0)
        for key, source in (
            ("void_grubs", "horde"),
            ("rift_heralds", "riftHerald"),
            ("dragons", "dragon"),
            ("barons", "baron"),
            ("towers", "tower"),
        )
    }


class _HoverTooltip:
    """Small delayed tooltip that stays inside the owning Tk application."""

    def __init__(self, widget: tk.Widget, text_provider: Callable[[], str]) -> None:
        self.widget = widget
        self.text_provider = text_provider
        self.window: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(280, self._show)

    def _cancel(self) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _show(self) -> None:
        self.after_id = None
        try:
            if not self.widget.winfo_exists():
                return
            text = self.text_provider().strip()
            if not text:
                return
            window = tk.Toplevel(self.widget)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg=COLORS["gold"], padx=1, pady=1)
            tk.Label(
                window, text=text, justify="left", anchor="w", wraplength=390,
                bg="#0b1220", fg=COLORS["text"], padx=11, pady=9,
                font=("Malgun Gothic", 8),
            ).pack()
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            window.geometry(f"+{x}+{y}")
            self.window = window
        except tk.TclError:
            self.window = None

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        if self.window:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


def candidate_score(
    counter: OpggCounter, personal: PersonalStat | None = None
) -> tuple[float, str]:
    """Blend public matchup data with local experience without hiding sample risk."""
    score = 50.0 + (counter.versus_win_rate - 50.0) * 2.8
    confidence_points = 2 if counter.games >= 5000 else (1 if counter.games >= 1500 else 0)
    if personal:
        if personal.games >= 3 and personal.win_rate is not None:
            weight = min(personal.games / 20.0, 1.0)
            score += (personal.win_rate - 50.0) * 0.22 * weight
        if personal.matchup_games >= 2 and personal.matchup_win_rate is not None:
            weight = min(personal.matchup_games / 10.0, 1.0)
            score += (personal.matchup_win_rate - 50.0) * 0.35 * weight
        confidence_points += 2 if personal.games >= 15 else (1 if personal.games >= 5 else 0)
        confidence_points += (
            2 if personal.matchup_games >= 8 else (1 if personal.matchup_games >= 3 else 0)
        )
    confidence = "높음" if confidence_points >= 5 else ("보통" if confidence_points >= 3 else "낮음")
    return max(0.0, min(100.0, score)), confidence


class AdvisorApp:
    def __init__(
        self,
        root: tk.Tk,
        storage: Storage,
        registry: ChampionRegistry,
        demo: bool = False,
    ) -> None:
        self.root = root
        self.storage = storage
        self.registry = registry
        self.demo = demo
        self.lcu = LcuClient(storage.get_setting("lcu_lockfile_path"))
        self.live_client = LiveClient(registry)
        self.opgg_client = OpggClient(registry)
        self.icon_cache = ChampionIconCache(root, registry, storage.db_path.parent / "icons")
        self.item_icon_cache = ItemIconCache(root, registry, storage.db_path.parent / "items")
        self.draft = self._demo_draft() if demo else DraftSnapshot()
        self.opgg_snapshot = (
            self._demo_opgg() if demo else storage.load_opgg_snapshot(None, self.draft.my_role)
        )
        self.live_game = self._demo_live_game() if demo else LiveGameSnapshot()
        self.player_profiles = self._demo_player_profiles() if demo else {}
        self.duo_pairs: dict[str, list[tuple[str, str, str]]] = (
            self._demo_duo_pairs() if demo else {}
        )
        self.recommendations: list[Recommendation] = []
        self.recommendation_snapshot_id = ""
        self._lcu_polling = False
        self._opgg_refreshing = False
        self._riot_syncing = False
        self._live_polling = False
        self._profiles_loading = False
        self._duo_checking = False
        self._duo_checked_signature = ""
        self._manual_enemy_support: str | None = None
        self._live_signature = ""
        self._selection_render_scheduled = False
        self._play_render_scheduled = False
        self._personal_cache_context: tuple[tuple[int, int], str, str | None] | None = None
        self._personal_stats_cache: dict[str, PersonalStat] = {}
        self._personal_stats_pending: set[str] = set()
        self._personal_stats_loading = False
        self._personal_load_scheduled = False
        self._support_filter = "ALL"
        self.history_overview: HistoryOverview | None = None
        self._history_loading = False
        self._history_reload_requested = False
        self._history_revision: tuple[int, int] | None = None
        self._history_visible_count = 12
        self._history_render_scheduled = False
        self.game_phase = "DEMO" if demo else "None"
        self._identity_checked = demo
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

        self._configure_root()
        self._configure_styles()
        self._build_ui()
        self._render_all()
        self.root.after(80, self._drain_ui_queue)
        self.root.after(1000, self._tick)
        self.root.after(100, self._refresh_registry_background)
        self.root.after(
            180, lambda: self.icon_cache.prefetch_all(self._schedule_selection_render)
        )
        self.root.after(420, self._ensure_history_loaded)
        if not self.demo:
            self.root.after(250, self._poll_lcu)

    def _configure_root(self) -> None:
        self.root.title("LOL Support Advisor")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1460x920")
        self.root.minsize(1120, 760)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Advisor.Treeview",
            background=COLORS["panel_2"],
            fieldbackground=COLORS["panel_2"],
            foreground=COLORS["text"],
            rowheight=34,
            borderwidth=0,
            font=("Malgun Gothic", 9),
        )
        style.configure(
            "Advisor.Treeview.Heading",
            background=COLORS["chip"],
            foreground=COLORS["muted"],
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
        )
        style.map("Advisor.Treeview", background=[("selected", "#29476f")])
        style.configure(
            "Advisor.TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(22, 4, 0, 0)
        )
        style.configure(
            "Advisor.TNotebook.Tab", background=COLORS["panel"], foreground=COLORS["muted"],
            padding=(28, 11), font=("Malgun Gothic", 10, "bold"), borderwidth=0,
        )
        style.map(
            "Advisor.TNotebook.Tab",
            background=[("selected", COLORS["panel_2"])],
            foreground=[("selected", COLORS["gold"])],
        )

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)
        self._build_header(shell)
        self.notebook = ttk.Notebook(shell, style="Advisor.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.selection_tab, self.selection_canvas, self.selection_content = self._scroll_tab()
        self.play_tab, self.play_canvas, self.play_content = self._scroll_tab()
        self.history_tab, self.history_canvas, self.history_content = self._scroll_tab()
        self.notebook.add(self.selection_tab, text="선택창")
        self.notebook.add(self.play_tab, text="플레이")
        self.notebook.add(self.history_tab, text="내 전적")
        self.notebook.select(self.selection_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self.content = self.selection_content
        self._build_draft_panel()
        self._build_opgg_panel()
        self._build_manual_panel()
        self._build_recommendations_panel()
        self._build_play_panel()
        self._build_history_panel()

    def _scroll_tab(self) -> tuple[tk.Frame, tk.Canvas, tk.Frame]:
        tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        canvas = tk.Canvas(tab, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=COLORS["bg"])
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.bind("<Configure>", lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        canvas.bind("<Configure>", lambda e, c=canvas, w=content_window: c.itemconfigure(w, width=e.width))
        return tab, canvas, content

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            top = event.widget.winfo_toplevel()
            detail_canvas = getattr(top, "_advisor_scroll_canvas", None)
            if detail_canvas and detail_canvas.winfo_exists():
                delta = getattr(event, "delta", 0)
                number = getattr(event, "num", 0)
                direction = -1 if delta > 0 or number == 4 else 1
                detail_canvas.yview_scroll(direction * 3, "units")
                return
        except tk.TclError:
            return
        index = self.notebook.index(self.notebook.select())
        canvas = (
            self.play_canvas if index == 1 else
            self.history_canvas if index == 2 else self.selection_canvas
        )
        delta = getattr(event, "delta", 0)
        number = getattr(event, "num", 0)
        direction = -1 if delta > 0 or number == 4 else 1
        canvas.yview_scroll(direction * 3, "units")

    def _on_tab_changed(self, _event: tk.Event | None = None) -> None:
        """Give the 10-player board more vertical room while the play tab is open."""
        if not hasattr(self, "play_tab"):
            return
        selected_index = self.notebook.index(self.notebook.select())
        compact_header = selected_index in {1, 2}
        if compact_header:
            self.header_metrics_frame.pack_forget()
            self.legal_notice_label.pack_forget()
            self.header_frame.configure(pady=7)
        else:
            if not self.header_metrics_frame.winfo_manager():
                self.header_metrics_frame.pack(fill="x", pady=(13, 0))
            if not self.legal_notice_label.winfo_manager():
                self.legal_notice_label.pack(anchor="w", pady=(6, 0))
            self.header_frame.configure(pady=12)
        if selected_index == 2:
            self._ensure_history_loaded()

    def _panel(self, parent: tk.Widget, title: str, accent: str | None = None) -> tk.Frame:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        outer.pack(fill="x", padx=22, pady=(0, 12))
        inner = tk.Frame(outer, bg=COLORS["panel"], padx=18, pady=14)
        inner.pack(fill="both", expand=True)
        heading = tk.Frame(inner, bg=COLORS["panel"])
        heading.pack(fill="x", pady=(0, 11))
        marker = tk.Frame(heading, bg=accent or COLORS["blue"], width=4, height=20)
        marker.pack(side="left", padx=(0, 9))
        marker.pack_propagate(False)
        tk.Label(
            heading, text=title, bg=COLORS["panel"], fg=COLORS["text"],
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side="left")
        tk.Frame(inner, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 11))
        return inner

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=COLORS["bg"], padx=24, pady=12)
        header.pack(fill="x")
        self.header_frame = header
        top = tk.Frame(header, bg=COLORS["bg"])
        top.pack(fill="x")
        left = tk.Frame(top, bg=COLORS["bg"])
        left.pack(side="left", fill="x", expand=True)
        self.app_title_label = tk.Label(
            left, text="LOL PICK ADVISOR", bg=COLORS["bg"], fg=COLORS["gold"],
            font=("Malgun Gothic", 19, "bold"),
        )
        self.app_title_label.pack(anchor="w")
        self.connection_label = tk.Label(
            left, text="롤 클라이언트 확인 중", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 10),
        )
        self.connection_label.pack(anchor="w", pady=(3, 0))
        actions = tk.Frame(top, bg=COLORS["bg"])
        actions.pack(side="right")
        self.opgg_header_label = tk.Label(
            actions, text="OP.GG 캐시 없음", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9),
        )
        self.opgg_header_label.grid(row=0, column=0, columnspan=3, sticky="e", pady=(0, 6))
        self.opgg_button = self._button(actions, "OP.GG 데이터 갱신", self._refresh_opgg, COLORS["blue"])
        self.opgg_button.grid(row=1, column=0, padx=(0, 8))
        self.riot_button = self._button(
            actions, "전적 데이터 미리 갱신", self._sync_riot, COLORS["green"]
        )
        self.riot_button.grid(row=1, column=1, padx=(0, 8))
        self.settings_button = self._button(actions, "Riot 설정", self._open_settings, COLORS["muted"])
        self.settings_button.grid(row=1, column=2)
        self.api_key_status_label = tk.Label(
            actions, text="", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self.api_key_status_label.grid(row=2, column=0, columnspan=2, sticky="e", pady=(7, 0))
        self.developer_portal_button = self._button(
            actions, "Riot 키 발급/갱신", self._open_developer_portal, COLORS["orange"]
        )
        self.developer_portal_button.grid(row=2, column=2, sticky="e", pady=(7, 0))

        metrics = tk.Frame(header, bg=COLORS["bg"])
        metrics.pack(fill="x", pady=(13, 0))
        self.header_metrics_frame = metrics
        self.header_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        metric_specs = (
            ("phase", "현재 단계", COLORS["gold"]),
            ("draft", "추천 기준", COLORS["blue"]),
            ("cache", "로컬 전적 DB", COLORS["green"]),
            ("data", "데이터 상태", COLORS["purple"]),
        )
        for index, (key, title, accent) in enumerate(metric_specs):
            outer, value, detail = self._metric_card(metrics, title, accent)
            self.header_metrics[key] = (value, detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 5, 5))
        self.legal_notice_label = tk.Label(
            header,
            text=("비공식 개인용 도구 · Riot Games가 보증하거나 공식 지원하지 않습니다. "
                  "Riot Games 및 관련 자산은 Riot Games, Inc.의 상표입니다."),
            bg=COLORS["bg"], fg="#5f6c82", font=("Malgun Gothic", 7),
        )
        self.legal_notice_label.pack(anchor="w", pady=(6, 0))

    def _metric_card(
        self, parent: tk.Widget, title: str, accent: str
    ) -> tuple[tk.Frame, tk.Label, tk.Label]:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        card = tk.Frame(outer, bg=COLORS["surface"], padx=12, pady=8)
        card.pack(fill="both", expand=True)
        tk.Frame(card, bg=accent, width=3).pack(side="left", fill="y", padx=(0, 9))
        text = tk.Frame(card, bg=COLORS["surface"])
        text.pack(side="left", fill="x", expand=True)
        tk.Label(
            text, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        value = tk.Label(
            text, text="--", bg=COLORS["surface"], fg=accent,
            font=("Malgun Gothic", 12, "bold"),
        )
        value.pack(anchor="w")
        detail = tk.Label(
            text, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        )
        detail.pack(anchor="w")
        return outer, value, detail

    def _button(
        self, parent: tk.Widget, text: str, command: Callable[[], None], color: str,
        width: int | None = None,
    ) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, bg=COLORS["chip"], fg=color,
            activebackground="#304563", activeforeground=color, relief="flat",
            bd=0, padx=14, pady=8, width=width, cursor="hand2",
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=color,
            font=("Malgun Gothic", 9, "bold"), disabledforeground="#5d6a7e",
        )
        button.bind(
            "<Enter>",
            lambda _e, widget=button: widget.configure(bg="#263a59")
            if str(widget.cget("state")) != "disabled" else None,
        )
        button.bind(
            "<Leave>",
            lambda _e, widget=button: widget.configure(bg=COLORS["chip"])
            if str(widget.cget("state")) != "disabled" else None,
        )
        return button

    def _build_draft_panel(self) -> None:
        panel = self._panel(self.content, "현재 드래프트", COLORS["gold"])
        self.pick_order_label = tk.Label(
            panel, bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 9)
        )
        self.pick_order_label.pack(anchor="w", pady=(0, 8))

        bans_row = tk.Frame(panel, bg=COLORS["panel"])
        bans_row.pack(fill="x", pady=(0, 10))
        self.ally_bans_frame = self._labeled_chip_row(bans_row, "우리 밴")
        self.enemy_bans_frame = self._labeled_chip_row(bans_row, "상대 밴")

        tk.Label(
            panel, text="우리 팀  ·  ■ 확정 픽   ◇ 픽 의사(HOVER)", bg=COLORS["panel"],
            fg=COLORS["muted"], font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        self.ally_picks_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.ally_picks_frame.pack(fill="x", pady=(6, 12))

        self.enemy_instruction_label = tk.Label(
            panel, text="상대 팀  ·  적 서포터를 직접 클릭해서 지정", bg=COLORS["panel"],
            fg=COLORS["muted"], font=("Malgun Gothic", 9, "bold"),
        )
        self.enemy_instruction_label.pack(anchor="w")
        self.enemy_picks_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.enemy_picks_frame.pack(fill="x", pady=(6, 8))
        self.enemy_support_label = tk.Label(
            panel, bg=COLORS["panel"], fg=COLORS["blue"], font=("Malgun Gothic", 10, "bold")
        )
        self.enemy_support_label.pack(anchor="w")
        self.stale_label = tk.Label(
            panel, text="", bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 9)
        )
        self.stale_label.pack(anchor="w", pady=(4, 0))

    def _labeled_chip_row(self, parent: tk.Widget, title: str) -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(side="left", fill="x", expand=True)
        tk.Label(row, text=title, bg=COLORS["panel"], fg=COLORS["muted"], width=8,
                 anchor="w", font=("Malgun Gothic", 9, "bold")).pack(side="left")
        chip_frame = tk.Frame(row, bg=COLORS["panel"])
        chip_frame.pack(side="left", fill="x", expand=True)
        return chip_frame

    def _build_opgg_panel(self) -> None:
        panel = self._panel(self.content, "OP.GG 카운터 및 승률", COLORS["blue"])
        self.opgg_summary_label = tk.Label(
            panel, text="캐시된 데이터가 없습니다.", bg=COLORS["panel"], fg=COLORS["muted"],
            justify="left", anchor="w", font=("Malgun Gothic", 9),
        )
        self.opgg_summary_label.pack(fill="x", pady=(0, 9))
        controls = tk.Frame(panel, bg=COLORS["panel"])
        controls.pack(fill="x", pady=(0, 10))
        self.position_filter_label = tk.Label(
            controls, text="플레이 유형", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self.position_filter_label.pack(side="left", padx=(0, 8))
        self.support_filter_buttons: dict[str, tk.Button] = {}
        for key, label in SUPPORT_FILTER_LABELS.items():
            button = self._button(
                controls, label, lambda selected=key: self._set_support_filter(selected),
                COLORS["blue"], width=9,
            )
            button.configure(padx=7, pady=5, font=("Malgun Gothic", 8, "bold"))
            button.pack(side="left", padx=(0, 5))
            button.bind("<Leave>", lambda _e: self._refresh_filter_buttons())
            self.support_filter_buttons[key] = button
        self.copy_top3_button = self._button(
            controls, "TOP 3 후보 복사", self._copy_top3_candidates, COLORS["gold"]
        )
        self.copy_top3_button.configure(padx=10, pady=5, font=("Malgun Gothic", 8, "bold"))
        self.copy_top3_button.pack(side="right")
        self.opgg_calc_label = tk.Label(
            controls, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        )
        self.opgg_calc_label.pack(side="right", padx=(0, 10))
        columns = (
            "rank", "score", "confidence", "winrate", "games", "personal", "matchup", "status"
        )
        self.counter_tree = ttk.Treeview(
            panel, columns=columns, show="tree headings", height=6, style="Advisor.Treeview"
        )
        headings = {
            "rank": "순위", "score": "종합 점수", "confidence": "신뢰도",
            "winrate": "OP.GG 상대 승률",
            "games": "OP.GG 표본", "personal": "내 챔피언 전적",
            "matchup": "내 맞상대 전적", "status": "추천 가능",
        }
        self.counter_tree.heading("#0", text="챔피언")
        self.counter_tree.column("#0", width=145, anchor="w", stretch=True)
        widths = {
            "rank": 45, "score": 80, "confidence": 65, "winrate": 120,
            "games": 95, "personal": 145, "matchup": 155, "status": 95,
        }
        for column in columns:
            self.counter_tree.heading(column, text=headings[column])
            self.counter_tree.column(column, width=widths[column], anchor="center", stretch=True)
        self.counter_tree.pack(fill="x")
        self.counter_tree.tag_configure("strong", foreground=COLORS["green"])
        self.counter_tree.tag_configure("good", foreground=COLORS["gold"])
        self.counter_tree.tag_configure("blocked", foreground=COLORS["muted"])
        self.weak_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.weak_frame.pack(fill="x", pady=(8, 0))

    def _build_manual_panel(self) -> None:
        panel = self._panel(self.content, "ChatGPT 수동 요청", COLORS["purple"])
        self.prompt_summary_label = tk.Label(
            panel, bg=COLORS["panel"], fg=COLORS["muted"], justify="left", anchor="w",
            font=("Malgun Gothic", 9),
        )
        self.prompt_summary_label.pack(fill="x", pady=(0, 10))
        action_row = tk.Frame(panel, bg=COLORS["panel"])
        action_row.pack(fill="x", pady=(0, 10))
        self.copy_button = self._button(
            action_row, "1. ChatGPT 질문 복사", self._copy_prompt, COLORS["purple"], width=23
        )
        self.copy_button.pack(side="left", padx=(0, 10))
        self.preview_prompt_button = self._button(
            action_row, "보낼 질문 미리보기", self._show_prompt_preview, COLORS["blue"], width=20
        )
        self.preview_prompt_button.pack(side="left", padx=(0, 10))
        self.paste_button = self._button(
            action_row, "2. 클립보드 답변 붙여넣기", self._paste_clipboard_response,
            COLORS["green"], width=28,
        )
        self.paste_button.pack(side="left")
        self.exchange_status = tk.Label(
            action_row, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9),
        )
        self.exchange_status.pack(side="left", padx=14)
        self.response_text = tk.Text(
            panel, height=6, bg="#0b1220", fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", bd=0, padx=10, pady=8, wrap="word", font=("Consolas", 9),
        )
        self.response_text.pack(fill="x")
        self.apply_button = self._button(
            panel, "붙여넣은 답변 적용", self._apply_response, COLORS["green"]
        )
        self.apply_button.pack(anchor="e", pady=(8, 0))

    def _build_recommendations_panel(self) -> None:
        outer = self._panel(self.content, "추천 결과", COLORS["green"])
        self.cards_frame = tk.Frame(outer, bg=COLORS["panel"])
        self.cards_frame.pack(fill="x")

    def _build_play_panel(self) -> None:
        panel = self._panel(self.play_content, "현재 게임 플레이어", COLORS["gold"])
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 10))
        self.live_game_label = tk.Label(
            top, text="게임 시작을 기다리는 중", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 10, "bold"),
        )
        self.live_game_label.pack(side="left")
        self.live_profile_status = tk.Label(
            top, text="", bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 9)
        )
        self.live_profile_status.pack(side="right")
        self.live_duo_status = tk.Label(
            panel,
            text="DUO: 현재 10명의 최근 100경기 교집합 확인 · 카드 수치는 로컬/Riot 결합",
            bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 8),
        )
        self.live_duo_status.pack(anchor="w", pady=(0, 8))
        summary = tk.Frame(panel, bg=COLORS["panel"])
        summary.pack(fill="x", pady=(0, 13))
        self.play_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title, accent) in enumerate((
            ("ally", "아군 시즌 평균", COLORS["green"]),
            ("enemy", "적군 시즌 평균", COLORS["red"]),
            ("cache", "확인된 플레이어", COLORS["blue"]),
            ("duo", "DUO 신호", COLORS["orange"]),
        )):
            outer, value, detail = self._mini_metric(summary, title, accent)
            self.play_metrics[key] = (value, detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 5, 5))
        board = tk.Frame(panel, bg=COLORS["panel"])
        board.pack(fill="x")
        ally_heading = tk.Frame(board, bg=COLORS["panel"])
        ally_heading.pack(fill="x", pady=(0, 6))
        tk.Label(
            ally_heading, text="아군  ·  TOP   JGL   MID   ADC   SUP",
            bg=COLORS["panel"], fg=COLORS["green"],
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            ally_heading, text="카드를 가로로 비교하세요",
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(side="right")
        self.live_allies_frame = tk.Frame(board, bg=COLORS["panel"])
        self.live_allies_frame.pack(fill="x")
        divider = tk.Frame(board, bg=COLORS["border"], height=1)
        divider.pack(fill="x", pady=9)
        enemy_heading = tk.Frame(board, bg=COLORS["panel"])
        enemy_heading.pack(fill="x", pady=(0, 6))
        tk.Label(
            enemy_heading, text="적군  ·  TOP   JGL   MID   ADC   SUP",
            bg=COLORS["panel"], fg=COLORS["red"],
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            enemy_heading, text="빨강은 패배·낮은 승률, 주황은 낮은 표본",
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(side="right")
        self.live_enemies_frame = tk.Frame(board, bg=COLORS["panel"])
        self.live_enemies_frame.pack(fill="x")
        for frame in (self.live_allies_frame, self.live_enemies_frame):
            for column in range(5):
                frame.grid_columnconfigure(column, weight=1, uniform="player_cards")

    def _mini_metric(
        self, parent: tk.Widget, title: str, accent: str
    ) -> tuple[tk.Frame, tk.Label, tk.Label]:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        card = tk.Frame(outer, bg=COLORS["surface"], padx=9, pady=5)
        card.pack(fill="both", expand=True)
        tk.Label(
            card, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w")
        row = tk.Frame(card, bg=COLORS["surface"])
        row.pack(fill="x")
        value = tk.Label(
            row, text="--", bg=COLORS["surface"], fg=accent,
            font=("Malgun Gothic", 10, "bold"),
        )
        value.pack(side="left")
        detail = tk.Label(
            row, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        )
        detail.pack(side="right")
        return outer, value, detail

    def _build_history_panel(self) -> None:
        panel = self._panel(self.history_content, "내 솔로랭크 전적", COLORS["purple"])
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 10))
        identity = self.storage.get_setting("riot_game_name") or "Riot 계정 미확인"
        tag = self.storage.get_setting("riot_tag_line")
        self.history_identity_label = tk.Label(
            top, text=f"{identity}{'#' + tag if tag else ''}", bg=COLORS["panel"],
            fg=COLORS["gold"], font=("Malgun Gothic", 14, "bold"),
        )
        self.history_identity_label.pack(side="left")
        self.history_status_label = tk.Label(
            top, text="로컬 전적 준비 중", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        )
        self.history_status_label.pack(side="left", padx=12)
        self._button(
            top, "통계 다시 계산", lambda: self._ensure_history_loaded(force=True), COLORS["blue"]
        ).pack(side="right")
        self._button(
            top, "Riot 전적 갱신", self._sync_riot, COLORS["green"]
        ).pack(side="right", padx=(0, 8))

        summary = tk.Frame(panel, bg=COLORS["panel"])
        summary.pack(fill="x", pady=(0, 12))
        self.history_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title, accent) in enumerate((
            ("rank", "현재 솔로랭크", COLORS["gold"]),
            ("games", "로컬 저장 경기", COLORS["blue"]),
            ("recent", "최근 20경기", COLORS["green"]),
            ("kda", "전체 KDA", COLORS["purple"]),
            ("vision", "평균 시야", COLORS["orange"]),
        )):
            outer, value, detail = self._mini_metric(summary, title, accent)
            self.history_metrics[key] = (value, detail)
            outer.pack(
                side="left", fill="x", expand=True,
                padx=(0 if index == 0 else 4, 4),
            )

        body = tk.Frame(panel, bg=COLORS["panel"])
        body.pack(fill="both", expand=True)
        left = tk.Frame(
            body, bg=COLORS["panel_2"], width=330, padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        tk.Label(
            left, text="챔피언 성능", bg=COLORS["panel_2"], fg=COLORS["text"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            left, text="저장된 솔로랭크 전체 표본", bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(1, 8))
        self.history_champions_frame = tk.Frame(left, bg=COLORS["panel_2"])
        self.history_champions_frame.pack(fill="x")

        right = tk.Frame(
            body, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        right.pack(side="left", fill="both", expand=True)
        match_header = tk.Frame(right, bg=COLORS["panel_2"])
        match_header.pack(fill="x", pady=(0, 8))
        tk.Label(
            match_header, text="최근 경기", bg=COLORS["panel_2"], fg=COLORS["text"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            match_header, text="경기 상세에서 양 팀 10명·전투·시야·아이템 비교",
            bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(side="right")
        self.history_matches_frame = tk.Frame(right, bg=COLORS["panel_2"])
        self.history_matches_frame.pack(fill="x")
        self.history_more_button = self._button(
            right, "경기 더 보기", self._show_more_history, COLORS["purple"]
        )
        self.history_more_button.pack(anchor="center", pady=(9, 0))

    def _chip(self, parent: tk.Widget, text: str, color: str = COLORS["text"],
              command: Callable[[], None] | None = None, selected: bool = False,
              champion_id: str | None = None) -> tk.Widget:
        cls = tk.Button if command else tk.Label
        icon = (
            self.icon_cache.get(champion_id, 32, self._schedule_selection_render)
            if champion_id else None
        )
        widget = cls(
            parent, text=text, bg="#24466e" if selected else COLORS["chip"], fg=color,
            relief="flat", bd=0, padx=10, pady=6, font=("Malgun Gothic", 9),
            image=icon or "", compound="left",
            **({"command": command, "cursor": "hand2", "activebackground": "#315c8e",
                "activeforeground": color} if command else {}),
        )
        widget.pack(side="left", padx=(0, 6), pady=2)
        return widget

    @staticmethod
    def _clear(frame: tk.Widget) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _render_all(self) -> None:
        self.draft.refresh_snapshot_id()
        self._render_selection()
        self._render_play()
        self._render_history()

    def _render_selection(self) -> None:
        self.draft.refresh_snapshot_id()
        self._render_header()
        self._render_draft()
        self._render_opgg()
        self._render_prompt_summary()
        self._render_recommendations()

    def _schedule_selection_render(self) -> None:
        if self._selection_render_scheduled:
            return
        self._selection_render_scheduled = True

        def render() -> None:
            self._selection_render_scheduled = False
            self._render_selection()

        self.root.after(80, render)

    def _schedule_play_render(self) -> None:
        if self._play_render_scheduled:
            return
        self._play_render_scheduled = True

        def render() -> None:
            self._play_render_scheduled = False
            self._render_play()

        self.root.after(80, render)

    def _schedule_history_render(self) -> None:
        if self._history_render_scheduled:
            return
        self._history_render_scheduled = True

        def render() -> None:
            self._history_render_scheduled = False
            self._render_history()

        self.root.after(100, render)

    def _personal_stats_for(
        self, champion_ids: list[str]
    ) -> dict[str, PersonalStat | None]:
        puuid = self.storage.get_setting("riot_puuid")
        if not puuid:
            return {champion_id: None for champion_id in champion_ids}
        context = (
            self.storage.match_revision(),
            puuid,
            self.draft.selected_enemy_support_id,
        )
        if context != self._personal_cache_context:
            self._personal_cache_context = context
            self._personal_stats_cache.clear()
            self._personal_stats_pending.clear()
        for champion_id in champion_ids:
            if champion_id and champion_id not in self._personal_stats_cache:
                self._personal_stats_pending.add(champion_id)
        self._schedule_personal_stats_load()
        return {
            champion_id: self._personal_stats_cache.get(champion_id)
            for champion_id in champion_ids
        }

    def _schedule_personal_stats_load(self) -> None:
        if self._personal_load_scheduled or self._personal_stats_loading:
            return
        if not self._personal_stats_pending:
            return
        self._personal_load_scheduled = True
        self.root.after(25, self._start_personal_stats_load)

    def _start_personal_stats_load(self) -> None:
        self._personal_load_scheduled = False
        if self._personal_stats_loading or not self._personal_stats_pending:
            return
        context = self._personal_cache_context
        if context is None:
            return
        champion_ids = sorted(self._personal_stats_pending)
        self._personal_stats_pending.clear()
        self._personal_stats_loading = True
        _revision, puuid, enemy_support_id = context

        def success(stats: dict[str, PersonalStat]) -> None:
            self._personal_stats_loading = False
            if context == self._personal_cache_context:
                self._personal_stats_cache.update(stats)
                self._schedule_selection_render()
            self._schedule_personal_stats_load()

        def error(_exc: Exception) -> None:
            self._personal_stats_loading = False
            self._schedule_personal_stats_load()

        self._background(
            lambda: self.storage.personal_stats(
                puuid, champion_ids, enemy_support_id, limit=1000
            ),
            success,
            error,
        )

    def _render_header(self) -> None:
        if self.demo:
            text, color = "● 데모 화면 · 실제 게임에는 입력하지 않습니다", COLORS["gold"]
        elif self.game_phase == "InProgress":
            text, color = "● 게임 진행 중 · 플레이 탭에서 플레이어 전적 확인", COLORS["green"]
        elif self.draft.connection_state == "CHAMP_SELECT":
            text, color = "● 롤 클라이언트 연결됨 · 챔피언 선택 읽기 전용", COLORS["green"]
        elif self.draft.connection_state == "LOBBY":
            text, color = "● 롤 클라이언트 연결됨 · 게임 시작 대기", COLORS["blue"]
        else:
            text, color = "○ 롤 클라이언트 대기 중 · 챔피언 선택에 들어가면 자동 연결", COLORS["muted"]
        self.connection_label.configure(text=text, fg=color)
        remaining = self.storage.opgg_cooldown_remaining()
        if self.demo:
            self.opgg_header_label.configure(text="OP.GG 데모 데이터 · 수치는 화면 예시")
        elif self.opgg_snapshot:
            stamp = self.opgg_snapshot.updated_at.replace("T", " ")[:16]
            self.opgg_header_label.configure(
                text=f"OP.GG {self.opgg_snapshot.patch} · {self.opgg_snapshot.tier} · {stamp} 갱신"
            )
        else:
            self.opgg_header_label.configure(text="OP.GG 캐시 없음")
        if self._opgg_refreshing:
            self.opgg_button.configure(state="disabled", text="OP.GG 갱신 중...")
        elif remaining.total_seconds() > 0:
            seconds = int(remaining.total_seconds())
            self.opgg_button.configure(state="disabled", text=f"다시 갱신 {seconds // 60:02d}:{seconds % 60:02d}")
        else:
            self.opgg_button.configure(state="normal", text="OP.GG 데이터 갱신")
        if self.game_phase == "InProgress":
            self.riot_button.configure(state="disabled", text="게임 중 · 로컬 조회")
        elif self._riot_syncing:
            self.riot_button.configure(state="disabled")
        else:
            self.riot_button.configure(state="normal", text="전적 데이터 미리 갱신")
        key = self.storage.get_setting("riot_api_key")
        key_remaining = self.storage.riot_api_key_refresh_remaining()
        if not key:
            self.api_key_status_label.configure(text="Riot API 키 없음", fg=COLORS["red"])
            self.settings_button.configure(text="Riot 설정")
        elif key_remaining.total_seconds() <= 0:
            self.api_key_status_label.configure(
                text="개발용 API 키 갱신 필요 · 24시간 만료", fg=COLORS["red"]
            )
            self.settings_button.configure(text="API 키 갱신")
        else:
            seconds = int(key_remaining.total_seconds())
            hours, seconds = divmod(seconds, 3600)
            minutes = seconds // 60
            self.api_key_status_label.configure(
                text=f"Riot 개발 키 · 저장 기준 남은 {hours:02d}:{minutes:02d}",
                fg=COLORS["green"],
            )
            self.settings_button.configure(text="Riot 설정")

        phase_value = "DEMO" if self.demo else {
            "InProgress": "PLAY",
        }.get(self.game_phase, "DRAFT" if self.draft.connection_state == "CHAMP_SELECT" else "대기 중")
        phase_detail = (
            "화면 예시 · 읽기 전용" if self.demo else
            "플레이 탭 자동 전환" if self.game_phase == "InProgress" else
            "실시간 픽·밴 감지" if self.draft.connection_state == "CHAMP_SELECT" else
            "클라이언트 연결 대기"
        )
        self.header_metrics["phase"][0].configure(text=phase_value, fg=color)
        self.header_metrics["phase"][1].configure(text=phase_detail)
        role_name = position_name(self.draft.my_role)
        target = self.draft.selected_enemy_support_name_ko or "블라인드"
        self.app_title_label.configure(text=f"LOL {role_name.upper()} PICK ADVISOR")
        self.root.title(f"LOL Pick Advisor · {role_name}")
        order = f"우리 팀 {self.draft.my_pick_order}픽" if self.draft.my_pick_order else "픽 순서 미확인"
        self.header_metrics["draft"][0].configure(text=target)
        self.header_metrics["draft"][1].configure(text=f"적 {role_name} 기준 · {order}")
        match_count = self.storage.count_matches()
        self.header_metrics["cache"][0].configure(text=f"{match_count:,} 경기")
        self.header_metrics["cache"][1].configure(text="내 전적·관계 기록 로컬 계산")
        key_ready = bool(key and key_remaining.total_seconds() > 0)
        data_value = "READY" if key_ready and self.opgg_snapshot else ("부분 준비" if key_ready or self.opgg_snapshot else "설정 필요")
        data_color = COLORS["green"] if data_value == "READY" else (COLORS["orange"] if data_value == "부분 준비" else COLORS["red"])
        self.header_metrics["data"][0].configure(text=data_value, fg=data_color)
        self.header_metrics["data"][1].configure(
            text=f"Riot {'OK' if key_ready else '갱신 필요'} · OP.GG {'OK' if self.opgg_snapshot else '없음'}"
        )

    def _render_draft(self) -> None:
        role_name = position_name(self.draft.my_role)
        order = f"우리 팀 {self.draft.my_pick_order}픽" if self.draft.my_pick_order else "픽 순서 확인 전"
        states = {"WAITING": "픽 대기", "SELECTING": "내 픽 진행 중", "LOCKED": "내 픽 확정"}
        self.pick_order_label.configure(
            text=f"내 포지션: {role_name}    나의 픽 순서: {order}    현재 상태: {states.get(self.draft.my_status, self.draft.my_status)}    "
                 f"스냅샷: {self.draft.snapshot_id}"
        )
        self.enemy_instruction_label.configure(
            text=f"상대 팀  ·  적 {role_name} 챔피언을 직접 클릭해서 지정"
        )
        for frame, values in ((self.ally_bans_frame, self.draft.ally_bans),
                              (self.enemy_bans_frame, self.draft.enemy_bans)):
            self._clear(frame)
            if not values:
                self._chip(frame, "아직 없음", COLORS["muted"])
            for champion_id in values:
                self._chip(
                    frame, self.registry.ko_name(champion_id), COLORS["red"], champion_id=champion_id
                )

        self._clear(self.ally_picks_frame)
        combined = list(self.draft.ally_locked) + list(self.draft.ally_hover)
        if self.draft.my_hover:
            combined.append(self.draft.my_hover)
        if not combined:
            self._chip(self.ally_picks_frame, "아직 공개된 픽 또는 픽 의사가 없습니다", COLORS["muted"])
        for member in combined:
            marker = "■" if member.state == "LOCKED" else "◇"
            role = ROLE_LABELS.get(member.role, "?")
            color = COLORS["text"] if member.state == "LOCKED" else COLORS["gold"]
            self._chip(
                self.ally_picks_frame, f"{marker} {role} {member.champion_name_ko}", color,
                champion_id=member.champion_id,
            )

        self._clear(self.enemy_picks_frame)
        unknown_selected = self.draft.selected_enemy_support_source == "MANUAL_UNKNOWN"
        self._chip(
            self.enemy_picks_frame,
            f"?  적 {role_name} 모르겠음",
            COLORS["orange"] if unknown_selected else COLORS["muted"],
            command=self._select_unknown_enemy_support,
            selected=unknown_selected,
        )
        if not self.draft.enemy_locked:
            self._chip(self.enemy_picks_frame, "상대 픽 공개 전", COLORS["muted"])
        for member in self.draft.enemy_locked:
            selected = member.champion_id == self.draft.selected_enemy_support_id
            self._chip(
                self.enemy_picks_frame,
                f"{'● ' if selected else ''}{member.champion_name_ko}",
                COLORS["blue"] if selected else COLORS["text"],
                command=lambda champion_id=member.champion_id: self._select_enemy_support(champion_id),
                selected=selected,
                champion_id=member.champion_id,
            )
        if unknown_selected:
            self.enemy_support_label.configure(
                text=f"선택한 적 {role_name}: 모르겠음 · ChatGPT가 블라인드 안정성을 우선 판단"
            )
        elif self.draft.selected_enemy_support_id:
            source = "직접 확정" if self.draft.selected_enemy_support_source == "MANUAL_ENEMY_SUPPORT" else "자동 추정 · 확실하지 않음"
            self.enemy_support_label.configure(
                text=f"선택한 적 {role_name}: {self.draft.selected_enemy_support_name_ko} · {source}"
            )
        else:
            self.enemy_support_label.configure(
                text=f"선택한 적 {role_name}: 아직 모름 · ChatGPT에는 미확정 정보로 전달"
            )
        stale = bool(self.recommendations and self.recommendation_snapshot_id != self.draft.snapshot_id)
        self.stale_label.configure(
            text="⚠ 픽·밴·호버가 변경되어 기존 추천이 오래되었습니다. 새 질문을 복사하세요." if stale else ""
        )

    def _render_opgg(self) -> None:
        for item in self.counter_tree.get_children():
            self.counter_tree.delete(item)
        self._clear(self.weak_frame)
        snapshot = self.opgg_snapshot
        self._refresh_filter_buttons()
        if not snapshot:
            self.opgg_summary_label.configure(
                text="캐시된 데이터가 없습니다. OP.GG 데이터 갱신 버튼을 눌러 현재 통계를 가져오세요."
            )
            self.copy_top3_button.configure(state="disabled")
            self.opgg_calc_label.configure(text="")
            return
        self.copy_top3_button.configure(state="normal")
        role_name = position_name(self.draft.my_role)
        if snapshot.enemy_support_id:
            self.counter_tree.heading("winrate", text="OP.GG 상대 승률")
            summary = (
                f"{snapshot.enemy_support_name_ko} 적 {role_name} · 패치 {snapshot.patch} · {snapshot.region} · "
                f"{snapshot.tier}    전체 승률 {_fmt_rate(snapshot.target_overall_win_rate)} · "
                f"픽률 {_fmt_rate(snapshot.target_pick_rate)} · 밴률 {_fmt_rate(snapshot.target_ban_rate)}"
            )
        else:
            self.counter_tree.heading("winrate", text="OP.GG 전체 승률")
            summary = (
                f"상대 {role_name} 미확인 · 패치 {snapshot.patch} · {snapshot.region} · {snapshot.tier} · "
                "블라인드 픽용 전체 승률 순위"
            )
        self.opgg_summary_label.configure(text=summary)
        puuid = self.storage.get_setting("riot_puuid")
        unavailable = set(self.draft.unavailable_champions())
        counters = self._filtered_counters()
        personal_stats = self._personal_stats_for(
            [counter.champion_id for counter in counters]
        ) if puuid else {}
        if not counters:
            self.opgg_calc_label.configure(text="해당 유형 후보 없음", fg=COLORS["orange"])
        elif puuid and any(personal_stats.get(item.champion_id) is None for item in counters):
            self.opgg_calc_label.configure(text="내 전적 조합 계산 중…", fg=COLORS["blue"])
        else:
            self.opgg_calc_label.configure(
                text=(
                    f"{SUPPORT_FILTER_LABELS[self._support_filter] if self.draft.my_role == 'SUPPORT' else role_name + ' 전체'} "
                    f"{len(counters)}개 · 점수 계산 완료"
                ),
                fg=COLORS["muted"],
            )
        for index, counter in enumerate(counters, start=1):
            personal = personal_stats.get(counter.champion_id)
            score, confidence = candidate_score(counter, personal)
            personal_text = (
                f"{personal.wins}승{personal.losses}패 · {_fmt_rate(personal.win_rate)}"
                if personal and personal.games
                else ("계산 중..." if puuid and personal is None else "기록 없음")
            )
            matchup_text = (
                f"{personal.matchup_wins}승{personal.matchup_losses}패 · {_fmt_rate(personal.matchup_win_rate)}"
                if personal and personal.matchup_games
                else ("계산 중..." if puuid and personal is None else "기록 없음")
            )
            status = "밴/픽 제외" if counter.champion_id in unavailable else "추천 가능"
            icon = self.icon_cache.get(
                counter.champion_id, 32, self._schedule_selection_render
            )
            self.counter_tree.insert(
                "", "end", text=f"  {counter.champion_name_ko}", image=icon or "", values=(
                    index, f"{score:.0f}", confidence, _fmt_rate(counter.versus_win_rate),
                    _fmt_games(counter.games), personal_text, matchup_text, status,
                ), tags=(("blocked",) if counter.champion_id in unavailable else
                         (("strong",) if score >= 60 else (("good",) if score >= 53 else ())))
            )
        if snapshot.weak_picks:
            tk.Label(
                self.weak_frame, text="상성이 불리한 픽", bg=COLORS["panel"], fg=COLORS["red"],
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="left", padx=(0, 8))
            for item in snapshot.weak_picks[:5]:
                self._chip(
                    self.weak_frame, f"{item.champion_name_ko} {_fmt_rate(item.versus_win_rate)}",
                    COLORS["red"], champion_id=item.champion_id,
                )

    def _filtered_counters(self) -> list[OpggCounter]:
        if not self.opgg_snapshot:
            return []
        if self.draft.my_role != "SUPPORT" or self._support_filter == "ALL":
            return self.opgg_snapshot.counters[:10]
        return [
            counter for counter in self.opgg_snapshot.counters
            if support_archetype(counter.champion_id) == self._support_filter
        ][:10]

    def _set_support_filter(self, support_filter: str) -> None:
        if support_filter not in SUPPORT_FILTER_LABELS:
            return
        self._support_filter = (
            support_filter if self.draft.my_role == "SUPPORT" else "ALL"
        )
        self._render_opgg()

    def _refresh_filter_buttons(self) -> None:
        support_mode = self.draft.my_role == "SUPPORT"
        if not support_mode:
            self._support_filter = "ALL"
        self.position_filter_label.configure(
            text="플레이 유형" if support_mode else f"추천 포지션 · {position_name(self.draft.my_role)}"
        )
        for key, button in self.support_filter_buttons.items():
            if key != "ALL" and not support_mode:
                button.pack_forget()
                continue
            if not button.winfo_manager():
                button.pack(side="left", padx=(0, 5))
            button.configure(
                text=(position_name(self.draft.my_role) + " 전체")
                if key == "ALL" and not support_mode else SUPPORT_FILTER_LABELS[key]
            )
            selected = key == self._support_filter
            button.configure(
                bg="#284c73" if selected else COLORS["chip"],
                fg=COLORS["text"] if selected else COLORS["blue"],
                highlightbackground=COLORS["blue"] if selected else COLORS["border"],
            )

    def _copy_top3_candidates(self) -> None:
        counters = [
            counter for counter in self._filtered_counters()
            if counter.champion_id not in set(self.draft.unavailable_champions())
        ]
        if not counters:
            self.opgg_calc_label.configure(text="복사할 추천 가능 후보가 없습니다.", fg=COLORS["orange"])
            return
        puuid = self.storage.get_setting("riot_puuid")
        personal_stats = self._personal_stats_for(
            [counter.champion_id for counter in counters]
        ) if puuid else {}
        ranked = sorted(
            counters,
            key=lambda counter: candidate_score(counter, personal_stats.get(counter.champion_id))[0],
            reverse=True,
        )[:3]
        role_name = position_name(self.draft.my_role)
        filter_name = (
            SUPPORT_FILTER_LABELS[self._support_filter]
            if self.draft.my_role == "SUPPORT" else f"{role_name} 전체"
        )
        lines = [
            f"내 포지션: {role_name} · 적 {role_name}: "
            f"{self.draft.selected_enemy_support_name_ko or '블라인드'} · 유형: {filter_name}"
        ]
        for index, counter in enumerate(ranked, start=1):
            personal = personal_stats.get(counter.champion_id)
            score, confidence = candidate_score(counter, personal)
            local = (
                f"내 전적 {personal.games}판 {_fmt_rate(personal.win_rate)}"
                if personal and personal.games else "내 전적 없음"
            )
            lines.append(
                f"{index}. {counter.champion_name_ko} · 종합 {score:.0f} · 신뢰도 {confidence} · "
                f"OP.GG {_fmt_rate(counter.versus_win_rate)} ({_fmt_games(counter.games)}) · {local}"
            )
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.opgg_calc_label.configure(text="TOP 3 후보를 복사했습니다.", fg=COLORS["gold"])

    def _render_prompt_summary(self) -> None:
        target = self.draft.selected_enemy_support_name_ko or "모르겠음"
        role_name = position_name(self.draft.my_role)
        certainty = {
            "MANUAL_ENEMY_SUPPORT": "사용자 확정",
            "AUTO_ENEMY_SUPPORT": "자동 추정 · 확실하지 않음",
            "MANUAL_UNKNOWN": "미확정 · 블라인드 판단",
        }.get(self.draft.selected_enemy_support_source, "미확정 · 블라인드 판단")
        ally_locked = len(self.draft.ally_locked)
        ally_hover = len(self.draft.ally_hover) + int(self.draft.my_hover is not None)
        opgg = "포함" if self.opgg_snapshot else "캐시 없음"
        self.prompt_summary_label.configure(
            text=(
                f"복사 내용: 내 포지션 {role_name} · 내 픽 순서 · 확정 픽 {ally_locked + len(self.draft.enemy_locked)}명 · "
                f"아군 픽 의사 {ally_hover}명 · 밴 {len(self.draft.ally_bans) + len(self.draft.enemy_bans)}명\n"
                f"적 {role_name}: {target} ({certainty}) · OP.GG 데이터: {opgg} · 응답 스냅샷: {self.draft.snapshot_id}"
            )
        )

    def _render_recommendations(self) -> None:
        self._clear(self.cards_frame)
        if not self.recommendations:
            tk.Label(
                self.cards_frame,
                text="질문을 ChatGPT에 붙여넣고 답변을 가져오면 추천 3개가 여기에 표시됩니다.",
                bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 10),
            ).pack(anchor="w", pady=12)
            return
        puuid = self.storage.get_setting("riot_puuid")
        personal_stats = self._personal_stats_for(
            [recommendation.champion_id for recommendation in self.recommendations]
        ) if puuid else {}
        for column, recommendation in enumerate(self.recommendations):
            card = tk.Frame(self.cards_frame, bg=COLORS["border"], padx=1, pady=1)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 6))
            self.cards_frame.grid_columnconfigure(column, weight=1, uniform="cards")
            inner = tk.Frame(card, bg=COLORS["panel_2"], padx=14, pady=12)
            inner.pack(fill="both", expand=True)
            title_row = tk.Frame(inner, bg=COLORS["panel_2"])
            title_row.pack(fill="x")
            icon = self.icon_cache.get(
                recommendation.champion_id, 60, self._schedule_selection_render
            )
            if icon:
                tk.Label(title_row, image=icon, bg=COLORS["panel_2"]).pack(side="left", padx=(0, 10))
            title_text = tk.Frame(title_row, bg=COLORS["panel_2"])
            title_text.pack(side="left", fill="x", expand=True)
            counter = self._counter_for(recommendation.champion_id)
            personal = personal_stats.get(recommendation.champion_id)
            tk.Label(
                title_text, text=f"{recommendation.rank}위  {recommendation.champion_name_ko}",
                bg=COLORS["panel_2"], fg=COLORS["gold"], font=("Malgun Gothic", 14, "bold"),
            ).pack(anchor="w")
            tk.Label(
                title_text, text=f"{recommendation.style} · 블라인드 안정성 {recommendation.blind_safety}",
                bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 9),
            ).pack(anchor="w")
            if counter:
                score, confidence = candidate_score(counter, personal)
                score_color = (
                    COLORS["green"] if score >= 60 else
                    COLORS["gold"] if score >= 53 else COLORS["muted"]
                )
                tk.Label(
                    title_text, text=f"종합 {score:.0f}점  ·  데이터 신뢰도 {confidence}",
                    bg=COLORS["panel_2"], fg=score_color,
                    font=("Malgun Gothic", 9, "bold"),
                ).pack(anchor="w", pady=(3, 10))
            else:
                tk.Label(
                    title_text, text="종합점수: OP.GG 후보표에 없는 추천",
                    bg=COLORS["panel_2"], fg=COLORS["muted"],
                    font=("Malgun Gothic", 8),
                ).pack(anchor="w", pady=(3, 10))
            self._stat_block(
                inner, "OP.GG 전체/상성", COLORS["blue"],
                f"상대 승률 {_fmt_rate(counter.versus_win_rate) if counter else '데이터 없음'}\n"
                f"표본 {_fmt_games(counter.games) if counter else '데이터 없음'}",
            )
            self._stat_block(
                inner, "내 챔피언 전적", COLORS["green"],
                (f"{personal.wins}승 {personal.losses}패 · {_fmt_rate(personal.win_rate)}\n"
                 f"KDA {personal.kda:.2f} · 시야 {personal.vision_score:.1f}"
                 if personal and personal.games
                 else ("계산 중..." if puuid and personal is None else "저장된 플레이 기록 없음")),
            )
            self._stat_block(
                inner, "내 상대 챔피언 전적", COLORS["purple"],
                (f"{personal.matchup_wins}승 {personal.matchup_losses}패 · "
                 f"{_fmt_rate(personal.matchup_win_rate)}\n{personal.matchup_confidence}"
                 if personal and personal.matchup_games
                 else ("계산 중..." if puuid and personal is None else "저장된 맞대결 기록 없음")),
            )
            self._paragraph(inner, "추천 이유", recommendation.reason)
            self._paragraph(inner, "팀 조합", recommendation.team_synergy)
            self._paragraph(inner, "라인전", recommendation.lane_plan)
            self._paragraph(inner, "주의", recommendation.watch_for, COLORS["orange"])

    def _render_play(self) -> None:
        self._clear(self.live_allies_frame)
        self._clear(self.live_enemies_frame)
        self._render_play_summary()
        if not self.live_game.players:
            self.live_game_label.configure(text="게임이 시작되면 자동으로 플레이 탭으로 이동합니다.")
            self.live_profile_status.configure(text="")
            self.live_duo_status.configure(
                text="DUO: 게임 시작 후 현재 10명의 최근 100경기 교집합을 확인합니다.",
                fg=COLORS["orange"],
            )
            for frame in (self.live_allies_frame, self.live_enemies_frame):
                tk.Label(
                    frame, text="플레이어 데이터 대기 중", bg=COLORS["panel_2"], fg=COLORS["muted"],
                    padx=14, pady=20, font=("Malgun Gothic", 9),
                ).grid(row=0, column=0, columnspan=5, sticky="ew", pady=3)
            return
        minutes = int(self.live_game.game_time // 60)
        seconds = int(self.live_game.game_time % 60)
        self.live_game_label.configure(
            text=f"{self.live_game.game_mode or '게임'} · {minutes:02d}:{seconds:02d} · "
                 f"플레이어 {len(self.live_game.players)}명"
        )
        if self._profiles_loading:
            self.live_profile_status.configure(text="로컬 전적 읽는 중...", fg=COLORS["blue"])
        else:
            loaded = sum(
                1 for profile in self.player_profiles.values()
                if profile.status in {"OK", "LOCAL_ONLY"}
            )
            self.live_profile_status.configure(
                text=f"플레이어 전적 로컬 캐시 {loaded}/{len(self.live_game.players)}",
                fg=COLORS["muted"],
            )
        if self._duo_checking:
            self.live_duo_status.configure(text="DUO 추정 확인 중...", fg=COLORS["blue"])
        position_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4,
                          "SUPPORT": 4, "UNKNOWN": 9}
        allies = sorted(self.live_game.allies, key=lambda p: position_order.get(p.position, 9))
        enemies = sorted(self.live_game.enemies, key=lambda p: position_order.get(p.position, 9))
        for slot, player in enumerate(allies):
            self._render_player_card(self.live_allies_frame, player, ally=True, slot=slot)
        for slot, player in enumerate(enemies):
            self._render_player_card(self.live_enemies_frame, player, ally=False, slot=slot)

    def _render_play_summary(self) -> None:
        def team_rates(players: list[LivePlayer]) -> list[float]:
            rates: list[float] = []
            for player in players:
                profile = self.player_profiles.get(player.riot_id)
                if profile and profile.status == "OK" and profile.season_win_rate is not None:
                    rates.append(profile.season_win_rate)
            return rates

        ally_rates = team_rates(self.live_game.allies)
        enemy_rates = team_rates(self.live_game.enemies)
        ally_average = sum(ally_rates) / len(ally_rates) if ally_rates else None
        enemy_average = sum(enemy_rates) / len(enemy_rates) if enemy_rates else None
        difference = (
            ally_average - enemy_average
            if ally_average is not None and enemy_average is not None else None
        )
        ally_detail = f"랭크 표본 {len(ally_rates)}명"
        enemy_detail = f"랭크 표본 {len(enemy_rates)}명"
        if difference is not None:
            ally_detail += f" · 적 대비 {difference:+.1f}%p"
            enemy_detail += f" · 아군 대비 {-difference:+.1f}%p"
        self.play_metrics["ally"][0].configure(text=_fmt_rate(ally_average))
        self.play_metrics["ally"][1].configure(text=ally_detail)
        self.play_metrics["enemy"][0].configure(text=_fmt_rate(enemy_average))
        self.play_metrics["enemy"][1].configure(text=enemy_detail)
        loaded = sum(
            1 for profile in self.player_profiles.values()
            if profile.status in {"OK", "LOCAL_ONLY"}
        )
        total = len(self.live_game.players)
        self.play_metrics["cache"][0].configure(text=f"{loaded}/{total or 10}명")
        self.play_metrics["cache"][1].configure(
            text="로컬+Riot 결합" if loaded else "게임 시작 후 자동 확인"
        )
        pair_levels: dict[tuple[str, str], str] = {}
        priority = {"가능": 1, "유력": 2, "매우 유력": 3}
        for player, values in self.duo_pairs.items():
            for other, level, _evidence in values:
                pair = tuple(sorted((player, other)))
                if priority.get(level, 0) > priority.get(pair_levels.get(pair, ""), 0):
                    pair_levels[pair] = level
        strongest = (
            max(pair_levels.values(), key=lambda level: priority.get(level, 0))
            if pair_levels else "없음"
        )
        self.play_metrics["duo"][0].configure(text=f"{len(pair_levels)}쌍")
        self.play_metrics["duo"][1].configure(text=f"가장 강한 신호 · {strongest}")

    def _render_player_card(
        self, parent: tk.Widget, player: LivePlayer, ally: bool, slot: int
    ) -> None:
        team_color = COLORS["blue"] if ally else COLORS["red"]
        border_color = COLORS["gold"] if player.is_active_player else team_color
        outer = tk.Frame(parent, bg=border_color, padx=1, pady=1)
        outer.grid(row=0, column=slot, sticky="nsew", padx=3)
        card = tk.Frame(outer, bg=COLORS["panel_2"], padx=9, pady=8)
        card.pack(fill="both", expand=True)

        profile = self.player_profiles.get(player.riot_id)
        available = bool(profile and profile.status in {"OK", "LOCAL_ONLY"})
        duo_values = self.duo_pairs.get(player.riot_id, [])
        duo_level = ""
        if duo_values:
            duo_level = max(
                (value[1] for value in duo_values),
                key=lambda level: {"가능": 1, "유력": 2, "매우 유력": 3}.get(level, 0),
            )

        top = tk.Frame(card, bg=COLORS["panel_2"])
        top.pack(fill="x")
        role = ROLE_LABELS.get(player.position, player.position)
        tk.Label(
            top, text=role, bg=team_color, fg="#07101b", padx=7, pady=2,
            font=("Malgun Gothic", 7, "bold"),
        ).pack(side="left")
        if duo_level:
            tk.Label(
                top, text=f"● DUO {duo_level}", bg=COLORS["panel_2"],
                fg=COLORS["red"] if duo_level == "매우 유력" else COLORS["orange"],
                font=("Malgun Gothic", 7, "bold"),
            ).pack(side="right")
        elif available and profile:
            tk.Label(
                top, text=f"{profile.season_wins}W - {profile.season_losses}L",
                bg=COLORS["panel_2"], fg=team_color, font=("Malgun Gothic", 8, "bold"),
            ).pack(side="right")

        display_name = player.riot_id if len(player.riot_id) <= 20 else player.riot_id[:19] + "…"
        tk.Label(
            card, text=f"{display_name}{'  · 나' if player.is_active_player else ''}",
            bg=COLORS["panel_2"], fg=COLORS["gold"] if player.is_active_player else COLORS["text"],
            font=("Malgun Gothic", 9, "bold"),
        ).pack(pady=(6, 1))

        identity = tk.Frame(card, bg=COLORS["panel_2"])
        identity.pack(fill="x", pady=(1, 6))
        icon = self.icon_cache.get(player.champion_id, 68, self._schedule_play_render)
        tk.Label(
            identity, image=icon or "", text="" if icon else player.champion_name_ko[:1],
            width=68 if not icon else 0, height=3 if not icon else 0,
            bg=COLORS["chip"], fg=COLORS["gold"], font=("Malgun Gothic", 16, "bold"),
            highlightthickness=1, highlightbackground=border_color,
        ).pack(side="left", padx=(0, 8))
        identity_text = tk.Frame(identity, bg=COLORS["panel_2"])
        identity_text.pack(side="left", fill="both", expand=True)
        tk.Label(
            identity_text, text=player.champion_name_ko, bg=COLORS["panel_2"],
            fg=COLORS["text"], font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")

        if not available or not profile:
            status_text = "전적 불러오는 중"
            if profile and profile.status == "NO_LOCAL_DATA":
                status_text = "로컬 데이터 없음"
            elif profile and profile.status not in {"NO_DATA", "LOADING"}:
                status_text = profile.status
            tk.Label(
                identity_text, text="랭크 --", bg=COLORS["panel_2"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7),
            ).pack(anchor="w", pady=(2, 0))
            tk.Label(
                card, text=status_text, bg=COLORS["surface"], fg=COLORS["muted"],
                padx=6, pady=18, font=("Malgun Gothic", 8),
            ).pack(fill="x", pady=(0, 6))
            self._winrate_bar(card, None, "시즌 데이터 대기")
            return

        rank_text = "언랭크"
        if profile.tier != "UNRANKED":
            rank_text = f"{profile.tier} {profile.rank} · {profile.league_points}LP"
        rank_color = {
            "IRON": "#8c8a87", "BRONZE": "#b8794c", "SILVER": "#b8c4cf",
            "GOLD": COLORS["gold"], "PLATINUM": "#54d6c0", "EMERALD": "#42d68a",
            "DIAMOND": "#72a8ff", "MASTER": COLORS["purple"],
            "GRANDMASTER": COLORS["red"], "CHALLENGER": "#62d7ff",
        }.get(profile.tier, COLORS["muted"])
        tk.Label(
            identity_text, text=rank_text, bg=COLORS["panel_2"], fg=rank_color,
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        if duo_level:
            tk.Label(
                identity_text,
                text=f"시즌 {profile.season_wins}W - {profile.season_losses}L",
                bg=COLORS["panel_2"], fg=team_color, font=("Malgun Gothic", 7, "bold"),
            ).pack(anchor="w", pady=(2, 0))

        stats = tk.Frame(card, bg=COLORS["panel_2"])
        stats.pack(fill="x", pady=(0, 5))
        stats.grid_columnconfigure(0, weight=1, uniform="compact_stats")
        stats.grid_columnconfigure(1, weight=1, uniform="compact_stats")
        champion_losses = max(profile.champion_games - profile.champion_wins, 0)
        champion_value = (
            f"{profile.champion_games}판 · {_fmt_rate(profile.champion_win_rate)}"
            if profile.champion_games else "기록 없음"
        )
        champion_detail = (
            f"{profile.champion_wins}승 {champion_losses}패 · 로컬 {profile.local_sample_games}"
            if profile.champion_games else f"로컬 표본 {profile.local_sample_games}경기"
        )
        sample_color = (
            COLORS["orange"] if 0 < profile.champion_games < 5 else COLORS["green"]
        )
        self._compact_stat(stats, 0, "현 챔프", champion_value, champion_detail, sample_color)

        if profile.last_game_champion_id:
            last_result = "승" if profile.last_game_won else "패"
            kda_text = "Perfect" if profile.last_game_deaths == 0 else f"{profile.last_game_kda:.1f}"
            last_value = f"{last_result} · KDA {kda_text}"
            last_detail = (
                f"{self.registry.ko_name(profile.last_game_champion_id)}  "
                f"{profile.last_game_kills}/{profile.last_game_deaths}/{profile.last_game_assists}"
            )
            last_color = COLORS["green"] if profile.last_game_won else COLORS["red"]
        else:
            last_value, last_detail, last_color = "데이터 없음", "솔로랭크", COLORS["muted"]
        self._compact_stat(stats, 1, "전판", last_value, last_detail, last_color)

        if player.is_active_player:
            relationship = "나와 기록 · 내 계정"
        else:
            relation_parts: list[str] = []
            if profile.together_games:
                relation_parts.append(
                    f"동팀 {profile.together_games}판 {_fmt_rate(profile.together_win_rate)}"
                )
            if profile.against_games:
                relation_parts.append(
                    f"상대 {profile.against_games}판 {_fmt_rate(profile.against_my_win_rate)}"
                )
            relationship = "나와 · " + (" / ".join(relation_parts) if relation_parts else "만난 기록 없음")
        tk.Label(
            card, text=relationship, bg=COLORS["panel_2"], fg=COLORS["purple"],
            font=("Malgun Gothic", 7, "bold"), anchor="w",
        ).pack(fill="x")

        if profile.last_met_game_number:
            when = "직전판" if profile.last_met_game_number == 1 else f"최근 {profile.last_met_game_number}번째"
            side = "동팀" if profile.last_met_same_team else "상대"
            result = "승" if profile.last_met_my_win else "패"
            meeting = f"최근 만남 · {when} · {side} · {result}"
        else:
            meeting = "최근 만남 · 없음"
        tk.Label(
            card, text=meeting, bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), anchor="w",
        ).pack(fill="x", pady=(2, 0))

        if duo_values:
            other, level, evidence = duo_values[0]
            other_short = other if len(other) <= 13 else other[:12] + "…"
            evidence_short = evidence if len(evidence) <= 18 else evidence[:17] + "…"
            tk.Label(
                card, text=f"DUO {level} · {other_short} · {evidence_short}",
                bg=COLORS["panel_2"],
                fg=COLORS["red"] if level == "매우 유력" else COLORS["orange"],
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x", pady=(2, 0))
        else:
            tk.Label(
                card, text="DUO 신호 없음", bg=COLORS["panel_2"], fg="#65738a",
                font=("Malgun Gothic", 7), anchor="w",
            ).pack(fill="x", pady=(2, 0))

        self._winrate_bar(
            card, profile.season_win_rate,
            f"시즌 {_fmt_rate(profile.season_win_rate)} · {profile.season_wins}W {profile.season_losses}L",
        )

    def _compact_stat(
        self, parent: tk.Widget, column: int, title: str, value: str, detail: str, color: str
    ) -> None:
        frame = tk.Frame(parent, bg=COLORS["surface"], padx=6, pady=5)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 3) if column == 0 else (3, 0))
        tk.Label(
            frame, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame, text=value, bg=COLORS["surface"], fg=color,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame, text=detail, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 6),
        ).pack(anchor="w")

    def _winrate_bar(self, parent: tk.Widget, rate: float | None, label: str) -> None:
        canvas = tk.Canvas(
            parent, height=18, bg="#26151d", highlightthickness=0, bd=0,
        )
        canvas.pack(fill="x", pady=(6, 0))

        def draw(_event: tk.Event | None = None) -> None:
            width = max(canvas.winfo_width(), 10)
            canvas.delete("all")
            canvas.create_rectangle(0, 0, width, 18, fill="#351a24", outline="")
            if rate is not None:
                filled = int(width * max(0.0, min(rate, 100.0)) / 100.0)
                fill = COLORS["green"] if rate >= 50 else COLORS["blue"]
                canvas.create_rectangle(0, 0, filled, 18, fill=fill, outline="")
            canvas.create_text(
                width // 2, 9, text=label, fill=COLORS["text"],
                font=("Malgun Gothic", 7, "bold"),
            )

        canvas.bind("<Configure>", draw)
        canvas.after_idle(draw)

    def _ensure_history_loaded(self, force: bool = False) -> None:
        if self._history_loading:
            if force:
                self._history_reload_requested = True
            return
        puuid = self._history_puuid()
        if not puuid:
            self.history_overview = HistoryOverview()
            self.history_status_label.configure(
                text="Riot 설정 후 전적을 갱신하세요.", fg=COLORS["orange"]
            )
            self._render_history()
            return
        revision = self.storage.match_revision()
        if not force and self.history_overview is not None and revision == self._history_revision:
            self._render_history()
            return
        self._history_loading = True
        self.history_status_label.configure(text="최대 1,000경기 분석 중…", fg=COLORS["blue"])
        self._render_history()

        def work() -> HistoryOverview:
            return analyze_history(self.storage.player_matches(puuid, limit=1000), puuid)

        def success(overview: HistoryOverview) -> None:
            self._history_loading = False
            self.history_overview = overview
            self._history_revision = revision
            self._history_visible_count = 12
            self._render_history()
            if self._history_reload_requested:
                self._history_reload_requested = False
                self.root.after(50, lambda: self._ensure_history_loaded(force=True))

        def error(exc: Exception) -> None:
            self._history_loading = False
            self._history_reload_requested = False
            self.history_status_label.configure(text=f"전적 분석 실패 · {exc}", fg=COLORS["red"])

        self._background(work, success, error)

    def _history_puuid(self) -> str:
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        riot_id = f"{game_name}#{tag_line}" if game_name and tag_line else ""
        return (
            self.storage.find_puuid_by_riot_id(riot_id) if riot_id else ""
        ) or self.storage.get_setting("riot_puuid")

    def _render_history(self) -> None:
        if not hasattr(self, "history_matches_frame"):
            return
        self._clear(self.history_champions_frame)
        self._clear(self.history_matches_frame)
        game_name = self.storage.get_setting("riot_game_name") or "Riot 계정 미확인"
        tag_line = self.storage.get_setting("riot_tag_line")
        self.history_identity_label.configure(
            text=f"{game_name}{'#' + tag_line if tag_line else ''}"
        )
        overview = self.history_overview
        if overview is None:
            message = "로컬 전적 분석 중…" if self._history_loading else "내 전적 탭을 열면 자동 분석합니다."
            tk.Label(
                self.history_matches_frame, text=message, bg=COLORS["surface"],
                fg=COLORS["muted"], padx=14, pady=35, font=("Malgun Gothic", 9),
            ).pack(fill="x")
            return

        rank_value, rank_detail = self._history_rank_text(game_name, tag_line)
        self.history_metrics["rank"][0].configure(text=rank_value)
        self.history_metrics["rank"][1].configure(text=rank_detail)
        self.history_metrics["games"][0].configure(text=f"{overview.games:,}경기")
        self.history_metrics["games"][1].configure(
            text=f"{overview.wins}승 {overview.games - overview.wins}패 · {_fmt_rate(overview.win_rate)}"
        )
        streak = (
            f"{overview.current_streak}연승" if overview.current_streak > 0 else
            f"{abs(overview.current_streak)}연패" if overview.current_streak < 0 else "연속 기록 없음"
        )
        self.history_metrics["recent"][0].configure(text=_fmt_rate(overview.recent_20_win_rate))
        self.history_metrics["recent"][1].configure(
            text=f"{overview.recent_20_wins}승 {overview.recent_20_games - overview.recent_20_wins}패 · {streak}"
        )
        self.history_metrics["kda"][0].configure(
            text=f"{overview.kda:.2f}" if overview.kda is not None else "--"
        )
        self.history_metrics["kda"][1].configure(
            text=f"누적 {overview.kills}/{overview.deaths}/{overview.assists}"
        )
        self.history_metrics["vision"][0].configure(
            text=f"{overview.average_vision:.1f}" if overview.average_vision is not None else "--"
        )
        self.history_metrics["vision"][1].configure(text="경기당 시야 점수")

        if self._history_loading:
            self.history_status_label.configure(text="새 로컬 데이터 분석 중…", fg=COLORS["blue"])
        else:
            self.history_status_label.configure(
                text=f"로컬 솔로랭크 {overview.games:,}경기 · 외부 요청 없음",
                fg=COLORS["green"],
            )

        if not overview.champions:
            tk.Label(
                self.history_champions_frame, text="저장된 챔피언 기록이 없습니다.",
                bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
            ).pack(anchor="w", pady=10)
        for stat in overview.champions[:12]:
            self._render_history_champion(stat)

        if not overview.entries:
            tk.Label(
                self.history_matches_frame,
                text="저장된 솔로랭크 경기가 없습니다. Riot 전적 갱신을 눌러 먼저 저장하세요.",
                bg=COLORS["surface"], fg=COLORS["muted"], padx=14, pady=35,
                font=("Malgun Gothic", 9),
            ).pack(fill="x")
        for entry in overview.entries[:self._history_visible_count]:
            self._render_history_match(entry)
        remaining = len(overview.entries) - self._history_visible_count
        self.history_more_button.configure(
            state="normal" if remaining > 0 else "disabled",
            text=f"경기 더 보기 · {remaining:,}개 남음" if remaining > 0 else "모든 저장 경기 표시됨",
        )

    def _history_rank_text(self, game_name: str, tag_line: str) -> tuple[str, str]:
        cached = self.storage.load_live_profile_any_age(
            f"{game_name}#{tag_line}" if tag_line else game_name
        )
        if not cached:
            return "랭크 미확인", "Riot 전적 갱신 필요"
        _puuid, payload, _updated_at = cached
        entry = payload.get("solo_entry") or {}
        tier = str(entry.get("tier") or "UNRANKED")
        if tier == "UNRANKED":
            return "UNRANKED", "솔로랭크 배치 전"
        wins = int(entry.get("wins") or 0)
        losses = int(entry.get("losses") or 0)
        return (
            f"{tier} {entry.get('rank') or ''}",
            f"{entry.get('leaguePoints') or 0}LP · {wins}승 {losses}패",
        )

    def _render_history_champion(self, stat: object) -> None:
        champion_id = str(getattr(stat, "champion_id", "Unknown"))
        outer = tk.Frame(
            self.history_champions_frame, bg=COLORS["surface"], padx=8, pady=6,
        )
        outer.pack(fill="x", pady=2)
        icon = self.icon_cache.get(champion_id, 34, self._schedule_history_render)
        tk.Label(
            outer, image=icon or "", text="" if icon else self.registry.ko_name(champion_id)[:1],
            bg=COLORS["chip"], fg=COLORS["gold"], width=34 if not icon else 0,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        name = tk.Frame(outer, bg=COLORS["surface"])
        name.pack(side="left", fill="x", expand=True)
        tk.Label(
            name, text=self.registry.ko_name(champion_id), bg=COLORS["surface"],
            fg=COLORS["text"], font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            name,
            text=f"{getattr(stat, 'games', 0)}판 · KDA {getattr(stat, 'kda', None) or 0:.2f}",
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w")
        rate = getattr(stat, "win_rate", None)
        color = COLORS["green"] if rate is not None and rate >= 50 else COLORS["red"]
        tk.Label(
            outer, text=_fmt_rate(rate), bg=COLORS["surface"], fg=color,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

    def _render_history_match(self, entry: MatchHistoryEntry) -> None:
        result_color = COLORS["green"] if entry.won else COLORS["red"]
        outer = tk.Frame(self.history_matches_frame, bg=result_color, padx=1, pady=1)
        outer.pack(fill="x", pady=3)
        card = tk.Frame(outer, bg=COLORS["surface"], padx=9, pady=7)
        card.pack(fill="x")
        icon = self.icon_cache.get(entry.champion_id, 52, self._schedule_history_render)
        tk.Label(
            card, image=icon or "", text="" if icon else self.registry.ko_name(entry.champion_id)[:1],
            bg=COLORS["chip"], fg=COLORS["gold"], width=52 if not icon else 0,
            font=("Malgun Gothic", 13, "bold"), highlightthickness=1,
            highlightbackground=result_color,
        ).pack(side="left", padx=(0, 9))
        result = tk.Frame(card, bg=COLORS["surface"], width=190)
        result.pack(side="left", fill="y")
        result.pack_propagate(False)
        tk.Label(
            result, text=f"{'승리' if entry.won else '패배'} · {self.registry.ko_name(entry.champion_id)}",
            bg=COLORS["surface"], fg=result_color, font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            result,
            text=f"{self._history_time(entry.game_creation)} · {entry.duration_seconds // 60}:{entry.duration_seconds % 60:02d}",
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(2, 0))

        core = tk.Frame(card, bg=COLORS["surface"], width=180)
        core.pack(side="left", fill="y")
        core.pack_propagate(False)
        tk.Label(
            core, text=f"KDA {entry.kda:.2f}  ·  {entry.kills}/{entry.deaths}/{entry.assists}",
            bg=COLORS["surface"], fg=COLORS["gold"], font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            core,
            text=f"CS {entry.cs} ({entry.cs_per_minute:.1f}/분) · 시야 {entry.vision_score}",
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(2, 0))

        items = tk.Frame(card, bg=COLORS["surface"], width=210)
        items.pack(side="left", fill="y", padx=(3, 8))
        items.pack_propagate(False)
        tk.Label(
            items, text="아이템", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 6, "bold"),
        ).pack(anchor="w")
        item_row = tk.Frame(items, bg=COLORS["surface"])
        item_row.pack(anchor="w", pady=(2, 0))
        if not entry.items:
            tk.Label(
                item_row, text="기록 없음", bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7),
            ).pack(side="left")
        for item_id in entry.items[:7]:
            image = self.item_icon_cache.get(item_id, 24, self._schedule_history_render)
            item_label = tk.Label(
                item_row, image=image or "", text="" if image else str(item_id)[-2:],
                bg=COLORS["chip"], fg=COLORS["muted"], width=24 if not image else 0,
                font=("Consolas", 6),
            )
            item_label.pack(side="left", padx=(0, 2))
            tooltip = _HoverTooltip(
                item_label,
                lambda value=item_id: self.item_icon_cache.tooltip_text(value),
            )
            setattr(item_label, "_advisor_tooltip", tooltip)

        lineup = tk.Frame(card, bg=COLORS["surface"])
        lineup.pack(side="left", fill="y", expand=True)
        for row_index, champions in enumerate((entry.ally_champions, entry.enemy_champions)):
            row = tk.Frame(lineup, bg=COLORS["surface"])
            row.pack(anchor="w", pady=(0, 2))
            tk.Label(
                row, text="A" if row_index == 0 else "E", bg=COLORS["surface"],
                fg=COLORS["blue"] if row_index == 0 else COLORS["red"],
                font=("Consolas", 6, "bold"), width=2,
            ).pack(side="left")
            for champion_id in champions[:5]:
                image = self.icon_cache.get(champion_id, 20, self._schedule_history_render)
                tk.Label(
                    row, image=image or "", text="" if image else "?", bg=COLORS["chip"],
                    fg=COLORS["muted"], width=20 if not image else 0,
                ).pack(side="left", padx=(0, 2))

        self._button(
            card, "경기 상세", lambda match_id=entry.match_id: self._open_match_detail(match_id),
            COLORS["purple"], width=9,
        ).pack(side="right", padx=(8, 0))

    @staticmethod
    def _history_time(game_creation: int) -> str:
        if not game_creation:
            return "시간 미상"
        played = datetime.fromtimestamp(game_creation / 1000)
        elapsed = datetime.now() - played
        if elapsed < timedelta(hours=1):
            return f"{max(int(elapsed.total_seconds() // 60), 1)}분 전"
        if elapsed < timedelta(days=1):
            return f"{int(elapsed.total_seconds() // 3600)}시간 전"
        if elapsed < timedelta(days=7):
            return f"{elapsed.days}일 전"
        return played.strftime("%m.%d %H:%M")

    def _show_more_history(self) -> None:
        self._history_visible_count += 10
        self._render_history()

    def _open_match_detail(self, match_id: str) -> None:
        match = self.storage.load_match(match_id)
        puuid = self._history_puuid()
        if not match or not puuid:
            messagebox.showwarning("경기 상세", "로컬 경기 데이터를 찾지 못했습니다.", parent=self.root)
            return
        info = match.get("info") or {}
        participants = info.get("participants") or []
        mine = next((row for row in participants if row.get("puuid") == puuid), None)
        if not mine:
            messagebox.showwarning("경기 상세", "내 참가자 기록을 찾지 못했습니다.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"경기 상세 · {match_id}")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("1320x860")
        dialog.minsize(1050, 700)
        dialog.transient(self.root)

        canvas = tk.Canvas(dialog, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=COLORS["bg"])
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        setattr(dialog, "_advisor_scroll_canvas", canvas)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(content_window, width=e.width))

        won = bool(mine.get("win"))
        result_color = COLORS["green"] if won else COLORS["red"]
        header = tk.Frame(content, bg=COLORS["bg"], padx=22, pady=16)
        header.pack(fill="x")
        icon = self.icon_cache.get(str(mine.get("championName") or "Unknown"), 64)
        tk.Label(
            header, image=icon or "", text="" if icon else "?", bg=COLORS["chip"],
            fg=COLORS["gold"], width=64 if not icon else 0,
        ).pack(side="left", padx=(0, 12))
        title = tk.Frame(header, bg=COLORS["bg"])
        title.pack(side="left", fill="x", expand=True)
        tk.Label(
            title,
            text=f"{'승리' if won else '패배'} · {self.registry.ko_name(str(mine.get('championName') or ''))}",
            bg=COLORS["bg"], fg=result_color, font=("Malgun Gothic", 17, "bold"),
        ).pack(anchor="w")
        duration = int(info.get("gameDuration") or mine.get("timePlayed") or 0)
        creation = int(info.get("gameCreation") or 0)
        tk.Label(
            title,
            text=f"솔로랭크 · {duration // 60}:{duration % 60:02d} · {self._history_time(creation)} · {match_id}",
            bg=COLORS["bg"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(anchor="w", pady=(3, 0))
        self._button(header, "닫기", dialog.destroy, COLORS["muted"]).pack(side="right")

        team_id = mine.get("teamId")
        team_kills = sum(
            int(row.get("kills") or 0) for row in participants if row.get("teamId") == team_id
        )
        kills = int(mine.get("kills") or 0)
        deaths = int(mine.get("deaths") or 0)
        assists = int(mine.get("assists") or 0)
        kda = (kills + assists) / max(deaths, 1)
        participation = (kills + assists) / team_kills * 100 if team_kills else None
        cs = int(mine.get("totalMinionsKilled") or 0) + int(mine.get("neutralMinionsKilled") or 0)
        metrics = tk.Frame(content, bg=COLORS["bg"])
        metrics.pack(fill="x", padx=22, pady=(0, 12))
        for index, (label, value, detail, accent) in enumerate((
            ("KDA", f"{kda:.2f}", f"{kills}/{deaths}/{assists}", COLORS["gold"]),
            ("킬 관여", _fmt_rate(participation), f"팀 {team_kills}킬", COLORS["green"]),
            ("챔피언 피해", f"{int(mine.get('totalDamageDealtToChampions') or 0):,}", "가한 피해량", COLORS["red"]),
            ("시야 점수", f"{int(mine.get('visionScore') or 0)}", f"와드 {int(mine.get('wardsPlaced') or 0)}개", COLORS["blue"]),
            ("CS/분", f"{cs / max(duration / 60, 1):.1f}", f"총 {cs} CS", COLORS["purple"]),
        )):
            outer, value_label, detail_label = self._mini_metric(metrics, label, accent)
            value_label.configure(text=value)
            detail_label.configure(text=detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 4, 4))

        detail_panel = self._panel(content, "내 상세 지표", COLORS["purple"])
        detail_columns = tk.Frame(detail_panel, bg=COLORS["panel"])
        detail_columns.pack(fill="x")
        combat = tk.Frame(detail_columns, bg=COLORS["panel_2"], padx=12, pady=10)
        vision = tk.Frame(detail_columns, bg=COLORS["panel_2"], padx=12, pady=10)
        combat.pack(side="left", fill="both", expand=True, padx=(0, 6))
        vision.pack(side="left", fill="both", expand=True, padx=(6, 0))
        tk.Label(
            combat, text="전투", bg=COLORS["panel_2"], fg=COLORS["red"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        for label, value in (
            ("가한 챔피언 피해", f"{int(mine.get('totalDamageDealtToChampions') or 0):,}"),
            ("받은 피해", f"{int(mine.get('totalDamageTaken') or 0):,}"),
            ("피해 감소", f"{int(mine.get('damageSelfMitigated') or 0):,}"),
            ("골드 획득", f"{int(mine.get('goldEarned') or 0):,}"),
            ("적 CC 시간", f"{int(mine.get('timeCCingOthers') or 0)}초"),
        ):
            self._detail_stat_row(combat, label, value)
        tk.Label(
            vision, text="시야·지원", bg=COLORS["panel_2"], fg=COLORS["blue"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        for label, value in (
            ("시야 점수", f"{int(mine.get('visionScore') or 0)}"),
            ("와드 설치 / 제거", f"{int(mine.get('wardsPlaced') or 0)} / {int(mine.get('wardsKilled') or 0)}"),
            ("제어 와드", f"{int(mine.get('detectorWardsPlaced') or 0)}"),
            ("아군 치유", f"{int(mine.get('totalHealsOnTeammates') or 0):,}"),
            ("아군 보호막", f"{int(mine.get('totalDamageShieldedOnTeammates') or 0):,}"),
        ):
            self._detail_stat_row(vision, label, value)

        for current_team in (team_id, next((row.get("teamId") for row in participants if row.get("teamId") != team_id), None)):
            if current_team is None:
                continue
            team_rows = [row for row in participants if row.get("teamId") == current_team]
            self._render_detail_team(content, team_rows, current_team == team_id, info)

    def _detail_stat_row(self, parent: tk.Widget, label: str, value: str) -> None:
        row = tk.Frame(parent, bg=COLORS["surface"], padx=9, pady=6)
        row.pack(fill="x", pady=2)
        tk.Label(
            row, text=label, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="left")
        tk.Label(
            row, text=value, bg=COLORS["surface"], fg=COLORS["text"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

    def _render_detail_team(
        self, parent: tk.Widget, participants: list[dict], ally: bool, info: dict
    ) -> None:
        accent = COLORS["blue"] if ally else COLORS["red"]
        title = "아군" if ally else "적군"
        team_id = participants[0].get("teamId") if participants else 0
        kills = sum(int(row.get("kills") or 0) for row in participants)
        deaths = sum(int(row.get("deaths") or 0) for row in participants)
        assists = sum(int(row.get("assists") or 0) for row in participants)
        gold = sum(int(row.get("goldEarned") or 0) for row in participants)
        team_payload = next(
            (row for row in (info.get("teams") or []) if row.get("teamId") == team_id), {}
        )
        objective_counts = team_objective_counts(team_payload)
        panel = self._panel(
            parent,
            f"{title} · {kills}/{deaths}/{assists} · {gold:,}골드 · "
            f"공허 유충 {objective_counts['void_grubs']} / "
            f"전령 {objective_counts['rift_heralds']} / "
            f"용 {objective_counts['dragons']} / 바론 {objective_counts['barons']} / "
            f"타워 {objective_counts['towers']}",
            accent,
        )
        header = tk.Frame(panel, bg=COLORS["chip"], padx=7, pady=5)
        header.pack(fill="x", pady=(0, 3))
        for text_value, width in (("플레이어", 28), ("KDA", 14), ("CS", 8), ("시야", 8), ("챔피언 피해", 18), ("아이템", 28)):
            tk.Label(
                header, text=text_value, width=width, bg=COLORS["chip"],
                fg=COLORS["muted"], anchor="w", font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left")
        position_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4, "SUPPORT": 4}
        rows = sorted(
            participants,
            key=lambda row: position_order.get(str(row.get("teamPosition") or "").upper(), 9),
        )
        max_damage = max((int(row.get("totalDamageDealtToChampions") or 0) for row in rows), default=1)
        my_puuid = self._history_puuid()
        for participant in rows:
            is_me = participant.get("puuid") == my_puuid
            row_bg = "#19263b" if is_me else COLORS["surface"]
            row = tk.Frame(
                panel, bg=row_bg, padx=7, pady=6,
                highlightthickness=1 if is_me else 0,
                highlightbackground=COLORS["gold"],
            )
            row.pack(fill="x", pady=1)
            identity = tk.Frame(row, bg=row_bg, width=235)
            identity.pack(side="left", fill="y")
            identity.pack_propagate(False)
            champion_id = str(participant.get("championName") or "Unknown")
            icon = self.icon_cache.get(champion_id, 34)
            tk.Label(
                identity, image=icon or "", text="" if icon else "?", bg=COLORS["chip"],
                fg=COLORS["muted"], width=34 if not icon else 0,
            ).pack(side="left", padx=(0, 7))
            names = tk.Frame(identity, bg=row_bg)
            names.pack(side="left", fill="x")
            riot_name = str(participant.get("riotIdGameName") or participant.get("summonerName") or "Unknown")
            tag = str(participant.get("riotIdTagline") or participant.get("riotIdTagLine") or "")
            tk.Label(
                names, text=f"{riot_name}{'#' + tag if tag else ''}{' · 나' if is_me else ''}",
                bg=row_bg, fg=COLORS["gold"] if is_me else COLORS["text"],
                font=("Malgun Gothic", 8, "bold"),
            ).pack(anchor="w")
            tk.Label(
                names, text=f"{self.registry.ko_name(champion_id)} · Lv.{int(participant.get('champLevel') or 0)}",
                bg=row_bg, fg=COLORS["muted"], font=("Malgun Gothic", 7),
            ).pack(anchor="w")
            kills = int(participant.get("kills") or 0)
            deaths = int(participant.get("deaths") or 0)
            assists = int(participant.get("assists") or 0)
            kda = (kills + assists) / max(deaths, 1)
            tk.Label(
                row, text=f"{kills}/{deaths}/{assists}\n{kda:.1f}", width=12,
                bg=row_bg, fg=COLORS["gold"] if kda >= 4 else COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), justify="left", anchor="w",
            ).pack(side="left")
            cs = int(participant.get("totalMinionsKilled") or 0) + int(participant.get("neutralMinionsKilled") or 0)
            tk.Label(
                row, text=str(cs), width=8, bg=row_bg, fg=COLORS["text"],
                font=("Malgun Gothic", 8), anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=str(int(participant.get("visionScore") or 0)), width=8,
                bg=row_bg, fg=COLORS["blue"], font=("Malgun Gothic", 8), anchor="w",
            ).pack(side="left")
            damage = int(participant.get("totalDamageDealtToChampions") or 0)
            damage_frame = tk.Frame(row, bg=row_bg, width=150)
            damage_frame.pack(side="left", fill="y", padx=(0, 8))
            damage_frame.pack_propagate(False)
            tk.Label(
                damage_frame, text=f"{damage:,}", bg=row_bg, fg=COLORS["text"],
                font=("Malgun Gothic", 7, "bold"),
            ).pack(anchor="w")
            bar = tk.Frame(damage_frame, bg="#2b3342", height=4, width=130)
            bar.pack(anchor="w", pady=(2, 0))
            tk.Frame(bar, bg=accent).place(
                x=0, y=0, relwidth=damage / max(max_damage, 1), relheight=1
            )
            item_frame = tk.Frame(row, bg=row_bg)
            item_frame.pack(side="left", fill="x", expand=True)
            item_ids = [int(participant.get(f"item{index}") or 0) for index in range(7)]
            for item_id in (value for value in item_ids if value):
                item_label = tk.Label(
                    item_frame, text=str(item_id)[-2:], bg=COLORS["chip"],
                    fg=COLORS["muted"], width=3,
                    font=("Consolas", 6),
                )
                item_label.pack(side="left", padx=(0, 2))
                item_icon = self.item_icon_cache.get(
                    item_id, 24,
                    lambda label=item_label, value=item_id: self._apply_detail_item_icon(label, value),
                )
                if item_icon:
                    item_label.configure(image=item_icon, text="", width=0)
                tooltip = _HoverTooltip(
                    item_label,
                    lambda value=item_id: self.item_icon_cache.tooltip_text(value),
                )
                setattr(item_label, "_advisor_tooltip", tooltip)

    def _apply_detail_item_icon(self, label: tk.Label, item_id: int) -> None:
        try:
            if not label.winfo_exists():
                return
            image = self.item_icon_cache.get(item_id, 24)
            if image:
                label.configure(image=image, text="", width=0)
        except tk.TclError:
            return

    def _stat_block(self, parent: tk.Widget, title: str, color: str, body: str) -> None:
        frame = tk.Frame(parent, bg="#101827", padx=9, pady=7)
        frame.pack(fill="x", pady=(0, 6))
        tk.Label(frame, text=title, bg="#101827", fg=color,
                 font=("Malgun Gothic", 9, "bold")).pack(anchor="w")
        tk.Label(frame, text=body, bg="#101827", fg=COLORS["text"], justify="left",
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(2, 0))

    def _paragraph(self, parent: tk.Widget, title: str, body: str, color: str | None = None) -> None:
        tk.Label(parent, text=f"{title}  {body}", bg=COLORS["panel_2"],
                 fg=color or COLORS["text"], justify="left", anchor="w",
                 wraplength=390, font=("Malgun Gothic", 9)).pack(fill="x", pady=(3, 0))

    def _counter_for(self, champion_id: str) -> OpggCounter | None:
        if not self.opgg_snapshot:
            return None
        return next((item for item in self.opgg_snapshot.counters + self.opgg_snapshot.weak_picks
                     if item.champion_id == champion_id), None)

    def _select_enemy_support(self, champion_id: str) -> None:
        self._manual_enemy_support = champion_id
        self.draft.selected_enemy_support_id = champion_id
        self.draft.selected_enemy_support_name_ko = self.registry.ko_name(champion_id)
        self.draft.selected_enemy_support_source = "MANUAL_ENEMY_SUPPORT"
        self.draft.refresh_snapshot_id()
        cached = self.storage.load_opgg_snapshot(champion_id, self.draft.my_role)
        self.opgg_snapshot = cached
        self._render_selection()

    def _select_unknown_enemy_support(self) -> None:
        self._manual_enemy_support = MANUAL_UNKNOWN_SUPPORT
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        self.draft.refresh_snapshot_id()
        self.opgg_snapshot = self.storage.load_opgg_snapshot(None, self.draft.my_role)
        self._render_selection()

    def _auto_select_enemy_support(self, draft: DraftSnapshot) -> None:
        enemy_ids = {member.champion_id for member in draft.enemy_locked}
        if self._manual_enemy_support == MANUAL_UNKNOWN_SUPPORT:
            draft.selected_enemy_support_id = None
            draft.selected_enemy_support_name_ko = "모르겠음"
            draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
            return
        if self._manual_enemy_support in enemy_ids:
            draft.selected_enemy_support_id = self._manual_enemy_support
            draft.selected_enemy_support_name_ko = self.registry.ko_name(self._manual_enemy_support)
            draft.selected_enemy_support_source = "MANUAL_ENEMY_SUPPORT"
            return
        self._manual_enemy_support = None
        role_match = next(
            (member for member in draft.enemy_locked if member.role == draft.my_role), None
        )
        candidates = (
            [
                member for member in draft.enemy_locked
                if self.registry.support_score(member.champion_id)
            ]
            if draft.my_role == "SUPPORT" else []
        )
        chosen = role_match or (
            max(candidates, key=lambda m: self.registry.support_score(m.champion_id))
            if candidates else None
        )
        if chosen:
            draft.selected_enemy_support_id = chosen.champion_id
            draft.selected_enemy_support_name_ko = chosen.champion_name_ko
            draft.selected_enemy_support_source = "AUTO_ENEMY_SUPPORT"

    def _copy_prompt(self) -> None:
        prompt = build_prompt(self.draft, self.opgg_snapshot)
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.root.update_idletasks()
        self.exchange_status.configure(
            text=f"질문 복사 완료 · {self.draft.snapshot_id}", fg=COLORS["purple"]
        )

    def _show_prompt_preview(self) -> None:
        prompt = build_prompt(self.draft, self.opgg_snapshot)
        dialog = tk.Toplevel(self.root)
        dialog.title("ChatGPT에 보낼 질문 미리보기")
        dialog.configure(bg=COLORS["panel"])
        dialog.geometry("1000x720")
        dialog.transient(self.root)
        header = tk.Frame(dialog, bg=COLORS["panel"], padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(
            header, text="ChatGPT에 아래 내용을 그대로 붙여넣으세요",
            bg=COLORS["panel"], fg=COLORS["gold"], font=("Malgun Gothic", 13, "bold"),
        ).pack(side="left")
        actions = tk.Frame(header, bg=COLORS["panel"])
        actions.pack(side="right")
        self._button(actions, "질문 복사", self._copy_prompt, COLORS["purple"]).pack(side="left", padx=(0, 8))
        self._button(actions, "닫기", dialog.destroy, COLORS["muted"]).pack(side="left")
        text = tk.Text(
            dialog, bg="#09111f", fg=COLORS["text"], insertbackground=COLORS["text"],
            selectbackground="#29476f", relief="flat", bd=0, padx=14, pady=12,
            wrap="word", font=("Consolas", 9),
        )
        text.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        text.insert("1.0", prompt)
        text.configure(state="disabled")

    def _paste_clipboard_response(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("클립보드", "클립보드에 텍스트가 없습니다.", parent=self.root)
            return
        self.response_text.delete("1.0", "end")
        self.response_text.insert("1.0", text)
        self._apply_response()

    def _apply_response(self) -> None:
        text = self.response_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("답변 없음", "ChatGPT 답변을 붙여넣어 주세요.", parent=self.root)
            return
        try:
            recommendations = parse_response(text, self.draft, self.registry)
        except StaleResponseError as exc:
            self.exchange_status.configure(text=str(exc), fg=COLORS["orange"])
            messagebox.showwarning("오래된 추천", str(exc), parent=self.root)
            return
        except ResponseError as exc:
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            messagebox.showerror("답변 형식 오류", str(exc), parent=self.root)
            return
        self.recommendations = recommendations
        self.recommendation_snapshot_id = self.draft.snapshot_id
        self.exchange_status.configure(text="추천 3개 적용 완료", fg=COLORS["green"])
        self._render_selection()

    def _refresh_opgg(self) -> None:
        if self._opgg_refreshing:
            return
        remaining = self.storage.opgg_cooldown_remaining()
        if remaining.total_seconds() > 0:
            messagebox.showinfo("OP.GG 쿨타임", "성공한 갱신 후 1시간 동안 다시 갱신할 수 없습니다.")
            return
        self._opgg_refreshing = True
        self._render_header()
        enemy_support = self.draft.selected_enemy_support_id
        position = self.draft.my_role

        def work() -> OpggSnapshot:
            if enemy_support:
                return self.opgg_client.refresh_matchup(enemy_support, position)
            return self.opgg_client.refresh_overall(position)

        def success(snapshot: OpggSnapshot) -> None:
            self._opgg_refreshing = False
            self.storage.save_opgg_snapshot(snapshot)
            self.storage.mark_opgg_success()
            self.opgg_snapshot = snapshot
            self.exchange_status.configure(text="OP.GG 데이터 갱신 완료", fg=COLORS["blue"])
            self._render_selection()

        def error(exc: Exception) -> None:
            self._opgg_refreshing = False
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            self._render_header()
            messagebox.showerror("OP.GG 갱신 실패", str(exc), parent=self.root)

        self._background(work, success, error)

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Riot API 설정")
        dialog.configure(bg=COLORS["panel"])
        dialog.geometry("560x455")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = [
            ("Riot 게임 이름", "riot_game_name", False),
            ("태그", "riot_tag_line", False),
            ("Riot API 키", "riot_api_key", True),
        ]
        entries: dict[str, tk.Entry] = {}
        tk.Label(dialog, text="내 전적 동기화 설정", bg=COLORS["panel"], fg=COLORS["gold"],
                 font=("Malgun Gothic", 14, "bold")).pack(anchor="w", padx=20, pady=(18, 12))
        form = tk.Frame(dialog, bg=COLORS["panel"])
        form.pack(fill="x", padx=20)
        for row, (label, key, secret) in enumerate(fields):
            tk.Label(form, text=label, bg=COLORS["panel"], fg=COLORS["muted"], width=15,
                     anchor="w", font=("Malgun Gothic", 9)).grid(row=row, column=0, sticky="w", pady=6)
            entry = tk.Entry(form, bg="#0b1220", fg=COLORS["text"], insertbackground=COLORS["text"],
                             relief="flat", show="*" if secret else "", font=("Malgun Gothic", 10))
            if not secret:
                entry.insert(0, self.storage.get_setting(key))
            entry.grid(row=row, column=1, sticky="ew", pady=6, ipady=7)
            entries[key] = entry
        form.grid_columnconfigure(1, weight=1)
        tk.Label(
            dialog,
            text=("게임 이름과 태그는 롤 클라이언트에서 자동 감지됩니다.\n"
                  "API 키가 이미 저장되어 있으면 입력란은 비워 두세요. 새 키만 입력하면 교체됩니다.\n"
                  "개발 키는 약 24시간마다 만료되며 이 PC의 data/advisor.db에만 저장됩니다."),
            bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 8),
        ).pack(anchor="w", padx=20, pady=(8, 12))

        self._button(
            dialog, "Riot Developer Portal 열기", self._open_developer_portal, COLORS["orange"]
        ).pack(anchor="w", padx=20, pady=(0, 10))

        validation_status = tk.Label(
            dialog, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9, "bold"),
        )
        validation_status.pack(anchor="w", padx=20, pady=(0, 8))

        def finish_save(game_name: str, tag_line: str, new_api_key: str, puuid: str = "") -> None:
            self.storage.set_setting("riot_game_name", game_name)
            self.storage.set_setting("riot_tag_line", tag_line)
            if new_api_key:
                self.storage.set_riot_api_key(new_api_key)
            if puuid:
                self.storage.set_setting("riot_puuid", puuid)
            dialog.destroy()
            status_text = (
                "Riot API 키 검증 및 저장 완료" if new_api_key else "Riot 설정 저장 완료"
            )
            self.exchange_status.configure(text=status_text, fg=COLORS["green"])
            self._render_header()
            if self.live_game.players and not self._profiles_loading:
                self.player_profiles = {
                    player.riot_id: PlayerProfileStat(status="LOADING")
                    for player in self.live_game.players
                }
                self._load_live_profiles()
            self.root.after(150, lambda: self._sync_riot(automatic=True))

        def save() -> None:
            game_name = entries["riot_game_name"].get().strip()
            tag_line = entries["riot_tag_line"].get().strip()
            new_api_key = entries["riot_api_key"].get().strip()
            if not new_api_key:
                finish_save(game_name, tag_line, "")
                return
            if not game_name or not tag_line:
                validation_status.configure(
                    text="검증 불가 · 롤 클라이언트 연결 후 Riot ID와 태그를 확인하세요.",
                    fg=COLORS["red"],
                )
                messagebox.showwarning(
                    "API 키 검증 불가",
                    "API 키를 확인하려면 Riot 게임 이름과 태그가 필요합니다.",
                    parent=dialog,
                )
                return
            save_button.configure(state="disabled", text="API 키 확인 중...")
            validation_status.configure(
                text="Riot 서버에서 API 키를 확인하는 중입니다...", fg=COLORS["blue"]
            )

            def success(puuid: str) -> None:
                if not dialog.winfo_exists():
                    return
                validation_status.configure(
                    text="API 키 확인 성공 · 저장합니다.", fg=COLORS["green"]
                )
                finish_save(game_name, tag_line, new_api_key, puuid)

            def error(exc: Exception) -> None:
                if not dialog.winfo_exists():
                    return
                save_button.configure(state="normal", text="검증 후 저장")
                validation_status.configure(
                    text=f"API 키 검증 실패 · {exc}", fg=COLORS["red"]
                )
                messagebox.showerror(
                    "API 키 검증 실패",
                    f"새 API 키를 저장하지 않았습니다.\n\n{exc}",
                    parent=dialog,
                )

            self._background(
                lambda: RiotApiClient(new_api_key).validate_key_for_account(
                    game_name, tag_line
                ),
                success,
                error,
            )

        save_button = self._button(
            dialog, "검증 후 저장", save, COLORS["green"], width=14
        )
        save_button.pack(anchor="e", padx=20)

    @staticmethod
    def _open_developer_portal() -> None:
        webbrowser.open("https://developer.riotgames.com/")

    def _sync_riot(self, automatic: bool = False) -> None:
        if self._riot_syncing:
            return
        if self.game_phase == "InProgress":
            if not automatic:
                messagebox.showinfo(
                    "게임 중 로컬 조회",
                    "게임 중에는 외부 전적 요청을 하지 않습니다. 게임 종료 후 자동 갱신됩니다.",
                    parent=self.root,
                )
            return
        if automatic and self.storage.riot_sync_cooldown_remaining().total_seconds() > 0:
            return
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        api_key = self.storage.get_setting("riot_api_key")
        if not api_key:
            if automatic:
                return
            self._open_settings()
            return
        if self.storage.riot_api_key_needs_refresh():
            if not automatic:
                messagebox.showwarning(
                    "개발용 API 키 갱신 필요",
                    "개발용 키는 약 24시간마다 만료됩니다. 상단의 Riot 키 발급/갱신 버튼에서 "
                    "새 키를 받은 뒤 Riot 설정에 입력하세요.",
                    parent=self.root,
                )
            return
        if not game_name or not tag_line:
            if not automatic:
                messagebox.showinfo(
                    "Riot ID 자동 감지",
                    "롤 클라이언트 연결 후 게임 이름과 태그를 자동으로 감지합니다.",
                    parent=self.root,
                )
            return
        self._riot_syncing = True
        self.riot_button.configure(state="disabled", text="내 전적 저장 중...")

        def progress(done: int, total: int) -> None:
            self._post_ui(lambda: self.riot_button.configure(text=f"전적 {done}/{total}"))

        def work() -> tuple[str, int, int]:
            return RiotApiClient(api_key).sync(
                self.storage, game_name, tag_line, count=1000, progress=progress
            )

        def success(result: tuple[str, int, int]) -> None:
            _puuid, saved, total = result
            self.storage.mark_riot_sync()
            self._riot_syncing = False
            self.riot_button.configure(text="전적 데이터 미리 갱신", state="normal")
            self.exchange_status.configure(
                text=f"내 전적 동기화 완료 · 신규 {saved} / 최근 {total}경기", fg=COLORS["green"]
            )
            self._history_revision = None
            self._ensure_history_loaded(force=True)
            self._render_selection()

        def error(exc: Exception) -> None:
            self._riot_syncing = False
            if isinstance(exc, RiotApiError) and "만료" in str(exc):
                self.storage.mark_riot_api_key_invalid()
            self.riot_button.configure(text="전적 데이터 미리 갱신", state="normal")
            self._render_header()
            messagebox.showerror("내 전적 갱신 실패", str(exc), parent=self.root)

        self._background(work, success, error)

    def _background(
        self, work: Callable[[], T], success: Callable[[T], None], error: Callable[[Exception], None]
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # All worker failures must return to the GUI thread.
                self._post_ui(lambda captured=exc: error(captured))
            else:
                self._post_ui(lambda captured=result: success(captured))
        threading.Thread(target=runner, daemon=True).start()

    def _post_ui(self, callback: Callable[[], None]) -> None:
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._drain_ui_queue)
        except tk.TclError:
            return

    def _refresh_registry_background(self) -> None:
        if self.registry.loaded_from_ddragon:
            return

        def success(_count: int) -> None:
            self.icon_cache.prefetch_all(self._schedule_selection_render)
            self._render_all()

        self._background(lambda: self.registry.refresh(), success, lambda _exc: None)

    def _poll_lcu(self) -> None:
        if self.demo or self._lcu_polling:
            return
        self._lcu_polling = True

        need_identity = not self._identity_checked

        def work() -> tuple[str, DraftSnapshot | None, dict]:
            phase = str(self.lcu.get("/lol-gameflow/v1/gameflow-phase"))
            draft = None
            if phase == "ChampSelect":
                draft = parse_lcu_session(self.lcu.champ_select_session(), self.registry)
            identity: dict = {}
            if need_identity:
                try:
                    identity = dict(self.lcu.get("/lol-summoner/v1/current-summoner"))
                except LcuUnavailable:
                    identity = {}
            return phase, draft, identity

        def success(result: tuple[str, DraftSnapshot | None, dict]) -> None:
            self._lcu_polling = False
            phase, draft, identity = result
            previous_phase = self.game_phase
            phase_changed = previous_phase != phase
            draft_changed = False
            self.game_phase = phase
            if identity:
                game_name = str(identity.get("gameName") or "").strip()
                tag_line = str(identity.get("tagLine") or "").strip()
                puuid = str(identity.get("puuid") or "").strip()
                if game_name:
                    self.storage.set_setting("riot_game_name", game_name)
                if tag_line:
                    self.storage.set_setting("riot_tag_line", tag_line)
                if puuid:
                    self.storage.set_setting("riot_puuid", puuid)
                self._identity_checked = bool(game_name and tag_line and puuid)
            if phase == "ChampSelect" and draft:
                role_changed = draft.my_role != self.draft.my_role
                if role_changed:
                    self._manual_enemy_support = None
                    self._support_filter = "ALL"
                self._auto_select_enemy_support(draft)
                draft.refresh_snapshot_id()
                old_support = self.draft.selected_enemy_support_id
                draft_changed = draft.snapshot_id != self.draft.snapshot_id
                if draft_changed:
                    self.draft = draft
                if role_changed or draft.selected_enemy_support_id != old_support:
                    self.opgg_snapshot = self.storage.load_opgg_snapshot(
                        draft.selected_enemy_support_id, draft.my_role
                    )
                if previous_phase != "ChampSelect":
                    self.notebook.select(self.selection_tab)
            elif phase == "InProgress":
                self.draft.connection_state = "IN_GAME"
                if previous_phase != "InProgress":
                    self.notebook.select(self.play_tab)
                    self.root.after(250, self._poll_live)
            else:
                if previous_phase == "InProgress":
                    self.live_game = LiveGameSnapshot()
                    self.player_profiles = {}
                    self.duo_pairs = {}
                    self._duo_checked_signature = ""
                    self._live_signature = ""
                if phase not in {"GameStart", "Reconnect"}:
                    self.draft.connection_state = "LOBBY"
            if phase_changed:
                self._render_all()
            elif draft_changed:
                self._render_selection()
            else:
                self._render_header()
            if identity and self.storage.get_setting("riot_api_key"):
                self.root.after(300, lambda: self._sync_riot(automatic=True))
            if previous_phase == "InProgress" and phase != "InProgress":
                self.root.after(20000, lambda: self._sync_riot(automatic=True))
            self.root.after(1400, self._poll_lcu)

        def error(_exc: Exception) -> None:
            self._lcu_polling = False
            self.game_phase = "None"
            if self.draft.connection_state not in {"DISCONNECTED", "IN_GAME"}:
                self.draft = DraftSnapshot()
                self._manual_enemy_support = None
                self._support_filter = "ALL"
                self.opgg_snapshot = self.storage.load_opgg_snapshot(
                    None, self.draft.my_role
                )
                self._render_all()
            self.root.after(2400, self._poll_lcu)

        self._background(work, success, error)

    def _poll_live(self) -> None:
        if self.demo or self.game_phase != "InProgress" or self._live_polling:
            return
        self._live_polling = True

        def success(snapshot: LiveGameSnapshot) -> None:
            self._live_polling = False
            signature = "|".join(
                sorted(f"{player.riot_id}:{player.champion_id}:{player.team}" for player in snapshot.players)
            )
            changed = signature != self._live_signature
            self.live_game = snapshot
            if changed:
                self._live_signature = signature
                self.duo_pairs = {}
                self._duo_checked_signature = ""
                self.player_profiles = {
                    player.riot_id: PlayerProfileStat(status="LOADING") for player in snapshot.players
                }
                self._load_live_profiles()
                self._check_live_duos()
            self._render_play()
            self.root.after(3000, self._poll_live)

        def error(_exc: Exception) -> None:
            self._live_polling = False
            self.root.after(1800, self._poll_live)

        self._background(self.live_client.snapshot, success, error)

    def _load_live_profiles(self) -> None:
        if self._profiles_loading or not self.live_game.players:
            return
        self._profiles_loading = True
        signature = self._live_signature
        players = list(self.live_game.players)
        active_team = self.live_game.active_team
        team_groups = (
            [player for player in players if player.team == active_team],
            [player for player in players if player.team != active_team],
        )

        def work() -> dict[str, PlayerProfileStat]:
            results: dict[str, PlayerProfileStat] = {}
            my_puuid = self.storage.get_setting("riot_puuid")
            for player in players:
                cached = self.storage.load_live_profile_any_age(player.riot_id)
                if cached:
                    puuid, payload, updated_at = cached
                else:
                    puuid = self.storage.find_puuid_by_riot_id(player.riot_id)
                    payload = {}
                    updated_at = ""
                if player.is_active_player and puuid:
                    my_puuid = puuid
                if not puuid:
                    profile = PlayerProfileStat(status="NO_LOCAL_DATA")
                else:
                    profile = self._make_player_profile(
                        player, puuid, payload, my_puuid, updated_at
                    )
                results[player.riot_id] = profile
            return results

        def success(results: dict[str, PlayerProfileStat]) -> None:
            self._profiles_loading = False
            if signature == self._live_signature:
                self.player_profiles.update(results)
                self._render_play()

        def error(exc: Exception) -> None:
            self._profiles_loading = False
            self.live_profile_status.configure(text=str(exc), fg=COLORS["red"])

        self._background(work, success, error)

    def _make_player_profile(
        self,
        player: LivePlayer,
        puuid: str,
        payload: dict,
        my_puuid: str,
        updated_at: str,
    ) -> PlayerProfileStat:
        entry = payload.get("solo_entry") or {}
        champion_games, champion_wins = self.storage.player_champion_record(
            puuid, player.champion_id, limit=20
        )
        sample_games = self.storage.count_player_matches(puuid, limit=20)
        last_game = self.storage.latest_player_match(puuid) or {}
        relationship: dict = {}
        if my_puuid and my_puuid != puuid:
            relationship = self.storage.relationship_summary(my_puuid, puuid, limit=1000)
        return PlayerProfileStat(
            puuid=puuid,
            tier=str(entry.get("tier") or "UNRANKED"),
            rank=str(entry.get("rank") or ""),
            league_points=int(entry.get("leaguePoints") or 0),
            season_wins=int(entry.get("wins") or 0),
            season_losses=int(entry.get("losses") or 0),
            champion_games=champion_games,
            champion_wins=champion_wins,
            local_sample_games=sample_games,
            together_games=int(relationship.get("together_games", 0)),
            together_wins=int(relationship.get("together_wins", 0)),
            against_games=int(relationship.get("against_games", 0)),
            against_my_wins=int(relationship.get("against_my_wins", 0)),
            recent_10_together_games=int(relationship.get("recent_10_together_games", 0)),
            recent_10_against_games=int(relationship.get("recent_10_against_games", 0)),
            last_met_game_number=int(relationship.get("last_met_game_number", 0)),
            last_met_same_team=relationship.get("last_met_same_team"),
            last_met_my_win=relationship.get("last_met_my_win"),
            last_met_my_champion_id=str(relationship.get("last_met_my_champion_id", "")),
            last_met_other_champion_id=str(relationship.get("last_met_other_champion_id", "")),
            last_game_champion_id=str(last_game.get("champion_id", "")),
            last_game_position=str(last_game.get("position", "UNKNOWN")),
            last_game_kills=int(last_game.get("kills", 0)),
            last_game_deaths=int(last_game.get("deaths", 0)),
            last_game_assists=int(last_game.get("assists", 0)),
            last_game_won=last_game.get("won"),
            sample_scope=f"저장 {sample_games}경기",
            updated_at=updated_at,
            status="OK" if "solo_entry" in payload else "LOCAL_ONLY",
        )

    def _apply_live_profile(
        self, riot_id: str, profile: PlayerProfileStat, signature: str
    ) -> None:
        if signature != self._live_signature:
            return
        self.player_profiles[riot_id] = profile
        self._render_play()

    def _check_live_duos(self) -> None:
        """Check only current same-team pairs, with a strict request budget.

        Fetching one Match-v5 ID page returns 100 IDs. Match detail calls are
        restricted to five common matches per pair and forty for the whole game.
        """
        if (
            self.demo
            or self._duo_checking
            or not self.live_game.players
            or self._duo_checked_signature == self._live_signature
        ):
            return
        api_key = self.storage.get_setting("riot_api_key")
        if not api_key or self.storage.riot_api_key_needs_refresh():
            self.live_duo_status.configure(
                text="DUO 추정 확인 불가 · Riot 개발용 API 키를 갱신하세요.",
                fg=COLORS["red"],
            )
            return
        signature = self._live_signature
        players = list(self.live_game.players)
        self._duo_checking = True
        self.live_duo_status.configure(text="DUO 추정 확인 중 0/10명", fg=COLORS["blue"])

        def work() -> tuple[dict[str, list[tuple[str, str, str]]], int, int, int]:
            client = RiotApiClient(api_key)
            puuids: dict[str, str] = {}
            histories: dict[str, list[str]] = {}
            rank_updates = 0
            fetched_details = 0
            for index, player in enumerate(players, start=1):
                if not player.riot_game_name or not player.riot_tag_line:
                    continue
                puuid = self.storage.find_puuid_by_riot_id(player.riot_id)
                if not puuid:
                    account = client.resolve_account(player.riot_game_name, player.riot_tag_line)
                    puuid = str(account.get("puuid") or "")
                    if puuid:
                        self.storage.save_player_identity(player.riot_id, puuid)
                if puuid:
                    puuids[player.riot_id] = puuid
                    if not self.storage.load_live_profile(
                        player.riot_id, max_age=timedelta(hours=24)
                    ):
                        entries = client.league_entries_by_puuid(puuid, platform="kr")
                        solo_entry = next(
                            (
                                entry for entry in entries
                                if entry.get("queueType") == "RANKED_SOLO_5x5"
                            ),
                            {},
                        )
                        existing = self.storage.load_live_profile_any_age(player.riot_id)
                        payload = dict(existing[1]) if existing else {}
                        payload["solo_entry"] = solo_entry
                        payload["rank_checked"] = True
                        self.storage.save_live_profile(player.riot_id, puuid, payload)
                        rank_updates += 1
                    history = client.match_ids(puuid, count=100)
                    histories[player.riot_id] = history
                    if history and self.storage.load_match(history[0]) is None:
                        if fetched_details < 40:
                            latest_match = client.match(history[0])
                            self.storage.save_matches([latest_match])
                            fetched_details += 1
                self._post_ui(
                    lambda done=index, total=len(players): self.live_duo_status.configure(
                        text=f"DUO 추정 확인 중 {done}/{total}명", fg=COLORS["blue"]
                    )
                )

            pairs: dict[str, list[tuple[str, str, str]]] = {}
            for team_players in team_groups:
                for pair_index, first in enumerate(team_players):
                    first_puuid = puuids.get(first.riot_id, "")
                    first_history = histories.get(first.riot_id, [])
                    if not first_puuid or not first_history:
                        continue
                    for second in team_players[pair_index + 1:]:
                        second_puuid = puuids.get(second.riot_id, "")
                        second_history = histories.get(second.riot_id, [])
                        if not second_puuid or not second_history:
                            continue
                        second_ids = set(second_history)
                        common_ids = [
                            match_id for match_id in first_history if match_id in second_ids
                        ][:5]
                        first_positions = {
                            match_id: position for position, match_id in enumerate(first_history)
                        }
                        second_positions = {
                            match_id: position for position, match_id in enumerate(second_history)
                        }
                        same_team_positions: list[tuple[int, int]] = []
                        for match_id in common_ids:
                            match = self.storage.load_match(match_id)
                            if match is None:
                                if fetched_details >= 40:
                                    break
                                match = client.match(match_id)
                                self.storage.save_matches([match])
                                fetched_details += 1
                            participants = match.get("info", {}).get("participants", [])
                            first_row = next(
                                (row for row in participants if row.get("puuid") == first_puuid), None
                            )
                            second_row = next(
                                (row for row in participants if row.get("puuid") == second_puuid), None
                            )
                            if (
                                first_row
                                and second_row
                                and first_row.get("teamId") == second_row.get("teamId")
                            ):
                                same_team_positions.append(
                                    (first_positions[match_id], second_positions[match_id])
                                )
                                if {(0, 0), (1, 1)}.issubset(set(same_team_positions)):
                                    break
                        classification = self._classify_duo_evidence(same_team_positions)
                        if classification:
                            level, evidence = classification
                            pairs.setdefault(first.riot_id, []).append(
                                (second.riot_id, level, evidence)
                            )
                            pairs.setdefault(second.riot_id, []).append(
                                (first.riot_id, level, evidence)
                            )
            return pairs, len(puuids), fetched_details, rank_updates

        def success(
            result: tuple[dict[str, list[tuple[str, str, str]]], int, int, int]
        ) -> None:
            self._duo_checking = False
            pairs, resolved, details, rank_updates = result
            if signature != self._live_signature:
                self.root.after(100, self._check_live_duos)
                return
            self._duo_checked_signature = signature
            self.duo_pairs = pairs
            if pairs:
                duo_count = sum(len(values) for values in pairs.values()) // 2
                text = (
                    f"DUO 추정 완료 · {duo_count}쌍 · 현재 {resolved}명 확인 · "
                    f"시즌 {rank_updates}명 갱신 · 상세 {details}건 요청"
                )
            else:
                text = (
                    f"DUO 추정 없음 · 현재 {resolved}명 확인 · "
                    f"시즌 {rank_updates}명 갱신 · 상세 {details}건 요청"
                )
            self.live_duo_status.configure(text=text, fg=COLORS["orange"])
            self._load_live_profiles()
            self._render_play()

        def error(exc: Exception) -> None:
            self._duo_checking = False
            if isinstance(exc, RiotApiError) and "만료" in str(exc):
                self.storage.mark_riot_api_key_invalid()
                self._render_header()
            self.live_duo_status.configure(text=f"DUO 추정 중단 · {exc}", fg=COLORS["red"])

        self._background(work, success, error)

    @staticmethod
    def _classify_duo_evidence(
        same_team_positions: list[tuple[int, int]],
    ) -> tuple[str, str] | None:
        positions = set(same_team_positions)
        if {(0, 0), (1, 1)}.issubset(positions):
            return "매우 유력", "서로의 직전 2경기가 모두 동팀"
        if (0, 0) in positions:
            return "유력", "직전판도 동팀 · 현재 포함 2연속"
        ordered = sorted(positions)
        if any(
            abs(first[0] - second[0]) == 1 and abs(first[1] - second[1]) == 1
            for index, first in enumerate(ordered)
            for second in ordered[index + 1:]
        ):
            return "유력", "최근 기록에서 2경기 연속 동팀"
        if len(positions) >= 2:
            return "가능", f"최근 100경기 중 동팀 {len(positions)}회 이상 확인"
        return None

    def _tick(self) -> None:
        self._render_header()
        self.root.after(1000, self._tick)

    def _demo_draft(self) -> DraftSnapshot:
        draft = DraftSnapshot(
            my_pick_order=2,
            my_status="WAITING",
            ally_locked=[DraftMember("LeeSin", "리 신", "JUNGLE", "LOCKED", 1)],
            ally_hover=[
                DraftMember("Ornn", "오른", "TOP", "HOVER", 0),
                DraftMember("Jinx", "징크스", "BOTTOM", "HOVER", 3),
            ],
            enemy_locked=[
                DraftMember("Darius", "다리우스", "TOP", "LOCKED", 5),
                DraftMember("Viego", "비에고", "JUNGLE", "LOCKED", 6),
                DraftMember("Katarina", "카타리나", "MIDDLE", "LOCKED", 7),
                DraftMember("Samira", "사미라", "BOTTOM", "LOCKED", 8),
                DraftMember("Leona", "레오나", "SUPPORT", "LOCKED", 9),
            ],
            ally_bans=["Zed", "Yuumi", "Aatrox", "Syndra", "Nidalee"],
            enemy_bans=["Thresh", "Lulu", "Blitzcrank", "Nami", "Pyke"],
            selected_enemy_support_id="Leona",
            selected_enemy_support_name_ko="레오나",
            selected_enemy_support_source="AUTO_ENEMY_SUPPORT",
            connection_state="CHAMP_SELECT",
        )
        draft.refresh_snapshot_id()
        return draft

    def _demo_live_game(self) -> LiveGameSnapshot:
        data = [
            ("Player", "KR1", "Janna", "잔나", "ORDER", "UTILITY", True),
            ("TopPlayer", "KR2", "Ornn", "오른", "ORDER", "TOP", False),
            ("JunglePlayer", "KR3", "LeeSin", "리 신", "ORDER", "JUNGLE", False),
            ("MidPlayer", "KR4", "Syndra", "신드라", "ORDER", "MIDDLE", False),
            ("AdcPlayer", "KR5", "Jinx", "징크스", "ORDER", "BOTTOM", False),
            ("EnemyTop", "KR6", "Darius", "다리우스", "CHAOS", "TOP", False),
            ("EnemyJungle", "KR7", "Viego", "비에고", "CHAOS", "JUNGLE", False),
            ("EnemyMid", "KR8", "Katarina", "카타리나", "CHAOS", "MIDDLE", False),
            ("EnemyAdc", "KR9", "Samira", "사미라", "CHAOS", "BOTTOM", False),
            ("EnemySupport", "KR10", "Leona", "레오나", "CHAOS", "UTILITY", False),
        ]
        return LiveGameSnapshot(
            players=[
                LivePlayer(
                    champion_id=champion_id,
                    champion_name_ko=name_ko,
                    riot_game_name=name,
                    riot_tag_line=tag,
                    team=team,
                    position=position,
                    is_active_player=active,
                )
                for name, tag, champion_id, name_ko, team, position, active in data
            ],
            active_riot_id="Player#KR1",
            active_team="ORDER",
            game_time=614,
            game_mode="CLASSIC",
        )

    def _demo_player_profiles(self) -> dict[str, PlayerProfileStat]:
        result: dict[str, PlayerProfileStat] = {}
        for index, player in enumerate(self._demo_live_game().players):
            result[player.riot_id] = PlayerProfileStat(
                puuid=f"demo-{index}",
                tier="PLATINUM" if index < 5 else "EMERALD",
                rank="II" if index % 2 == 0 else "III",
                league_points=34 + index,
                season_wins=28 + index,
                season_losses=24 + (index % 4),
                champion_games=3 + (index % 8),
                champion_wins=min(2 + (index % 5), 3 + (index % 8)),
                local_sample_games=20,
                together_games=0 if index == 0 else index % 4,
                together_wins=(
                    0 if index == 0 else min(index % 3, index % 4)
                ),
                against_games=0 if index < 5 else (index - 4),
                against_my_wins=0 if index < 5 else (index - 5) // 2,
                recent_10_together_games=0 if index == 0 else index % 2,
                recent_10_against_games=0 if index < 5 else 1,
                last_met_game_number=0 if index == 0 else (1 if index in {2, 8} else index + 2),
                last_met_same_team=index < 5,
                last_met_my_win=index % 2 == 0,
                last_met_my_champion_id="Janna",
                last_met_other_champion_id=player.champion_id,
                last_game_champion_id=("Nami" if index % 2 == 0 else "Thresh"),
                last_game_position=player.position,
                last_game_kills=1 + index % 4,
                last_game_deaths=index % 5,
                last_game_assists=8 + index,
                last_game_won=index % 2 == 0,
                sample_scope="저장 20경기",
                updated_at=datetime.now().isoformat(timespec="seconds"),
                status="OK",
            )
        return result

    def _demo_duo_pairs(self) -> dict[str, list[tuple[str, str, str]]]:
        return {
            "TopPlayer#KR2": [("JunglePlayer#KR3", "가능", "최근 100경기 중 동팀 3회")],
            "JunglePlayer#KR3": [("TopPlayer#KR2", "가능", "최근 100경기 중 동팀 3회")],
            "EnemyAdc#KR9": [("EnemySupport#KR10", "매우 유력", "직전 2경기가 모두 동팀")],
            "EnemySupport#KR10": [("EnemyAdc#KR9", "매우 유력", "직전 2경기가 모두 동팀")],
        }

    def _demo_opgg(self) -> OpggSnapshot:
        return OpggSnapshot(
            enemy_support_id="Leona", enemy_support_name_ko="레오나", region="GLOBAL",
            tier="EMERALD_PLUS", patch="DEMO", updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url="https://op.gg/lol/champions/leona/counters/support",
            counters=[
                OpggCounter("Taric", "타릭", 55.1, 3840),
                OpggCounter("Janna", "잔나", 53.6, 8420),
                OpggCounter("Braum", "브라움", 52.4, 4180),
                OpggCounter("Thresh", "쓰레쉬", 52.0, 9210),
                OpggCounter("Morgana", "모르가나", 51.7, 6150),
            ],
            weak_picks=[
                OpggCounter("Nautilus", "노틸러스", 46.8, 3512),
                OpggCounter("Senna", "세나", 47.3, 3199),
                OpggCounter("Sona", "소나", 47.9, 2870),
            ],
            target_overall_win_rate=50.7, target_pick_rate=8.4, target_ban_rate=6.1,
            raw_status="DEMO",
        )
