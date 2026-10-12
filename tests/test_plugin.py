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
    p._apps_fn = lambda: ([], "")                             # настоящий список приложений в тестах не читаем
    return p


NOON = datetime(2026, 10, 12, 10, 0)  # понедельник, утро, не тихие часы


# ── инструменты и настройки ──────────────────────────────────────────────

def test_tools_are_registered():
    with Harness(FirstWord()) as h:
        assert h.tool_names() == ["check_presence", "initiative_status", "pause_initiative", "remember_topic", "resume_initiative", "trigger_now"]
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
    assert p.cfg["character"] == mod.PERSONA


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
    assert "Твоя личность и манера общения, сохраняй их" in p._compose(NOON, "тема")


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
        assert "СНАЧАЛА посмотри на экран" not in p._compose(NOON, "тема", kind)








def test_screen_text_config_is_clamped():
    p = FirstWord()
    asyncio.run(p.on_config_changed({"screen_privacy": "я" * 5000, "screen_style": "ы" * 5000, "look_at_screen": "да"}))
    assert len(p.cfg["screen_privacy"]) == 400 and len(p.cfg["screen_style"]) == 400
    assert p.cfg["look_at_screen"] is True                       # мусор вместо булевого значения: берём значение по умолчанию


# ── 0.1.2: проверка присутствия ──────────────────────────────────────────

import os  # noqa: E402
import struct  # noqa: E402

from src import presence  # noqa: E402


class FakeBackend(presence.Backend):
    name = "fake"

    def __init__(self):
        self.token = (100, 100)
        self.calls = 0
        self.fail = False

    def sample(self):
        self.calls += 1
        return None if self.fail else self.token


def presence_plugin(**cfg):
    """Плагин, который хочет заговорить прямо сейчас (утро), на подставных часах."""
    base = dict(enable_night=False, enable_silence=False, enable_random=False, quiet_enabled=False,
                min_interval_min=0, after_chat_pause_min=0, morning_hour=0, morning_until=24, presence_check=True)
    p = make(**{**base, **cfg})
    p._morning_pending = True
    p.backend_ = FakeBackend()
    p._backend = p.backend_
    clock = [datetime(2026, 10, 12, 10, 0).timestamp()]
    p.clock = lambda: clock[0]
    sent = []
    p.host.send_chat_message = lambda text, **kw: (sent.append(text), stream_of("привет"))[1]
    p._last_activity = 0.0
    return p, clock, sent


def test_presence_backs_off_5_10_20_to_300_then_speaks_when_the_mouse_moves():
    p, clock, sent = presence_plugin()
    delays = []

    async def go():
        for _ in range(12):
            d = await p._tick()
            delays.append(d)
            clock[0] += d or 1
    asyncio.run(go())
    assert delays[:9] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
    assert not sent                                               # никого нет: молчим
    assert "не видно" in asyncio.run(p.initiative_status())
    p.backend_.token = (500, 321)                                 # мышь двинулась
    clock[0] += 300
    assert asyncio.run(p._tick()) is None and len(sent) == 1       # на ближайшей проверке заговорила


def test_a_baseline_is_taken_first_and_a_move_after_it_wins_immediately():
    p, clock, sent = presence_plugin()
    assert asyncio.run(p._tick()) == 5.0 and not sent
    p.backend_.token = (1, 2)
    clock[0] += 5
    assert asyncio.run(p._tick()) is None and len(sent) == 1


def test_the_interval_is_configurable_and_capped_by_the_max():
    p, clock, sent = presence_plugin(presence_check_sec=2, presence_max_sec=10)
    out = []
    for _ in range(6):
        d = asyncio.run(p._tick())
        out.append(d)
        clock[0] += d
    assert out == [2.0, 4.0, 8.0, 10.0, 10.0, 10.0]


def test_recent_activity_skips_the_mouse_check():
    p, clock, sent = presence_plugin()
    p._last_activity = clock[0] - 30                              # только что разговаривали или разблокировали
    assert asyncio.run(p._tick()) is None and len(sent) == 1
    assert p.backend_.calls == 0


def test_presence_check_can_be_switched_off_and_a_missing_backend_does_not_block():
    p, clock, sent = presence_plugin(presence_check=False)
    asyncio.run(p._tick())
    assert len(sent) == 1
    q, _, sent_q = presence_plugin()
    q._backend, q._backend_note = None, "нет способа"
    asyncio.run(q._tick())
    assert len(sent_q) == 1
    assert "отключена: нет способа" in asyncio.run(q.initiative_status())


def test_a_failing_sample_lets_the_initiative_through_instead_of_blocking_forever():
    p, clock, sent = presence_plugin()
    p.backend_.fail = True
    asyncio.run(p._tick())
    assert len(sent) == 1


def test_a_crashing_backend_is_dropped():
    p, clock, sent = presence_plugin()

    class Boom(presence.Backend):
        def sample(self):
            raise RuntimeError("x connection lost")

    p._backend = Boom()
    asyncio.run(p._tick())
    assert p._backend is None and len(sent) == 1


def test_waiting_state_is_reset_when_the_initiative_is_no_longer_wanted():
    p, clock, sent = presence_plugin()
    asyncio.run(p._tick())
    clock[0] += 5
    asyncio.run(p._tick())
    assert p._pw["interval"] == 10.0
    p.state["pause_until"] = clock[0] + 3600                       # попросили помолчать
    assert asyncio.run(p._tick()) is None
    assert p._pw["base"] is None and p._pw["interval"] is None


def test_trigger_now_ignores_presence():
    p, clock, sent = presence_plugin()

    async def go():
        msg = await p.trigger_now("morning")
        await asyncio.gather(*p._bg)
        return msg

    assert "Запускаю" in asyncio.run(go()) and len(sent) == 1 and p.backend_.calls == 0


def test_check_presence_tool_reports_motion(monkeypatch):
    p, clock, _ = presence_plugin()

    async def fast_sleep(_):
        p.backend_.token = (9, 9)                                  # пока «спали», мышь двинулась

    monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)
    assert "были" in asyncio.run(p.check_presence())
    p._backend, p._backend_note = None, "Wayland без input"
    assert "недоступна: Wayland без input" in asyncio.run(p.check_presence())


# ── 0.1.2: бэкенды присутствия ───────────────────────────────────────────

def _ev(etype, code=0, value=0):
    return struct.pack("llHHi", 0, 0, etype, code, value)


def test_evdev_counts_motion_and_presses_but_not_noise(tmp_path):
    r, w = os.pipe()
    os.set_blocking(r, False)
    be = presence.EvdevBackend([])
    be._fds[r] = "pipe"
    be._touchpad[r] = False
    assert be.sample() == 0
    os.write(w, _ev(presence.EV_REL, 0, 5) + _ev(presence.EV_KEY, 30, 1))   # сдвиг мыши, нажатие
    os.write(w, _ev(0, 0, 0) + _ev(4, 4, 30) + _ev(presence.EV_KEY, 30, 0)   # SYN, MSC, отпускание
             + _ev(presence.EV_ABS, 0, 7))                                    # ABS не от тачпада
    assert be.sample() == 2
    assert be.sample() == 2                                                  # ничего нового
    be._touchpad[r] = True
    os.write(w, _ev(presence.EV_ABS, 0, 7))
    assert be.sample() == 3                                                  # тачпад считается
    be.close()
    os.close(w)


def test_evdev_drops_a_device_that_disappears():
    r, w = os.pipe()
    be = presence.EvdevBackend([])
    be._fds[r], be._touchpad[r] = "pipe", False
    os.set_blocking(r, False)
    os.close(w)                                                              # устройство «отвалилось»
    assert be.sample() in (None, 0)
    be.close()


def test_detect_prefers_evdev_when_the_input_devices_are_readable(tmp_path):
    dev = tmp_path / "event0"
    dev.write_bytes(b"")
    be, note = presence.detect(env={}, platform="linux", evdev_paths=[str(dev)])
    assert be.name == "evdev" and "1 устройств" in note
    be.close()


def test_detect_on_wayland_without_input_access_switches_the_check_off_with_advice(tmp_path):
    be, note = presence.detect(env={"XDG_SESSION_TYPE": "wayland"}, platform="linux", evdev_paths=[])
    assert be is None and "usermod -aG input" in note


def test_wayland_is_recognised_without_env_by_its_socket(tmp_path):
    (tmp_path / "wayland-0").write_text("")
    assert presence.is_wayland({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert not presence.is_wayland({"XDG_RUNTIME_DIR": str(tmp_path / "none")})
    assert presence.is_wayland({"WAYLAND_DISPLAY": "wayland-1"})
    assert not presence.is_wayland({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-1"})


def test_unsupported_platform_is_reported():
    be, note = presence.detect(env={}, platform="darwin")
    assert be is None and "darwin" in note


def test_windows_backend_uses_last_input_and_falls_back_to_the_cursor():
    import ctypes

    class User32:
        def __init__(self):
            self.last = 1000
            self.last_ok = True

        def GetLastInputInfo(self, ref):
            if not self.last_ok:
                return 0
            ref._obj.dwTime = self.last
            return 1

        def GetCursorPos(self, ref):
            ref._obj.x, ref._obj.y = 640, 480
            return 1

    u = User32()
    be = presence.WindowsBackend(user32=u)
    assert be.sample() == 1000
    u.last = 2500                                                            # было нажатие или движение
    assert be.sample() == 2500
    u.last_ok = False
    assert be.sample() == (640, 480)                                         # запасной путь: курсор
    be2, note = presence.detect(env={}, platform="win32")
    assert be2 is None or be2.name == "windows"                              # на Linux user32 нет: честный отказ


def test_a_real_backend_is_found_on_this_machine_or_explained():
    be, note = presence.detect()
    assert (be is not None) or note
    if be:
        be.close()


# ── 0.1.2: тест из настроек ──────────────────────────────────────────────

def plugin_in_test_mode(**cfg):
    """Плагин с включённым ползунком «Тест» на подставных часах (10:00), отправки пишутся в `sent`."""
    p = make(**{**dict(test_mode=True, test_gap_sec=5, quiet_enabled=True, quiet_start=3, quiet_end=8,
                       daily_limit=1, presence_check=True), **cfg})
    p._task = "off"                                           # фоновый цикл не запускаем: тики вызываем сами
    p._apps_fn = lambda: ([], "")                             # настоящий список приложений в тестах не читаем
    p.backend_ = FakeBackend()
    p.backend_.token = (1, 1)
    p._backend = p.backend_                                   # «вас нет»: тест проверку присутствия обходит
    clock = [datetime(2026, 10, 12, 10, 0).timestamp()]
    p.clock = lambda: clock[0]
    sent = []
    p.host.send_chat_message = lambda text, **kw: (sent.append(text), stream_of("тест"))[1]
    return p, clock, sent


async def _no_sleep(_):
    return None


def _run_ticks(p, clock, n=1, step=20):
    async def go():
        for _ in range(n):
            await p._tick()
            await asyncio.gather(*list(p._bg))
            clock[0] += step
    asyncio.run(go())


def test_test_mode_with_no_time_runs_at_once_and_only_the_chosen_events(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True, test_night=True, test_evening=False)
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert len(sent) == 2
    assert "утро" in sent[0] and "поздняя ночь" in sent[1]
    assert all("Служебная подсказка" in s_ for s_ in sent)


def test_a_test_does_not_touch_real_counters_history_or_the_morning_flag(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True, test_random=True, daily_limit=1)
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert len(sent) == 2                                     # лимит 1, а тест прошёл оба события
    assert p.state.get("sent_today", 0) == 0 and not p.state.get("history") and not p.state.get("log")
    assert p.state.get("morning_done") is None and not p.state.get("last_sent")
    assert p.state.get("pending_reply") is None
    assert not any(n.get("used") for n in p.notes)


def test_a_test_runs_once_and_again_after_the_slider_is_toggled(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True)
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock, n=3)
    assert len(sent) == 1                                     # повторно сам не запускается
    assert "уже выполнен" in asyncio.run(p.initiative_status())
    asyncio.run(p.on_config_changed({**p.cfg, "test_mode": False}))
    assert p.state.get("test_done") is None and p._test is None
    asyncio.run(p.on_config_changed({**p.cfg, "test_mode": True}))
    _run_ticks(p, clock)
    assert len(sent) == 2


def test_a_test_survives_a_restart_without_rerunning(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True)
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert (tmp_path / "state.json").exists()
    q, clock2, sent2 = plugin_in_test_mode(test_morning=True)
    q.state = dict(p.state)                                   # состояние читается с диска
    asyncio.run(q.on_config_changed(q.cfg))
    _run_ticks(q, clock2)
    assert not sent2


def test_the_test_waits_for_its_time_and_goes_to_tomorrow_if_the_time_has_passed(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_time="10:05")
    asyncio.run(p.on_config_changed(p.cfg))
    assert p._test["target"] == datetime(2026, 10, 12, 10, 5).timestamp()
    _run_ticks(p, clock, n=10, step=20)                       # 10:00..10:03
    assert not sent and "запланирован на 12.10 10:05" in asyncio.run(p.initiative_status())
    clock[0] = datetime(2026, 10, 12, 10, 5, 10).timestamp()
    _run_ticks(p, clock)
    assert len(sent) == 1
    q, clock_q, sent_q = plugin_in_test_mode(test_time="09:00")       # 9:00 уже прошло
    asyncio.run(q.on_config_changed(q.cfg))
    assert q._test["target"] == datetime(2026, 10, 13, 9, 0).timestamp()


def test_bad_time_or_nothing_selected_never_fires_and_says_why(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_time="25:99")
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert not sent and "нужен формат ЧЧ:ММ" in asyncio.run(p.initiative_status())
    q, clock_q, sent_q = plugin_in_test_mode(test_morning=False)
    asyncio.run(q.on_config_changed(q.cfg))
    _run_ticks(q, clock_q)
    assert not sent_q and "ни одно событие не выбрано" in asyncio.run(q.initiative_status())


def test_the_test_ignores_quiet_hours_limits_and_presence_but_not_a_locked_screen(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True, daily_limit=0, presence_check=True)
    clock[0] = datetime(2026, 10, 12, 4, 0).timestamp()       # тихие часы, лимит 0, вас «нет»
    p._locked = True
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert not sent                                           # экран заблокирован: ждём
    p._locked = False
    _run_ticks(p, clock)
    assert len(sent) == 1


def test_the_test_blocks_real_initiatives_while_it_runs_and_shows_in_status(monkeypatch):
    p, clock, sent = plugin_in_test_mode()
    p._test_running = True
    assert "идёт тест" in p._blocked(datetime.fromtimestamp(clock[0]))
    asyncio.run(p.on_config_changed(p.cfg))
    assert "идёт прямо сейчас" in asyncio.run(p.initiative_status())


def test_dry_run_test_only_writes_prompts_to_the_log(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(dry_run=True, test_morning=True, test_silence=True, look_at_screen=True,
                                         screen_tool="screen_capture")
    asyncio.run(p.on_config_changed(p.cfg))
    with caplog.at_level(logging.INFO, logger="first-word"):
        _run_ticks(p, clock)
    msgs = [r.getMessage() for r in caplog.records if "[пробный режим] тест" in r.getMessage()]
    assert not sent and len(msgs) == 2 and "СНАЧАЛА посмотри на экран" in msgs[0] and "«screen_capture»" in msgs[0]


def test_custom_prompts_are_tested_without_regard_to_their_time(monkeypatch):
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=False, test_custom=True,
                                 custom_prompts="23:00 первое\nпн-пт 07:00 второе\n12:00 третье\n12:30 четвёртое")
    asyncio.run(p.on_config_changed(p.cfg))
    _run_ticks(p, clock)
    assert len(sent) == 3 and "первое" in sent[0] and "третье" in sent[2]
    assert not p.state.get("custom_done")


def test_the_gap_between_events_and_the_wait_for_speech_are_used(monkeypatch):
    sleeps = []

    async def rec_sleep(sec):
        sleeps.append(sec)
        p._tts_since = 0.0                                    # «договорила»

    monkeypatch.setattr(mod.asyncio, "sleep", rec_sleep)
    p, clock, sent = plugin_in_test_mode(test_morning=True, test_evening=True, test_gap_sec=45)
    asyncio.run(p.on_config_changed(p.cfg))
    p._tts_since = clock[0]                                   # Астра говорит
    _run_ticks(p, clock)
    assert len(sent) == 2 and 45 in sleeps and 2 in sleeps


# ── настройки: схема в plugin.toml не расходится с кодом ─────────────────

def _schema():
    import json
    import tomllib
    toml = tomllib.loads((Path(__file__).resolve().parent.parent / "plugin.toml").read_text(encoding="utf-8"))
    return json.loads(toml["config"]["schema"])["properties"]






# ── 0.1.2: прямой приказ смотреть на экран и имя инструмента ─────────────





def test_the_tool_name_is_learned_from_a_started_tool_and_settings_win():
    p = make(look_at_screen=True)
    for payload, expected in [
        ({"name": "screen_capture"}, "screen_capture"),
        ({"tool_name": "get_screenshot"}, "get_screenshot"),
        ({"tool": "Сделать_скриншот"}, "Сделать_скриншот"),
        ({"tool": {"name": "desktop_screenshot"}}, "desktop_screenshot"),
    ]:
        p.state.pop("screen_tool_seen", None)
        p._handle("tool_started", payload)
        assert p.state["screen_tool_seen"] == expected, payload
    assert "«desktop_screenshot»" in p._compose(NOON, "т", "ночь")
    p.cfg["screen_tool"] = "мой_инструмент"
    assert "«мой_инструмент»" in p._compose(NOON, "т", "ночь") and "desktop_screenshot" not in p._compose(NOON, "т", "ночь")


def test_unrelated_tools_and_our_own_are_not_taken_for_the_screen_tool():
    p = make(look_at_screen=True)
    for name in ("web_search", "pause_initiative", "trigger_now", "check_presence", "remember_topic", "x" * 200):
        p._handle("tool_started", {"name": name})
    p._handle("tool_started", {"arguments": "скриншот экрана"})         # в аргументах не ищем
    assert "screen_tool_seen" not in p.state


def test_first_tool_event_is_logged_by_keys_only_so_arguments_do_not_leak(caplog):
    import logging
    p = make()
    with caplog.at_level(logging.INFO, logger="first-word"):
        p._handle("tool_started", {"name": "web_search", "arguments": {"q": "мой секретный запрос"}})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "первое событие tool_started" in text and "arguments" in text and "секретный" not in text




def test_the_manifest_asks_for_tool_started_and_the_code_subscribes_to_it():
    import tomllib
    toml = tomllib.loads((Path(__file__).resolve().parent.parent / "plugin.toml").read_text(encoding="utf-8"))
    types = toml["permissions"]["subscribe_events"]["types"]
    assert "tool_started" in types
    assert set(FirstWord().subscribed_events()) <= set(types)             # кодом не просим больше, чем в манифесте
    assert len(toml["permissions"]["subscribe_events"]["reason"]) <= 140


# ── 0.1.2: приказ смотреть на экран только при известном инструменте, иначе список приложений ──

ALL_KINDS = ("утро", "ночь", "молчание", "мысль", "вечер", "своё")


def with_apps(p, labels=("браузер", "Steam", "Discord"), foreground=""):
    p.cfg["apps_hint_percent"] = 100                                                  # в тестах намёк всегда есть
    p._apps_cache = (p.clock(), list(labels), foreground)
    return p


def test_screen_order_is_given_only_when_the_tool_name_is_known():
    p = make(look_at_screen=True, screen_tool="screen_capture", screen_privacy="Не называй банки.",
             screen_style="Подколи меня.")
    for kind in ALL_KINDS:
        out = p._compose(NOON, "тема", kind)
        assert "СНАЧАЛА посмотри на экран" in out and "«screen_capture»" in out
        assert "Это обязательно, не отвечай до вызова" in out and "дождись результата" in out
        assert "Подколи меня." in out and "Не называй банки." in out
        assert "если у тебя есть такая возможность" not in out
    assert "СНАЧАЛА посмотри на экран" not in p._compose(NOON, "тема")                # вид сценария не задан
    assert "СНАЧАЛА посмотри на экран" not in make(look_at_screen=False, look_at_apps=False, screen_tool="x")._compose(NOON, "т", "утро")   # выключено












def test_apps_are_refreshed_in_the_background_cached_and_failures_are_survived():
    p = make(look_at_apps=True)
    calls = []
    p._apps_fn = lambda: (calls.append(1), (["Discord"], "терминал"))[1]
    asyncio.run(p._refresh_apps())
    assert p._apps_cache[1:] == (["Discord"], "терминал") and len(calls) == 1
    asyncio.run(p._refresh_apps())
    assert len(calls) == 1                                                            # кэш на 30 секунд
    p.clock = lambda: time.time() + mod.APPS_CACHE_SECONDS + 1
    asyncio.run(p._refresh_apps())
    assert len(calls) == 2

    def boom():
        raise OSError("нет /proc")

    q = make(look_at_apps=True)
    q._apps_fn = boom
    asyncio.run(q._refresh_apps())
    assert q._apps_cache[1:] == ([], "")
    r = make(look_at_screen=False, look_at_apps=False)                                                                    # выключено: не ходим за списком
    r._apps_fn = lambda: pytest.fail("список не нужен")
    asyncio.run(r._refresh_apps())






def test_the_real_apps_module_finds_known_programs_and_nothing_else():
    from src import apps as apps_mod
    assert apps_mod.describe({"firefox", "konsole", "unknown-tool", "steam", "bash", "spotify"}) == \
        ["Steam", "браузер", "терминал", "Spotify"]
    assert apps_mod.describe({"keepassxc", "thunderbird", "mullvad-vpn"}) == []        # личное и неизвестное не называем
    assert apps_mod.label_for("Code") == "редактор кода" and apps_mod.label_for("dota2") == "игра"
    assert len(apps_mod.describe({"steam", "discord", "firefox", "code", "konsole", "spotify", "vlc", "obs"})) == apps_mod.MAX_LABELS
    labels, fg = apps_mod.snapshot("darwin")
    assert (labels, fg) == ([], "")


def test_windows_tasklist_output_is_parsed():
    from src import apps as apps_mod
    out = '"System Idle Process","0","Services","0","8 K"\n"chrome.exe","1234","Console","1","200 000 K"\n"Discord.exe","99","Console","1","90 000 K"\n'
    assert apps_mod.parse_tasklist(out) == {"system idle process", "chrome", "discord"}
    assert apps_mod.describe(apps_mod.parse_tasklist(out)) == ["браузер", "Discord"]


def test_the_linux_scan_reads_only_own_processes_and_returns_names(monkeypatch, tmp_path):
    from src import apps as apps_mod
    proc = tmp_path / "proc"
    for pid, comm, cmd, uid in [("100", "firefox", "/usr/lib/firefox/firefox\0-new-tab", os.getuid()),
                                ("101", "Discord", "/opt/discord/Discord", os.getuid()),
                                ("102", "chrome", "/opt/google/chrome", os.getuid() + 1)]:
        d = proc / pid
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(cmd.encode())
        if uid == os.getuid():
            continue
    real_stat, real_listdir = os.stat, os.listdir
    monkeypatch.setattr(apps_mod.os, "listdir", lambda p: real_listdir(proc) if p == "/proc" else real_listdir(p))
    monkeypatch.setattr(apps_mod.os, "stat", lambda p, *a, **k: type("S", (), {"st_uid": os.getuid() + 1 if "/102" in str(p) else os.getuid()})()
                        if str(p).startswith("/proc/") else real_stat(p, *a, **k))
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(str(p).replace("/proc/", str(proc) + "/"), *a, **k)
                        if str(p).startswith("/proc/") else real_open(p, *a, **k))
    names = apps_mod._linux_names()
    assert {"firefox", "discord"} <= names and "chrome" not in names                   # чужой процесс не читаем


def test_without_a_tool_name_no_order_is_sent_but_one_app_hint_is_given_instead_of_a_list():
    p = with_apps(make(look_at_screen=True))
    out = p._compose(NOON, "тема", "утро")
    assert "СНАЧАЛА" not in out and "вызови инструмент снимка экрана" not in out
    assert "Необязательный намёк" in out
    mentioned = [a for a in ("браузер", "Steam", "Discord") if f"запущено: {a}." in out]
    assert len(mentioned) == 1                                                        # одно приложение, не список


def test_the_hint_never_enumerates_and_forbids_enumerating():
    for _ in range(30):                                                               # случайный выбор: проверяем много раз
        out = with_apps(make(look_at_apps=True))._compose(NOON, "тема", "ночь")
        assert out.count("запущено:") == 1
        hinted = out.split("запущено:")[1].split(".")[0]
        assert "," not in hinted and sum(a in hinted for a in ("браузер", "Steam", "Discord")) == 1
        assert "НИКОГДА не перечисляй программы" in out
        assert "Не говори, что получил это от плагина" in out


def test_the_foreground_app_is_preferred_and_a_style_about_looking_is_not_added_to_an_app_hint():
    p = with_apps(make(look_at_apps=True, screen_style="Спроси, что ты опять смотришь.", screen_privacy="Не называй банки."),
                  foreground="браузер")
    out = p._compose(NOON, "тема", "ночь")
    assert "запущено: браузер." in out and "Не называй банки." in out
    assert "что ты опять смотришь" not in out                                         # тон про экран нужен только снимку


def test_the_style_is_kept_for_a_real_screenshot_tool():
    out = make(look_at_screen=True, screen_tool="screen_capture", screen_style="Подколи меня.")._compose(NOON, "т", "утро")
    assert "«screen_capture»" in out and "Подколи меня." in out


def test_the_hint_appears_only_as_often_as_configured_and_never_with_nothing_to_say():
    import random as rnd
    hits = 0
    for seed in range(400):
        rnd.seed(seed)
        p = with_apps(make(look_at_apps=True))
        p.cfg["apps_hint_percent"] = 50
        hits += "Необязательный намёк" in p._compose(NOON, "т", "утро")
    assert 140 < hits < 260                                                           # около половины
    p = with_apps(make(look_at_apps=True))
    p.cfg["apps_hint_percent"] = 0
    assert "Необязательный намёк" not in p._compose(NOON, "т", "утро")
    assert "Необязательный намёк" not in with_apps(make(look_at_apps=True), labels=())._compose(NOON, "т", "утро")
    assert "Необязательный намёк" not in with_apps(make(look_at_screen=False, look_at_apps=False))._compose(NOON, "т", "утро")  # выключено
    assert "Необязательный намёк" not in with_apps(make(look_at_apps=True))._compose(NOON, "т")  # вид не задан


def test_tool_and_apps_hint_can_work_together():
    out = with_apps(make(look_at_screen=True, look_at_apps=True, screen_tool="screen_capture"))._compose(NOON, "т", "утро")
    assert "«screen_capture»" in out and "Необязательный намёк" in out


def test_a_known_tool_alone_does_not_add_an_apps_hint():
    out = with_apps(make(look_at_screen=True, look_at_apps=False, screen_tool="screen_capture"))._compose(NOON, "т", "утро")
    assert "«screen_capture»" in out and "Необязательный намёк" not in out


def test_a_real_initiative_fetches_the_apps_before_it_composes_the_prompt():
    p, clock, sent = presence_plugin(look_at_apps=True, presence_check=False, apps_hint_percent=100)
    p._apps_fn = lambda: (["Steam"], "")
    asyncio.run(p._tick())
    assert len(sent) == 1 and "запущено: Steam." in sent[0]


def test_status_explains_what_astra_is_given_about_the_screen():
    p = make(look_at_screen=True, look_at_apps=False)
    out = asyncio.run(p.initiative_status())
    assert "не найден" in out and "приказ «посмотри на экран» не отправляется" in out
    assert "по одному, списком не передаются" in out and "пока не определено" in out
    p._handle("tool_started", {"name": "screen_capture"})
    out = asyncio.run(p.initiative_status())
    assert "Инструмент экрана: «screen_capture»" in out and "по одному" not in out
    assert "Инструмент экрана" not in asyncio.run(make(look_at_screen=False, look_at_apps=False).initiative_status())   # выключено: молчим
    assert "в 100% инициатив" in asyncio.run(with_apps(make(look_at_apps=True)).initiative_status())


# ── личность Астры ───────────────────────────────────────────────────────

def test_astras_persona_is_the_default_character_and_reaches_every_initiative():
    assert mod.DEFAULTS["character"] == mod.PERSONA
    p = make()
    for kind in ("утро", "вечер", "ночь", "молчание", "мысль", "своё"):
        out = p._compose(NOON, "тема", kind)
        assert "Ты Астра" in out and "Твоя личность и манера общения, сохраняй их" in out


def test_the_persona_keeps_the_essentials_of_the_original_character():
    low = mod.PERSONA.lower()
    for trait in ("энергичная", "заботливая", "позитивная", "наивная", "восклицательными знаками", "в женском роде",
                  "ласково", "железо", "выпить воды", "не будь холодной и сухой", "технических описаний"):
        assert trait in low, trait


def test_the_persona_does_not_contradict_the_plugin_rules():
    p = make(look_at_apps=True, apps_hint_percent=100)
    p._apps_cache = (p.clock(), ["браузер", "Steam"], "")
    out = p._compose(NOON, "тема", "ночь")
    persona = mod.PERSONA.lower()
    # личность просит коротко и без списков, как и сам плагин
    assert "одна-две короткие живые фразы" in persona and "без отчётов и списков" in persona
    assert "одна-две короткие живые фразы" in out
    # не просит ни нравоучений, ни длинных описаний, ни перечислений
    assert "без нравоучений" in persona
    for bad in ("подробно опиши", "перечисли", "длинн", "список приложений"):
        assert bad not in persona.replace("никаких длинных технических описаний", "")
    assert "НИКОГДА не перечисляй программы" in out                                  # правило плагина на месте
    assert len(mod.PERSONA) <= mod.STR_LIMITS["character"]


def test_the_persona_can_be_replaced_or_switched_off():
    assert "Ты Астра" not in make(character="")._compose(NOON, "тема", "утро")
    assert "Твоя личность" not in make(character="")._compose(NOON, "тема", "утро")
    assert "Говори как пират" in make(character="Говори как пират")._compose(NOON, "тема", "утро")
    assert "Ты Астра" not in make(character="Говори как пират")._compose(NOON, "тема", "утро")


# ── настройки: группы, ползунки, многострочные поля ──────────────────────



def test_sliders_are_only_on_bounded_integers_and_textareas_only_on_strings():
    for key, p in _schema().items():
        kind = p.get("x-astra-field-type")
        if kind == "slider":
            assert p["type"] == "integer" and p["maximum"] - p["minimum"] <= 120, key   # узкий диапазон: ползунком удобно
        elif kind == "textarea":
            assert p["type"] == "string" and p["maxLength"] >= 200, key
        elif kind is not None:
            raise AssertionError(f"неизвестный тип поля {kind} у {key}")






def test_float_values_from_a_slider_are_accepted_as_whole_numbers():
    p = FirstWord()
    asyncio.run(p.on_config_changed({"morning_hour": 7.0, "daily_limit": 5.6, "random_chance_percent": 33.3}))
    assert (p.cfg["morning_hour"], p.cfg["daily_limit"], p.cfg["random_chance_percent"]) == (7, 5, 33)


# ── короткая форма настроек ──────────────────────────────────────────────
















# ── форма настроек: все поля на месте, понятные и по порядку ─────────────

GROUP_ORDER = ["general", "morning", "evening", "night", "silence", "random", "custom", "quiet", "screen", "test"]


def test_every_setting_is_in_the_form_and_agrees_with_the_code():
    props = _schema()
    assert set(props) == set(mod.DEFAULTS)                                           # ничего не спрятано и не потеряно
    for key, p in props.items():
        default = mod.DEFAULTS[key]
        assert p["default"] == default, key
        assert p["type"] == ("boolean" if isinstance(default, bool) else "integer" if isinstance(default, int) else "string")
        if key in mod.RANGES:
            assert (p["minimum"], p["maximum"]) == mod.RANGES[key], key
        if key in mod.STR_LIMITS:
            assert p["maxLength"] == mod.STR_LIMITS[key], key


def test_every_title_starts_with_its_section_so_the_context_is_clear():
    props = _schema()
    for key, p in props.items():
        section = p["x-astra-group-label"]
        assert p["title"].startswith(section + ": "), (key, p["title"])
    titles = [p["title"] for p in props.values()]
    assert len(set(titles)) == len(titles)
    assert max(len(t) for t in titles) <= 44, [t for t in titles if len(t) > 44]
    assert not any(ch in "".join(titles) for ch in "🌅🌆🌙⏳🎲📝🤫👁🧪💬")                # без значков


def test_hints_are_one_short_line_and_every_non_obvious_setting_has_one():
    props = _schema()
    for key, p in props.items():
        assert len(p.get("description", "")) <= 90, key
        assert "\n" not in p.get("description", ""), key
    for key in ("character", "daily_limit", "enable_morning", "morning_until", "enable_night", "enable_silence",
                "custom_prompts", "presence_check", "look_at_screen", "look_at_apps", "screen_tool", "test_mode", "dry_run"):
        assert props[key].get("description"), f"у {key} нет пояснения"


def test_sections_come_in_a_logical_order_and_are_never_split():
    order, labels = [], {}
    for key, p in _schema().items():
        gid = p["x-astra-group"]
        assert labels.setdefault(gid, p["x-astra-group-label"]) == p["x-astra-group-label"], key
        if not order or order[-1] != gid:
            assert gid not in order, f"раздел {gid} разорван на {key}"
            order.append(gid)
    assert order == GROUP_ORDER
    keys = list(_schema())
    assert keys[0] == "character" and keys[-1] == "dry_run"                           # главное сверху, отладка снизу


def test_each_scenario_switch_is_followed_by_its_own_time_setting():
    keys = list(_schema())
    for switch, knob in [("enable_morning", "morning_hour"), ("enable_evening", "evening_hour"),
                         ("enable_night", "night_hour"), ("enable_silence", "silence_hours"),
                         ("enable_random", "random_chance_percent"), ("quiet_enabled", "quiet_start"),
                         ("presence_check", "presence_check_sec"), ("look_at_screen", "look_at_apps")]:
        assert 0 < keys.index(knob) - keys.index(switch) <= 3, (switch, knob)


def test_time_and_small_numbers_are_sliders_and_long_texts_are_textareas():
    props = _schema()
    for key in ("morning_hour", "morning_until", "evening_hour", "night_hour", "quiet_start", "quiet_end",
                "silence_hours", "random_chance_percent", "daily_limit", "weekend_shift_hours",
                "after_chat_pause_min", "presence_check_sec", "apps_hint_percent"):
        assert props[key]["x-astra-field-type"] == "slider", key
        assert props[key]["maximum"] - props[key]["minimum"] <= 120, key
    for key in ("character", "custom_prompts", "screen_style", "screen_privacy"):
        assert props[key]["x-astra-field-type"] == "textarea", key
    for key in ("test_time", "min_interval_min", "presence_max_sec", "test_gap_sec"):
        assert "x-astra-field-type" not in props[key], key                           # широкий диапазон или текст


def test_the_six_test_switches_are_back_and_each_maps_to_a_scenario():
    props = _schema()
    for flag, kind in mod.TEST_FLAGS:
        assert flag in props and props[flag]["type"] == "boolean", flag
        assert props[flag]["title"].startswith("Тест: "), flag
    assert [k for _, k in mod.TEST_FLAGS] == ["утро", "вечер", "ночь", "молчание", "мысль", "своё"]
    assert "test_event" not in props


# ── просмотр экрана и приложений включён по умолчанию ────────────────────

def test_screen_and_apps_are_on_by_default_in_code_and_in_the_form():
    assert mod.DEFAULTS["look_at_screen"] is True and mod.DEFAULTS["look_at_apps"] is True
    props = _schema()
    assert props["look_at_screen"]["default"] is True and props["look_at_apps"]["default"] is True


def test_a_fresh_install_gives_astra_a_hint_without_touching_any_setting():
    p = FirstWord()
    asyncio.run(p.on_config_changed({}))                              # первая конфигурация новой установки пустая
    assert p.cfg["look_at_screen"] is True and p.cfg["look_at_apps"] is True
    p._apps_fn = lambda: (["Steam"], "")
    p.cfg["apps_hint_percent"] = 100
    asyncio.run(p._refresh_apps())
    out = p._compose(NOON, "тема", "утро")
    assert "запущено: Steam." in out and "НИКОГДА не перечисляй программы" in out
    assert "СНАЧАЛА посмотри на экран" not in out                      # имени инструмента нет: приказа нет, зря не ищем


def test_by_default_a_learned_screen_tool_is_ordered_and_the_apps_hint_is_added_too():
    p = FirstWord()
    asyncio.run(p.on_config_changed({}))
    p.cfg["apps_hint_percent"] = 100
    p._apps_fn = lambda: (["браузер"], "")
    p._handle("tool_started", {"name": "screen_capture"})
    asyncio.run(p._refresh_apps())
    out = p._compose(NOON, "тема", "ночь")
    assert "«screen_capture»" in out and "запущено: браузер." in out


def test_a_user_can_still_switch_both_off():
    p = FirstWord()
    asyncio.run(p.on_config_changed({"look_at_screen": False, "look_at_apps": False}))
    p._apps_cache = (p.clock(), ["Steam"], "")
    p._handle("tool_started", {"name": "screen_capture"})
    out = p._compose(NOON, "тема", "утро")
    assert "СНАЧАЛА" not in out and "Необязательный намёк" not in out


def test_with_both_on_by_default_the_status_shows_the_tool_and_the_apps_it_hints_from():
    p = FirstWord()
    asyncio.run(p.on_config_changed({}))
    p._handle("tool_started", {"name": "screen_capture"})
    p._apps_cache = (p.clock(), ["Steam", "браузер"], "")
    out = asyncio.run(p.initiative_status())
    assert "Инструмент экрана: «screen_capture»" in out
    assert "Steam, браузер" in out and "по одному, списком не передаются" in out
