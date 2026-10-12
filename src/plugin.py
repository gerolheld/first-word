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

log = logging.getLogger("first-word")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

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
    "quiet_enabled": True,
    "quiet_start": 3,
    "quiet_end": 8,
    "after_chat_pause_min": 3,
    "speak_aloud": True,
    "character": "",
    "look_at_screen": False,
    "screen_style": "Поприветствуй как обычно, а потом по-доброму подколи и спроси про увиденное, например: «что ты опять смотришь?» или «опять этим занимаешься?». Без нравоучений и без злости.",
    "screen_privacy": "Не упоминай и не пересказывай личное: переписки, почту, пароли, банки, сообщения других людей.",
    "dry_run": False,
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
}
STR_LIMITS = {"character": 600, "custom_prompts": 1500, "screen_privacy": 400, "screen_style": 400}
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

TICK_SECONDS = 20
NIGHT_SPAN_HOURS = 7
EVENING_SPAN_HOURS = 3
CUSTOM_WINDOW_MIN = 180
PRESENCE_WINDOW = 30 * 60
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
            else:
                cfg[key] = v.strip()[:STR_LIMITS[key]] if isinstance(v, str) else default
        self.cfg = cfg
        self._ensure_started()
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
            log.info("первое событие %s: %s", event_type, json.dumps(payload, ensure_ascii=False)[:300])
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
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой в такте планировщика")
            await asyncio.sleep(TICK_SECONDS)

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
        if self.cfg["look_at_screen"] and kind in SCREEN_KINDS:
            lines.append(
                "Прежде чем заговорить, если у тебя есть такая возможность, сделай снимок экрана пользователя и "
                "посмотри, чем он занят; опирайся на это в разговоре, но не описывай экран подробно. "
                + (self.cfg["screen_style"] + " " if self.cfg["screen_style"] else "")
                + (self.cfg["screen_privacy"] + " " if self.cfg["screen_privacy"] else "")
                + "Если снимок сделать не удаётся, просто продолжай без него.")
        history = self._history_line(dt)
        if history:
            lines.append(history)
        if self.cfg["character"]:
            lines.append(f"Как заводить разговор: {self.cfg['character']}")
        lines.append(FOOTER)
        return "\n".join(lines)

    async def _tick(self, dt: datetime | None = None):
        dt = dt or self._dt()
        self._roll_day(dt)
        now = dt.timestamp()
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
            return
        picked = self._pick(dt)
        if picked is None:
            return
        await self._fire(dt, *picked)

    async def _fire(self, dt: datetime, kind: str, body: str, done):
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

    async def _send(self, dt: datetime, kind: str, prompt: str, done):
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
                    self._mark_sent(dt, done, kind)
                if which == "text":
                    reply.append(chunk.text)
                elif which == "done":
                    return

        try:
            log.info("начинаю разговор: сценарий «%s»", kind)
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
            if said:
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
        if cfg["quiet_enabled"]:
            lines.append(f"Тихие часы: {cfg['quiet_start']:02d}:00–{cfg['quiet_end']:02d}:00.")
        if cfg["dry_run"]:
            lines.append("Пробный режим: модели ничего не отправляется.")
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
