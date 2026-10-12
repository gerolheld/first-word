"""Какие приложения сейчас открыты, словами, которые можно показать Астре.

Это не экран, а список запущенных программ из заведомо известных: браузер, редактор кода, игровой лаунчер, мессенджер и
так далее. Остальные процессы (неизвестные программы, системные службы) не попадают никуда: плагин называет только то,
что есть в `KNOWN`. Заголовки окон не читаются. На Windows дополнительно определяется программа активного окна.
"""

import os
import re
import subprocess
import sys

# (регулярное выражение по имени процесса без .exe, подпись). Порядок задаёт порядок в ответе.
KNOWN = [
    (r"^(steam|steamwebhelper-launcher)$", "Steam"),
    (r"^(lutris|heroic|bottles|gamescope|prismlauncher|minecraft-launcher)$", "игровой лаунчер"),
    (r"^(dota2|cs2|csgo_linux64|hl2_linux|eldenring|witcher3|gta5|valorant|rocketleague|factorio|terraria|"
     r"stardew valley|stardewvalley|hades|cyberpunk2077|bg3|baldursgate3|overwatch|fortniteclient-win64-shipping|"
     r"leagueclient|league of legends|genshinimpact|yuanshen|starrail|ds3|sekiro)$", "игра"),
    (r"^(firefox|firefox-bin|librewolf|zen|chrome|google-chrome|google-chrome-stable|chromium|brave|brave-browser|"
     r"vivaldi|vivaldi-bin|opera|msedge|yandex|yandexbrowser)$", "браузер"),
    (r"^(code|code-oss|codium|vscodium|cursor|idea|idea64|pycharm|pycharm64|clion|clion64|webstorm|webstorm64|goland|"
     r"goland64|rider|rider64|sublime_text|zed|devenv)$", "редактор кода"),
    (r"^(konsole|alacritty|kitty|wezterm|wezterm-gui|gnome-terminal|gnome-terminal-server|foot|windowsterminal|wt)$",
     "терминал"),
    (r"^discord$", "Discord"),
    (r"^(telegram|telegram-desktop|telegramdesktop)$", "Telegram"),
    (r"^(slack|zoom|teams|ms-teams|skype|element|signal-desktop)$", "мессенджер или созвон"),
    (r"^spotify$", "Spotify"),
    (r"^(vlc|mpv|smplayer|celluloid|totem)$", "видеоплеер"),
    (r"^(obs|obs64|obs-studio)$", "OBS"),
    (r"^(blender|gimp|gimp-2\.10|krita|inkscape|kdenlive|resolve|photoshop|aseprite)$", "творческая программа"),
    (r"^(soffice|soffice\.bin|libreoffice|winword|excel|powerpnt|onlyoffice|desktopeditors)$", "офисная программа"),
    (r"^(obsidian|notion|logseq)$", "заметки"),
]
COMPILED = [(re.compile(p, re.I), label) for p, label in KNOWN]
MAX_LABELS = 6


def _linux_names() -> set:
    names, uid = set(), os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return names
    for entry in entries:
        if not entry.isdigit():
            continue
        base = f"/proc/{entry}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(f"{base}/comm", encoding="utf-8", errors="replace") as f:
                names.add(f.read().strip().lower())
            with open(f"{base}/cmdline", "rb") as f:
                first = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            if first:
                names.add(os.path.basename(first).lower())
        except OSError:
            continue
    return names


def _windows_names() -> set:
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout          # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_tasklist(out)


def parse_tasklist(text: str) -> set:
    names = set()
    for line in text.splitlines():
        m = re.match(r'^"([^"]+)"', line.strip())
        if m:
            names.add(re.sub(r"\.exe$", "", m.group(1).strip().lower()))
    return names


def foreground_windows() -> str:
    """Программа активного окна на Windows (имя процесса без .exe) или пустая строка."""
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return re.sub(r"\.exe$", "", os.path.basename(buf.value).lower())
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        pass
    return ""


def label_for(name: str) -> str:
    for rx, label in COMPILED:
        if rx.match(name):
            return label
    return ""


def describe(names: set) -> list:
    """Подписи известных программ среди `names`, без повторов, в порядке `KNOWN`."""
    found = []
    for rx, label in COMPILED:
        if label not in found and any(rx.match(n) for n in names):
            found.append(label)
    return found[:MAX_LABELS]


def snapshot(platform: str | None = None) -> tuple:
    """(подписи открытых программ, подпись активного окна или пустая строка)."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return describe(_windows_names()), label_for(foreground_windows())
    if platform.startswith("linux"):
        return describe(_linux_names()), ""
    return [], ""
