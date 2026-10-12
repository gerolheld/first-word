"""Присутствие пользователя: двигалась ли мышь или был ли ввод с прошлой проверки.

У каждого бэкенда один метод, `sample()`. Он возвращает значение, которое меняется, когда пользователь что-то
сделал, или None, если прочитать не удалось. Плагин сравнивает два значения подряд и не знает, что внутри.

* Windows: `GetLastInputInfo` (мышь и клавиатура), запасной путь `GetCursorPos`.
* Linux X11: координаты курсора через `XQueryPointer` из libX11 (без зависимостей).
* Linux Wayland: события ввода из /dev/input (нужна группа `input`). Координаты через XWayland там
  не годятся: они замирают, когда курсор не над окном X11. Плагин только считает события определённых типов
  и никогда не читает и не хранит коды клавиш.
"""

import ctypes
import ctypes.util
import glob
import os
import struct
import sys

EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
EVENT = struct.Struct("llHHi")            # timeval, type, code, value: как struct input_event на этой платформе
EVIOCGNAME = 0x81004506                   # _IOC(read, 'E', 0x06, 256)
TOUCHPAD_WORDS = ("touchpad", "trackpad", "clickpad")


class Backend:
    name = "?"

    def sample(self):
        raise NotImplementedError

    def close(self):
        pass


class WindowsBackend(Backend):
    name = "windows"

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def __init__(self, user32=None):
        self.user32 = user32 if user32 is not None else ctypes.windll.user32

    def sample(self):
        info = self._LastInput(ctypes.sizeof(self._LastInput), 0)
        if self.user32.GetLastInputInfo(ctypes.byref(info)):
            return info.dwTime
        point = self._Point()
        if self.user32.GetCursorPos(ctypes.byref(point)):
            return (point.x, point.y)
        return None


class X11Backend(Backend):
    name = "x11"

    def __init__(self):
        self._lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self._lib.XOpenDisplay.restype = ctypes.c_void_p
        self._lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self._lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._lib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        self._lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._display = None

    def _open(self):
        if self._display is None:
            self._display = self._lib.XOpenDisplay(None)
        return self._display

    def sample(self):
        display = self._open()
        if not display:
            return None
        root = self._lib.XDefaultRootWindow(display)
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
        mask = ctypes.c_uint()
        ok = self._lib.XQueryPointer(display, root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                                     ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        if not ok:
            self.close()
            return None
        return (rx.value, ry.value)

    def close(self):
        if self._display:
            self._lib.XCloseDisplay(self._display)
            self._display = None


class EvdevBackend(Backend):
    """Считает события «мышь двинулась» и «нажали клавишу или кнопку». Коды клавиш не смотрим."""

    name = "evdev"

    def __init__(self, paths):
        self._fds = {}
        self._touchpad = {}
        self.count = 0
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self._fds[fd] = path
            self._touchpad[fd] = self._is_touchpad(fd)

    @staticmethod
    def _is_touchpad(fd) -> bool:
        try:
            import fcntl
            buf = bytearray(256)
            fcntl.ioctl(fd, EVIOCGNAME, buf)
            name = bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace").lower()
            return any(w in name for w in TOUCHPAD_WORDS)
        except (OSError, ImportError):
            return False

    @property
    def devices(self) -> int:
        return len(self._fds)

    def _drain(self, fd):
        while True:
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                return
            if not data:
                return
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, etype, _, value = EVENT.unpack_from(data, off)
                if etype == EV_REL or (etype == EV_KEY and value == 1) or (etype == EV_ABS and self._touchpad[fd]):
                    self.count += 1

    def sample(self):
        if not self._fds:
            return None
        for fd in list(self._fds):
            try:
                self._drain(fd)
            except OSError:
                os.close(fd)
                del self._fds[fd], self._touchpad[fd]
        return self.count if self._fds else None

    def close(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()


def _runtime_dir(env) -> str:
    return env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def is_wayland(env, runtime_dir=None) -> bool:
    kind = (env.get("XDG_SESSION_TYPE") or "").lower()
    if kind:
        return kind == "wayland"
    if env.get("WAYLAND_DISPLAY"):
        return True
    return bool(glob.glob(os.path.join(runtime_dir or _runtime_dir(env), "wayland-*[0-9]")))


def _prepare_x11_env(env=None):
    """Демон чистит окружение плагина: находим дисплей и cookie сами."""
    env = os.environ if env is None else env
    if not env.get("DISPLAY"):
        sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if sockets:
            env["DISPLAY"] = ":" + sockets[0].rsplit("X", 1)[1]
    if not env.get("XAUTHORITY"):
        runtime = _runtime_dir(env)
        for pattern in (os.path.join(runtime, "xauth_*"), os.path.join(runtime, ".mutter-Xwaylandauth.*"),
                        os.path.join(os.path.expanduser("~"), ".Xauthority")):
            found = sorted(glob.glob(pattern))
            if found:
                env["XAUTHORITY"] = found[0]
                break


def detect(env=None, platform=None, evdev_paths=None):
    """(бэкенд или None, пояснение для журнала)."""
    env = dict(os.environ) if env is None else env
    platform = platform or sys.platform
    if platform.startswith("win"):
        try:
            return WindowsBackend(), "Windows: GetLastInputInfo"
        except (AttributeError, OSError) as e:
            return None, f"Windows: user32 недоступен ({e})"
    if not platform.startswith("linux"):
        return None, f"платформа {platform} не поддерживается"

    paths = sorted(glob.glob("/dev/input/event*")) if evdev_paths is None else evdev_paths
    evdev = EvdevBackend(paths) if paths else None
    if evdev is not None and evdev.devices:
        return evdev, f"evdev: {evdev.devices} устройств ввода (мышь и клавиатура)"
    if evdev is not None:
        evdev.close()

    if is_wayland(env):
        return None, ("Wayland: координаты мыши через XWayland ненадёжны, а /dev/input недоступен; добавьте себя "
                      "в группу input (sudo usermod -aG input $USER, затем перелогиньтесь), иначе проверка присутствия "
                      "отключена")
    try:
        _prepare_x11_env()
        backend = X11Backend()
        if backend.sample() is None:
            backend.close()
            return None, "X11: не удалось подключиться к дисплею"
        return backend, "X11: координаты курсора (XQueryPointer)"
    except OSError as e:
        return None, f"X11: libX11 недоступна ({e})"
