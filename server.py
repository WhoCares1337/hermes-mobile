"""Phone view for Hermes Desktop bots, over a private network (e.g. Tailscale).

- Read: Bot Chat transcript of every Hermes profile, from its state.db (read-only).
- Switch: clicks the bot's button in the Hermes Desktop sidebar (UI Automation).
- Write: pastes text into the Hermes Desktop window and presses Enter, only for
  the bot that is currently active in the desktop.
The desktop stays the only writer. Windows only (PowerShell + UI Automation).

Setup:   python server.py --init [--host <private-ip>]
Run:     python server.py
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
from version import VERSION
TOKEN = ""
CONFIG_FILE = os.path.join(HERE, "config.json")
TOKEN_FILE = os.path.join(HERE, "token.txt")
ACTIVE_FILE = os.path.join(HERE, "active.txt")
OUTBOX = os.path.join(HERE, "outbox.txt")
MSG_LIMIT = 120
UPLOAD_DIR = os.path.join(HERE, "uploads")
MAX_UPLOAD = 15 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif"}


def hermes_home():
    if os.environ.get("HERMES_HOME"):
        return os.path.expandvars(os.environ["HERMES_HOME"])
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "hermes")


def tailscale_ip():
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip().splitlines()[0] if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def load_config():
    if not os.path.exists(CONFIG_FILE):
        sys.exit("config.json is missing. Run: python server.py --init [--host <ip>]")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def discover_bots(overrides):
    """One bot per Hermes profile that has desktop sessions.

    The default profile is 'hermes'; a named profile 'x' is 'x'.
    Label = Title Case of the id; UI handle = '@' + id. Override any of it in
    config.json under "bots": {"<id>": {"label": ..., "ui_name": ..., "db": ...}}.
    """
    home = hermes_home()
    candidates = [("hermes", os.path.join(home, "state.db"))]
    prof_dir = os.path.join(home, "profiles")
    if os.path.isdir(prof_dir):
        for name in sorted(os.listdir(prof_dir)):
            db = os.path.join(prof_dir, name, "state.db")
            if os.path.exists(db):
                candidates.append((name, db))
    bots = {}
    for bid, db in candidates:
        if bid in overrides and "db" in overrides[bid]:
            db = os.path.expandvars(overrides[bid]["db"])
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            n = con.execute("SELECT COUNT(*) FROM sessions WHERE source='desktop'").fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
        if n == 0 and bid not in overrides:
            continue
        ov = overrides.get(bid, {})
        bots[bid] = {
            "label": ov.get("label", bid.replace("-", " ").title()),
            "ui_name": ov.get("ui_name", "@" + bid),
            "db": db,
        }
    return bots


CFG = load_config() if os.path.exists(CONFIG_FILE) else {}
HOST = CFG.get("host", "127.0.0.1")
PORT = int(CFG.get("port", 8765))
DESKTOP_WINDOW = CFG.get("desktop_window", "Hermes")
BOTS = discover_bots(CFG.get("bots", {}))


def refresh_bots():
    """Re-scan profiles so bots created after server start (new profile/sessions) show up."""
    global BOTS
    BOTS = discover_bots(CFG.get("bots", {}))

lock = threading.Lock()  # one desktop action at a time
state = {"error": ""}


def get_active():
    if os.path.exists(ACTIVE_FILE):
        v = open(ACTIVE_FILE, encoding="utf-8").read().strip()
        if v in BOTS:
            return v
    return next(iter(BOTS), "")


def set_active(bot):
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(bot)


def bot_session(bot):
    """Bot Chat session of the bot; falls back to its newest desktop session."""
    try:
        con = sqlite3.connect(f"file:{BOTS[bot]['db']}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT id FROM sessions WHERE source='desktop' AND title='Bot Chat' "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            row = con.execute(
                "SELECT id FROM sessions WHERE source='desktop' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return row[0] if row else None


PREVIEW_CHARS = 8000   # read at most this much of each message from the DB
SHOW_CHARS = 4000      # show at most this much of each message on the phone
_TEXT_PART = re.compile(r'"type": "text", "text": "((?:[^"\\]|\\.)*)"')


def _display_text(raw):
    """Turn a stored message into plain text. Multimodal rows are stored as
    '\\x00json:[...]' with base64 images inline; keep the text, show images as [Bild]."""
    if raw.startswith("\x00json:"):
        texts = []
        for part in _TEXT_PART.findall(raw):
            try:
                texts.append(json.loads('"' + part + '"'))
            except ValueError:
                pass
        text = "\n".join(texts)
        if '"image_url"' in raw:
            text = (text + "\n" if text else "") + "[Bild]"
    else:
        text = raw
    if len(text) > SHOW_CHARS:
        text = text[:SHOW_CHARS] + " …"
    return text


def load_messages(bot):
    sid = bot_session(bot)
    if not sid:
        return []
    con = sqlite3.connect(f"file:{BOTS[bot]['db']}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT role, substr(content, 1, ?), timestamp FROM messages "
            "WHERE session_id=? AND role IN ('user','assistant') AND content IS NOT NULL AND content <> '' "
            "ORDER BY id DESC LIMIT ?", (PREVIEW_CHARS, sid, MSG_LIMIT)).fetchall()
    finally:
        con.close()
    msgs = [{"role": r, "text": _display_text(c), "ts": t}
            for r, c, t in reversed(rows) if c and c.strip()]
    return msgs


def list_bots():
    return [{"id": b, "label": BOTS[b]["label"], "session": bot_session(b)}
            for b in BOTS]


def _ps_quote(s):
    return s.replace("'", "''")


# Common preamble: find the desktop window and bring it to the front.
PS_HEAD = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class HmFg {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
}
"@
$ws = New-Object -ComObject WScript.Shell
if (-not $ws.AppActivate('__WINDOW__')) { exit 2 }
Start-Sleep -Milliseconds 500
# Guard: never send keys unless the desktop window really is in front.
$sb = New-Object System.Text.StringBuilder 512
[void][HmFg]::GetWindowText([HmFg]::GetForegroundWindow(), $sb, 512)
if (-not $sb.ToString().EndsWith('__WINDOW__')) { exit 5 }
"""

# Text goes through the clipboard so umlauts and SendKeys special characters
# are never interpreted as keys.
PS_SEND = PS_HEAD + r"""
$text = [System.IO.File]::ReadAllText('__OUTBOX__', [System.Text.Encoding]::UTF8)
Set-Clipboard -Value $text
Start-Sleep -Milliseconds 200
$ws.SendKeys('^v')
Start-Sleep -Milliseconds 300
$ws.SendKeys('{ENTER}')
"""

# Clicks the bot button in the sidebar via UI Automation (InvokePattern).
PS_SWITCH = PS_HEAD + r"""
Add-Type -AssemblyName UIAutomationClient
$root = [System.Windows.Automation.AutomationElement]::RootElement
$nameCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NameProperty, '__WINDOW__')
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $nameCond)
if (-not $win) { exit 2 }
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button)
$buttons = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
foreach ($b in $buttons) {
  $n = $b.Current.Name
  if ($n -and $n.Contains('__UINAME__')) {
    $p = $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $p.Invoke()
    exit 0
  }
}
exit 3
"""


def _ps(script, sta=False):
    # Write to a .ps1 file and run with -File: multi-line scripts passed via
    # -Command get mangled on the command line, which broke exit codes.
    path = os.path.join(HERE, "run.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if sta:
        cmd.append("-Sta")
    cmd += ["-File", path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _fill(template, **kw):
    s = template.replace("__WINDOW__", _ps_quote(DESKTOP_WINDOW))
    for k, v in kw.items():
        s = s.replace(f"__{k.upper()}__", _ps_quote(v))
    return s


def _set_error(r):
    if r.returncode == 2:
        state["error"] = T("err_window_missing", w=DESKTOP_WINDOW)
    elif r.returncode == 5:
        state["error"] = T("err_not_foreground")
    elif r.returncode == 3:
        state["error"] = T("err_bot_button")
    elif r.returncode != 0:
        state["error"] = (r.stderr or r.stdout)[-800:]
    else:
        state["error"] = ""


def _run_send(text):
    try:
        with open(OUTBOX, "w", encoding="utf-8") as f:
            f.write(text)
        _set_error(_ps(_fill(PS_SEND, outbox=OUTBOX)))
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)
    finally:
        lock.release()


# Puts an image on the clipboard and pastes it into the focused desktop chat.
PS_IMAGE = PS_HEAD + r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('__IMAGE__')
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
Start-Sleep -Milliseconds 300
$ws.SendKeys('^v')
Start-Sleep -Milliseconds 1500
$ws.SendKeys('{ENTER}')
"""


def _run_image(path):
    try:
        r = _ps(_fill(PS_IMAGE, image=path), sta=True)
        _set_error(r)
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)
    finally:
        lock.release()


def _run_switch(bot):
    try:
        r = _ps(_fill(PS_SWITCH, uiname=BOTS[bot]["ui_name"]))
        _set_error(r)
        if r.returncode == 0:
            set_active(bot)
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)
    finally:
        lock.release()


# UI and message texts. Language is set by "lang" in config.json (en default, de).
STR = {
    "en": {
        "activate": "Activate in desktop", "placeholder": "Message...", "image_title": "Image",
        "send": "Send", "error_status": "Error ", "input_to": "Input goes to: ",
        "viewing": "Viewing: ", "activate_hint": " (activate here to write)",
        "busy_loading": "Desktop busy...", "last_error": "Last error: ",
        "busy_retry": "Desktop busy, try again", "not_active": "Not active: activate in desktop first",
        "send_failed": "Send failed: ", "unknown_bot": "Unknown bot", "activate_failed": "Activate failed: ",
        "switching": "Desktop switching...", "only_images": "Images only", "uploading": "Uploading image...",
        "upload_failed": "Upload failed: ", "inserting": "Inserting image in desktop...",
        "image_send_failed": "Image send failed: ",
        "err_window_missing": "Desktop window '{w}' not found. Is the app open (not minimized)?",
        "err_not_foreground": "Hermes was not in front. Nothing was inserted, please send again.",
        "err_bot_button": "Bot button not found (sidebar collapsed?)",
        "status_starting": "starting", "status_ok": "running, desktop open",
        "status_no_desktop": "running, desktop window not found", "status_no_agent": "running, no Hermes agent found", "status_stopped": "stopped",
        "menu_status": "Status", "menu_version": "Version", "menu_restart": "Restart",
        "menu_reconnect": "Reconnect (check Tailscale IP)", "menu_copy_url": "Copy URL",
        "menu_open_log": "Open log", "menu_autostart": "Start at login", "menu_quit": "Quit",
        "menu_language": "Language",
        "setup_needed": "Not set up yet. Run once in this folder:",
    },
    "de": {
        "activate": "Im Desktop aktivieren", "placeholder": "Nachricht...", "image_title": "Bild",
        "send": "Senden", "error_status": "Fehler ", "input_to": "Eingabe geht in: ",
        "viewing": "Angesehen: ", "activate_hint": " (zum Schreiben hier aktivieren)",
        "busy_loading": "Desktop beschäftigt...", "last_error": "Letzter Fehler: ",
        "busy_retry": "Desktop beschäftigt, gleich nochmal", "not_active": "Nicht aktiv: erst im Desktop aktivieren",
        "send_failed": "Senden fehlgeschlagen: ", "unknown_bot": "Unbekannter Bot",
        "activate_failed": "Aktivieren fehlgeschlagen: ", "switching": "Desktop wechselt...",
        "only_images": "Nur Bilder", "uploading": "Lade Bild hoch...",
        "upload_failed": "Upload fehlgeschlagen: ", "inserting": "Füge Bild im Desktop ein...",
        "image_send_failed": "Bild senden fehlgeschlagen: ",
        "err_window_missing": "Desktop-Fenster '{w}' nicht gefunden. Ist die App offen (nicht minimiert)?",
        "err_not_foreground": "Hermes war nicht im Vordergrund. Nichts wurde eingefügt, bitte nochmal senden.",
        "err_bot_button": "Bot-Schaltfläche nicht gefunden (Sidebar eingeklappt?)",
        "status_starting": "startet", "status_ok": "läuft, Desktop offen",
        "status_no_desktop": "läuft, Desktop nicht gefunden", "status_no_agent": "läuft, kein Hermes-Agent gefunden", "status_stopped": "gestoppt",
        "menu_status": "Status", "menu_version": "Version", "menu_restart": "Neu starten",
        "menu_reconnect": "Neu verbinden (Tailscale-IP prüfen)", "menu_copy_url": "URL kopieren",
        "menu_open_log": "Log öffnen", "menu_autostart": "Autostart bei Anmeldung", "menu_quit": "Beenden",
        "menu_language": "Sprache",
        "setup_needed": "Noch nicht eingerichtet. Im Ordner einmal ausführen:",
    },
}
LANG = CFG.get("lang", "en") if CFG.get("lang") in ("en", "de") else "en"


def T(key, **kw):
    text = STR[LANG][key]
    return text.format(**kw) if kw else text


def render_page():
    page = PAGE
    for key, value in STR[LANG].items():
        page = page.replace("{{" + key + "}}", value)
    return page


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes</title><style>
body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee;
display:flex;flex-direction:column;height:100vh}
#top{display:flex;gap:6px;padding:8px;background:#1b1b1b;border-bottom:1px solid #333}
#sel{flex:1;font-size:15px;padding:8px;border-radius:8px;background:#222;color:#eee;border:1px solid #444}
#act{font-size:14px;padding:0 12px;border-radius:8px;border:0;background:#2d6cdf;color:#fff}
#info{font-size:12px;color:#aaa;padding:6px 10px;background:#161616}
#log{flex:1;overflow-y:auto;padding:12px;box-sizing:border-box}
.m{margin:0 0 14px;white-space:pre-wrap;word-wrap:break-word;line-height:1.4}
.u{color:#9ecbff}.a{color:#eee}.r{font-size:12px;color:#888;margin-bottom:2px}
#bar{display:flex;gap:8px;padding:8px;background:#1b1b1b;border-top:1px solid #333}
#t{flex:1;font-size:16px;padding:10px;border-radius:8px;border:1px solid #444;
background:#222;color:#eee;resize:none}
#b{font-size:16px;padding:0 16px;border-radius:8px;border:0;background:#2d6cdf;color:#fff}
button:disabled,#t:disabled{opacity:.4}
#st{font-size:12px;color:#aaa;padding:0 10px 6px}
</style></head><body>
<div id="top"><select id="sel"></select><button id="act">{{activate}}</button></div>
<div id="info"></div>
<div id="log"></div><div id="st"></div>
<div id="bar"><input type="file" id="f" accept="image/*" style="display:none">
<button id="ib" title="{{image_title}}">📷</button><textarea id="t" rows="2" placeholder="{{placeholder}}"></textarea>
<button id="b">{{send}}</button></div>
<script>
const TOK=new URLSearchParams(location.search).get("t")||"";
const q=(p,extra="")=>p+"?t="+encodeURIComponent(TOK)+extra;
const log=document.getElementById("log"),sel=document.getElementById("sel");
const st=document.getElementById("st"),info=document.getElementById("info");
const t=document.getElementById("t"),btn=document.getElementById("b"),act=document.getElementById("act");
const fi=document.getElementById("f"),ib=document.getElementById("ib");
let atBottom=true, active="", bots=[];
log.addEventListener("scroll",()=>{atBottom=log.scrollTop+log.clientHeight>log.scrollHeight-40});
sel.addEventListener("change",()=>load());
async function loadBots(){
 const r=await fetch(q("/api/bots"));if(!r.ok)return;
 const d=await r.json();active=d.active;bots=d.bots;
 const cur=sel.value||active;sel.innerHTML="";
 for(const b of bots){
  const o=document.createElement("option");o.value=b.id;
  o.textContent=(b.id===active?"● ":"")+b.label;
  sel.append(o);
 }
 sel.value=cur;
}
function label(id){const b=bots.find(x=>x.id===id);return b?b.label:id}
async function load(){
 if(!sel.value)await loadBots();
 const r=await fetch(q("/api/messages",`&bot=${encodeURIComponent(sel.value)}`));
 if(!r.ok){st.textContent="{{error_status}}"+r.status;return}
 const d=await r.json();
 const canWrite=sel.value===d.active;
 info.textContent=canWrite
   ? "{{input_to}}"+label(d.active)
   : "{{viewing}}"+label(sel.value)+" · {{input_to}}"+label(d.active)+"{{activate_hint}}";
 t.disabled=btn.disabled=ib.disabled=!canWrite;act.disabled=canWrite;
 log.innerHTML="";
 for(const m of d.messages){
  const div=document.createElement("div");div.className="m "+(m.role==="user"?"u":"a");
  const h=document.createElement("div");h.className="r";h.textContent=m.role==="user"?"Du":label(sel.value);
  const p=document.createElement("div");p.textContent=m.text;
  div.append(h,p);log.append(div);
 }
 if(atBottom)log.scrollTop=log.scrollHeight;
 st.textContent=d.busy?"{{busy_loading}}":(d.error?"{{last_error}}"+d.error:"");
}
async function send(){
 const text=t.value.trim();if(!text)return;
 const r=await fetch(q("/api/send"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({text,bot:sel.value})});
 if(r.status===409){st.textContent="{{busy_retry}}";return}
 if(r.status===403){st.textContent="{{not_active}}";return}
 if(!r.ok){st.textContent="{{send_failed}}"+r.status;return}
 t.value="";atBottom=true;setTimeout(load,1500);
}
async function activate(){
 const r=await fetch(q("/api/activate"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({bot:sel.value})});
 if(r.status===409){st.textContent="{{busy_retry}}";return}
 if(r.status===400){st.textContent="{{unknown_bot}}";return}
 if(!r.ok){st.textContent="{{activate_failed}}"+r.status;return}
 st.textContent="{{switching}}";setTimeout(async()=>{await loadBots();load()},2500);
}
btn.onclick=send;act.onclick=activate;
ib.onclick=()=>fi.click();
fi.onchange=async()=>{
 const file=fi.files[0];fi.value="";if(!file)return;
 if(!file.type.startsWith("image/")){st.textContent="{{only_images}}";return}
 st.textContent="{{uploading}}";
 const up=await fetch(q("/api/upload"),{method:"POST",headers:{"Content-Type":file.type},body:file});
 if(!up.ok){st.textContent="{{upload_failed}}"+up.status;return}
 const d=await up.json();
 st.textContent="{{inserting}}";
 const r=await fetch(q("/api/send_image"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({bot:sel.value,file:d.file})});
 if(r.status===409){st.textContent="{{busy_retry}}";return}
 if(r.status===403){st.textContent="{{not_active}}";return}
 if(!r.ok){st.textContent="{{image_send_failed}}"+r.status;return}
 atBottom=true;setTimeout(load,4000);
};
loadBots().then(load);setInterval(load,4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self, q):
        return secrets.compare_digest(q.get("t", [""])[0], TOKEN)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return self._send(403, "forbidden", "text/plain")
        if u.path == "/":
            return self._send(200, render_page(), "text/html")
        if u.path == "/api/bots":
            refresh_bots()
            try:
                return self._send(200, json.dumps({
                    "active": get_active(), "bots": list_bots()}, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/messages":
            bot = q.get("bot", [""])[0] or get_active()
            if bot not in BOTS:
                return self._send(400, "unknown bot", "text/plain")
            try:
                msgs = load_messages(bot)
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({
                "messages": msgs, "active": get_active(),
                "busy": lock.locked(), "error": state["error"]}, ensure_ascii=False))
        if u.path == "/api/version":
            return self._send(200, json.dumps({"version": VERSION}))
        self._send(404, "not found", "text/plain")

    def _upload(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = IMAGE_TYPES.get(ctype)
        if not ext:
            return self._send(415, "unsupported type", "text/plain")
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            return self._send(413, "bad size", "text/plain")
        data = self.rfile.read(n)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + ext
        with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
            f.write(data)
        self._send(200, json.dumps({"file": name}))

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return self._send(403, "forbidden", "text/plain")
        if u.path == "/api/upload":
            return self._upload()
        refresh_bots()
        try:
            body = self._body()
        except Exception:  # noqa: BLE001
            return self._send(400, "bad request", "text/plain")
        bot = str(body.get("bot", ""))

        if u.path == "/api/activate":
            if bot not in BOTS:
                return self._send(400, "unknown bot", "text/plain")
            if not lock.acquire(blocking=False):
                return self._send(409, "busy", "text/plain")
            threading.Thread(target=_run_switch, args=(bot,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))

        if u.path == "/api/send_image":
            name = os.path.basename(str(body.get("file", "")))
            path = os.path.join(UPLOAD_DIR, name)
            if not name or not os.path.isfile(path):
                return self._send(400, "unknown file", "text/plain")
            if bot != get_active():
                return self._send(403, "not active", "text/plain")
            if not lock.acquire(blocking=False):
                return self._send(409, "busy", "text/plain")
            threading.Thread(target=_run_image, args=(os.path.abspath(path),),
                             daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))

        if u.path == "/api/send":
            text = str(body.get("text", "")).strip()
            if not text or len(text) > 8000:
                return self._send(400, "bad text", "text/plain")
            if bot != get_active():
                return self._send(403, "not active", "text/plain")
            if not lock.acquire(blocking=False):
                return self._send(409, "busy", "text/plain")
            threading.Thread(target=_run_send, args=(text,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))

        self._send(404, "not found", "text/plain")


def init(argv):
    host = None
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    host = host or tailscale_ip()
    if not host:
        sys.exit("No IP found. Run: python server.py --init --host <private-ip>")
    cfg = {"host": host, "port": 8765, "desktop_window": "Hermes", "bots": {}, "lang": "en"}
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(24))
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    print(f"config.json written. Bots found: {', '.join(discover_bots({})) or 'none'}")
    print(f"URL: http://{host}:8765/?t={token}")


def start_server(host, port, token):
    """Start the HTTP server in a background thread and return it (tray app)."""
    global TOKEN
    TOKEN = token
    httpd = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    if "--init" in sys.argv:
        init(sys.argv)
    elif "--version" in sys.argv:
        print(VERSION)
    else:
        TOKEN = open(TOKEN_FILE, encoding="utf-8").read().strip() if os.path.exists(TOKEN_FILE) else ""
        if not TOKEN:
            sys.exit("token.txt is missing. Run first: python server.py --init")
        print(f"listening on http://{HOST}:{PORT}/?t={TOKEN}", flush=True)
        ThreadingHTTPServer((HOST, PORT), H).serve_forever()
