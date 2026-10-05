"""Per-user tray icon for the SemSearch service (the service itself runs in Session 0 and
cannot show UI). Runs as the logged-on operator, started by a logon task the installer
registers. Menu: status line, open the settings page, pause/resume, Folders (add, remove,
use the Windows Search scope), Indexing hardware (light / dedicated GPU; asked once after
setup), open logs, start/stop/restart the service, quit.

Everything that needs the operator's rights lives here: granting the service account read
access on a new folder (icacls, as the folder's owner), and service control through the
service DACL. The actual configuration change goes through the token-gated API.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser

log = logging.getLogger(__name__)
MUTEX_NAME = r"Local\SemSearch.Tray"


# ---------------------------------------------------------------- helpers (pure, testable)

def make_icon_image(size: int = 64, color=(42, 109, 244), state: str = "ok"):
    """A magnifier glyph; the lens is grey when the service is unreachable, amber when paused."""
    from PIL import Image, ImageDraw
    lens = {"ok": color, "paused": (235, 160, 30), "down": (140, 140, 140)}.get(state, color)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size * 0.33
    cx, cy = size * 0.42, size * 0.42
    w = max(3, size // 9)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=lens, width=w)
    d.line((cx + r * 0.7, cy + r * 0.7, size * 0.9, size * 0.9), fill=lens, width=w + 2)
    return img


def status_line(health: dict | None, status: dict | None) -> str:
    if not health:
        return "SemSearch: service not reachable"
    ix = (status or {}).get("indexer") or {}
    q = ix.get("queue") or {}
    state = "paused" if ix.get("paused") else ("indexing" if q.get("pending") or q.get("running") else "idle")
    return f"SemSearch {health.get('version', '')}: {health.get('documents', 0):,} documents, {state}" + (f" ({q.get('pending', 0)} queued)" if q.get("pending") else "")


def icon_state(health: dict | None, status: dict | None) -> str:
    if not health:
        return "down"
    if ((status or {}).get("indexer") or {}).get("paused"):
        return "paused"
    return "ok"


def hardware_question(opts: dict) -> str:
    """The one-time question (Yes / No / Cancel on a Win32 message box, which cannot relabel its
    buttons, so the text says which button is which)."""
    labels = opts.get("labels") or {}
    light_dev = labels.get("integrated") or ("the integrated GPU" if (opts.get("present") or {}).get("integrated") else "the CPU")
    gpu_dev = labels.get("dedicated") or "the dedicated GPU"
    return ("How should SemSearch use this computer's hardware for indexing?\n\n"
            f"YES = Light: everyday indexing runs on {light_dev}. The dedicated GPU ({gpu_dev}) is used "
            "only for big jobs (the first build, large backlogs). Keeps the dedicated GPU free for games and other work.\n\n"
            f"NO = Dedicated GPU: all indexing runs on {gpu_dev}. Much faster at catching up after "
            "changes, but uses the dedicated GPU whenever files change.\n\n"
            "CANCEL = ask me next time.\n\n"
            "Searching runs on the CPU either way. You can change this later: tray icon > Indexing hardware.")


def hardware_label(profile: str, opts: dict) -> str:
    labels = opts.get("labels") or {}
    if profile == "gpu":
        return f"Dedicated GPU ({labels.get('dedicated') or 'all indexing'})"
    return f"Light ({labels.get('integrated') or 'CPU'}; dedicated GPU for big jobs only)" if (opts.get("options") or {}).get("gpu") \
        else f"Light ({labels.get('integrated') or 'CPU'})"


class TrayApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = f"http://{cfg.api.host}:{cfg.api.port}"
        self.health: dict | None = None
        self.status: dict | None = None
        self.icon = None
        self._stop = threading.Event()
        self._state = "ok"
        self.devices: dict | None = None  # GET /config/devices; fetched at start and after a change, not every poll

    # ---- service I/O ----
    def _client(self):
        """Token attached only when the listener on the configured port is the SemSearch service
        process: the tray polls every 10 s, and a token handed to whoever holds the port while
        the service restarts would be a standing leak."""
        import httpx
        from .clientauth import token_headers
        headers, _warning = token_headers(self.base, self.token(), self.cfg.service.name)
        return httpx.Client(base_url=self.base, timeout=30.0, headers=headers)

    def token(self) -> str | None:
        try:
            return (self.cfg.state_path / "admin.token").read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def poll(self) -> None:
        try:
            with self._client() as c:
                self.health = c.get("/health").json()
                self.status = c.get("/status").json()
        except Exception:  # noqa: BLE001
            self.health = self.status = None

    def roots(self) -> list[str]:
        try:
            with self._client() as c:
                return list(c.get("/config").json().get("roots", []))
        except Exception:  # noqa: BLE001
            return [str(r) for r in self.cfg.roots]

    # ---- UI primitives (Win32 dialogs; no toolkit) ----
    @staticmethod
    def message(text: str, title: str = "SemSearch", kind: str = "info") -> None:
        import win32api
        import win32con
        flags = {"info": win32con.MB_ICONINFORMATION, "warn": win32con.MB_ICONWARNING, "error": win32con.MB_ICONERROR}[kind]
        win32api.MessageBox(0, text, title, win32con.MB_OK | flags | win32con.MB_SETFOREGROUND)

    @staticmethod
    def confirm(text: str, title: str = "SemSearch") -> bool:
        import win32api
        import win32con
        return win32api.MessageBox(0, text, title, win32con.MB_OKCANCEL | win32con.MB_ICONQUESTION | win32con.MB_SETFOREGROUND) == win32con.IDOK

    @staticmethod
    def pick_folder(title: str = "Choose a folder to index") -> str | None:
        from win32com.shell import shell, shellcon
        try:
            pidl, _, _ = shell.SHBrowseForFolder(0, None, title, shellcon.BIF_RETURNONLYFSDIRS | shellcon.BIF_NEWDIALOGSTYLE | shellcon.BIF_EDITBOX, None, None)
        except Exception:  # noqa: BLE001 - cancelled
            return None
        if not pidl:
            return None
        p = shell.SHGetPathFromIDList(pidl)
        return p.decode("mbcs") if isinstance(p, bytes) else p

    # ---- actions ----
    def open_settings(self, *_):
        """Open the settings page with a single-use 60 s nonce (in the URL fragment, which is
        never sent to a server); the page trades it for a tab-scoped session. The admin token
        itself never leaves this process."""
        frag = ""
        try:
            with self._client() as c:
                r = c.post("/ui/nonce")
                if r.status_code < 400:
                    frag = "#n=" + r.json()["nonce"]
        except Exception:  # noqa: BLE001 - the page still opens; it will say it is not signed in
            pass
        webbrowser.open(f"{self.base}/ui{frag}")

    def open_logs(self, *_):
        d = str(self.cfg.log_dir)
        # startfile EXECUTES a file; the log path comes from the config, so only ever open a directory
        if os.path.isdir(d) and not os.path.islink(d):
            os.startfile(d)  # noqa: S606 - explorer on our own log directory
        else:
            self.message(f"log directory not found: {d}", kind="warn")

    def toggle_pause(self, *_):
        paused = bool(((self.status or {}).get("indexer") or {}).get("paused"))
        try:
            with self._client() as c:
                r = c.post("/indexer/resume" if paused else "/indexer/pause")
                if r.status_code >= 400:
                    self.message(f"could not {'resume' if paused else 'pause'}: {r.json().get('detail', r.text)}", kind="warn")
        except Exception as e:  # noqa: BLE001
            self.message(f"service not reachable: {e}", kind="warn")
        self.refresh()

    def add_folder(self, *_):
        from .roots_admin import add_root, service_account_from_config
        p = self.pick_folder()
        if not p:
            return
        tok = self.token()
        if not tok:
            self.message("The admin token is not readable by this account; folders can only be changed by the operator who installed SemSearch.", kind="warn")
            return
        try:
            with self._client() as c:
                out = add_root(c, p, tok, service_account_from_config(self.cfg))
            self.message(f"Indexing {p}\n\nread access for the service: {out.get('grant')}\nindexing starts in the background.")
        except Exception as e:  # noqa: BLE001
            self.message(str(e), kind="error")
        self.refresh()

    def remove_folder(self, path: str):
        from .roots_admin import remove_root, service_account_from_config
        if not self.confirm(f"Stop indexing {path}?\n\nIts documents are removed from the index; the files are untouched. The service's read access on the folder is revoked."):
            return
        tok = self.token()
        try:
            with self._client() as c:
                out = remove_root(c, path, tok or "", service_account_from_config(self.cfg))
            self.message(f"Removed {path}\nread access: {out.get('grant')}")
        except Exception as e:  # noqa: BLE001
            self.message(str(e), kind="error")
        self.refresh()

    def use_windows_scope(self, *_):
        from .roots_admin import add_root, service_account_from_config
        try:
            with self._client() as c:
                s = c.get("/config/windows-scope").json()
        except Exception as e:  # noqa: BLE001
            self.message(f"service not reachable: {e}", kind="warn")
            return
        roots = s.get("roots") or []
        if not roots:
            self.message("Windows Search has no content-indexed folders in your profile to import.", kind="info")
            return
        cur = {os.path.normcase(r) for r in self.roots()}
        new = [r for r in roots if os.path.normcase(r) not in cur]
        if not new:
            self.message("Every folder Windows indexes for content is already indexed by SemSearch.")
            return
        if not self.confirm("Add these folders (what Windows Search indexes for content in your profile)?\n\n" + "\n".join(new)
                            + "\n\n(Windows' exclusion rules are a separate menu item.)"):
            return
        tok = self.token() or ""
        acct = service_account_from_config(self.cfg)
        done, failed = [], []
        try:
            with self._client() as c:
                for r in new:
                    try:
                        add_root(c, r, tok, acct)
                        done.append(r)
                    except Exception as e:  # noqa: BLE001
                        failed.append(f"{r}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(str(e))
        self.message("Added:\n" + "\n".join(done) + ("\n\nFailed:\n" + "\n".join(failed) if failed else ""), kind="warn" if failed else "info")
        self.refresh()

    def adopt_windows_excludes(self, *_):
        """Windows' exclusion rules are the user's curated list, but they were written for a
        filename index: adopting them over roots Windows does not content-index (a development
        tree, say) can remove a lot. Measured here: 9,998 of 19,325 documents. So: separate
        action, explicit warning, never part of the folder import."""
        try:
            with self._client() as c:
                s = c.get("/config/windows-scope").json()
                cur = c.get("/config").json().get("excludes", [])
        except Exception as e:  # noqa: BLE001
            self.message(f"service not reachable: {e}", kind="warn")
            return
        new = sorted(set(s.get("excludes") or []) - set(cur))
        if not new:
            self.message("Every Windows exclusion rule is already in the configuration.")
            return
        if not self.confirm(f"Add {len(new)} exclusion rules from Windows Search to the configuration?\n\n"
                            "Documents already indexed under them are REMOVED from the index (the files are untouched). "
                            "Review the list on the settings page first if unsure.\n\nFirst rules:\n"
                            + "\n".join(new[:12]) + ("\n..." if len(new) > 12 else "")):
            return
        try:
            with self._client() as c:
                r = c.post("/config/excludes", json={"excludes": sorted(set(cur) | set(new))})
                if r.status_code >= 400:
                    raise RuntimeError(r.json().get("detail", r.text))
            self.message(f"{len(new)} rules added; out-of-scope documents are being removed in the background.")
        except Exception as e:  # noqa: BLE001
            self.message(str(e), kind="error")
        self.refresh()

    def fetch_devices(self) -> dict | None:
        try:
            with self._client() as c:
                r = c.get("/config/devices")
                self.devices = r.json() if r.status_code < 400 else None
        except Exception:  # noqa: BLE001
            self.devices = None
        return self.devices

    def set_hardware(self, profile: str) -> bool:
        if not self.token():
            self.message("The admin token is not readable by this account; the hardware choice can only be changed by the operator who installed SemSearch.", kind="warn")
            return False
        try:
            with self._client() as c:
                r = c.post("/config/devices", json={"profile": profile})
                if r.status_code >= 400:
                    raise RuntimeError(r.json().get("detail", r.text))
        except Exception as e:  # noqa: BLE001
            self.message(f"could not change the indexing hardware: {e}", kind="error")
            return False
        self.fetch_devices()
        self.refresh()
        return True

    def ask_hardware_if_unset(self) -> str | None:
        """Ask once (after install, or on an upgrade that predates the choice) when there is a
        real choice to make. Cancel leaves it unset, so the next tray start asks again."""
        import win32api
        import win32con
        opts = self.fetch_devices()
        if not opts or not opts.get("ask") or not opts.get("editable") or not self.token():
            return None
        ans = win32api.MessageBox(0, hardware_question(opts), "SemSearch: indexing hardware",
                                  win32con.MB_YESNOCANCEL | win32con.MB_ICONQUESTION | win32con.MB_SETFOREGROUND)
        profile = {win32con.IDYES: "light", win32con.IDNO: "gpu"}.get(ans)
        if profile and self.set_hardware(profile):
            return profile
        return None

    def service(self, action: str):
        from .cli import service_control
        rc = service_control(action, self.cfg.service.name)
        if rc != 0:
            self.message(f"service {action} failed (see semsearch logs)", kind="warn")
        self.refresh()

    def quit(self, *_):
        self._stop.set()
        if self.icon:
            self.icon.stop()

    # ---- menu ----
    def build_menu(self):
        import pystray
        from pystray import Menu, MenuItem as Item
        paused = bool(((self.status or {}).get("indexer") or {}).get("paused"))
        roots = self.roots()
        folders = Menu(
            Item("Add folder...", self.add_folder),
            Item("Add the folders Windows Search indexes...", self.use_windows_scope),
            Item("Adopt Windows Search exclusion rules...", self.adopt_windows_excludes),
            Menu.SEPARATOR,
            *[Item(f"Remove {r}", (lambda p: (lambda *_: self.remove_folder(p)))(r)) for r in roots],
        )
        dev = self.devices or {}
        hw_items = [Item(hardware_label(p, dev), (lambda q: (lambda *_: self.set_hardware(q)))(p),
                         checked=(lambda q: (lambda _item: dev.get("profile") == q))(p), radio=True,
                         enabled=bool((dev.get("options") or {}).get(p)) and bool(dev.get("editable")))
                    for p in ("light", "gpu")]
        return Menu(
            Item(status_line(self.health, self.status), None, enabled=False),
            Item("Open settings page", self.open_settings, default=True),
            Item("Resume indexing" if paused else "Pause indexing", self.toggle_pause, enabled=self.health is not None),
            Item("Folders", folders),
            Item("Indexing hardware", Menu(*hw_items), enabled=bool(dev)),
            Item("Open logs folder", self.open_logs),
            Item("Service", Menu(Item("Start", lambda *_: self.service("start")), Item("Stop", lambda *_: self.service("stop")), Item("Restart", lambda *_: self.service("restart")))),
            Menu.SEPARATOR,
            Item("Quit tray (the service keeps running)", self.quit),
        )

    def refresh(self) -> None:
        self.poll()
        state = icon_state(self.health, self.status)
        if self.icon is not None:
            if state != self._state:
                self.icon.icon = make_icon_image(state=state)
                self._state = state
            self.icon.title = status_line(self.health, self.status)[:127]
            self.icon.menu = self.build_menu()

    def _first_run(self) -> None:
        # the installer starts the tray while the service may still be warming up: wait for it
        # (up to ~2 minutes) before deciding whether to ask
        for _ in range(24):
            if self._stop.is_set():
                return
            if self.devices is not None or self.fetch_devices() is not None:
                break
            self._stop.wait(5.0)
        try:
            self.ask_hardware_if_unset()
        except Exception as e:  # noqa: BLE001
            log.debug("hardware question: %s", e)

    def _poll_loop(self) -> None:
        while not self._stop.wait(10.0):
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001
                log.debug("tray refresh: %s", e)

    def run(self) -> int:
        import pystray
        self.poll()
        self.fetch_devices()
        self._state = icon_state(self.health, self.status)
        self.icon = pystray.Icon("semsearch", make_icon_image(state=self._state), status_line(self.health, self.status)[:127], self.build_menu())
        threading.Thread(target=self._poll_loop, name="semsearch-tray-poll", daemon=True).start()
        threading.Thread(target=self._first_run, name="semsearch-tray-ask", daemon=True).start()
        self.icon.run()
        return 0


def _single_instance() -> bool:
    try:
        import win32api
        import win32event
        import winerror
        win32event.CreateMutex(None, False, MUTEX_NAME)
        return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
    except ImportError:
        return True


def main(argv: list[str] | None = None) -> int:
    from .config import load_config, machine_config_path
    from .service import _null_std_streams
    _null_std_streams()
    argv = sys.argv[1:] if argv is None else argv
    cfg_path = argv[argv.index("--config") + 1] if "--config" in argv else (str(machine_config_path()) if machine_config_path().is_file() else None)
    cfg = load_config(cfg_path)
    logging.basicConfig(level=logging.WARNING)
    if not _single_instance():
        return 0
    return TrayApp(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
