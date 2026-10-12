"""Тесты «Инициативы Астры».

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
from src.plugin import FirstWord, parse_duration  # noqa: E402


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
        assert h.tool_names() == ["initiative_status", "pause_initiative", "remember_topic", "resume_initiative"]
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
