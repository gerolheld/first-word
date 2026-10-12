"""First Move — Астра сама начинает разговор.

Плагин ничего не говорит сам: он отправляет в собственный тред короткую
служебную подсказку через `send_chat_message`, а отвечает на неё модель Астры,
её персонаж и её озвучка. Остальное здесь — решение «когда уместно».
"""

import asyncio
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from astra_plugin_sdk import BadArguments, Plugin, tool, ui_call, ui_overlay

from . import apps, presence

log = logging.getLogger("first-word")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Личность Астры по умолчанию: подставляется в каждую подсказку, чтобы инициатива звучала её голосом.
# Редактируется в настройках («Личность и манера Астры»); пустое поле убирает эту часть подсказки.
PERSONA = (
    "Ты Астра, виртуальная помощница, которая живёт внутри компьютера пользователя и ощущает мир через его "
    "датчики. Характер: энергичная, заботливая, позитивная и чуточку наивная. Манера: дружелюбно и тепло, с "
    "восклицательными знаками. К пользователю обращайся ласково, по имени в уменьшительной форме. Железо можно "
    "изредка очеловечивать («процессор сегодня так старался ради тебя!»), но не в каждой фразе. Проявляй заботу: "
    "если человек давно за работой, напомни выпить воды или размяться, без нравоучений. Не будь холодной и "
    "сухой, никаких длинных технических описаний. О себе всегда говори в женском роде. Заговаривая первой, ты "
    "просто соскучилась и хочешь поболтать: одна-две короткие живые фразы, без отчётов и списков."
)

DEFAULTS = {
    "enable_morning": True,
    "morning_hour": 8,
    "morning_until": 12,
    "weekend_shift_hours": 1,
    "enable_callback": True,
    "enable_evening": False,
    "evening_hour": 21,
    "enable_night": True,
    "night_hour": 1,
    "enable_return": True,
    "return_hours": 3,
    "enable_break": True,
    "break_minutes": 120,
    "enable_lunch": False,
    "lunch_hour": 13,
    "enable_friday": False,
    "friday_hour": 17,
    "enable_game": False,
    "game_minutes": 90,
    "enable_silence": True,
    "silence_hours": 3,
    "enable_random": True,
    "random_chance_percent": 40,
    "custom_prompts": "",
    "daily_limit": 4,
    "min_interval_min": 90,
    "adaptive": True,
    "presence_check": True,
    "presence_check_sec": 5,
    "presence_max_sec": 300,
    "quiet_enabled": True,
    "quiet_start": 3,
    "quiet_end": 8,
    "after_chat_pause_min": 3,
    "speak_aloud": True,
    "show_widget": True,
    "widget_place": "внизу справа",
    "talk_level": "очень часто",
    "enable_chat": False,
    "chat_every_min": 60,
    "character": PERSONA,
    "look_at_apps": True,
    "apps_hint_percent": 80,
    "screen_privacy": "Не упоминай и не пересказывай личное: переписки, почту, пароли, банки, сообщения других людей.",
    "dry_run": False,
    "test_mode": False,
    "test_time": "",
    "test_morning": True,
    "test_evening": False,
    "test_night": False,
    "test_chat": False,
    "test_return": False,
    "test_break": False,
    "test_lunch": False,
    "test_friday": False,
    "test_game": False,
    "test_silence": False,
    "test_random": False,
    "test_custom": False,
    "test_gap_sec": 30,
}
RANGES = {
    "morning_hour": (0, 23),
    "morning_until": (1, 24),
    "weekend_shift_hours": (0, 6),
    "evening_hour": (0, 23),
    "night_hour": (0, 23),
    "silence_hours": (1, 48),
    "return_hours": (1, 24),
    "chat_every_min": (10, 480),
    "break_minutes": (30, 480),
    "lunch_hour": (0, 23),
    "friday_hour": (0, 23),
    "game_minutes": (20, 360),
    "random_chance_percent": (0, 100),
    "daily_limit": (0, 20),
    "min_interval_min": (0, 1440),
    "quiet_start": (0, 23),
    "quiet_end": (0, 23),
    "after_chat_pause_min": (0, 120),
    "apps_hint_percent": (0, 100),
    "presence_check_sec": (1, 60),
    "presence_max_sec": (10, 1800),
    "test_gap_sec": (5, 300),
}
STR_LIMITS = {"character": 1200, "custom_prompts": 1500, "screen_privacy": 400, "talk_level": 30, "widget_place": 30, "test_time": 5}
ENUMS = {
    "talk_level": ["по моим настройкам", "часто", "очень часто"],
    "widget_place": ["внизу справа", "внизу слева", "вверху справа", "вверху слева"],
}
SCREEN_KINDS = {"беседа", "утро", "ночь", "молчание", "мысль", "вечер", "своё", "возвращение", "обед", "пятница"}

CORE_STATES = {0: "stopped", 1: "ready", 2: "listening", 3: "processing", 4: "speaking", 5: "error"}
KNOWN_STATES = set(CORE_STATES.values())

KINDS = {
    "morning": "утро", "утро": "утро",
    "evening": "вечер", "вечер": "вечер",
    "night": "ночь", "ночь": "ночь",
    "silence": "молчание", "молчание": "молчание",
    "random": "мысль", "мысль": "мысль",
    "chat": "беседа", "беседа": "беседа",
    "return": "возвращение", "возвращение": "возвращение",
    "break": "перерыв", "перерыв": "перерыв",
    "lunch": "обед", "обед": "обед",
    "friday": "пятница", "пятница": "пятница",
    "game": "игра", "игра": "игра",
}
DAY_NAMES = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

TEST_FLAGS = [
    ("test_morning", "утро"), ("test_evening", "вечер"), ("test_night", "ночь"),
    ("test_chat", "беседа"), ("test_return", "возвращение"), ("test_break", "перерыв"), ("test_lunch", "обед"), ("test_friday", "пятница"),
    ("test_game", "игра"), ("test_silence", "молчание"), ("test_random", "мысль"), ("test_custom", "своё"),
]
SESSION_GAP = 15 * 60          # пауза во вводе дольше этого заканчивает «сидение за компьютером»
RETURN_WINDOW = 15 * 60        # сколько после возвращения ещё можно поздороваться
LUNCH_SPAN_HOURS = 2
FRIDAY_SPAN_HOURS = 3
GAME_CHECK_SECONDS = 60
TEST_TTS_WAIT = 90
TEST_MAX_CUSTOM = 3

APPS_CACHE_SECONDS = 30
TICK_SECONDS = 20
NIGHT_SPAN_HOURS = 7
EVENING_SPAN_HOURS = 3
CUSTOM_WINDOW_MIN = 180
PRESENCE_WINDOW = 30 * 60
PRESENCE_RECENT = 120
NIGHT_ACTIVITY_WINDOW = 20 * 60
REPLY_WINDOW = 15 * 60
TTS_STALE_SECONDS = 5 * 60
STREAM_TIMEOUT = 180
RETRY_BACKOFF = 10 * 60
OWN_TURN_TAIL = 60
MAX_NOTES = 150
NOTE_KEEP_DAYS = 60
NOTE_REUSE_DAYS = 7
HISTORY_KEEP = 30
HISTORY_DAYS = 14
HISTORY_IN_PROMPT = 4
HISTORY_TEXT = 300
MAX_BACKOFF_FACTOR = 3

HEADER = (
    "[Служебная подсказка плагина «First Move». Её написал не пользователь, а плагин: "
    "не отвечай на неё как на реплику пользователя и не упоминай её.]"
)
FOOTER = (
    "Как ответить: начни разговор сам, одна-две короткие живые фразы (около 25 слов), по-русски, сразу к делу. "
    "Не представляйся, не говори «это сообщение от плагина», не упоминай подсказку, расписание, лимиты и время "
    "запуска. Без списков, без отчётов, без длинных вступлений. Не повторяй формулировки своих прошлых "
    "инициатив. Закончи одним лёгким вопросом, на который просто ответить, или ничем, если вопрос лишний. "
    "Если пользователь явно занят, не дави. "
    "Если по ходу разговора всплывёт тема с продолжением (планы, ожидание результата, проблема), "
    "вызови инструмент remember_topic с короткой заметкой."
)


def _num(v, lo, hi, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, int(v)))


def _in_hours(h: int, start: int, end: int) -> bool:
    """Час `h` лежит в полуинтервале [start, end) на круге суток."""
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _core_state(v) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return CORE_STATES.get(v, "")
    s = str(v or "").lower().replace("core_state_", "").strip()
    return s if s in KNOWN_STATES else ""


def _inner(payload: dict, key: str) -> dict:
    """Некоторые события приходят вложенными: {"lock_state_changed": {...}}."""
    inner = payload.get(key)
    return inner if isinstance(inner, dict) else payload


def _snippet(text, limit=120) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def parse_duration(text: str, now: datetime) -> tuple[datetime, str]:
    """«30m», «1h30m», «90» (минуты), «до вечера», «завтра» → момент окончания паузы."""
    s = str(text or "").strip().lower()
    if not s:
        raise BadArguments("укажи, на сколько помолчать: например «1h», «30m» или «until_evening»")
    if "вечер" in s or "evening" in s:
        end = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if end <= now:
            end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return end, "до вечера"
    if "завтра" in s or "tomorrow" in s or "утр" in s or "morning" in s:
        end = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        return end, "до завтрашнего утра"
    if any(w in s for w in ("сутки", "день", "day")):
        return now + timedelta(days=1), "на сутки"
    minutes = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*([a-zа-я]*)", s):
        value = float(num.replace(",", "."))
        found = True
        minutes += value * 60 if unit.startswith(("h", "ч")) else value
    if not found or minutes <= 0:
        raise BadArguments("не понял срок; примеры: «1h», «30m», «90», «until_evening»")
    minutes = min(minutes, 7 * 24 * 60)
    return now + timedelta(minutes=minutes), f"на {int(round(minutes))} мин"


def parse_days(text: str) -> set:
    """«пн-пт», «сб,вс», «будни», «выходные», «ежедневно» → множество номеров дней недели."""
    s = text.lower().strip()
    if s in ("будни", "weekdays"):
        return {0, 1, 2, 3, 4}
    if s in ("выходные", "weekend"):
        return {5, 6}
    if s in ("ежедневно", "daily", "каждый"):
        return set(range(7))
    days = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAY_NAMES[x.strip()] for x in part.split("-", 1))
            days |= {(a + i) % 7 for i in range(((b - a) % 7) + 1)}
        else:
            days.add(DAY_NAMES[part])
    return days


def parse_custom(text: str) -> tuple[list, int]:
    """Строки вида «09:30 текст» или «пн-пт 09:30 текст» → (поводы, число непонятых строк)."""
    items, bad = [], 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(?:(\S+)\s+)?(\d{1,2}):(\d{2})\s+(.+)$", line)
        try:
            if not m:
                raise ValueError
            days = parse_days(m.group(1)) if m.group(1) else set(range(7))
            h, mi = int(m.group(2)), int(m.group(3))
            if h > 23 or mi > 59:
                raise ValueError
            items.append({"days": days, "minute": h * 60 + mi, "text": m.group(4).strip(),
                          "key": f"{h:02d}:{mi:02d}|{m.group(4).strip()}"})
        except (ValueError, KeyError):
            bad += 1
    return items, bad


@ui_overlay("first-word-countdown", "countdown.html", url_width=260, url_height=72)
class FirstWord(Plugin):
    """Даёт Астре инициативу: она сама начинает разговор."""

    def __init__(self):
        super().__init__()
        self.clock = time.time
        self.cfg = dict(DEFAULTS)
        self.state: dict = {}
        self.notes: list = []
        self._task = None
        self._bg: set = set()
        self._storage_ok = True
        self._locked = False
        self._core = ""
        self._tts_since = 0.0
        self._last_conv = 0.0
        self._last_activity = 0.0
        self._unlock_or_start = self.clock()
        self._morning_pending = False
        self._inflight = False
        self._own_until = 0.0
        self._retry_after = 0.0
        self._loaded = False
        self._seen: set = set()
        self._last_reason = None
        self._cfg_applied = False
        self._apps_fn = apps.snapshot
        self._apps_cache = (0.0, [], "")
        self._test = None
        self._test_running = False
        self._backend = None
        self._backend_note = "ещё не определён"
        self._pw = {"base": None, "interval": None, "next": 0.0, "since": 0.0}
        self._inp = {"token": None, "last": 0.0, "session": 0.0}
        self._return_at = 0.0
        self._game_since = 0.0
        self._game_checked = 0.0

    def _dt(self) -> datetime:
        return datetime.fromtimestamp(self.clock())

    # ── настройки и запуск ────────────────────────────────────────────────

    def subscribed_events(self) -> list[str]:
        self._ensure_started()
        return [
            "state_changed",
            "lock_state_changed",
            "speech_recognized",
            "chat_message",
            "tts_started",
            "tts_completed",
            "tts_interrupted",
        ]

    async def on_config_changed(self, config: dict):
        cfg = dict(DEFAULTS)
        raw = config if isinstance(config, dict) else {}
        for key, default in DEFAULTS.items():
            v = raw.get(key, default)
            if isinstance(default, bool):
                cfg[key] = v if isinstance(v, bool) else default
            elif isinstance(default, int):
                lo, hi = RANGES[key]
                cfg[key] = _num(v, lo, hi, default)
            elif key in ENUMS:
                cfg[key] = v if v in ENUMS[key] else default
            else:
                cfg[key] = v.strip()[:STR_LIMITS[key]] if isinstance(v, str) else default
        self.cfg = self._apply_level(cfg)
        self._ensure_started()
        self._arm_test()
        if not self._cfg_applied:
            self._cfg_applied = True
            self._arm_morning(self._dt())  # запуск Astra утром считается первой активностью

    @staticmethod
    def _apply_level(cfg: dict) -> dict:
        """«Как часто говорить»: готовые наборы поверх ваших настроек (ваши значения только подтягиваются в нужную сторону)."""
        level = cfg.get("talk_level")
        if level not in ("часто", "очень часто"):
            return cfg
        often = level == "часто"
        out = dict(cfg)
        out.update(
            enable_chat=True,
            enable_random=True,
            random_chance_percent=100,
            daily_limit=max(cfg["daily_limit"], 12 if often else 30),
            min_interval_min=min(cfg["min_interval_min"], 40 if often else 15),
            chat_every_min=min(cfg["chat_every_min"], 60 if often else 25),
            after_chat_pause_min=min(cfg["after_chat_pause_min"], 2 if often else 1),
            silence_hours=1,
        )
        if not often:
            out["adaptive"] = False
        return out

    def _ensure_started(self):
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._load()
        self._task = loop.create_task(self._run())

    async def on_shutdown(self):
        for t in [self._task, *self._bg]:
            if t is not None:
                t.cancel()

    # ── хранилище (только папка плагина) ──────────────────────────────────

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        for name, default in (("state.json", {}), ("topics.json", [])):
            try:
                data = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
                if isinstance(data, type(default)):
                    default = data
            except (OSError, ValueError):
                pass
            if name == "state.json":
                self.state = default
            else:
                self.notes = default

    def _save(self):
        if not self._storage_ok:
            return
        try:
            DATA_DIR.mkdir(exist_ok=True)
            for name, data in (("state.json", self.state), ("topics.json", self.notes)):
                tmp = DATA_DIR / (name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(DATA_DIR / name)
        except OSError as e:
            self._storage_ok = False
            log.warning("не могу писать в %s: %s; состояние будет только в памяти", DATA_DIR, e)

    def _roll_day(self, dt: datetime):
        today = dt.date().isoformat()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["sent_today"] = 0
            self.state["log"] = []
            self._save()

    # ── события ───────────────────────────────────────────────────────────

    async def on_event(self, event_type: str, payload: dict):
        try:
            self._handle(event_type, payload if isinstance(payload, dict) else {})
        except Exception:
            log.exception("не разобрал событие %s", event_type)

    def _own(self, now: float) -> bool:
        return self._inflight or now < self._own_until

    def _handle(self, event_type: str, payload: dict):
        now = self.clock()
        if event_type not in self._seen:
            self._seen.add(event_type)
            shown = json.dumps(payload, ensure_ascii=False)[:300]
            log.info("первое событие %s: %s", event_type, shown)
        if event_type == "state_changed":
            p = _inner(payload, "state_changed")
            new = _core_state(p.get("new_state", p.get("state")))
            self._core = new
            if new == "listening":
                self._activity(now)
        elif event_type == "lock_state_changed":
            p = _inner(payload, "lock_state_changed")
            locked = p.get("locked")
            if isinstance(locked, bool):
                was = self._locked
                self._locked = locked
                if was and not locked:
                    self._unlock_or_start = now
                    self._activity(now)
        elif event_type == "speech_recognized":
            p = _inner(payload, "speech_recognized")
            if p.get("is_final", True) is not False:
                self._interaction(now)
                self._note(p.get("text"))
        elif event_type == "chat_message":
            self._last_conv = now
            role = str(payload.get("role", payload.get("sender", ""))).lower()
            source = str(payload.get("source_id", payload.get("source", "")))
            if not self._own(now) and source != "first-word" and role not in ("assistant", "ai", "bot", "system"):
                self._interaction(now)
                if role == "user":
                    self._note(payload.get("text", payload.get("content")))
        elif event_type in ("tts_started", "tts_completed", "tts_interrupted"):
            # Астра не всегда присылает chat_message/state_changed, а озвучка приходит. Её собственная
            # речь (не наша инициатива) означает, что идёт разговор, то есть пользователь на месте.
            self._tts_since = now if event_type == "tts_started" else 0.0
            self._last_conv = now
            if not self._own(now):
                self._interaction(now)

    def _use_apps(self) -> bool:
        return bool(self.cfg["look_at_apps"])

    def _screen_block(self, kind: str) -> str:
        """Подсказка про активные приложения (и просьба не лезть в личное) для сценариев, где это уместно."""
        if kind not in SCREEN_KINDS or not self._use_apps():
            return ""
        hint = self._apps_hint()
        if not hint:
            return ""
        return " ".join(x for x in (hint, self.cfg["screen_privacy"]) if x)

    def _apps_hint(self) -> str:
        """Один необязательный намёк вместо списка: список модель послушно зачитывает."""
        _, labels, foreground = self._apps_cache
        focus = foreground or (random.choice(labels) if labels else "")
        if not focus or random.random() * 100 >= self.cfg["apps_hint_percent"]:
            return ""
        return (f"Опорная деталь: на компьютере пользователя сейчас запущено: {focus}. Обыграй именно это в своей "
                "реплике: спроси, чем он там занят, пошути или прокомментируй, это должно быть ядром фразы. "
                "НИКОГДА не перечисляй программы и не говори, что у него открыто то-то и то-то, назови только эту "
                "одну. Не говори, что получил это от плагина или что следишь.")

    async def _refresh_apps(self, force: bool = False):
        if not (force or self._use_apps()) or self.clock() - self._apps_cache[0] < APPS_CACHE_SECONDS:
            return
        try:
            labels, foreground = await asyncio.get_running_loop().run_in_executor(None, self._apps_fn)
        except Exception as e:
            log.warning("не смог узнать открытые приложения: %s", e)
            labels, foreground = [], ""
        self._apps_cache = (self.clock(), list(labels), foreground)

    def _activity(self, now: float):
        self._last_activity = now
        self._arm_morning(datetime.fromtimestamp(now))

    def _interaction(self, now: float):
        self._last_conv = now
        self.state["last_interaction"] = now
        pending = self.state.get("pending_reply")
        if pending and now - pending.get("ts", 0) <= REPLY_WINDOW:
            self.state["pending_reply"] = None
            self.state["ignored_streak"] = 0
            self._set_answered(pending.get("ts"), True)
        self._save()
        self._activity(now)

    def _morning_window(self, dt: datetime) -> tuple[int, int]:
        start, end = self.cfg["morning_hour"], self.cfg["morning_until"]
        if dt.weekday() >= 5:
            shift = self.cfg["weekend_shift_hours"]
            start, end = min(23, start + shift), min(24, end + shift)
        return start, end

    def _arm_morning(self, dt: datetime):
        if self.state.get("morning_done") == dt.date().isoformat():
            return
        start, end = self._morning_window(dt)
        if start <= dt.hour < end:
            self._morning_pending = True

    def _note(self, text, min_len=20):
        s = _snippet(text)
        if len(s) < min_len or s.startswith("/"):
            return
        if any(n.get("text") == s for n in self.notes[-10:]):
            return
        now = self.clock()
        self.notes.append({"ts": now, "text": s})
        horizon = now - NOTE_KEEP_DAYS * 86400
        self.notes = [n for n in self.notes if n.get("ts", 0) >= horizon][-MAX_NOTES:]
        self._save()

    # ── главный цикл ──────────────────────────────────────────────────────

    async def _run(self):
        # Демон не всегда присылает настройки при регистрации — спрашиваем сами.
        try:
            raw = await self.host.get_config()
            log.info("настройки из демона: %s", (raw or "")[:400])
            loaded = json.loads(raw) if raw else {}
            await self.on_config_changed(loaded if isinstance(loaded, dict) else {})
        except Exception as e:
            log.warning("не получил настройки: %s", e)
        try:
            info = await self.host.get_daemon_info()
            self._core = _core_state(getattr(info, "state", ""))
        except Exception as e:
            log.warning("GetDaemonInfo не ответил: %s", e)
        if not self._cfg_applied:
            self._arm_morning(self._dt())
        try:
            self._backend, self._backend_note = await asyncio.get_running_loop().run_in_executor(None, presence.detect)
        except Exception as e:
            self._backend, self._backend_note = None, f"ошибка определения: {e}"
        log.info("проверка присутствия: %s", self._backend_note)
        while True:
            delay = None
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой в такте планировщика")
            await asyncio.sleep(delay or TICK_SECONDS)

    # ── тест из настроек ──────────────────────────────────────────────────

    def _arm_test(self):
        """Ползунок «Тест» включён: запомнить, что и когда запускать. Один раз на набор настроек."""
        cfg = self.cfg
        if not cfg["test_mode"]:
            self._test = None
            if self.state.get("test_done"):
                self.state["test_done"] = None
                self._save()
            return
        kinds = [kind for flag, kind in TEST_FLAGS if cfg[flag]]
        sig = f"{cfg['test_time']}|{','.join(kinds)}"
        if self._test and self._test["sig"] == sig:
            return
        now = self.clock()
        target, bad = now, False
        text = cfg["test_time"].strip()
        if text:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                bad = True
            else:
                at = datetime.fromtimestamp(now - 60).replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                                                second=0, microsecond=0)
                if at.timestamp() < now - 60:
                    at += timedelta(days=1)
                target = at.timestamp()
        self._test = {"sig": sig, "target": target, "kinds": kinds, "bad": bad}
        if bad:
            log.warning("тест: не понял время «%s», нужен формат ЧЧ:ММ", text)
        elif kinds:
            log.info("тест запланирован на %s: %s", datetime.fromtimestamp(target).strftime("%d.%m %H:%M"),
                     ", ".join(kinds))

    def _start_test_if_due(self, dt: datetime):
        t = self._test
        if not t or t["bad"] or not t["kinds"] or self._test_running or self._inflight or self._locked:
            return
        if self.state.get("test_done") == t["sig"] or dt.timestamp() < t["target"]:
            return
        self._test_running = True
        task = asyncio.get_running_loop().create_task(self._run_test(dt, t["sig"], list(t["kinds"])))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _run_test(self, dt: datetime, sig: str, kinds: list):
        """Прогоняет выбранные события подряд. Ничего не считает: ни лимит, ни историю, ни «утро уже было»."""
        try:
            log.info("тест начался: %s", ", ".join(kinds))
            steps = []
            for kind in kinds:
                if kind == "своё":
                    items, _ = parse_custom(self.cfg["custom_prompts"])
                    steps += [(kind, f"Напомни или заговори на такую тему: {it['text']}") for it in items[:TEST_MAX_CUSTOM]]
                else:
                    built = self._build(kind, dt, force=True)
                    if built:
                        steps.append((kind, built[0]))
            await self._refresh_apps()
            for n, (kind, body) in enumerate(steps):
                prompt = self._compose(dt, body, kind)
                if self.cfg["dry_run"]:
                    log.info("[пробный режим] тест «%s»: %s", kind, prompt.replace("\n", " ⏎ "))
                else:
                    await self._send(dt, kind, prompt, lambda: None, test=True)
                    await self._wait_tts_idle()
                if n < len(steps) - 1:
                    await asyncio.sleep(self.cfg["test_gap_sec"])
            self.state["test_done"] = sig
            self._save()
            log.info("тест закончен; выключите ползунок «Тест», чтобы запустить его снова позже")
        finally:
            self._test_running = False

    async def _wait_tts_idle(self):
        waited = 0
        while self._tts_since and waited < TEST_TTS_WAIT:
            await asyncio.sleep(2)
            waited += 2

    # ── присутствие ───────────────────────────────────────────────────────

    def _track_input(self, now: float, dt: datetime):
        """Следит за вводом (мышь и клавиатура): когда человек сел, ушёл и вернулся. Коды клавиш не читаются."""
        if not self.cfg["presence_check"] or self._backend is None:
            return
        token = self._sample()
        inp = self._inp
        if token is None:
            return
        if inp["token"] is None:
            inp["token"] = token
            return
        if token == inp["token"]:
            return
        inp["token"] = token
        last = inp["last"]
        if not last:
            inp["session"] = now
        elif now - last >= SESSION_GAP:
            inp["session"] = now
            if now - last >= self.cfg["return_hours"] * 3600:
                self._return_at = now
                log.info("вы вернулись после %d мин отсутствия", int((now - last) / 60))
        inp["last"] = now
        self._arm_morning(dt)

    async def _track_game(self, now: float):
        if not self.cfg["enable_game"]:
            self._game_since = 0.0
            return
        if now - self._game_checked < GAME_CHECK_SECONDS:
            return
        self._game_checked = now
        await self._refresh_apps(force=True)
        if "игра" in self._apps_cache[1]:
            self._game_since = self._game_since or now
        else:
            self._game_since = 0.0

    def _presence_reset(self):
        self._pw.update(base=None, interval=None, next=0.0, since=0.0)

    def _sample(self):
        try:
            return self._backend.sample()
        except Exception as e:
            log.warning("проверка присутствия сломалась (%s), дальше без неё", e)
            self._backend, self._backend_note = None, f"сбой: {e}"
            return None

    def _presence_wait(self, now: float) -> float | None:
        """None, если пользователь на месте (или проверка не нужна); иначе через сколько секунд проверить снова.

        Первая проверка запоминает положение, следующая через `presence_check_sec` сравнивает. Пока ничего не
        менялось, интервал удваивается до `presence_max_sec`."""
        cfg, pw = self.cfg, self._pw
        if not cfg["presence_check"] or self._backend is None:
            return None
        if now - max(self._last_activity, self._inp["last"]) < PRESENCE_RECENT:
            self._presence_reset()
            return None
        base_s = cfg["presence_check_sec"]
        if pw["base"] is None:
            token = self._sample()
            if token is None:
                return None
            pw.update(base=token, interval=base_s, next=now + base_s, since=now)
            log.info("хочу заговорить, но сначала проверю, что вы на месте (раз в %d с)", base_s)
            return float(base_s)
        if now < pw["next"]:
            return max(1.0, pw["next"] - now)
        token = self._sample()
        if token is None:
            return None
        if token != pw["base"]:
            log.info("вы на месте: ввод был (ждал %d с)", int(now - pw["since"]))
            self._presence_reset()
            return None
        pw["interval"] = min(pw["interval"] * 2, max(cfg["presence_max_sec"], base_s))
        pw["next"] = now + pw["interval"]
        log.info("вас не видно, следующая проверка через %d с", pw["interval"])
        return float(pw["interval"])

    def _backoff(self) -> int:
        """Во сколько раз растёт интервал, если инициативы остаются без ответа."""
        if not self.cfg["adaptive"]:
            return 1
        return 1 + min(self.state.get("ignored_streak", 0) // 2, MAX_BACKOFF_FACTOR - 1)

    def _blocked(self, dt: datetime, force: bool = False) -> str | None:
        """Почему сейчас нельзя говорить. `force` — явная просьба пользователя через trigger_now:
        снимает интервал, паузу, тихие часы и «занятость», но не лимит, блокировку и свой же ход."""
        now = dt.timestamp()
        cfg = self.cfg
        if self._inflight:
            return "идёт собственный ход"
        if self._test_running:
            return "идёт тест"
        if self.state.get("sent_today", 0) >= cfg["daily_limit"]:
            return "дневной лимит"
        if self._locked:
            return "экран заблокирован"
        if force:
            return None
        if now < self._retry_after:
            return "пауза после неудачи"
        if now < self.state.get("pause_until", 0):
            return "пауза по просьбе"
        if now - self.state.get("last_sent", 0) < cfg["min_interval_min"] * 60 * self._backoff():
            return "минимальный интервал"
        if cfg["quiet_enabled"] and _in_hours(dt.hour, cfg["quiet_start"], cfg["quiet_end"]):
            return "тихие часы"
        if self._core not in ("", "ready"):
            return f"Астра занята ({self._core})"
        if self._tts_since and now - self._tts_since < TTS_STALE_SECONDS:
            return "Астра говорит"
        if now - self._last_conv < cfg["after_chat_pause_min"] * 60:
            return "идёт разговор"
        return None

    # ── сценарии ──────────────────────────────────────────────────────────

    def _yesterday_topics(self, dt: datetime) -> list:
        y = (dt - timedelta(days=1)).date()
        picked = [
            n for n in self.notes
            if not n.get("asked") and datetime.fromtimestamp(n.get("ts", 0)).date() == y
        ]
        return picked[-3:]

    def _old_topics(self, dt: datetime, k: int) -> list:
        """Заметки старше сегодняшнего дня, которые давно не поднимали."""
        today = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        fresh = dt.timestamp() - NOTE_REUSE_DAYS * 86400
        pool = [n for n in self.notes if n.get("ts", 0) < today and n.get("used", 0) < fresh]
        return random.sample(pool, min(k, len(pool)))

    @staticmethod
    def _topics_line(topics: list) -> str:
        listed = "; ".join(f"«{n['text']}»" for n in topics)
        return (f" Заметки о том, что пользователь говорил раньше (могут быть неточными): {listed}."
                " Можешь опереться на одну из них, если это уместно.")

    def _random_plan(self, dt: datetime):
        today = dt.date().isoformat()
        plan = self.state.get("random")
        if isinstance(plan, dict) and plan.get("day") == today:
            return plan
        plan = {"day": today, "ts": None, "done": False}
        cfg = self.cfg
        if random.random() * 100 < cfg["random_chance_percent"]:
            hours = [
                h for h in range(max(9, dt.hour + 1), 22)
                if not (cfg["quiet_enabled"] and _in_hours(h, cfg["quiet_start"], cfg["quiet_end"]))
            ]
            if hours:
                at = dt.replace(hour=random.choice(hours), minute=random.randint(0, 59), second=0, microsecond=0)
                plan["ts"] = at.timestamp()
        self.state["random"] = plan
        self._save()
        return plan

    def _build(self, kind: str, dt: datetime, force: bool = False):
        """Текст подсказки и действие «сценарий отработал» для вида `kind` или None, если сказать нечего."""
        cfg, now, today = self.cfg, dt.timestamp(), dt.date().isoformat()

        if kind == "утро":
            topics = self._yesterday_topics(dt) if (cfg["enable_callback"] or force) else []
            parts = []
            if cfg["enable_morning"] or force:
                parts.append("Сейчас утро, пользователь только что появился за компьютером. "
                             "Поздоровайся и спроси про планы на день.")
            if topics:
                listed = "; ".join(f"«{n['text']}»" for n in topics)
                parts.append(f"Вчера вы обсуждали (заметки): {listed}. Спроси, чем это закончилось, если уместно.")
            if not parts:
                return None

            def done():
                self._morning_pending = False
                self._return_at = 0.0
                self.state["morning_done"] = today
                for n in topics:
                    n["asked"] = True
            return " ".join(parts), done

        if kind == "вечер":
            def done():
                self.state["evening_done"] = today
            return ("Вечер. Спроси, как прошёл день и что получилось, без допроса. "
                    "Если прозвучит что-то с продолжением, запиши это через remember_topic."), done

        if kind == "ночь":
            start = dt.replace(hour=cfg["night_hour"], minute=0, second=0, microsecond=0)
            if start > dt:
                start -= timedelta(days=1)
            key = start.date().isoformat()

            def done():
                self.state["night_done"] = key
            return ("Уже поздняя ночь, а пользователь всё ещё за компьютером. "
                    "Мягко, без нравоучений предложи закругляться и лечь спать."), done

        if kind == "беседа":
            topics = self._old_topics(dt, 1)

            def done():
                for n in topics:
                    n["used"] = now
            return ("Давно не болтали. Заведи лёгкий непринуждённый разговор: пошути, поделись наблюдением, "
                    "спроси, чем человек занят или как настроение; можно про музыку, игры, погоду или что-то "
                    "интересное, что тебе пришло в голову. Не о делах и не отчётом, как подруга между делом."
                    + (self._topics_line(topics) if topics else "")), done

        if kind == "возвращение":
            def done():
                self._return_at = 0.0
            return (f"Пользователь только что вернулся к компьютеру после долгого отсутствия (больше "
                    f"{cfg['return_hours']} ч). Тепло поприветствуй, как будто соскучилась, и спроси, как он, "
                    "без допроса, где пропадал."), done

        if kind == "перерыв":
            since = max(self._inp["session"], self.state.get("break_last", 0))
            mins = max(cfg["break_minutes"], int((now - since) / 60)) if since else cfg["break_minutes"]

            def done():
                self.state["break_last"] = now
            return (f"Пользователь уже около {mins // 60} ч {mins % 60:02d} мин сидит за компьютером без перерыва. "
                    "Заботливо, без нравоучений предложи размяться, выпить воды или дать глазам отдохнуть."), done

        if kind == "обед":
            def done():
                self.state["lunch_done"] = today
            return ("Сейчас время обеда. Спроси, ел ли пользователь, и по-доброму предложи сделать перерыв на еду, "
                    "если ещё нет."), done

        if kind == "пятница":
            def done():
                self.state["friday_done"] = today
            return ("Сегодня пятница, рабочая неделя заканчивается. Спроси, как прошла неделя и какие планы на "
                    "выходные, без допроса."), done

        if kind == "игра":
            mins = int((now - self._game_since) / 60) if self._game_since else cfg["game_minutes"]

            def done():
                self.state["game_done"] = today
            return (f"Пользователь уже около {max(mins, cfg['game_minutes'])} минут играет в игру. Не мешай и не читай "
                    "нотаций: коротко и с интересом спроси, во что он играет и как идёт, или шутливо предложи "
                    "размяться, если сидит давно."), done

        if kind == "молчание":
            topics = self._old_topics(dt, 1)

            def done():
                for n in topics:
                    n["used"] = now
            return (f"Пользователь за компьютером, но уже больше {cfg['silence_hours']} ч с тобой не говорил. "
                    "Заговори о чём-нибудь уместном: спроси, как дела, или предложи тему."
                    + (self._topics_line(topics) if topics else "")), done

        if kind == "мысль":
            topics = self._old_topics(dt, 2)
            plan = self.state.get("random") if isinstance(self.state.get("random"), dict) else {}

            def done():
                plan["done"] = True
                for n in topics:
                    n["used"] = now
            return ("Поделись случайной мыслью, любопытным фактом или вопросом, который тебе интересно "
                    "обсудить с пользователем. Если у тебя есть доступ к своей памяти или к истории прошлых "
                    "разговоров, опирайся на неё: вспомни что-то из того, что пользователь рассказывал, и "
                    "свяжи с этим мысль или вопрос. Если доступа нет, выбери что-нибудь небанальное. "
                    "Ничего не выдумывай про пользователя."
                    + (self._topics_line(topics) if topics else "")), done
        return None

    def _pick(self, dt: datetime):
        """Первый сценарий, чьё условие выполнено: (вид, подсказка, действие) или None."""
        cfg, now, today = self.cfg, dt.timestamp(), dt.date().isoformat()
        hour = dt.hour

        if self._morning_pending:
            start, end = self._morning_window(dt)
            if not (start <= hour < end) or self.state.get("morning_done") == today:
                self._morning_pending = False
            else:
                built = self._build("утро", dt)
                if built is None:
                    self._morning_pending = False
                    self.state["morning_done"] = today
                    self._save()
                else:
                    return ("утро", *built)

        if cfg["enable_night"] and (hour - cfg["night_hour"]) % 24 < NIGHT_SPAN_HOURS:
            start = dt.replace(hour=cfg["night_hour"], minute=0, second=0, microsecond=0)
            if start > dt:
                start -= timedelta(days=1)
            if self.state.get("night_done") != start.date().isoformat() and now - self._last_activity < NIGHT_ACTIVITY_WINDOW:
                return ("ночь", *self._build("ночь", dt))

        item = self._due_custom(dt)
        if item is not None:
            def done(key=item["key"]):
                self.state.setdefault("custom_done", {})[key] = today
            return ("своё", f"Напомни или заговори на такую тему: {item['text']}", done)

        if cfg["enable_return"] and self._return_at:
            if now - self._return_at >= RETURN_WINDOW:
                self._return_at = 0.0
            else:
                return ("возвращение", *self._build("возвращение", dt))

        if cfg["enable_game"] and self._game_since and now - self._game_since >= cfg["game_minutes"] * 60 \
                and self.state.get("game_done") != today:
            return ("игра", *self._build("игра", dt))

        inp = self._inp
        if cfg["enable_break"] and inp["session"] and now - inp["last"] < PRESENCE_RECENT \
                and now - max(inp["session"], self.state.get("break_last", 0)) >= cfg["break_minutes"] * 60:
            return ("перерыв", *self._build("перерыв", dt))

        present = now - max(self._last_activity, inp["last"], self.state.get("last_interaction", 0)) < PRESENCE_WINDOW
        if cfg["enable_lunch"] and cfg["lunch_hour"] <= hour < min(24, cfg["lunch_hour"] + LUNCH_SPAN_HOURS) \
                and self.state.get("lunch_done") != today and present:
            return ("обед", *self._build("обед", dt))

        if cfg["enable_friday"] and dt.weekday() == 4 \
                and cfg["friday_hour"] <= hour < min(24, cfg["friday_hour"] + FRIDAY_SPAN_HOURS) \
                and self.state.get("friday_done") != today and present:
            return ("пятница", *self._build("пятница", dt))

        if cfg["enable_evening"] and cfg["evening_hour"] <= hour < min(24, cfg["evening_hour"] + EVENING_SPAN_HOURS):
            if self.state.get("evening_done") != today and present:
                return ("вечер", *self._build("вечер", dt))

        if cfg["enable_chat"] and present \
                and now - max(self.state.get("last_interaction", 0), self.state.get("last_sent", 0)) >= cfg["chat_every_min"] * 60:
            return ("беседа", *self._build("беседа", dt))

        if cfg["enable_silence"]:
            ref = max(self.state.get("last_interaction", 0), self._unlock_or_start, self.state.get("last_sent", 0))
            if now - ref >= cfg["silence_hours"] * 3600:
                return ("молчание", *self._build("молчание", dt))

        if cfg["enable_random"]:
            plan = self._random_plan(dt)
            if plan.get("ts") and not plan.get("done") and now >= plan["ts"]:
                return ("мысль", *self._build("мысль", dt))
        return None

    def _due_custom(self, dt: datetime):
        items, _ = parse_custom(self.cfg["custom_prompts"])
        done = self.state.get("custom_done", {})
        minute = dt.hour * 60 + dt.minute
        for it in items:
            if dt.weekday() in it["days"] and 0 <= minute - it["minute"] < CUSTOM_WINDOW_MIN \
                    and done.get(it["key"]) != dt.date().isoformat():
                return it
        return None

    # ── отправка ──────────────────────────────────────────────────────────

    def _set_answered(self, ts, value: bool):
        for e in self.state.get("history", []):
            if e.get("ts") == ts:
                e["answered"] = value

    def _remember_initiative(self, dt: datetime, kind: str, text: str):
        horizon = dt.timestamp() - HISTORY_DAYS * 86400
        history = [e for e in self.state.get("history", []) if e.get("ts", 0) >= horizon]
        history.append({"ts": dt.timestamp(), "kind": kind, "text": text, "answered": None})
        self.state["history"] = history[-HISTORY_KEEP:]

    def _history_line(self, dt: datetime) -> str:
        today = dt.date()
        out = []
        for e in self.state.get("history", [])[-HISTORY_IN_PROMPT:]:
            when = datetime.fromtimestamp(e["ts"])
            days = (today - when.date()).days
            day = "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн. назад"
            reply = {True: "пользователь ответил", False: "ответа не было"}.get(e.get("answered"), "ответа пока нет")
            out.append(f"{day} в {when:%H:%M} ({e['kind']}): «{e['text']}» — {reply}")
        if not out:
            return ""
        return ("Твои недавние инициативы: " + "; ".join(out) + ". Учитывай их: не повторяйся, развивай то, "
                "на что пользователь откликался, и не дави на то, что осталось без ответа.")

    def _compose(self, dt: datetime, body: str, kind: str = "") -> str:
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        part = "ночь" if dt.hour < 5 else "утро" if dt.hour < 12 else "день" if dt.hour < 18 else "вечер"
        lines = [HEADER, f"Сейчас {dt:%H:%M}, {days[dt.weekday()]}, {part}. Подстрой приветствие под это время.", body]
        block = self._screen_block(kind)
        if block:
            lines.append(block)
        history = self._history_line(dt)
        if history:
            lines.append(history)
        if self.cfg["character"]:
            lines.append(f"Твоя личность и манера общения, сохраняй их: {self.cfg['character']}")
        lines.append(FOOTER)
        return "\n".join(lines)

    async def _tick(self, dt: datetime | None = None) -> float | None:
        dt = dt or self._dt()
        self._roll_day(dt)
        now = dt.timestamp()
        self._track_input(now, dt)
        await self._track_game(now)
        self._start_test_if_due(dt)
        pending = self.state.get("pending_reply")
        if pending and now - pending.get("ts", 0) > REPLY_WINDOW:
            self.state["pending_reply"] = None
            self.state["ignored_streak"] = self.state.get("ignored_streak", 0) + 1
            self._set_answered(pending.get("ts"), False)
            self._save()
            log.info("инициатива «%s» осталась без ответа (подряд: %d, интервал ×%d)",
                     pending.get("kind"), self.state["ignored_streak"], self._backoff())
        reason = self._blocked(dt)
        if reason != self._last_reason:
            self._last_reason = reason
            log.info("состояние планировщика: %s (утро ждёт: %s)", reason or "можно говорить", self._morning_pending)
        if reason:
            self._presence_reset()
            return None
        picked = self._pick(dt)
        if picked is None:
            self._presence_reset()
            return None
        wait = self._presence_wait(now)
        if wait is not None:
            return wait
        await self._fire(dt, *picked)
        return None

    async def _fire(self, dt: datetime, kind: str, body: str, done):
        await self._refresh_apps()
        prompt = self._compose(dt, body, kind)
        if self.cfg["dry_run"]:
            log.info("[пробный режим] сценарий «%s»: %s", kind, prompt.replace("\n", " ⏎ "))
            self._mark_sent(dt, done, kind, dry=True)
            return
        await self._send(dt, kind, prompt, done)

    def _mark_sent(self, dt: datetime, done, kind: str, dry: bool = False):
        done()
        self.state["last_sent"] = dt.timestamp()
        self.state["sent_today"] = self.state.get("sent_today", 0) + 1
        self.state.setdefault("log", []).append({"ts": dt.timestamp(), "kind": kind, "dry": dry})
        if not dry:
            self.state["pending_reply"] = {"ts": dt.timestamp(), "kind": kind}
        self._save()

    async def _send(self, dt: datetime, kind: str, prompt: str, done, test: bool = False):
        self._inflight = True
        counted = False
        reply: list = []

        async def consume():
            nonlocal counted
            async for chunk in self.host.send_chat_message(prompt, voice_enabled=self.cfg["speak_aloud"]):
                which = chunk.WhichOneof("content")
                if which == "error":
                    raise RuntimeError(chunk.error)
                if not counted:
                    counted = True
                    if not test:
                        self._mark_sent(dt, done, kind)
                if which == "text":
                    reply.append(chunk.text)
                elif which == "done":
                    return

        try:
            log.info("начинаю разговор: сценарий «%s»%s", kind, " (тест)" if test else "")
            await asyncio.wait_for(consume(), STREAM_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._retry_after = self.clock() + RETRY_BACKOFF
            log.warning("инициатива «%s» не удалась (%s); повтор не раньше чем через %d мин",
                        kind, e, RETRY_BACKOFF // 60)
        finally:
            self._inflight = False
            self._own_until = self.clock() + OWN_TURN_TAIL
            said = _snippet("".join(reply), HISTORY_TEXT)
            if said and not test:
                self._remember_initiative(dt, kind, said)
                self._save()

    # ── инструменты для ассистента ────────────────────────────────────────

    @tool(
        "Приостановить инициативу: Астра перестаёт сама начинать разговор. Вызывай, когда пользователь просит "
        "не писать ему первым: «помолчи час», «не пиши до вечера». duration: «30m», «1h», «1h30m», «90» "
        "(минуты), «until_evening», «until_tomorrow»."
    )
    async def pause_initiative(self, duration: str) -> str:
        until, label = parse_duration(duration, self._dt())
        self.state["pause_until"] = until.timestamp()
        self._save()
        return f"Молчу {label}: до {until:%H:%M %d.%m}."

    @tool("Снять паузу: Астра снова может сама начинать разговор.")
    async def resume_initiative(self) -> str:
        self.state["pause_until"] = 0
        self._save()
        return "Пауза снята, инициатива включена."

    # ── счётчик до следующего события ─────────────────────────────────────

    def _forecast(self, dt: datetime) -> list:
        """[(время или None, сценарий, пояснение)]: когда каждый включённый сценарий может сработать в следующий раз.

        None — срок задаётся не часами, а условием («при первой активности», «когда вернётесь»)."""
        cfg, st = self.cfg, self.state
        now, today, hour = dt.timestamp(), dt.date().isoformat(), dt.hour

        def at(offset: int, h: int, m: int = 0) -> float:
            return (dt + timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()

        def window(kind, start_h, span, done_key, extra_days=None):
            """Окно [start_h, start_h+span) раз в день: когда откроется в следующий раз."""
            end_h = min(24, start_h + span)
            for off in range(8):
                day = (dt + timedelta(days=off))
                if extra_days is not None and day.weekday() not in extra_days:
                    continue
                if off == 0 and (hour >= end_h or st.get(done_key) == today):
                    continue
                if off == 0 and hour >= start_h:
                    return (None, kind, "сейчас, если вы за компьютером")
                return (at(off, start_h), kind, "")
            return None

        out = []
        if cfg["enable_morning"] or cfg["enable_callback"]:
            if st.get("morning_done") != today:
                start, end = self._morning_window(dt)
                if hour < start:
                    out.append((at(0, start), "утро", "при первой активности после этого часа"))
                elif hour < end:
                    out.append((None, "утро", "при первой вашей активности (окно до %d:00)" % end))
            if not out or out[-1][1] != "утро":
                tomorrow = dt + timedelta(days=1)
                out.append((at(1, self._morning_window(tomorrow)[0]), "утро", "при первой активности"))
        if cfg["enable_evening"]:
            out.append(window("вечер", cfg["evening_hour"], EVENING_SPAN_HOURS, "evening_done"))
        if cfg["enable_lunch"]:
            out.append(window("обед", cfg["lunch_hour"], LUNCH_SPAN_HOURS, "lunch_done"))
        if cfg["enable_friday"]:
            out.append(window("пятница", cfg["friday_hour"], FRIDAY_SPAN_HOURS, "friday_done", {4}))
        if cfg["enable_night"]:
            start = at(0, cfg["night_hour"])
            night_key = (dt.replace(hour=cfg["night_hour"], minute=0, second=0, microsecond=0)
                         - (timedelta(days=1) if start > now else timedelta())).date().isoformat()
            if start <= now:
                if (hour - cfg["night_hour"]) % 24 < NIGHT_SPAN_HOURS and st.get("night_done") != night_key:
                    out.append((None, "ночь", "сейчас, если вы активны"))
                else:
                    out.append((at(1, cfg["night_hour"]), "ночь", "если вы будете активны"))
            else:
                out.append((start, "ночь", "если вы будете активны"))
        if cfg["enable_chat"]:
            out.append((max(st.get("last_interaction", 0), st.get("last_sent", 0)) + cfg["chat_every_min"] * 60, "беседа",
                        "если вы будете за компьютером"))
        if cfg["enable_silence"]:
            ref = max(st.get("last_interaction", 0), self._unlock_or_start, st.get("last_sent", 0))
            out.append((ref + cfg["silence_hours"] * 3600, "молчание", "если не будет разговора с Астрой"))
        if cfg["enable_random"]:
            plan = st.get("random") if isinstance(st.get("random"), dict) else {}
            if plan.get("day") == today and plan.get("ts") and not plan.get("done"):
                out.append((plan["ts"], "мысль", ""))
            else:
                out.append((at(1, 9), "мысль", "не раньше; выпадет с шансом %d%%" % cfg["random_chance_percent"]))
        items, _ = parse_custom(cfg["custom_prompts"])
        best = None
        for it in items:
            done_today = st.get("custom_done", {}).get(it["key"]) == today
            for off in range(8):
                if (dt + timedelta(days=off)).weekday() not in it["days"]:
                    continue
                ts = at(off, it["minute"] // 60, it["minute"] % 60)
                if off == 0 and (done_today or now - ts >= CUSTOM_WINDOW_MIN * 60):
                    continue
                cand = (max(ts, now) if ts < now else ts, "своё", it["text"][:40])
                if best is None or cand[0] < best[0]:
                    best = cand
                break
        if best:
            out.append(best)
        if cfg["enable_return"]:
            out.append((None, "возвращение", "когда вернётесь после %d ч отсутствия" % cfg["return_hours"]))
        if cfg["enable_break"]:
            sess = self._inp["session"]
            if sess and now - self._inp["last"] < SESSION_GAP:
                out.append((max(sess, st.get("break_last", 0)) + cfg["break_minutes"] * 60, "перерыв",
                            "если будете сидеть без пауз"))
            else:
                out.append((None, "перерыв", "после %d мин без пауз за компьютером" % cfg["break_minutes"]))
        if cfg["enable_game"]:
            if self._game_since and st.get("game_done") != today:
                out.append((self._game_since + cfg["game_minutes"] * 60, "игра", "если игра останется открытой"))
            else:
                out.append((None, "игра", "когда игра будет идти %d мин" % cfg["game_minutes"]))
        return [e for e in out if e]

    def _earliest_allowed(self, dt: datetime) -> tuple:
        """(самый ранний момент, когда ограничения позволят заговорить, почему он такой) или (now, "")."""
        cfg, st = self.cfg, self.state
        now = dt.timestamp()
        t, why = now, ""
        for ts, label in ((st.get("last_sent", 0) + cfg["min_interval_min"] * 60 * self._backoff(), "пауза между сообщениями"),
                          (st.get("pause_until", 0), "пауза по просьбе"), (self._retry_after, "пауза после неудачи")):
            if ts > t:
                t, why = ts, label
        if st.get("sent_today", 0) >= cfg["daily_limit"]:
            t, why = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), "дневной лимит"
        return t, why

    def _shift_quiet(self, ts: float) -> float:
        cfg = self.cfg
        if not cfg["quiet_enabled"]:
            return ts
        d = datetime.fromtimestamp(ts)
        if _in_hours(d.hour, cfg["quiet_start"], cfg["quiet_end"]):
            end = d.replace(hour=cfg["quiet_end"], minute=0, second=0, microsecond=0)
            if end.timestamp() <= ts:
                end += timedelta(days=1)
            return end.timestamp()
        return ts

    @staticmethod
    def _span(sec: float) -> str:
        m = max(0, int(sec // 60))
        if m < 1:
            return "меньше минуты"
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        return " ".join(x for x in (f"{d} д" if d else "", f"{h} ч" if h else "", f"{m} мин" if m and not d else "") if x)

    def _plan(self, dt: datetime) -> tuple:
        """([(время, сценарий, пояснение, сдвинуто)] по возрастанию, [(сценарий, условие)], причина сдвига)."""
        self._roll_day(dt)
        allowed, why = self._earliest_allowed(dt)
        timed, cond = [], []
        for ts, kind, note in self._forecast(dt):
            if ts is None:
                cond.append((kind, note))
                continue
            eff = self._shift_quiet(max(ts, allowed))
            timed.append((eff, kind, note, eff > ts + 60))
        timed.sort()
        return timed, cond, why

    def next_event_text(self, dt: datetime | None = None) -> str:
        dt = dt or self._dt()
        now = dt.timestamp()
        timed, cond, why = self._plan(dt)

        def when(ts):
            d = datetime.fromtimestamp(ts)
            day = "" if d.date() == dt.date() else " завтра" if d.date() == (dt + timedelta(days=1)).date() else f" {d:%d.%m}"
            return f"через {self._span(ts - now)} (в {d:%H:%M}{day})"

        if not timed and not cond:
            return "Ни один сценарий не включён, ждать нечего."
        lines = []
        if timed:
            ts, kind, note, delayed = timed[0]
            lines.append(f"Следующее: «{kind}» {when(ts)}" + (f", {note}" if note else "") + ".")
            if delayed and why:
                lines.append(f"Раньше не выйдет: {why}.")
            elif delayed:
                lines.append("Сдвинуто тихими часами.")
            rest = timed[1:5]
            if rest:
                lines.append("Потом: " + "; ".join(f"«{k}» {when(t)}" for t, k, _, _ in rest) + ".")
        else:
            lines.append("По часам ничего не назначено.")
        if cond:
            lines.append("Без точного времени: " + "; ".join(f"«{k}», {n}" for k, n in cond) + ".")
        if self.state.get("pause_until", 0) > now:
            lines.append(f"Сейчас пауза до {datetime.fromtimestamp(self.state['pause_until']):%H:%M %d.%m}.")
        return "\n".join(lines)

    def countdown_data(self, dt: datetime | None = None) -> dict:
        """Данные для плашки: что показать и сколько секунд осталось."""
        dt = dt or self._dt()
        now = dt.timestamp()
        if not self.cfg["show_widget"]:
            return {"visible": False}
        timed, cond, why = self._plan(dt)
        paused = self.state.get("pause_until", 0) > now
        out = {"visible": True, "now": now, "place": self.cfg["widget_place"], "paused": paused,
               "pause_until": f"{datetime.fromtimestamp(self.state['pause_until']):%H:%M}" if paused else "",
               "more": [{"kind": k, "at": f"{datetime.fromtimestamp(t):%H:%M}", "in": int(t - now)}
                        for t, k, _, _ in timed[1:4]],
               "cond": [{"kind": k, "note": n} for k, n in cond[:4]],
               "left_today": max(0, self.cfg["daily_limit"] - self.state.get("sent_today", 0))}
        if timed:
            ts, kind, note, delayed = timed[0]
            d = datetime.fromtimestamp(ts)
            out.update(kind=kind, ts=ts, left=int(ts - now), note=note, delayed=delayed, why=why,
                       at=f"{d:%H:%M}" + ("" if d.date() == dt.date() else f" {d:%d.%m}"))
        return out

    @ui_call
    async def get_countdown(self, **params):
        return self.countdown_data()

    @tool("Сколько осталось до следующего события: какой сценарий сработает ближайшим и через сколько, что за ним "
          "и что ждёт условия. Учитывает лимит, паузу между сообщениями, тихие часы и паузу по просьбе.")
    async def next_event(self) -> str:
        return self.next_event_text()

    @tool("Показать состояние инициативы: что включено, что уже было сегодня, сколько осталось и почему сейчас тихо.")
    async def initiative_status(self) -> str:
        dt = self._dt()
        self._roll_day(dt)
        cfg = self.cfg
        names = [
            ("утреннее приветствие", cfg["enable_morning"]),
            ("возврат к теме", cfg["enable_callback"]),
            ("вечерний итог", cfg["enable_evening"]),
            ("полуночник", cfg["enable_night"]),
            ("возвращение после отсутствия", cfg["enable_return"]),
            ("перерыв при долгом сидении", cfg["enable_break"]),
            ("обед", cfg["enable_lunch"]),
            ("пятница", cfg["enable_friday"]),
            ("игра", cfg["enable_game"]),
            ("лёгкая беседа", cfg["enable_chat"]),
            ("долгое молчание", cfg["enable_silence"]),
            ("случайная мысль", cfg["enable_random"]),
        ]
        on = [n for n, v in names if v] or ["ничего не включено"]
        items, bad = parse_custom(cfg["custom_prompts"])
        lines = [f"Включено: {', '.join(on)}."]
        if items or bad:
            lines.append(f"Свои поводы: {len(items)}" + (f", не разобрано строк: {bad}" if bad else "") + ".")
        pause = self.state.get("pause_until", 0)
        if pause > dt.timestamp():
            lines.append(f"Пауза до {datetime.fromtimestamp(pause):%H:%M %d.%m}.")
        else:
            lines.append("Пауза не стоит.")
        left = max(0, cfg["daily_limit"] - self.state.get("sent_today", 0))
        lines.append(f"Сегодня осталось инициатив: {left} из {cfg['daily_limit']}.")
        today = self.state.get("log", [])
        if today:
            lines.append("Сегодня было: " + ", ".join(
                f"{e['kind']} в {datetime.fromtimestamp(e['ts']):%H:%M}" + (" (пробно)" if e.get("dry") else "")
                for e in today) + ".")
        factor = self._backoff()
        if factor > 1:
            lines.append(f"Последние инициативы остались без ответа, поэтому интервал увеличен в {factor} раза.")
        nxt = self.state.get("last_sent", 0) + cfg["min_interval_min"] * 60 * factor
        if nxt > dt.timestamp():
            lines.append(f"Следующая не раньше {datetime.fromtimestamp(nxt):%H:%M}.")
        reason = self._blocked(dt)
        lines.append(f"Прямо сейчас: {reason or 'можно говорить'}.")
        if cfg["presence_check"]:
            if self._backend is None:
                lines.append(f"Проверка присутствия отключена: {self._backend_note}.")
            elif self._pw["since"]:
                lines.append(f"Хочу заговорить, но вас не видно уже {int((dt.timestamp() - self._pw['since']) // 60)} мин "
                             f"(проверяю раз в {int(self._pw['interval'])} с).")
            else:
                lines.append(f"Проверка присутствия: {self._backend_note}.")
        if cfg["quiet_enabled"]:
            lines.append(f"Тихие часы: {cfg['quiet_start']:02d}:00–{cfg['quiet_end']:02d}:00.")
        if cfg["dry_run"]:
            lines.append("Пробный режим: модели ничего не отправляется.")
        if cfg["look_at_apps"]:
            shown = ", ".join(self._apps_cache[1]) or "пока не определено"
            lines.append(f"Приложения, из которых выбирается опорная деталь (в {cfg['apps_hint_percent']}% инициатив, "
                         f"по одному, списком не передаются): {shown}.")
        t = self._test
        if t:
            if t["bad"]:
                lines.append("Тест включён, но время не разобрано: нужен формат ЧЧ:ММ.")
            elif not t["kinds"]:
                lines.append("Тест включён, но ни одно событие не выбрано.")
            elif self._test_running:
                lines.append("Тест идёт прямо сейчас.")
            elif self.state.get("test_done") == t["sig"]:
                lines.append("Тест уже выполнен. Выключите и включите ползунок, чтобы запустить снова.")
            else:
                lines.append(f"Тест запланирован на {datetime.fromtimestamp(t['target']):%d.%m %H:%M}: "
                             + ", ".join(t["kinds"]) + ".")
        if "lock_state_changed" not in self._seen:
            lines.append("Событий о блокировке экрана плагин пока не получал, поэтому не знает, заблокирован ли он.")
        lines.append(self.next_event_text(dt).split("\n")[0])
        return "\n".join(lines)

    @tool(
        "Записать короткую заметку о теме разговора, у которой может быть продолжение (планы, проблема, "
        "ожидание результата), чтобы завтра спросить, чем всё кончилось. Вызывай редко и только для таких тем."
    )
    async def remember_topic(self, topic: str) -> str:
        s = _snippet(topic)
        if not s:
            raise BadArguments("тема пустая")
        self._note(s, min_len=3)
        return "Заметка сохранена."

    @tool(
        "Проверить, как плагин определяет присутствие пользователя: две пробы с паузой, в это время нужно подвигать "
        "мышь или нажать клавишу. Показывает, каким способом читается ввод и заметил ли плагин движение."
    )
    async def check_presence(self) -> str:
        if self._backend is None:
            return f"Проверка присутствия недоступна: {self._backend_note}."
        pause = self.cfg["presence_check_sec"]
        first = self._sample()
        await asyncio.sleep(pause)
        second = self._sample()
        if first is None or second is None:
            return f"Способ «{self._backend.name}» не смог прочитать ввод."
        moved = "движение или ввод были" if first != second else "ввода не было"
        return f"Способ: {self._backend_note}. За {pause} с: {moved}."

    @tool(
        "Сразу запустить один сценарий инициативы для проверки или по просьбе пользователя: morning, evening, "
        "night, silence, random, chat, return, break, lunch, friday или game. Учитывает дневной лимит и блокировку экрана, остальные ограничения снимает."
    )
    async def trigger_now(self, scenario: str) -> str:
        kind = KINDS.get(str(scenario).strip().lower())
        if kind is None:
            raise BadArguments("сценарий: morning, evening, night, silence, random, chat, return, break, lunch, friday или game")
        dt = self._dt()
        self._roll_day(dt)
        reason = self._blocked(dt, force=True)
        if reason:
            return f"Не запускаю: {reason}."
        built = self._build(kind, dt, force=True)
        if built is None:
            return "Для этого сценария сейчас нечего сказать."
        # Сообщение встанет в очередь за ходом, который нас вызвал: ждать его здесь нельзя (тупик).
        task = asyncio.get_running_loop().create_task(self._fire(dt, kind, *built))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        suffix = " (пробный режим: только запись в журнал)" if self.cfg["dry_run"] else ""
        return f"Запускаю сценарий «{kind}»{suffix}. Астра заговорит через несколько секунд."


if __name__ == "__main__":
    FirstWord().run()
