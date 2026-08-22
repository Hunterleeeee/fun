"""Terminal key decoding and raw-mode handling.

Reading a keystroke from a POSIX terminal means decoding three overlapping
things from one byte stream: UTF-8 sequences (so typing Chinese works), escape
sequences for the arrow and navigation keys, and bare control characters.  The
decoder below reads exactly as many bytes as the leading byte promises, which is
what keeps multi-byte input from being split into replacement characters.

Escape sequences are consumed to their terminating byte even when the sequence
is one this program does not implement.  Stopping early leaves the tail of the
sequence in the buffer, where it is then read as ordinary typing — which is how
pasting an API key used to cancel the dialog and then type ``00~`` in front of
the key.  Bracketed paste is handled explicitly for the same reason: a paste is
one event, not a burst of keystrokes that happens to start with an escape.
"""
from __future__ import annotations

import os
import select
import time
import sys
from typing import Any

try:  # Windows keeps the plain, non-raw fallback path.
    import termios
    import tty
except ImportError:  # pragma: no cover - exercised only on Windows
    termios = None
    tty = None

ESCAPE_KEYS = {
    "[A": "up",
    "[B": "down",
    "[C": "right",
    "[D": "left",
    "[H": "home",
    "[F": "end",
    "OA": "up",
    "OB": "down",
    "OC": "right",
    "OD": "left",
}

# CSI sequences terminated by '~' carry their identity in the parameter bytes.
TILDE_KEYS = {"1": "home", "3": "delete", "4": "end", "5": "pageup", "6": "pagedown", "7": "home", "8": "end"}

PASTE_PREFIX = "paste:"

# Wheel reporting.  ``?1007l`` turns *off* the terminal's own "alternate scroll",
# which translates the wheel into arrow keys — that is why scrolling used to
# replace the draft with a history entry.  ``?1000h`` + ``?1006h`` ask for real
# button events in SGR encoding instead, so the wheel arrives as a wheel.  The
# cost is that selecting text needs Shift held (every major terminal honours
# that), which is why only the fullscreen frontend turns this on: --stream
# exists precisely for people who want the terminal's own selection.
MOUSE_ON = "\033[?1007l\033[?1000h\033[?1006h"
MOUSE_OFF = "\033[?1006l\033[?1000l"

WHEEL_UP, WHEEL_DOWN = 64, 65
PASTE_BEGIN = "200~"
PASTE_END = "\x1b[201~"
BRACKETED_PASTE_ON = "\033[?2004h"
BRACKETED_PASTE_OFF = "\033[?2004l"


def paste_text(key: str) -> str | None:
    """The payload of a paste event, or None if ``key`` is not one."""
    return key[len(PASTE_PREFIX):] if key.startswith(PASTE_PREFIX) else None

CONTROL_KEYS = {
    "\r": "enter",
    "\n": "enter",
    "\x01": "home",
    "\x02": "left",
    "\x04": "eof",
    "\x05": "end",
    "\x06": "right",
    "\x0b": "kill_to_end",
    "\x0c": "redraw",
    "\x0e": "newline",
    "\x0f": "toggle_output",
    "\x10": "palette",
    "\x14": "sidebar",
    "\x03": "cancel",
    "\x7f": "backspace",
    "\b": "backspace",
    "\x15": "kill_to_start",
    "\x17": "kill_word_left",
    "\x19": "yank",
    "\t": "tab",
}

# Alt/Meta chords arrive as ESC followed by the bare character.
META_KEYS = {
    "f": "word_right",
    "b": "word_left",
    "d": "kill_word_right",
    "\x7f": "kill_word_left",
}


def supports_raw_mode() -> bool:
    return termios is not None and tty is not None and sys.stdin.isatty() and hasattr(termios, "tcgetattr")


class RawMode:
    """Context manager putting the terminal into cbreak mode and restoring it."""

    def __init__(self, fd: int | None = None, output_fd: int | None = None) -> None:
        self.fd = sys.stdin.fileno() if fd is None else fd
        if output_fd is None:
            try:
                output_fd = sys.stdout.fileno()
            except (AttributeError, OSError):
                output_fd = None
        self.output_fd = output_fd
        self._saved: Any = None

    def __enter__(self) -> "RawMode":
        if termios is None or tty is None:
            raise RuntimeError("TUI_REQUIRES_TTY")
        self._saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        # Ask the terminal to bracket pastes so a pasted key arrives as one
        # event.  Terminals that do not understand this ignore it silently.
        self._write(BRACKETED_PASTE_ON)
        return self

    def _write(self, sequence: str) -> None:
        if self.output_fd is None:
            return
        try:
            os.write(self.output_fd, sequence.encode())
        except OSError:  # pragma: no cover - closed or non-writable tty
            pass

    def __exit__(self, *_: object) -> None:
        self._write(BRACKETED_PASTE_OFF)
        if self._saved is not None and termios is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None


def _utf8_length(head: int) -> int:
    """Return how many continuation bytes follow a UTF-8 lead byte."""
    if head & 0xE0 == 0xC0:
        return 1
    if head & 0xF0 == 0xE0:
        return 2
    if head & 0xF8 == 0xF0:
        return 3
    return 0


def _wait_readable(fd: int, timeout: float) -> bool:
    if os.name != "nt":
        ready, _, _ = select.select([fd], [], [], timeout)
        return bool(ready)
    import ctypes
    import msvcrt
    handle = msvcrt.get_osfhandle(fd)
    deadline = time.monotonic() + timeout
    available = ctypes.c_ulong()
    while True:
        if ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None) and available.value:
            return True
        try:
            if msvcrt.kbhit():
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def read_key(fd: int, timeout: float = 0.08) -> str | None:
    """Read one decoded key, or ``None`` if nothing arrived within ``timeout``."""
    if not _wait_readable(fd, timeout):
        return None
        return None
    first = os.read(fd, 1)
    if not first:
        return None
    if first != b"\x1b":
        raw = first + (os.read(fd, _utf8_length(first[0])) if _utf8_length(first[0]) else b"")
        key = raw.decode("utf-8", "replace")
        return CONTROL_KEYS.get(key, key)
    if not _wait_readable(fd, 0.02):
        return "escape"
    lead = _read_byte(fd)
    if not lead:
        return "escape"
    if lead not in {"[", "O"}:
        # Not a CSI/SS3 sequence, so this is a Meta (Alt) chord.
        return META_KEYS.get(lead, "escape")
    if lead == "O":
        return ESCAPE_KEYS.get("O" + _read_byte(fd), "escape")
    body = _read_csi(fd)
    if body == PASTE_BEGIN:
        return PASTE_PREFIX + _read_paste(fd)
    if body.startswith("<"):
        return _mouse_key(body)
    if body == "M":
        # The legacy encoding puts three raw bytes after the M.
        raw = [_read_byte(fd) for _ in range(3)]
        button = (ord(raw[0]) - 32) if raw[0] else -1
        return {WHEEL_UP: "wheel_up", WHEEL_DOWN: "wheel_down"}.get(button, "mouse")
    if body.endswith("~"):
        return TILDE_KEYS.get(body[:-1].split(";")[0], "escape")
    return ESCAPE_KEYS.get("[" + body, "escape")


def _read_byte(fd: int, timeout: float = 0.12) -> str:
    """Read one byte of an escape sequence, or "" if it never arrives.

    A truncated sequence — a lone ESC[ at the end of a paste, a terminal that
    drops bytes — used to block ``os.read`` forever with the UI thread inside
    it, so the whole interface froze until another key was pressed.
    """
    if not _wait_readable(fd, timeout):
        return ""
    return os.read(fd, 1).decode("utf-8", "replace")


def _mouse_key(body: str) -> str:
    """Decode an SGR mouse report.  Everything but the wheel is ignored.

    Ignored, not passed through: an unrecognised report used to come back as
    "escape", so a click or a drag closed whatever dialog was open.
    """
    try:
        button = int(body[1:].split(";")[0])
    except (ValueError, IndexError):
        return "mouse"
    return {WHEEL_UP: "wheel_up", WHEEL_DOWN: "wheel_down"}.get(button, "mouse")


def _read_csi(fd: int) -> str:
    """Read a CSI sequence's body, up to and including its final byte.

    Per ECMA-48 a CSI ends at the first byte in ``0x40``–``0x7E``; everything
    before that is parameters and intermediates.  Reading to that boundary is
    what stops an unhandled sequence from spilling into the text stream.
    """
    body = ""
    while len(body) < 32:
        char = _read_byte(fd)
        if not char:
            break
        body += char
        if "\x40" <= char <= "\x7e":
            break
    return body


def _read_paste(fd: int, limit: int = 64000, timeout: float = 0.5, total: float = 10.0) -> str:
    """Collect a bracketed paste up to its ``ESC[201~`` terminator.

    Bounded twice: by silence, and by total time.  A paste whose terminator
    never arrives — a disconnected terminal, a truncated sequence — would
    otherwise hold the UI thread for as long as bytes kept trickling.
    """
    buffer = b""
    deadline = time.monotonic() + total
    while len(buffer) < limit and time.monotonic() < deadline:
        if not _wait_readable(fd, timeout):
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buffer += chunk
        if buffer.endswith(b"\x1b[201~"):
            buffer = buffer[: -len(b"\x1b[201~")]
            break
        marker = buffer.find(b"\x1b[201~")
        if marker >= 0:
            buffer = buffer[:marker]
            break
    return buffer.decode("utf-8", "replace")


def is_text(key: str) -> bool:
    """Whether ``key`` is ordinary printable input rather than a named key."""
    return len(key) == 1 and key.isprintable()
