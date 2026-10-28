"""System tray icon, menu (pystray) and the text dialog (tkinter). Knows nothing about BLE."""

import logging
import threading
import tkinter as tk
from collections.abc import Callable
from typing import Any

import pystray

from furbyhack.icons import furby_icon

log = logging.getLogger(__name__)

APP_NAME = "FurbyHack"


class Tray:
    """All callbacks run on the tray thread and must return quickly: blocking there
    freezes the icon, and other threads updating the icon can then deadlock."""

    def __init__(
        self,
        on_say: Callable[[str], None],
        on_toggle_connection: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self._on_say = on_say
        self._on_toggle_connection = on_toggle_connection
        self._on_exit = on_exit
        self._connected = False
        self._paused = False  # user chose Disconnect
        self._busy = False  # dialog open or Furby speaking
        self._stopped = False
        self._stop_lock = threading.Lock()
        self._icons = {state: furby_icon(state) for state in (True, False)}
        # pystray accepts callables for text/enabled; its type hints are just too narrow.
        self._icon = pystray.Icon(
            APP_NAME,
            icon=self._icons[False],
            title=f"{APP_NAME}: starting",
            menu=pystray.Menu(
                # default=True also makes it the left-click action.
                pystray.MenuItem(
                    "Say text…",
                    self._say,
                    default=True,
                    enabled=lambda _: self._can_say(),  # pyright: ignore[reportArgumentType]
                ),
                pystray.MenuItem(
                    lambda _: "Connect" if self._paused else "Disconnect",  # pyright: ignore[reportArgumentType]
                    self._toggle_connection,
                    enabled=lambda _: not self._busy,  # pyright: ignore[reportArgumentType]
                ),
                pystray.MenuItem("Exit", self._exit),
            ),
        )

    def run(self) -> None:
        """Blocks until stop(). Must run on the main thread."""
        self._icon.run()

    def stop(self) -> None:
        """Remove the icon and end run(). Safe to call from any thread, more than once."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._icon.stop()

    def set_paused(self, paused: bool) -> None:
        """Safe to call from any thread."""
        self._paused = paused
        self._icon.update_menu()

    def set_status(self, connected: bool, text: str) -> None:
        """Safe to call from any thread."""
        self._connected = connected
        self._icon.icon = self._icons[connected]
        self._icon.title = f"{APP_NAME}: {text}"
        self._icon.update_menu()

    def notify(self, message: str) -> None:
        """Windows toast from the tray icon. Safe to call from any thread."""
        self._icon.notify(message, APP_NAME)

    def set_busy(self, busy: bool) -> None:
        """Safe to call from any thread."""
        self._busy = busy
        self._icon.update_menu()

    def _can_say(self) -> bool:
        return self._connected and not self._busy

    def _say(self, _icon: Any, _item: Any) -> None:
        if not self._can_say():
            return
        self.set_busy(True)
        # Tk gets its own thread so the tray loop keeps running.
        threading.Thread(target=self._dialog_thread, name="say-dialog", daemon=True).start()

    def _dialog_thread(self) -> None:
        try:
            text = ask_text()
        except Exception:
            # Otherwise "Say text…" would stay disabled until restart.
            log.exception("Text dialog failed")
            text = None
        if text:
            self._on_say(text)  # the app clears busy when speaking is done
        else:
            self.set_busy(False)

    def _toggle_connection(self, _icon: Any, _item: Any) -> None:
        self._on_toggle_connection()

    def _exit(self, _icon: Any, _item: Any) -> None:
        self._on_exit()


# Windows virtual-key codes of the physical V/C/X/A keys, whatever the keyboard layout.
_CLIPBOARD_KEYS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}


def _clipboard_keys_any_layout(event: Any) -> str | None:
    """Tk binds Ctrl+V/C/X/A to the Latin letters, so with a Ukrainian layout (Ctrl+М...)
    they do nothing. Map the physical keys instead; Latin layouts keep Tk's own binding."""
    virtual = _CLIPBOARD_KEYS.get(event.keycode)
    if virtual is None or event.keysym.lower() in ("v", "c", "x", "a"):
        return None
    event.widget.event_generate(virtual)
    return "break"


# One phrase must fit a single <= 11.5 s track. Ukrainian, the slowest voice, reads
# ~13 characters per second, so ~150 fit; 130 leaves room for numbers spelled as words.
MAX_TEXT_CHARS = 130


def ask_text(max_chars: int = MAX_TEXT_CHARS) -> str | None:
    """Modal "what should Furby say" window. Blocks; all Tk calls stay in the calling thread.

    Typing stops at `max_chars`, and pasted text is cut to it.
    """
    result: str | None = None

    root = tk.Tk()
    root.title("Furby says…")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    text = tk.StringVar(root)
    entry = tk.Entry(root, width=50, font=("Segoe UI", 11), textvariable=text)
    entry.pack(padx=12, pady=(12, 2), fill="x")
    entry.bind("<Control-KeyPress>", _clipboard_keys_any_layout)
    counter = tk.Label(root, text=f"0 / {max_chars}", fg="gray", font=("Segoe UI", 9))
    counter.pack(padx=12, anchor="e")

    def limit(*_args: Any) -> None:
        value = text.get()
        if len(value) > max_chars:
            cut = value[:max_chars]
            if value[max_chars] != " " and " " in cut:
                cut = cut.rsplit(" ", 1)[0]  # pasted text: don't end mid-word
            text.set(cut)  # re-enters limit() once with the short value
            entry.icursor(tk.END)
            return
        counter.config(text=f"{len(value)} / {max_chars}")

    text.trace_add("write", limit)

    def ok(_event: Any = None) -> None:
        nonlocal result
        result = entry.get().strip()
        root.destroy()

    buttons = tk.Frame(root)
    buttons.pack(padx=12, pady=(0, 12), anchor="e")
    tk.Button(buttons, text="Say", width=10, command=ok).pack(side="left", padx=(0, 6))
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side="left")

    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda _e: root.destroy())

    # Center on screen and grab focus (tray apps don't get it automatically).
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    root.after(50, lambda: (root.focus_force(), entry.focus_set()))

    root.mainloop()
    return result or None
