"""Тесты плагина First Move.

Запуск: `pytest`. Логика решений проверяется напрямую, с подставленным временем;
инструменты и настройки — через Harness SDK, как их видит демон.
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from astra_plugin_sdk import BadArguments  # noqa: E402
from astra_plugin_sdk.testing import Harness, RecordingHost, fuzz_configs  # noqa: E402

import src.plugin as mod  # noqa: E402
from src.plugin import FirstWord, parse_custom, parse_days, parse_duration  # noqa: E402


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)


def make(**cfg) -> FirstWord:
    p = FirstWord()
    p.cfg = {**mod.DEFAULTS, **cfg}
    p._loaded = True
    p.host = RecordingHost()
    return p


NOON = datetime(2026, 10, 12, 10, 0)  # понедельник, утро, не тихие часы


# ── инструменты и настройки ──────────────────────────────────────────────

def test_tools_are_registered():
    with Harness(FirstWord()) as h:
        assert h.tool_names() == ["initiative_status", "pause_initiative", "remember_topic", "resume_initiative", "trigger_now"]
        h.assert_schema_accepts("pause_initiative", "duration")


def test_pause_resume_status_roundtrip():
    with Harness(FirstWord()) as h:
        assert h.call_tool("pause_initiative", duration="1h").success
        assert "Пауза до" in h.call_tool("initiative_status").json
        assert h.call_tool("resume_initiative").success
        assert "Пауза не стоит" in h.call_tool("initiative_status").json


def test_bad_duration_is_a_bad_argument():
    with Harness(FirstWord()) as h:
        r = h.call_tool("pause_initiative", duration="когда-нибудь")
        assert not r.success and r.code == "BAD_ARGUMENTS"


def test_no_config_the_daemon_can_deliver_crashes_this_plugin():
    with Harness(FirstWord()) as h:
        for payload in fuzz_configs():
            h.set_config(payload)


def test_garbage_config_falls_back_to_defaults():
    p = FirstWord()
    asyncio.run(p.on_config_changed({"daily_limit": "много", "quiet_start": 99, "dry_run": 1, "character": 5}))
    assert p.cfg["daily_limit"] == 4
    assert p.cfg["quiet_start"] == 23
    assert p.cfg["dry_run"] is False
    assert p.cfg["character"] == ""


def test_parse_duration():
    assert parse_duration("90", NOON)[0] == NOON + timedelta(minutes=90)
    assert parse_duration("1h30m", NOON)[0] == NOON + timedelta(minutes=90)
    assert parse_duration("1,5 часа", NOON)[0] == NOON + timedelta(minutes=90)
    assert parse_duration("до вечера", NOON)[0] == NOON.replace(hour=18)
    assert parse_duration("until_evening", NOON.replace(hour=20))[0].hour == 0
    with pytest.raises(BadArguments):
        parse_duration("", NOON)


# ── когда молчать ────────────────────────────────────────────────────────

def test_open_state_is_not_blocked():
    assert make()._blocked(NOON) is None


@pytest.mark.parametrize("setup, reason", [
    (lambda p: setattr(p, "_locked", True), "заблокирован"),
    (lambda p: setattr(p, "_core", "speaking"), "занята"),
    (lambda p: setattr(p, "_core", "listening"), "занята"),
    (lambda p: setattr(p, "_tts_since", time.time()), "говорит"),
    (lambda p: setattr(p, "_last_conv", time.time()), "разговор"),
    (lambda p: setattr(p, "_inflight", True), "собственный"),
    (lambda p: p.state.update(pause_until=time.time() + 600), "пауза"),
    (lambda p: p.state.update(day=NOON.date().isoformat(), sent_today=mod.DEFAULTS["daily_limit"]), "лимит"),
    (lambda p: p.state.update(last_sent=time.time() - 60), "интервал"),
])
def test_blockers(setup, reason):
    p = make()
    setup(p)
    assert reason in (p._blocked(datetime.now()) or "")


def test_quiet_hours_wrap_and_win():
    p = make(quiet_start=23, quiet_end=7)
    assert "тихие" in p._blocked(NOON.replace(hour=2))
    assert p._blocked(NOON.replace(hour=12)) is None
    assert make(quiet_enabled=False)._blocked(NOON.replace(hour=4)) is None


def test_events_drive_the_gates():
    p = make()
    p._handle("lock_state_changed", {"locked": True})
    assert p._locked
    p._handle("state_changed", {"previous_state": "CORE_STATE_READY", "new_state": "CORE_STATE_SPEAKING"})
    assert p._core == "speaking"
    p._handle("state_changed", {"new_state": 1})
    assert p._core == "ready"
    p._handle("tts_started", {})
    assert p._tts_since
    p._handle("tts_interrupted", {})
    assert not p._tts_since


def test_own_turn_does_not_count_as_user_activity():
    p = make()
    p._inflight = True
    p._handle("chat_message", {"role": "assistant", "text": "привет"})
    assert "last_interaction" not in p.state
    p._inflight = False
    p._handle("chat_message", {"role": "assistant", "text": "привет"})
    assert "last_interaction" not in p.state


# ── сценарии ─────────────────────────────────────────────────────────────

def test_morning_fires_once_after_first_activity():
    p = make(enable_silence=False)
    p._handle("lock_state_changed", {"locked": True})
    p._handle("lock_state_changed", {"locked": False})
    p._arm_morning(datetime.now().replace(hour=9))  # разблокировка в 9 утра
    kind, body, done = p._pick(datetime.now().replace(hour=9))
    assert kind == "утро"
    done()
    assert p._pick(datetime.now().replace(hour=9)) is None


def test_morning_ignored_outside_window():
    p = make(enable_silence=False, morning_hour=8, morning_until=12)
    p._arm_morning(NOON.replace(hour=15))
    assert not p._morning_pending


def test_callback_rides_with_the_morning_and_uses_only_yesterday():
    p = make(enable_silence=False)
    now = datetime.now().replace(hour=9, minute=0)
    p.notes = [
        {"ts": (now - timedelta(days=1)).timestamp(), "text": "собеседование в пятницу"},
        {"ts": (now - timedelta(days=3)).timestamp(), "text": "давняя тема"},
    ]
    p._morning_pending = True
    kind, body, done = p._pick(now)
    assert "собеседование" in body and "давняя" not in body
    done()
    assert p.notes[0]["asked"]


def test_callback_alone_when_morning_is_off():
    p = make(enable_morning=False, enable_silence=False)
    now = datetime.now().replace(hour=9)
    p._morning_pending = True
    assert p._pick(now) is None  # нечего вспоминать — и не здороваемся
    assert not p._morning_pending


def test_night_needs_recent_activity_and_fires_once_per_night():
    p = make(enable_morning=False, enable_silence=False, night_hour=1)
    at = datetime.now().replace(hour=1, minute=30)
    assert p._pick(at) is None
    p._last_activity = at.timestamp() - 60
    kind, _, done = p._pick(at)
    assert kind == "ночь"
    done()
    assert p._pick(at) is None


def test_night_window_wraps_midnight():
    p = make(enable_morning=False, enable_silence=False, night_hour=23)
    at = datetime.now().replace(hour=1, minute=0)
    p._last_activity = at.timestamp()
    kind, _, done = p._pick(at)
    assert kind == "ночь"
    done()
    assert p.state["night_done"] == (at - timedelta(days=1)).date().isoformat()


def test_silence_counts_from_the_latest_signal():
    p = make(enable_morning=False, enable_night=False, silence_hours=4)
    now = datetime.now().replace(hour=14)
    t = now.timestamp()
    p._unlock_or_start = t - 5 * 3600
    assert p._pick(now)[0] == "молчание"
    p.state["last_interaction"] = t - 3600
    assert p._pick(now) is None


def test_random_thought_is_off_by_default_and_planned_once():
    assert make(enable_morning=False, enable_night=False, enable_silence=False)._pick(NOON) is None
    p = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=True,
             random_chance_percent=100)
    p._pick(NOON)
    plan = p.state["random"]
    assert plan["ts"] and not plan["done"]
    p._pick(NOON)
    assert p.state["random"] is plan


def test_random_zero_percent_never_plans():
    p = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=True,
             random_chance_percent=0)
    p._pick(NOON)
    assert p.state["random"]["ts"] is None


# ── отправка ─────────────────────────────────────────────────────────────

def test_send_goes_to_own_thread_with_voice_and_counts():
    p = make()
    fired = []

    class Chunk:
        def __init__(self, which, text=""):
            self._w, self.text = which, text

        def WhichOneof(self, _):
            return self._w

    async def stream():
        yield Chunk("text", "доброе утро")
        yield Chunk("done")

    p.host.send_chat_message = lambda text, **kw: (fired.append((text, kw)), stream())[1]
    asyncio.run(p._send(NOON, "утро", p._compose(NOON, "привет"), lambda: fired.append("done")))
    assert fired[0][1] == {"voice_enabled": True}
    assert "Служебная подсказка" in fired[0][0]
    assert fired[1] == "done"
    assert p.state["sent_today"] == 1
    assert not p._inflight


def test_failed_send_is_not_counted_and_backs_off():
    p = make()

    def boom(text, **kw):
        raise RuntimeError("Astra locked")

    p.host.send_chat_message = boom
    asyncio.run(p._send(NOON, "утро", "x", lambda: None))
    assert p.state.get("sent_today", 0) == 0
    assert p._retry_after > time.time()


def test_dry_run_sends_nothing():
    p = make(dry_run=True, enable_night=False, enable_silence=False, quiet_enabled=False,
             morning_hour=0, morning_until=24)
    p._morning_pending = True
    p.host.send_chat_message = lambda *a, **k: pytest.fail("в пробном режиме отправлять нельзя")
    asyncio.run(p._tick())
    assert p.state["sent_today"] == 1  # сценарий сработал, но только в журнал


def test_character_reaches_the_prompt():
    p = make(character="Говори с лёгкой иронией")
    assert "лёгкой иронией" in p._compose(NOON, "тема")


def test_topics_are_written_only_inside_the_data_dir(tmp_path):
    p = make()
    p._handle("speech_recognized", {"text": "завтра у меня важное собеседование", "is_final": True})
    assert (tmp_path / "topics.json").exists()
    p._handle("speech_recognized", {"text": "завтра у меня важное собеседование", "is_final": True})
    assert len(p.notes) == 1  # повтор не плодит заметки
    p._handle("speech_recognized", {"text": "привет", "is_final": True})
    assert len(p.notes) == 1  # короткое — не тема


def test_random_thought_leans_on_old_notes_and_does_not_repeat_them():
    p = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=True)
    old = (NOON - timedelta(days=5)).timestamp()
    p.notes = [{"ts": old, "text": "учит испанский по вечерам"}, {"ts": NOON.timestamp(), "text": "сегодняшнее дело"}]
    p.state["random"] = {"day": NOON.date().isoformat(), "ts": NOON.timestamp() - 1, "done": False}
    kind, body, done = p._pick(NOON)
    assert kind == "мысль" and "испанский" in body and "сегодняшнее" not in body
    done()
    assert p.notes[0]["used"] == NOON.timestamp()
    assert p._old_topics(NOON, 2) == []  # неделю не повторяем


def test_silence_may_use_one_old_note():
    p = make(enable_morning=False, enable_night=False, silence_hours=4)
    now = NOON.replace(hour=14)
    p._unlock_or_start = now.timestamp() - 5 * 3600
    p.notes = [{"ts": (now - timedelta(days=3)).timestamp(), "text": "ждёт ответа от работодателя"}]
    assert "работодателя" in p._pick(now)[1]


# ── v2: свои поводы, выходные, вечер ──────────────────────────────────────

def test_parse_days_and_custom_lines():
    assert parse_days("пн-пт") == {0, 1, 2, 3, 4}
    assert parse_days("сб,вс") == {5, 6}
    assert parse_days("пт-пн") == {4, 5, 6, 0}
    assert parse_days("выходные") == {5, 6}
    items, bad = parse_custom("09:30 выпей воды\nпн-пт 18:00 спроси про проект\n# коммент\nмусор\n25:00 не бывает\nкогда-то 10:00 x")
    assert [i["minute"] for i in items] == [570, 1080]
    assert items[1]["days"] == {0, 1, 2, 3, 4}
    assert bad == 3


def test_custom_prompt_fires_in_its_window_once_a_day():
    p = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=False,
             custom_prompts="пн-пт 09:30 напомни выпить воды")
    mon = datetime(2026, 10, 12, 9, 45)       # понедельник
    assert p._pick(mon.replace(hour=9, minute=0)) is None   # рано
    kind, body, done = p._pick(mon)
    assert kind == "своё" and "выпить воды" in body
    done()
    assert p._pick(mon) is None
    assert p._pick(mon.replace(hour=13)) is None             # окно в 3 часа прошло
    sat = datetime(2026, 10, 17, 9, 45)
    p2 = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=False,
              custom_prompts="пн-пт 09:30 напомни выпить воды")
    assert p2._pick(sat) is None                             # суббота не подходит


def test_weekend_shifts_the_morning_window():
    p = make(morning_hour=8, morning_until=12, weekend_shift_hours=2)
    sat = datetime(2026, 10, 17, 8, 30)
    p._arm_morning(sat)
    assert not p._morning_pending                            # на выходных утро с 10
    p._arm_morning(sat.replace(hour=10, minute=15))
    assert p._morning_pending
    q = make(morning_hour=8, morning_until=12, weekend_shift_hours=2)
    q._arm_morning(datetime(2026, 10, 12, 8, 30))            # понедельник
    assert q._morning_pending


def test_evening_recap_needs_presence_and_fires_once():
    p = make(enable_morning=False, enable_night=False, enable_silence=False, enable_random=False,
             enable_evening=True, evening_hour=21)
    at = datetime(2026, 10, 12, 21, 30)
    assert p._pick(at) is None                                # вас не видно
    p._last_activity = at.timestamp() - 60
    kind, body, done = p._pick(at)
    assert kind == "вечер" and "remember_topic" in body
    done()
    assert p._pick(at) is None


# ── v2: адаптивная частота ───────────────────────────────────────────────

def test_ignored_initiatives_stretch_the_interval_and_a_reply_resets_it():
    p = make(min_interval_min=60)
    clock = [1_000_000.0]
    p.clock = lambda: clock[0]
    dt = lambda: datetime.fromtimestamp(clock[0])
    p.state.update(last_sent=clock[0] - 3600 * 1.5, ignored_streak=0)
    assert "интервал" not in (p._blocked(dt()) or "")        # 90 мин > 60 мин
    p.state["ignored_streak"] = 2                             # ×2 = 120 мин
    assert "интервал" in p._blocked(dt())
    p.state["ignored_streak"] = 99
    assert p._backoff() == 3                                  # потолок ×3
    assert make(adaptive=False)._backoff() == 1
    p.state["pending_reply"] = {"ts": clock[0], "kind": "утро"}
    clock[0] += 120
    p._handle("tts_started", {})                              # Астра ответила на ваш ход
    assert p.state["ignored_streak"] == 0 and p.state["pending_reply"] is None


def test_a_reply_after_the_window_does_not_reset_the_streak():
    p = make()
    clock = [1_000_000.0]
    p.clock = lambda: clock[0]
    p.state.update(ignored_streak=2, pending_reply={"ts": clock[0], "kind": "утро"})
    clock[0] += mod.REPLY_WINDOW + 5
    p._handle("tts_started", {})
    assert p.state["ignored_streak"] == 2


def test_tick_counts_an_unanswered_initiative():
    p = make(min_interval_min=0, after_chat_pause_min=0, quiet_enabled=False)
    clock = [1_000_000.0]
    p.clock = lambda: clock[0]
    p.state.update(pending_reply={"ts": clock[0], "kind": "утро"}, ignored_streak=1)
    clock[0] += mod.REPLY_WINDOW + 5
    asyncio.run(p._tick())
    assert p.state["ignored_streak"] == 2 and p.state["pending_reply"] is None


# ── v2: зачины, trigger_now, статус ──────────────────────────────────────

class Chunk:
    def __init__(self, which, text=""):
        self._w, self.text = which, text

    def WhichOneof(self, _):
        return self._w


def stream_of(*texts):
    async def gen():
        for t in texts:
            yield Chunk("text", t)
        yield Chunk("done")
    return gen()


def test_initiatives_are_remembered_with_her_words_and_fed_back():
    p = make()
    p.host.send_chat_message = lambda text, **kw: stream_of("Доброе утро!", " Какие планы?")
    asyncio.run(p._send(NOON, "утро", "x", lambda: None))
    e = p.state["history"][0]
    assert e["kind"] == "утро" and e["text"] == "Доброе утро! Какие планы?" and e["answered"] is None
    later = NOON + timedelta(hours=3)
    line = p._compose(later, "тема")
    assert "Твои недавние инициативы" in line and "сегодня в 10:00 (утро)" in line
    assert "Какие планы?" in line and "ответа пока нет" in line and "не повторяйся" in line


def test_history_records_whether_the_user_answered():
    p = make()
    clock = [NOON.timestamp()]
    p.clock = lambda: clock[0]
    p.host.send_chat_message = lambda text, **kw: stream_of("Как дела?")
    asyncio.run(p._send(NOON, "молчание", "x", lambda: None))
    p._own_until = 0
    clock[0] += 120
    p._handle("tts_started", {})                                  # вы ответили, Астра заговорила в ответ
    assert p.state["history"][-1]["answered"] is True
    assert "пользователь ответил" in p._compose(NOON + timedelta(hours=1), "т")
    # вторая инициатива остаётся без ответа
    later = NOON + timedelta(hours=2)
    p.host.send_chat_message = lambda text, **kw: stream_of("Что нового?")
    asyncio.run(p._send(later, "мысль", "x", lambda: None))
    clock[0] = later.timestamp() + mod.REPLY_WINDOW + 5
    p.state.update(min_interval_min=0)
    asyncio.run(p._tick(datetime.fromtimestamp(clock[0])))
    assert p.state["history"][-1]["answered"] is False
    assert "ответа не было" in p._compose(datetime.fromtimestamp(clock[0]), "т")


def test_history_is_trimmed_by_count_and_age_and_shows_only_the_latest():
    p = make()
    for i in range(mod.HISTORY_KEEP + 5):
        p._remember_initiative(NOON + timedelta(minutes=i), "мысль", f"реплика {i}")
    assert len(p.state["history"]) == mod.HISTORY_KEEP
    p._remember_initiative(NOON + timedelta(days=mod.HISTORY_DAYS + 1), "утро", "новая")
    assert [e["text"] for e in p.state["history"]] == ["новая"]
    p2 = make()
    for i in range(10):
        p2._remember_initiative(NOON + timedelta(minutes=i), "мысль", f"реплика {i}")
    line = p2._history_line(NOON + timedelta(hours=1))
    assert "реплика 9" in line and "реплика 6" in line and "реплика 5" not in line


def test_long_replies_are_cut_and_empty_ones_are_not_remembered():
    p = make()
    p.host.send_chat_message = lambda text, **kw: stream_of("а" * 1000)
    asyncio.run(p._send(NOON, "утро", "x", lambda: None))
    assert len(p.state["history"][0]["text"]) == mod.HISTORY_TEXT
    q = make()
    q.host.send_chat_message = lambda text, **kw: stream_of()
    asyncio.run(q._send(NOON, "утро", "x", lambda: None))
    assert not q.state.get("history")


def test_a_failed_send_leaves_no_history():
    p = make()

    def boom(text, **kw):
        raise RuntimeError("locked")

    p.host.send_chat_message = boom
    asyncio.run(p._send(NOON, "утро", "x", lambda: None))
    assert not p.state.get("history")


def test_pending_reply_is_armed_by_a_real_send_but_not_by_dry_run():
    p = make()
    p.host.send_chat_message = lambda text, **kw: stream_of("привет")
    asyncio.run(p._send(NOON, "утро", "x", lambda: None))
    assert p.state["pending_reply"]["kind"] == "утро"
    q = make(dry_run=True, enable_night=False, enable_silence=False, quiet_enabled=False,
             morning_hour=0, morning_until=24)
    q._morning_pending = True
    asyncio.run(q._tick(NOON))
    assert q.state.get("pending_reply") is None and q.state["log"][0]["dry"] is True
    assert not q.state.get("history")                           # пробный режим историю не пишет


def test_trigger_now_sends_in_the_background_and_respects_the_limit():
    p = make(daily_limit=1, min_interval_min=999, after_chat_pause_min=999)
    sent = []
    p.host.send_chat_message = lambda text, **kw: (sent.append(text), stream_of("привет"))[1]

    async def go():
        p._core, p._last_conv = "processing", time.time()    # идёт ход, который нас вызвал
        msg = await p.trigger_now("morning")
        assert "Запускаю" in msg
        await asyncio.gather(*p._bg)
        return await p.trigger_now("evening")

    assert "лимит" in asyncio.run(go())
    assert len(sent) == 1 and "Служебная подсказка" in sent[0]


def test_trigger_now_refuses_while_locked_and_rejects_unknown_scenarios():
    p = make()
    p._locked = True
    assert "заблокирован" in asyncio.run(p.trigger_now("night"))
    with pytest.raises(BadArguments):
        asyncio.run(p.trigger_now("полночь-не-та"))


def test_status_explains_the_silence_and_the_day():
    p = make(daily_limit=2)
    p._roll_day(NOON)
    p.state["log"] = [{"ts": NOON.timestamp(), "kind": "утро", "dry": False}]
    p.state["sent_today"] = 1
    p.state["ignored_streak"] = 2
    out = asyncio.run(p.initiative_status())
    assert "утро в 10:00" in out and "1 из 2" in out and "в 2 раза" in out and "Прямо сейчас" in out
    assert "блокировке экрана" in out
    p._handle("lock_state_changed", {"locked": False})
    assert "блокировке экрана" not in asyncio.run(p.initiative_status())


def test_every_prompt_asks_astra_to_note_follow_up_topics():
    assert "remember_topic" in make()._compose(NOON, "тема")


# ── v2: сутки целиком на подставных часах ────────────────────────────────

def test_a_whole_day_on_a_fake_clock():
    """Пользователь живёт обычным днём; плагин не превышает лимит, не говорит ночью и не перебивает."""
    p = make(daily_limit=3, min_interval_min=90, silence_hours=3, enable_random=False, enable_night=True,
             quiet_start=3, quiet_end=8, after_chat_pause_min=3, adaptive=False)
    clock = [datetime(2026, 10, 12, 0, 30).timestamp()]       # понедельник, полночь
    p.clock = lambda: clock[0]
    sent = []

    def fake_send(text, **kw):
        sent.append((datetime.fromtimestamp(clock[0]).strftime("%H:%M"), text))
        return stream_of("ответ")

    p.host.send_chat_message = fake_send

    def at(h, m=0):
        clock[0] = datetime(2026, 10, 12, h, m).timestamp()

    async def day():
        # 00:30 вы активны, но ещё не «ночь» (окно с 1:00)
        p._handle("tts_started", {}); p._handle("tts_completed", {})
        await p._tick()
        # 01:30 активны ночью: ночное предложение
        at(1, 30); p._handle("tts_started", {}); p._handle("tts_completed", {})
        at(1, 36); await p._tick()
        # 03:30 тихие часы: молчим, хотя вы активны
        at(3, 30); p._handle("tts_started", {}); p._handle("tts_completed", {})
        at(3, 40); await p._tick()
        # 08:15 разблокировка: утро
        at(8, 15); p._locked = True; p._handle("lock_state_changed", {"locked": False})
        await p._tick()
        # 08:16 идёт разговор: молчим
        p._handle("tts_started", {})
        at(8, 16); await p._tick()
        p._handle("tts_completed", {})
        # 13:00 вы молчали 3+ часа: молчание
        at(13, 0); await p._tick()
        # 13:10 интервал не прошёл
        at(13, 10); await p._tick()
        # 15:00 экран заблокирован: молчим
        at(15, 0); p._handle("lock_state_changed", {"locked": True}); await p._tick()
        # 22:00 разблокировали, давно не говорили: молчание, но лимит ещё есть (3-й)
        at(22, 0); p._handle("lock_state_changed", {"locked": False})
        at(22, 5); await p._tick()
        # 23:50 лимит исчерпан
        at(23, 50); await p._tick()

    asyncio.run(day())
    log_ = [(e["kind"], datetime.fromtimestamp(e["ts"]).strftime("%H:%M")) for e in p.state["log"]]
    # ночью один раз, тихие часы молчат, утро после разблокировки, молчание в 13:00;
    # 08:16 (идёт разговор), 13:10 (интервал), 15:00 (блокировка) и 22:05 (лимит 3) пропущены
    assert log_ == [("ночь", "01:36"), ("утро", "08:15"), ("молчание", "13:00")]
    assert [t for t, _ in sent] == ["01:36", "08:15", "13:00"]
    assert p.state["sent_today"] == 3


# ── v2: взгляд на экран ──────────────────────────────────────────────────

def test_screen_look_is_off_by_default():
    p = make()
    for kind in ("ночь", "молчание", "мысль", "вечер", "своё", "утро"):
        assert "снимок экрана" not in p._compose(NOON, "тема", kind)


def test_screen_look_covers_every_scenario_with_style_and_privacy():
    p = make(look_at_screen=True, screen_privacy="Не называй банки.", screen_style="Подколи меня.")
    for kind in ("утро", "ночь", "молчание", "мысль", "вечер", "своё"):
        out = p._compose(NOON, "тема", kind)
        assert "снимок экрана" in out and "Подколи меня." in out and "Не называй банки." in out and "без него" in out
    assert "снимок экрана" not in p._compose(NOON, "тема")            # без вида сценария — не добавляем


def test_default_style_is_a_friendly_tease():
    out = make(look_at_screen=True)._compose(NOON, "тема", "утро")
    assert "что ты опять смотришь" in out and "Без нравоучений" in out


def test_screen_look_reaches_the_morning_dry_run_prompt(caplog):
    import logging
    p = make(look_at_screen=True, dry_run=True, enable_night=False, enable_silence=False, enable_random=False,
             quiet_enabled=False, min_interval_min=0, after_chat_pause_min=0, morning_hour=0, morning_until=24)
    p._morning_pending = True
    with caplog.at_level(logging.INFO, logger="first-word"):
        asyncio.run(p._tick(NOON))
    assert any("снимок экрана" in r.getMessage() and "«утро»" in r.getMessage() for r in caplog.records)


def test_screen_text_config_is_clamped():
    p = FirstWord()
    asyncio.run(p.on_config_changed({"screen_privacy": "я" * 5000, "screen_style": "ы" * 5000, "look_at_screen": "да"}))
    assert len(p.cfg["screen_privacy"]) == 400 and len(p.cfg["screen_style"]) == 400
    assert p.cfg["look_at_screen"] is False
