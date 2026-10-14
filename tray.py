"""Tray app for the Hermes phone view: runs the server, shows status, restarts it.

Runs in the user session (needed for the desktop automation). Not a Windows service.
"""
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import pystray
from PIL import Image, ImageDraw

import server as srv

APP_NAME = "Hermes Mobile"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "HermesMobile"
SCAN_INTERVAL = 15 * 60  # seconds: re-scan Hermes profiles so a late-starting agent gets picked up
HERE = srv.HERE
LOG_FILE = os.path.join(HERE, "tray.log")


def setup_logging():
    log = logging.getLogger()
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)


def single_instance():
    """Exit if another tray instance already holds the named mutex."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "HermesMobileTraySingleton")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)


def message_box(text):
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)


def desktop_window_open():
    return bool(ctypes.windll.user32.FindWindowW(None, srv.DESKTOP_WINDOW))


def port_answers(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


def asset(name):
    base = sys._MEIPASS if getattr(sys, "frozen", False) else HERE
    return os.path.join(base, "assets", name)


_LOGO = None


def make_icon(color):
    """Logo on a dark rounded backdrop (readable on light and dark taskbars),
    with a status dot in the lower right corner."""
    global _LOGO
    if _LOGO is None:
        _LOGO = Image.open(asset("tray.png")).convert("RGBA").resize((52, 52), Image.LANCZOS)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=12, fill=(14, 32, 58, 255))
    img.alpha_composite(_LOGO, (6, 6))
    d = ImageDraw.Draw(img)
    d.ellipse((38, 38, 62, 62), fill=color, outline=(20, 20, 20, 255), width=2)
    return img


GREEN, YELLOW, RED = (46, 160, 67, 255), (230, 170, 0, 255), (200, 40, 40, 255)


class App:
    def __init__(self):
        self.httpd = None
        self.started_at = None
        self.stopping = False
        self.status = srv.T("status_starting")
        self.icon = None
        self.lock = threading.Lock()

    # ---- server lifecycle ----
    def server_running(self):
        return self.httpd is not None and port_answers(srv.HOST, srv.PORT)

    def start(self):
        with self.lock:
            try:
                token = open(srv.TOKEN_FILE, encoding="utf-8").read().strip()
                self.httpd = srv.start_server(srv.HOST, srv.PORT, token)
                self.started_at = time.time()
                logging.info("server started on %s:%s", srv.HOST, srv.PORT)
            except OSError as e:
                logging.error("server start failed: %s", e)
                self.httpd = None

    def stop(self):
        with self.lock:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
                self.httpd = None
                logging.info("server stopped")

    def restart(self, _=None):
        logging.info("restart requested")
        self.stop()
        time.sleep(1)
        self.start()
        self.refresh()

    def reconnect(self, _=None):
        """Re-read the Tailscale IP; rebind if it changed."""
        ip = srv.tailscale_ip()
        logging.info("reconnect: tailscale ip=%s, configured=%s", ip, srv.HOST)
        if ip and ip != srv.HOST:
            with open(srv.CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["host"] = ip
            with open(srv.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            srv.HOST = ip
        self.restart()

    def watchdog(self):
        """Restart the server if its thread died without a stop request; re-scan bots every SCAN_INTERVAL."""
        last_scan = 0.0
        while not self.stopping:
            time.sleep(10)
            if time.time() - last_scan >= SCAN_INTERVAL:
                last_scan = time.time()
                srv.refresh_bots()
                logging.info("bot scan: %s", ", ".join(srv.BOTS) or "none")
            if self.httpd is not None and not port_answers(srv.HOST, srv.PORT):
                logging.warning("server not answering, restarting")
                self.restart()
            self.refresh()

    # ---- UI ----
    def refresh(self):
        srv_ok = self.server_running()
        desk_ok = desktop_window_open()
        if srv_ok and desk_ok:
            self.status, color = srv.T("status_ok"), GREEN
        elif srv_ok:
            self.status, color = srv.T("status_no_desktop"), YELLOW
        else:
            self.status, color = srv.T("status_stopped"), RED
        if self.icon:
            self.icon.icon = make_icon(color)
            self.icon.title = f"{APP_NAME}: {self.status}"
            self.icon.update_menu()

    def url(self):
        try:
            token = open(srv.TOKEN_FILE, encoding="utf-8").read().strip()
        except OSError:
            token = ""
        return f"http://{srv.HOST}:{srv.PORT}/?t={token}"

    def copy_url(self, _=None):
        subprocess.run(["clip"], input=self.url(), text=True)

    def open_log(self, _=None):
        if not os.path.exists(LOG_FILE):
            open(LOG_FILE, "a").close()
        os.startfile(LOG_FILE)

    def set_lang(self, lang):
        """Switch UI language at runtime and store it in config.json."""
        with open(srv.CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["lang"] = lang
        with open(srv.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        srv.LANG = lang
        logging.info("language set to %s", lang)
        self.refresh()

    def toggle_autostart(self, _=None):
        set_autostart(not autostart_enabled())

    def quit(self, _=None):
        logging.info("quit")
        self.stopping = True
        self.stop()
        self.icon.stop()

    def menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda _: f"{srv.T('menu_status')}: {self.status}", None, enabled=False),
            pystray.MenuItem(lambda _: f"{srv.T('menu_version')} {srv.VERSION}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: srv.T("menu_restart"), self.restart),
            pystray.MenuItem(lambda _: srv.T("menu_reconnect"), self.reconnect),
            pystray.MenuItem(lambda _: srv.T("menu_copy_url"), self.copy_url),
            pystray.MenuItem(lambda _: srv.T("menu_open_log"), self.open_log),
            pystray.MenuItem(lambda _: srv.T("menu_autostart"), self.toggle_autostart,
                             checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: srv.T("menu_quit"), self.quit),
            pystray.MenuItem(lambda _: srv.T("menu_language"), pystray.Menu(
                pystray.MenuItem("English", lambda icon, item: self.set_lang("en"),
                                 checked=lambda _: srv.LANG == "en", radio=True),
                pystray.MenuItem("Deutsch", lambda icon, item: self.set_lang("de"),
                                 checked=lambda _: srv.LANG == "de", radio=True),
            )),
        )

    def run(self):
        self.icon = pystray.Icon("hermes-mobile", make_icon(YELLOW), APP_NAME, self.menu())

        def setup(icon):
            icon.visible = True
            self.start()
            self.refresh()
            threading.Thread(target=self.watchdog, daemon=True).start()

        self.icon.run(setup)


def main():
    if "--version" in sys.argv:
        print(srv.VERSION)
        return
    if "--init" in sys.argv:
        srv.init(sys.argv)
        return
    setup_logging()
    single_instance()
    if not os.path.exists(srv.CONFIG_FILE) or not os.path.exists(srv.TOKEN_FILE):
        message_box(srv.T("setup_needed") + "\n"
                    "hermes-mobile.exe --init")
        return
    logging.info("tray start, version %s", srv.VERSION)
    App().run()


if __name__ == "__main__":
    main()
