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
import time
from datetime import datetime, timedelta
from pathlib import Path

from astra_plugin_sdk import BadArguments, Plugin, tool

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
    "character": PERSONA,
    "look_at_screen": True,
    "look_at_apps": True,
    "apps_hint_percent": 50,
    "screen_tool": "",
    "screen_style": "Поприветствуй как обычно, а потом по-доброму подколи и спроси про увиденное, например: «что ты опять смотришь?» или «опять этим занимаешься?». Без нравоучений и без злости.",
    "screen_privacy": "Не упоминай и не пересказывай личное: переписки, почту, пароли, банки, сообщения других людей.",
    "dry_run": False,
    "test_mode": False,
    "test_time": "",
    "test_morning": True,
    "test_evening": False,
    "test_night": False,
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
STR_LIMITS = {"character": 1200, "custom_prompts": 1500, "screen_privacy": 400, "screen_style": 400, "screen_tool": 80, "test_time": 5}
SCREEN_KINDS = {"утро", "ночь", "молчание", "мысль", "вечер", "своё"}

CORE_STATES = {0: "stopped", 1: "ready", 2: "listening", 3: "processing", 4: "speaking", 5: "error"}
KNOWN_STATES = set(CORE_STATES.values())

KINDS = {
    "morning": "утро", "утро": "утро",
    "evening": "вечер", "вечер": "вечер",
    "night": "ночь", "ночь": "ночь",
    "silence": "молчание", "молчание": "молчание",
    "random": "мысль", "мысль": "мысль",
}
DAY_NAMES = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

TEST_FLAGS = [
    ("test_morning", "утро"), ("test_evening", "вечер"), ("test_night", "ночь"),
    ("test_silence", "молчание"), ("test_random", "мысль"), ("test_custom", "своё"),
]
TEST_TTS_WAIT = 90
TEST_MAX_CUSTOM = 3

SCREEN_TOOL_RE = re.compile(r"screen|скрин|capture|snapshot", re.I)
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
    "Начни разговор сам: одна-две короткие живые фразы, без списков и без пересказа этой подсказки. "
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
        self._screen_skipped_logged = False
        self._test = None
        self._test_running = False
        self._backend = None
        self._backend_note = "ещё не определён"
        self._pw = {"base": None, "interval": None, "next": 0.0, "since": 0.0}

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
            "tool_started",
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
            else:
                cfg[key] = v.strip()[:STR_LIMITS[key]] if isinstance(v, str) else default
        self.cfg = cfg
        self._ensure_started()
        self._arm_test()
        if not self._cfg_applied:
            self._cfg_applied = True
            self._arm_morning(self._dt())  # запуск Astra утром считается первой активностью

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
            shown = sorted(payload) if event_type == "tool_started" else json.dumps(payload, ensure_ascii=False)[:300]
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
        elif event_type == "tool_started":
            self._learn_screen_tool(payload)
        elif event_type in ("tts_started", "tts_completed", "tts_interrupted"):
            # Астра не всегда присылает chat_message/state_changed, а озвучка приходит. Её собственная
            # речь (не наша инициатива) означает, что идёт разговор, то есть пользователь на месте.
            self._tts_since = now if event_type == "tts_started" else 0.0
            self._last_conv = now
            if not self._own(now):
                self._interaction(now)

    def _learn_screen_tool(self, payload: dict):
        """Запоминает имя инструмента, которым Астра смотрит на экран, чтобы звать его по имени."""
        inner = payload.get("tool")
        candidates = [payload.get("name"), payload.get("tool_name"),
                      inner if isinstance(inner, str) else (inner or {}).get("name") if isinstance(inner, dict) else None]
        for name in candidates:
            if isinstance(name, str) and SCREEN_TOOL_RE.search(name) and len(name) <= 80:
                if self.state.get("screen_tool_seen") != name:
                    self.state["screen_tool_seen"] = name
                    self._save()
                    log.info("запомнил инструмент экрана: %s", name)
                return

    def _use_tool(self) -> bool:
        """Приказ «посмотри на экран» отдаём только когда известно имя инструмента: иначе Астра зря ищет его."""
        return bool(self.cfg["look_at_screen"] and self._screen_tool())

    def _use_apps(self) -> bool:
        cfg = self.cfg
        return bool(cfg["look_at_apps"] or (cfg["look_at_screen"] and not self._screen_tool()))

    def _screen_block(self, kind: str) -> str:
        if kind not in SCREEN_KINDS:
            return ""
        parts = []
        if self._use_tool():
            parts.append(
                f"СНАЧАЛА посмотри на экран пользователя: вызови инструмент снимка экрана «{self._screen_tool()}» "
                "и дождись результата, и только потом начинай говорить. Это обязательно, не отвечай до вызова. "
                "Опирайся на увиденное, но не описывай экран подробно. Если вызов не удался, продолжай без него "
                "и не упоминай об этом.")
        apps_hint = self._apps_hint() if self._use_apps() else ""
        if apps_hint:
            parts.append(apps_hint)
        if not parts:
            if self.cfg["look_at_screen"] and not self._screen_skipped_logged:
                self._screen_skipped_logged = True
                log.info("взгляд на экран пропущен: имя инструмента не известно, а список приложений пуст")
            return ""
        # Тон про «что ты смотришь» относится к настоящему снимку экрана; к намёку про приложение он не нужен
        # и толкал модель на перечисление.
        tail = [self.cfg["screen_style"]] if self._use_tool() else []
        tail.append(self.cfg["screen_privacy"])
        parts += [p for p in tail if p]
        return " ".join(parts)

    def _apps_hint(self) -> str:
        """Один необязательный намёк вместо списка: список модель послушно зачитывает."""
        _, labels, foreground = self._apps_cache
        focus = foreground or (random.choice(labels) if labels else "")
        if not focus or random.random() * 100 >= self.cfg["apps_hint_percent"]:
            return ""
        return (f"Необязательный намёк: на компьютере пользователя сейчас запущено: {focus}. Если это естественно "
                "вписывается, вскользь, одной короткой репликой, пошути или спроси об этом. НИКОГДА не перечисляй "
                "программы и не говори, что у него открыто то-то и то-то. Если не получается естественно или "
                "звучит натянуто, просто не упоминай. Не говори, что получил это от плагина.")

    async def _refresh_apps(self):
        if not self._use_apps() or self.clock() - self._apps_cache[0] < APPS_CACHE_SECONDS:
            return
        try:
            labels, foreground = await asyncio.get_running_loop().run_in_executor(None, self._apps_fn)
        except Exception as e:
            log.warning("не смог узнать открытые приложения: %s", e)
            labels, foreground = [], ""
        self._apps_cache = (self.clock(), list(labels), foreground)

    def _screen_tool(self) -> str:
        return self.cfg["screen_tool"] or self.state.get("screen_tool_seen") or ""

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
        if now - self._last_activity < PRESENCE_RECENT:
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

        if cfg["enable_evening"] and cfg["evening_hour"] <= hour < min(24, cfg["evening_hour"] + EVENING_SPAN_HOURS):
            present = now - max(self._last_activity, self.state.get("last_interaction", 0)) < PRESENCE_WINDOW
            if self.state.get("evening_done") != today and present:
                return ("вечер", *self._build("вечер", dt))

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
        lines = [HEADER, f"Сейчас {dt:%H:%M}, {days[dt.weekday()]}.", body]
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
        if cfg["look_at_screen"] or cfg["look_at_apps"]:
            tool = self._screen_tool()
            if self._use_tool():
                lines.append(f"Инструмент экрана: «{tool}».")
            elif cfg["look_at_screen"]:
                lines.append("Инструмент экрана у Астры не найден, поэтому приказ «посмотри на экран» не отправляется. "
                             "Если он у неё есть, попросите её в чате посмотреть на экран, и плагин запомнит имя.")
            if self._use_apps():
                shown = ", ".join(self._apps_cache[1]) or "пока не определено"
                lines.append(f"Приложения, из которых выбирается намёк (в {cfg['apps_hint_percent']}% инициатив, "
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
        "night, silence или random. Учитывает дневной лимит и блокировку экрана, остальные ограничения снимает."
    )
    async def trigger_now(self, scenario: str) -> str:
        kind = KINDS.get(str(scenario).strip().lower())
        if kind is None:
            raise BadArguments("сценарий: morning, evening, night, silence или random")
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
