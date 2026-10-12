"""Инициатива Астры — Астра сама начинает разговор.

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
    "enable_callback": True,
    "enable_night": True,
    "night_hour": 1,
    "enable_silence": True,
    "silence_hours": 3,
    "enable_random": True,
    "random_chance_percent": 40,
    "daily_limit": 4,
    "min_interval_min": 90,
    "quiet_enabled": True,
    "quiet_start": 3,
    "quiet_end": 8,
    "after_chat_pause_min": 3,
    "speak_aloud": True,
    "character": "",
    "dry_run": False,
}
RANGES = {
    "morning_hour": (0, 23),
    "morning_until": (1, 24),
    "night_hour": (0, 23),
    "silence_hours": (1, 48),
    "random_chance_percent": (0, 100),
    "daily_limit": (0, 20),
    "min_interval_min": (0, 1440),
    "quiet_start": (0, 23),
    "quiet_end": (0, 23),
    "after_chat_pause_min": (0, 120),
}

CORE_STATES = {0: "stopped", 1: "ready", 2: "listening", 3: "processing", 4: "speaking", 5: "error"}
KNOWN_STATES = set(CORE_STATES.values())

TICK_SECONDS = 20
NIGHT_SPAN_HOURS = 7
NIGHT_ACTIVITY_WINDOW = 20 * 60
TTS_STALE_SECONDS = 5 * 60
STREAM_TIMEOUT = 180
RETRY_BACKOFF = 10 * 60
OWN_TURN_TAIL = 30
MAX_NOTES = 150
NOTE_KEEP_DAYS = 60
NOTE_REUSE_DAYS = 7

HEADER = (
    "[Служебная подсказка плагина «Инициатива Астры». Её написал не пользователь, а плагин: "
    "не отвечай на неё как на реплику пользователя и не упоминай её.]"
)
FOOTER = "Начни разговор сам: одна-две короткие живые фразы, без списков и без пересказа этой подсказки."


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


def _snippet(text) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:120]


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
        if unit.startswith(("h", "ч")):
            minutes += value * 60
        else:
            minutes += value
    if not found or minutes <= 0:
        raise BadArguments("не понял срок; примеры: «1h», «30m», «90», «until_evening»")
    minutes = min(minutes, 7 * 24 * 60)
    return now + timedelta(minutes=minutes), f"на {int(round(minutes))} мин"


class FirstWord(Plugin):
    """Даёт Астре инициативу: она сама начинает разговор."""

    def __init__(self):
        super().__init__()
        self.cfg = dict(DEFAULTS)
        self.state: dict = {}
        self.notes: list = []
        self._task = None
        self._storage_ok = True
        self._locked = False
        self._core = ""
        self._tts_since = 0.0
        self._last_conv = 0.0
        self._last_activity = 0.0
        self._unlock_or_start = time.time()
        self._morning_pending = False
        self._inflight = False
        self._own_until = 0.0
        self._retry_after = 0.0
        self._loaded = False
        self._seen: set = set()
        self._last_reason = None
        self._cfg_applied = False

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
                cfg[key] = v.strip()[:600] if isinstance(v, str) else default
        self.cfg = cfg
        self._ensure_started()
        if not self._cfg_applied:
            self._cfg_applied = True
            self._arm_morning(datetime.now())  # запуск Astra утром считается первой активностью

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
        if self._task is not None:
            self._task.cancel()

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

    def _roll_day(self, now: datetime):
        today = now.date().isoformat()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["sent_today"] = 0
            self._save()

    # ── события ───────────────────────────────────────────────────────────

    async def on_event(self, event_type: str, payload: dict):
        try:
            self._handle(event_type, payload if isinstance(payload, dict) else {})
        except Exception:
            log.exception("не разобрал событие %s", event_type)

    def _handle(self, event_type: str, payload: dict):
        now = time.time()
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
            own = self._inflight or now < self._own_until or source == "first-word"
            if not own and role not in ("assistant", "ai", "bot", "system"):
                self._interaction(now)
                if role == "user":
                    self._note(payload.get("text", payload.get("content")))
        elif event_type in ("tts_started", "tts_completed", "tts_interrupted"):
            # Астра не всегда присылает chat_message/state_changed, а озвучка приходит. Её собственная
            # речь (не наша инициатива) означает, что идёт разговор, то есть пользователь на месте.
            self._tts_since = now if event_type == "tts_started" else 0.0
            self._last_conv = now
            if not (self._inflight or now < self._own_until):
                self._interaction(now)

    def _activity(self, now: float):
        self._last_activity = now
        self._arm_morning(datetime.fromtimestamp(now))

    def _interaction(self, now: float):
        self._last_conv = now
        self.state["last_interaction"] = now
        self._save()
        self._activity(now)

    def _arm_morning(self, dt: datetime):
        if self.state.get("morning_done") == dt.date().isoformat():
            return
        if self.cfg["morning_hour"] <= dt.hour < self.cfg["morning_until"]:
            self._morning_pending = True

    def _note(self, text, min_len=20):
        s = _snippet(text)
        if len(s) < min_len or s.startswith("/"):
            return
        if any(n.get("text") == s for n in self.notes[-10:]):
            return
        self.notes.append({"ts": time.time(), "text": s})
        horizon = time.time() - NOTE_KEEP_DAYS * 86400
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
            self._arm_morning(datetime.now())
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой в такте планировщика")
            await asyncio.sleep(TICK_SECONDS)

    def _blocked(self, dt: datetime) -> str | None:
        now = dt.timestamp()
        cfg = self.cfg
        if self._inflight:
            return "идёт собственный ход"
        if now < self._retry_after:
            return "пауза после неудачи"
        if now < self.state.get("pause_until", 0):
            return "пауза по просьбе"
        if self.state.get("sent_today", 0) >= cfg["daily_limit"]:
            return "дневной лимит"
        if now - self.state.get("last_sent", 0) < cfg["min_interval_min"] * 60:
            return "минимальный интервал"
        if cfg["quiet_enabled"] and _in_hours(dt.hour, cfg["quiet_start"], cfg["quiet_end"]):
            return "тихие часы"
        if self._locked:
            return "экран заблокирован"
        if self._core not in ("", "ready"):
            return f"Астра занята ({self._core})"
        if self._tts_since and now - self._tts_since < TTS_STALE_SECONDS:
            return "Астра говорит"
        if now - self._last_conv < cfg["after_chat_pause_min"] * 60:
            return "идёт разговор"
        return None

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

    def _pick(self, dt: datetime):
        """Возвращает (вид, подсказка, побочный эффект при успехе) или None."""
        cfg = self.cfg
        now = dt.timestamp()
        today = dt.date().isoformat()

        if self._morning_pending:
            if not (cfg["morning_hour"] <= dt.hour < cfg["morning_until"]) or self.state.get("morning_done") == today:
                self._morning_pending = False
            else:
                topics = self._yesterday_topics(dt) if cfg["enable_callback"] else []
                parts = []
                if cfg["enable_morning"]:
                    parts.append("Сейчас утро, пользователь только что появился за компьютером. Поздоровайся и спроси про планы на день.")
                if topics:
                    listed = "; ".join(f"«{n['text']}»" for n in topics)
                    parts.append(f"Вчера вы обсуждали (заметки): {listed}. Спроси, чем это закончилось, если уместно.")
                if not parts:
                    self._morning_pending = False
                    self.state["morning_done"] = today
                    self._save()
                else:
                    def done(topics=topics):
                        self._morning_pending = False
                        self.state["morning_done"] = today
                        for n in topics:
                            n["asked"] = True
                    return "утро", " ".join(parts), done

        if cfg["enable_night"] and (dt.hour - cfg["night_hour"]) % 24 < NIGHT_SPAN_HOURS:
            start = dt.replace(hour=cfg["night_hour"], minute=0, second=0, microsecond=0)
            if start > dt:
                start -= timedelta(days=1)
            key = start.date().isoformat()
            if self.state.get("night_done") != key and now - self._last_activity < NIGHT_ACTIVITY_WINDOW:
                def done(key=key):
                    self.state["night_done"] = key
                return "ночь", (
                    "Уже поздняя ночь, а пользователь всё ещё за компьютером. "
                    "Мягко, без нравоучений предложи закругляться и лечь спать."
                ), done

        if cfg["enable_silence"]:
            ref = max(self.state.get("last_interaction", 0), self._unlock_or_start, self.state.get("last_sent", 0))
            if now - ref >= cfg["silence_hours"] * 3600:
                topics = self._old_topics(dt, 1)

                def done(topics=topics):
                    for n in topics:
                        n["used"] = now
                return "молчание", (
                    f"Пользователь за компьютером, но уже больше {cfg['silence_hours']} ч с тобой не говорил. "
                    "Заговори о чём-нибудь уместном: спроси, как дела, или предложи тему."
                    + (self._topics_line(topics) if topics else "")
                ), done

        if cfg["enable_random"]:
            plan = self._random_plan(dt)
            if plan.get("ts") and not plan.get("done") and now >= plan["ts"]:
                topics = self._old_topics(dt, 2)

                def done(plan=plan, topics=topics):
                    plan["done"] = True
                    for n in topics:
                        n["used"] = now
                return "мысль", (
                    "Поделись случайной мыслью, любопытным фактом или вопросом, который тебе интересно "
                    "обсудить с пользователем. Если у тебя есть доступ к своей памяти или к истории прошлых "
                    "разговоров, опирайся на неё: вспомни что-то из того, что пользователь рассказывал, и "
                    "свяжи с этим мысль или вопрос. Если доступа нет, выбери что-нибудь небанальное. "
                    "Ничего не выдумывай про пользователя."
                    + (self._topics_line(topics) if topics else "")
                ), done
        return None

    def _compose(self, dt: datetime, body: str) -> str:
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        lines = [HEADER, f"Сейчас {dt:%H:%M}, {days[dt.weekday()]}.", body]
        if self.cfg["character"]:
            lines.append(f"Как заводить разговор: {self.cfg['character']}")
        lines.append(FOOTER)
        return "\n".join(lines)

    async def _tick(self):
        dt = datetime.now()
        self._roll_day(dt)
        reason = self._blocked(dt)
        if reason != self._last_reason:
            self._last_reason = reason
            log.info("состояние планировщика: %s (утро ждёт: %s)", reason or "можно говорить", self._morning_pending)
        if reason:
            return
        picked = self._pick(dt)
        if picked is None:
            return
        kind, body, done = picked
        prompt = self._compose(dt, body)
        if self.cfg["dry_run"]:
            log.info("[пробный режим] сценарий «%s»: %s", kind, prompt.replace("\n", " ⏎ "))
            self._mark_sent(dt, done)
            return
        await self._send(dt, kind, prompt, done)

    def _mark_sent(self, dt: datetime, done):
        done()
        self.state["last_sent"] = dt.timestamp()
        self.state["sent_today"] = self.state.get("sent_today", 0) + 1
        self._save()

    async def _send(self, dt: datetime, kind: str, prompt: str, done):
        self._inflight = True
        counted = False

        async def consume():
            nonlocal counted
            async for chunk in self.host.send_chat_message(prompt, voice_enabled=self.cfg["speak_aloud"]):
                which = chunk.WhichOneof("content")
                if which == "error":
                    raise RuntimeError(chunk.error)
                if not counted:
                    counted = True
                    self._mark_sent(dt, done)
                if which == "done":
                    return

        try:
            log.info("начинаю разговор: сценарий «%s»", kind)
            await asyncio.wait_for(consume(), STREAM_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._retry_after = time.time() + RETRY_BACKOFF
            log.warning("инициатива «%s» не удалась (%s); повтор не раньше чем через %d мин",
                        kind, e, RETRY_BACKOFF // 60)
        finally:
            self._inflight = False
            self._own_until = time.time() + OWN_TURN_TAIL

    # ── инструменты для ассистента ────────────────────────────────────────

    @tool(
        "Приостановить инициативу: Астра перестаёт сама начинать разговор. Вызывай, когда пользователь просит "
        "не писать ему первым: «помолчи час», «не пиши до вечера». duration: «30m», «1h», «1h30m», «90» "
        "(минуты), «until_evening», «until_tomorrow»."
    )
    async def pause_initiative(self, duration: str) -> str:
        now = datetime.now()
        until, label = parse_duration(duration, now)
        self.state["pause_until"] = until.timestamp()
        self._save()
        return f"Молчу {label}: до {until:%H:%M %d.%m}."

    @tool("Снять паузу: Астра снова может сама начинать разговор.")
    async def resume_initiative(self) -> str:
        self.state["pause_until"] = 0
        self._save()
        return "Пауза снята, инициатива включена."

    @tool("Показать состояние инициативы: что включено, сколько инициатив осталось на сегодня, есть ли пауза.")
    async def initiative_status(self) -> str:
        now = datetime.now()
        self._roll_day(now)
        cfg = self.cfg
        names = [
            ("утреннее приветствие", cfg["enable_morning"]),
            ("возврат к теме", cfg["enable_callback"]),
            ("полуночник", cfg["enable_night"]),
            ("долгое молчание", cfg["enable_silence"]),
            ("случайная мысль", cfg["enable_random"]),
        ]
        on = [n for n, v in names if v] or ["ничего не включено"]
        left = max(0, cfg["daily_limit"] - self.state.get("sent_today", 0))
        lines = [f"Включено: {', '.join(on)}."]
        pause = self.state.get("pause_until", 0)
        if pause > now.timestamp():
            lines.append(f"Пауза до {datetime.fromtimestamp(pause):%H:%M %d.%m}.")
        else:
            lines.append("Пауза не стоит.")
        lines.append(f"Сегодня осталось инициатив: {left} из {cfg['daily_limit']}.")
        nxt = self.state.get("last_sent", 0) + cfg["min_interval_min"] * 60
        if nxt > now.timestamp():
            lines.append(f"Следующая не раньше {datetime.fromtimestamp(nxt):%H:%M}.")
        if cfg["quiet_enabled"]:
            lines.append(f"Тихие часы: {cfg['quiet_start']:02d}:00–{cfg['quiet_end']:02d}:00.")
        if cfg["dry_run"]:
            lines.append("Пробный режим: модели ничего не отправляется.")
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


if __name__ == "__main__":
    FirstWord().run()
