"""Claude Desktop account profiles: one --user-data-dir per account, one app open at a time."""
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
STORE = os.path.join(HOME, ".claude-accounts")
INDEX = os.path.join(STORE, "profiles.json")
PROFILES_DIR = os.path.join(STORE, "profiles")
LAUNCHER = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "WindowsApps", "claude-desktop.exe")
LOCAL = os.environ["LOCALAPPDATA"]


def _seed_dirs():
    """Existing Claude Desktop data folders on this machine (imported on first run)."""
    pats = [os.path.join(os.environ.get("APPDATA", ""), "Claude"),
            os.path.join(LOCAL, "Claude-Data", "*"),
            os.path.join(LOCAL, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"),
            os.path.join(HOME, ".claude-desktop-*")]
    import glob
    out = []
    for p in pats:
        for d in sorted(glob.glob(p)):
            if os.path.exists(os.path.join(d, "config.json")) and os.path.exists(os.path.join(d, "Local State")):
                out.append(d)
    return out
COLORS = ["#d97757", "#7c5cff", "#1f9d6b", "#e08a1e", "#2f7de1", "#c2408f"]
NO_WINDOW = 0x08000000


# ---------- store
def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def load():
    os.makedirs(STORE, exist_ok=True)
    if os.path.exists(INDEX):
        with open(INDEX, encoding="utf-8") as f:
            idx = json.load(f)
    else:
        idx = {"active": None, "accounts": []}
    known = {_norm(a["dir"]) for a in idx["accounts"]}
    for d in _seed_dirs():
        if _norm(d) not in known and not d.endswith((".old", ".bak")) and ".old-" not in d:
            idx["accounts"].append({"key": d, "dir": d, "name": os.path.basename(d), "email": "?",
                                    "plan": "", "color": COLORS[len(idx["accounts"]) % len(COLORS)]})
    return idx


def save(idx):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)
    os.replace(tmp, INDEX)


# ---------- token cache (Electron safeStorage: DPAPI-wrapped AES-GCM key)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(b):
    i, o = _Blob(len(b), ctypes.create_string_buffer(b, len(b))), _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(i), None, None, None, None, 0, ctypes.byref(o)):
        raise OSError("DPAPI falhou")
    r = ctypes.string_at(o.pbData, o.cbData)
    ctypes.windll.kernel32.LocalFree(o.pbData)
    return r


def _tokens(d):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(os.path.join(d, "Local State"), encoding="utf-8") as f:
        key = _dpapi(base64.b64decode(json.load(f)["os_crypt"]["encrypted_key"])[5:])
    with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    raw = base64.b64decode(cfg.get("oauth:tokenCacheV2") or "")
    if not raw:
        return {}
    return json.loads(AESGCM(key).decrypt(raw[3:15], raw[15:], None))


def _best_token(d):
    toks = _tokens(d)
    if not toks:
        raise RuntimeError("Sem login neste perfil ainda.")
    k, v = max(toks.items(), key=lambda kv: kv[1].get("expiresAt", 0))
    acct, org = k.split("|")[0][5:], k.split(":")[2]
    return v, acct, org


def _api(path, token):
    req = urllib.request.Request("https://api.anthropic.com" + path, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def refresh_info(idx, acc):
    """Fill name/email/plan from the profile's own login."""
    tok, acct, org = _best_token(acc["dir"])
    acc["account_uuid"], acc["org_uuid"] = acct, org
    if idx.get("active") == acc["key"]:
        idx["active"] = acct
    acc["key"] = acct
    acc["plan"] = (tok.get("subscriptionType") or "").capitalize()
    if tok["expiresAt"] > time.time() * 1000:
        p = _api("/api/oauth/profile", tok["token"])
        a = p.get("account", {})
        acc["email"] = a.get("email", acc["email"])
        acc["name"] = a.get("display_name") or a.get("full_name") or acc["email"].split("@")[0]
    save(idx)
    return acc


def usage(idx, key):
    acc = next(a for a in idx["accounts"] if a["key"] == key)
    tok, _, _ = _best_token(acc["dir"])
    if tok["expiresAt"] < time.time() * 1000:
        raise RuntimeError("Login expirado: abra esta conta para atualizar.")
    d = _api("/api/oauth/usage", tok["token"])
    now = dt.datetime.now(dt.timezone.utc)

    def win(w):
        if not w:
            return None
        left = max(0, 100 - int(round(w.get("utilization") or 0)))
        r = w.get("resets_at")
        secs = int((dt.datetime.fromisoformat(r) - now).total_seconds()) if r else 0
        return left, max(secs, 0)
    return {"session": win(d.get("five_hour")), "weekly": win(d.get("seven_day"))}


# ---------- processes
def _desktop_mains():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='Claude.exe'\" | Where-Object { "
          "$_.ExecutablePath -match 'WindowsApps\\\\Claude_' -and $_.CommandLine -notmatch '--type=' } | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return [l.split("|", 1) for l in out.splitlines() if "|" in l]


def default_dir():
    """Folder the app uses when opened without --user-data-dir (Start menu / taskbar icon)."""
    import glob
    for p in sorted(glob.glob(os.path.join(LOCAL, "Claude-Data", "claude_*"))) + [
            os.path.join(os.environ.get("APPDATA", ""), "Claude")]:
        if os.path.exists(os.path.join(p, "config.json")):
            return p
    return None


def running_dir():
    mains = _desktop_mains()
    for _, cmd in mains:
        if "--user-data-dir=" in cmd:
            return cmd.split("--user-data-dir=", 1)[1].strip().strip('"').split('" ')[0].strip('"')
    if mains:                      # opened from the normal Claude icon: that is the default folder
        return default_dir()
    return None


def detect_active(idx):
    d = running_dir()
    if d:
        for a in idx["accounts"]:
            if _norm(a["dir"]) == _norm(d):
                idx["active"] = a["key"]
    return idx


# ---------- shared Code-tab sessions
# Claude Desktop refuses to write into a session folder that is a link ("symlink/file plant"
# protection), so every profile keeps a real folder and the switcher copies the session
# entries between them while Claude is closed.
def _sessions_dir(acc):
    return os.path.join(acc["dir"], "claude-code-sessions", acc["account_uuid"], acc["org_uuid"])


def _is_link(p):
    try:
        return os.path.isdir(p) and _norm(os.path.realpath(p)) != _norm(p)
    except OSError:
        return False


def _unlink_dir(p):
    """Turn a junction left by older versions into a real folder with the same files."""
    target = os.path.realpath(p)
    files = [(f, os.path.join(target, f)) for f in os.listdir(p)] if os.path.isdir(target) else []
    subprocess.run(["cmd", "/c", "rmdir", p], capture_output=True, creationflags=NO_WINDOW)  # removes the link only
    os.makedirs(p, exist_ok=True)
    for name, src in files:
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(p, name))
        elif os.path.isdir(src):
            shutil.copytree(src, os.path.join(p, name), dirs_exist_ok=True)


def sync_sessions(idx):
    """Two-way merge of Code-tab session entries between all profiles (call only with Claude closed).

    local_<id>.json: newest copy wins. deleted_<id>: the session is removed everywhere.
    """
    dirs = []
    for a in idx["accounts"]:
        if a.get("account_uuid") and a.get("org_uuid"):
            p = _sessions_dir(a)
            if _is_link(p):
                _unlink_dir(p)
            os.makedirs(p, exist_ok=True)
            dirs.append(p)
    if len(dirs) < 2:
        return 0
    deleted, best = set(), {}
    for d in dirs:
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if f.startswith("deleted_"):
                deleted.add(f[len("deleted_"):])
            elif f.startswith("local_") and f.endswith(".json") and os.path.isfile(fp):
                m = os.path.getmtime(fp)
                if f not in best or m > best[f][0]:
                    best[f] = (m, fp)
    changed = 0
    for d in dirs:
        for sid in deleted:
            marker = os.path.join(d, "deleted_" + sid)
            if not os.path.exists(marker):
                open(marker, "w").close()
            stale = os.path.join(d, "local_" + sid + ".json")
            if os.path.exists(stale):
                os.remove(stale)
                changed += 1
        for f, (m, src) in best.items():
            if f[len("local_"):-len(".json")] in deleted:
                continue
            dst = os.path.join(d, f)
            if _norm(src) != _norm(dst) and (not os.path.exists(dst) or os.path.getmtime(dst) < m):
                shutil.copy2(src, dst)
                changed += 1
    return changed


# ---------- actions
def close_app():
    mains = _desktop_mains()
    for pid, _ in mains:
        subprocess.run(["taskkill", "/T", "/PID", pid], capture_output=True, creationflags=NO_WINDOW)
    for _ in range(20):
        if not _desktop_mains():
            break
        time.sleep(0.5)
    for pid, _ in _desktop_mains():
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(1.5)
    return bool(mains)


def launch(acc):
    subprocess.Popen([LAUNCHER, f"--user-data-dir={acc['dir']}"], creationflags=NO_WINDOW)


def open_last():
    """Open Claude in the account used last (what the 'Claude (última conta)' shortcut runs)."""
    idx = load()
    if running_dir():
        # already open: just bring it forward by asking the same profile again (single-instance app)
        idx = detect_active(idx)
    else:
        try:
            sync_sessions(idx)          # Claude is closed: bring every profile's session list up to date
        except Exception:  # noqa: BLE001
            pass
    acc = next((a for a in idx["accounts"] if a["key"] == idx.get("active")), None)
    if acc is None and idx["accounts"]:
        acc = idx["accounts"][0]
    if acc is None:
        subprocess.Popen([LAUNCHER], creationflags=NO_WINDOW)
        return
    launch(acc)


def switch(idx, key):
    acc = next(a for a in idx["accounts"] if a["key"] == key)
    close_app()
    try:
        sync_sessions(idx)
    except Exception:  # noqa: BLE001
        pass
    launch(acc)
    idx["active"] = key
    save(idx)
    return True


def add_account(idx):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(PROFILES_DIR, f"conta{n}")):
        n += 1
    d = os.path.join(PROFILES_DIR, f"conta{n}")
    os.makedirs(d)
    acc = {"key": d, "dir": d, "name": f"Nova conta {n}", "email": "faça login no app", "plan": "",
           "color": COLORS[len(idx["accounts"]) % len(COLORS)]}
    idx["accounts"].append(acc)
    save(idx)
    close_app()
    launch(acc)
    idx["active"] = acc["key"]
    save(idx)
    return acc


def remove_account(idx, key):
    if key == idx.get("active") and running_dir():
        raise RuntimeError("Troque para outra conta antes de remover a que está aberta.")
    idx["accounts"] = [a for a in idx["accounts"] if a["key"] != key]
    save(idx)  # profile folder is kept on disk; only removed from the switcher
