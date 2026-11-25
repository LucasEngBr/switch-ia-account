"""Account store and switching logic for Codex (no UI)."""
import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time

HOME = os.path.expanduser("~")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
AUTH = os.path.join(CODEX_HOME, "auth.json")
STORE = os.path.join(HOME, ".codex-accounts")
INDEX = os.path.join(STORE, "accounts.json")
APP_AUMID = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
COLORS = ["#d9452b", "#7c5cff", "#1f9d6b", "#e08a1e", "#2f7de1", "#c2408f", "#4b9fa8"]
NO_WINDOW = 0x08000000


def _claims(auth):
    tok = (auth.get("tokens") or {}).get("id_token") or ""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def describe(auth):
    c = _claims(auth)
    a = c.get("https://api.openai.com/auth", {})
    return {
        "key": (auth.get("tokens") or {}).get("account_id") or a.get("chatgpt_account_id") or c.get("sub"),
        "email": c.get("email", "?"),
        "name": c.get("name") or c.get("email", "?").split("@")[0],
        "plan": (a.get("chatgpt_plan_type") or "").capitalize(),
    }


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_atomic(path, data_path):
    tmp = path + ".switch-tmp"
    shutil.copyfile(data_path, tmp)
    os.replace(tmp, path)


def load():
    os.makedirs(STORE, exist_ok=True)
    if os.path.exists(INDEX):
        return _read(INDEX)
    return {"active": None, "accounts": []}


def save(idx):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)
    os.replace(tmp, INDEX)


def slot(key):
    d = os.path.join(STORE, key.replace("/", "_"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "auth.json")


def _upsert(idx, info, src_auth_path):
    shutil.copyfile(src_auth_path, slot(info["key"]))
    for acc in idx["accounts"]:
        if acc["key"] == info["key"]:
            acc.update({k: info[k] for k in ("email", "name", "plan")})
            return acc
    info["color"] = COLORS[len(idx["accounts"]) % len(COLORS)]
    idx["accounts"].append(info)
    return info


def sync_current(idx):
    """Copy live auth.json back into its slot (tokens get refreshed while in use)."""
    if not os.path.exists(AUTH):
        return idx
    info = describe(_read(AUTH))
    if info["key"]:
        _upsert(idx, info, AUTH)
        idx["active"] = info["key"]
        save(idx)
    return idx


def find_codex_cli():
    cands = sorted(glob.glob(os.path.join(HOME, ".cursor", "extensions", "openai.chatgpt-*",
                                          "bin", "windows-x86_64", "codex.exe")), reverse=True)
    cands += sorted(glob.glob(os.path.join(HOME, ".vscode", "extensions", "openai.chatgpt-*",
                                           "bin", "windows-x86_64", "codex.exe")), reverse=True)
    w = shutil.which("codex")
    if w:
        cands.append(w)
    return cands[0] if cands else None


def add_account(idx):
    """Run `codex login` in an isolated CODEX_HOME, then store the result."""
    cli = find_codex_cli()
    if not cli:
        raise RuntimeError("codex.exe não encontrado (extensão do Cursor ou CLI).")
    tmp = tempfile.mkdtemp(prefix="codex-login-")
    try:
        env = dict(os.environ, CODEX_HOME=tmp)
        subprocess.run([cli, "login"], env=env, creationflags=NO_WINDOW, timeout=600)
        new = os.path.join(tmp, "auth.json")
        if not os.path.exists(new):
            raise RuntimeError("Login cancelado ou falhou.")
        info = describe(_read(new))
        _upsert(idx, info, new)
        save(idx)
        return info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove_account(idx, key):
    if key == idx.get("active"):
        raise RuntimeError("Troque para outra conta antes de remover a ativa.")
    idx["accounts"] = [a for a in idx["accounts"] if a["key"] != key]
    shutil.rmtree(os.path.dirname(slot(key)), ignore_errors=True)
    save(idx)


def _codex_procs():
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -match "
          "'OpenAI\\.Codex_|openai\\.chatgpt-' } | ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                         text=True, creationflags=NO_WINDOW).stdout
    return [l.split("|", 1) for l in out.splitlines() if "|" in l]


def switch(idx, key):
    idx = sync_current(idx)
    if key == idx.get("active"):
        return False
    procs = _codex_procs()
    app_was_open = any("OpenAI.Codex_" in p for _, p in procs)
    for pid, _ in procs:
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(1.5)
    if os.path.exists(AUTH):
        shutil.copyfile(AUTH, os.path.join(STORE, "auth.last-switch.bak"))
    _write_atomic(AUTH, slot(key))
    idx["active"] = key
    save(idx)
    if app_was_open:
        open_codex()
    return True


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"


def _http_json(url, headers, body=None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers, **({"Content-Type": "application/json"} if data else {}))
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _refresh(path, auth):
    """Refresh tokens of an INACTIVE account and save them back to its slot."""
    t = auth["tokens"]
    client = (t.get("oauth_provenance") or {}).get("client_id")
    new = _http_json(TOKEN_URL, {"User-Agent": "codex-cli"}, {
        "client_id": client, "grant_type": "refresh_token",
        "refresh_token": t["refresh_token"], "scope": "openid profile email"})
    for k in ("id_token", "access_token", "refresh_token"):
        if new.get(k):
            t[k] = new[k]
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    os.replace(tmp, path)
    return auth


def usage(idx, key):
    """Return {'session': (left%, reset_s), 'weekly': (left%, reset_s)}."""
    import urllib.error
    active = key == idx.get("active")
    path = AUTH if active else slot(key)
    auth = _read(path)

    def get(a):
        t = a["tokens"]
        return _http_json(USAGE_URL, {"Authorization": "Bearer " + t["access_token"],
                                      "ChatGPT-Account-Id": t.get("account_id") or "",
                                      "User-Agent": "codex-cli", "Accept": "application/json"})
    try:
        d = get(auth)
    except urllib.error.HTTPError as e:
        # never refresh the live account: the running Codex owns its tokens
        if e.code != 401 or active:
            raise
        d = get(_refresh(path, auth))
    rl = d.get("rate_limit") or {}

    def win(w):
        if not w:
            return None
        return max(0, 100 - int(w.get("used_percent", 0))), int(w.get("reset_after_seconds") or 0)
    return {"session": win(rl.get("primary_window")), "weekly": win(rl.get("secondary_window"))}


def fmt_dur(s):
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}min"
    return f"{max(m, 1)}min"


def open_codex():
    subprocess.Popen(["explorer.exe", APP_AUMID], creationflags=NO_WINDOW)
