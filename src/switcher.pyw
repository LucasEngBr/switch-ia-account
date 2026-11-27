"""Switch IA Account: tray icon + split Claude/Codex account menu in dark liquid glass."""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox

import pystray
from PIL import Image, ImageDraw, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_core  # noqa: E402
import codex_core  # noqa: E402
import codex_autoresume  # noqa: E402
import claude_autoresume  # noqa: E402
import glass  # noqa: E402

BACKDROP = "noise"  # "noise" = fixed dark gradient (fast) | "screen" = what is behind the window
UI_SCALE = 0.62  # menu size relative to the original design
KEY_HEX = "#%02x%02x%02x" % glass.KEY


class Provider:
    def __init__(self, name, mod, accent):
        self.name, self.mod, self.accent = name, mod, accent
        self.idx = {"active": None, "accounts": []}

    def reload(self):
        with codex_autoresume.OPERATION_LOCK:
            if self.mod is codex_core:
                self.idx = codex_core.sync_current(codex_core.load())
            else:
                self.idx = claude_core.detect_active(claude_core.load())
                claude_core.save(self.idx)
        return self.idx

    def enrich(self, acc):
        if self.mod is claude_core:
            claude_core.refresh_info(self.idx, acc)

    def active(self):
        return next((a for a in self.idx["accounts"] if a["key"] == self.idx.get("active")), None)


_FAMILY = {"regular": ("Segoe UI Variable Text", "normal"), "semibold": ("Segoe UI Variable Text Semibold", "normal"),
           "bold": ("Segoe UI", "bold"), "icon": ("Segoe Fluent Icons", "normal")}
_ANCHOR = {"lm": "w", "mm": "center", "la": "nw", "rm": "e"}


_FAMS = None


def _tk_family(name):
    global _FAMS
    if _FAMS is None:
        _FAMS = {f.lower() for f in tkfont.families()}
    fams = _FAMS
    fb = {"Segoe UI Variable Text": "Segoe UI", "Segoe UI Variable Text Semibold": "Segoe UI Semibold",
          "Segoe Fluent Icons": "Segoe MDL2 Assets"}
    return name if name.lower() in fams else fb.get(name, "Segoe UI")


def draw_text(canvas, ops, img):
    """Draw text natively (ClearType, hinted) on top of the glass image; RGBA colours blended on the pixel below."""
    canvas.delete("txt")
    px = img.convert("RGB")
    for x, y, text, (size, weight), fill, anchor in ops:
        fam, wt = _FAMILY[weight]
        col = fill
        if isinstance(col, str):
            col = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        if len(col) == 4:
            under = px.getpixel((min(max(int(x), 0), px.width - 1), min(max(int(y), 0), px.height - 1)))
            a = col[3] / 255
            col = tuple(int(col[i] * a + under[i] * (1 - a)) for i in range(3))
        f = (_tk_family(fam), -int(size), wt)
        a = _ANCHOR.get(anchor, "nw")
        canvas.create_text(x, y, text=text, font=f, fill="#%02x%02x%02x" % col[:3], anchor=a, tags="txt")


def initials(acc):
    parts = acc["name"].replace(".", " ").split()
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


def tray_image():
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((78, 14, 178, 114), fill="white")
    d.pieslice((18, 124, 238, 344), 180, 360, fill="white")
    d.rectangle((18, 232, 238, 240), fill="white")
    return img.resize((64, 64), Image.LANCZOS)


def usage_text(data):
    if data is None:
        return "Carregando limites…"
    if isinstance(data, Exception):
        return f"Não foi possível ler o limite:\n{data}"
    s, w = data.get("session"), data.get("weekly")
    fmt = codex_core.fmt_dur
    if w and w[0] <= 0:
        return f"Limite semanal esgotado\nRenova em {fmt(w[1])}"
    if s and s[0] <= 0:
        return f"Limite de sessão esgotado\nReseta em {fmt(s[1])}" + (f"\nSemanal: {w[0]}% restantes" if w else "")
    lines = []
    if s:
        lines.append(f"Sessão (5h): {s[0]}% restantes")
    if w:
        lines.append(f"Semanal: {w[0]}% restantes")
    return "\n".join(lines) or "Sem dados de limite"


def ring_left(data):
    if data is None:
        return None
    if isinstance(data, Exception):
        return "err"
    s, w = data.get("session"), data.get("weekly")
    if w and w[0] <= 0:
        return 0
    return s[0] if s else (w[0] if w else 0)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96 * UI_SCALE
        self.providers = [Provider("Claude", claude_core, "#d97757"), Provider("Codex", codex_core, "#10a37f")]
        for p in self.providers:
            try:
                p.reload()
            except Exception:  # noqa: BLE001
                pass
        self.popup = self.tip = None
        self.busy = False
        self.usage, self.armed, self.hover = {}, None, None
        self.bg_cache = {}
        self.tray = pystray.Icon("switch-ia-account", tray_image(), self.tooltip(),
                                 menu=pystray.Menu(
                                     pystray.MenuItem("Contas", lambda: self.call(self.show), default=True),
                                     pystray.MenuItem("Abrir Codex", lambda: codex_core.open_codex()),
                                     pystray.MenuItem("Retomar Codex ao parar por limite",
                                                      lambda: self.call(self.toggle_autoresume),
                                                      checked=lambda item: self.monitor.enabled()),
                                     pystray.MenuItem("Retomar Claude ao parar por limite",
                                                      lambda: self.call(self.toggle_autoresume, "Claude"),
                                                      checked=lambda item: self.claude_monitor.enabled()),
                                     pystray.MenuItem("Sair", lambda: self.call(self.quit))))
        self.monitor = codex_autoresume.Monitor(
            notify=lambda text: self.call(self.tray.notify, text, "Retomada por limite"),
            changed=lambda: self.call(self.refresh_after_auto))
        self.claude_monitor = claude_autoresume.Monitor(
            notify=lambda text: self.call(self.tray.notify, text, "Retomada por limite"),
            changed=lambda: self.call(self.refresh_after_auto, "Claude"))
        self.monitors = {"Codex": self.monitor, "Claude": self.claude_monitor}
        for monitor in self.monitors.values():
            monitor.start()

    def auto_busy(self):
        return any(monitor.recovering for monitor in self.monitors.values())

    def toggle_autoresume(self, provider="Codex"):
        monitor = self.monitors[provider]
        monitor.set_enabled(not monitor.enabled())
        self.tray.update_menu()
        self.tip_hide()
        self.paint()

    def refresh_after_auto(self, provider="Codex"):
        # Wait for the recovery worker to release the account-operation lock.
        if self.auto_busy():
            self.root.after(500, self.refresh_after_auto, provider)
            return
        self.prov(provider).reload()
        self.usage = {key: value for key, value in self.usage.items() if key[0] != provider}
        self.tray.title = self.tooltip()
        if self.popup:
            self.show(keep_pos=True)

    # ---- helpers
    def call(self, fn, *a):
        self.root.after(0, fn, *a)

    def tooltip(self):
        return "\n".join(f"{p.name}: {(p.active() or {}).get('email', '—')}" for p in self.providers)[:120]

    def prov(self, name):
        return next(p for p in self.providers if p.name == name)

    # ---- popup
    def sort_key(self, p, acc):
        """Most limit left first; exhausted accounts after, soonest reset first; unknown last."""
        data = (self.usage.get((p.name, acc["key"])) or (0, None))[1]
        if not isinstance(data, dict):
            return (2, 0, 0)
        s, w = data.get("session"), data.get("weekly")
        if w and w[0] <= 0:
            return (1, w[1], 0)                      # weekly gone: wait for the weekly reset
        if s and s[0] <= 0:
            return (1, s[1], 0)                      # session gone: wait for the session reset
        return (0, -(s[0] if s else 100), -(w[0] if w else 100))

    def ordered(self, p):
        return sorted(p.idx["accounts"], key=lambda a: self.sort_key(p, a))

    def columns(self):
        cols = []
        for p in self.providers:
            groups = []                              # consecutive accounts with the same e-mail share a header
            for acc in self.ordered(p):
                if groups and groups[-1][0] == acc["email"]:
                    groups[-1][1].append(acc)
                else:
                    groups.append((acc["email"], [acc]))
            cols.append({"name": p.name, "accent": p.accent, "groups": groups})
        return cols

    def schedule_reorder(self):
        """Usage answers arrive one by one: wait for a short quiet gap, then re-order once."""
        if getattr(self, "_reorder_job", None):
            self.root.after_cancel(self._reorder_job)
        self._reorder_job = self.root.after(250, self.reorder)
        self.paint()

    def reorder(self):
        self._reorder_job = None
        """Re-layout once fresh usage changes the order."""
        if not self.popup or self.busy or self.armed:
            return
        shown = [a["key"] for k, b, c, a in self.items if k == "row"]
        want = [a["key"] for p in self.providers for a in self.ordered(p)]
        if shown != want:
            self.show(keep_pos=True)
        else:
            self.paint()

    def state(self):
        return {"hover": self.hover, "armed": self.armed, "initials": initials,
                "active": {p.name: p.idx.get("active") for p in self.providers},
                "autoresume": {name: monitor.enabled() for name, monitor in self.monitors.items()},
                "usage": {k: ring_left(v[1]) for k, v in self.usage.items()}}

    def show(self, keep_pos=False):
        if self.auto_busy():
            return
        if self.popup and not keep_pos:
            self.close()
            return
        pos = self.popup_pos if keep_pos and self.popup else None
        if self.popup:
            self.popup.destroy()
            self.popup = None
        for p in self.providers:
            try:
                p.reload()
            except Exception:  # noqa: BLE001
                pass
        self.tray.title = self.tooltip()
        (w, h), self.items, self.L = glass.build(self.columns(), self.scale)
        if pos is None:
            x, y = self.root.winfo_pointerxy()
            sw = self.root.winfo_screenwidth()
            pos = (max(8, min(x - w // 2, sw - w - 8)), max(y - h - 16, 8))
        self.popup_pos = pos
        if BACKDROP == "noise":
            # fixed black/grey noise gradient: lenses are computed once per layout and reused
            sig = (w, h, tuple(b for _, b, _, _ in self.items))
            if sig not in self.bg_cache:
                self.bg_cache = {sig: glass.noise_backdrop((w, h), self.scale)}
            self.bg = self.bg_cache[sig]
        else:
            self.bg = glass.backdrop((pos[0], pos[1], pos[0] + w, pos[1] + h), (w, h), self.scale)

        pop = self.popup = tk.Toplevel(self.root, bg=KEY_HEX)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.attributes("-transparentcolor", KEY_HEX)
        pop.geometry(f"{w}x{h}+{pos[0]}+{pos[1]}")
        self.canvas = tk.Canvas(pop, width=w, height=h, bd=0, highlightthickness=0, bg=KEY_HEX, cursor="arrow")
        self.canvas.pack()
        self.canvas_img = self.canvas.create_image(0, 0, anchor="nw")
        self.paint()
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: self.set_hover(None))
        self.canvas.bind("<Button-1>", self.on_click)
        pop.bind("<FocusOut>", lambda e: self.root.after(150, self.close_if_unfocused))
        pop.bind("<Escape>", lambda e: self.close())
        pop.focus_force()
        for p in self.providers:
            for acc in list(p.idx["accounts"]):
                self.fetch_usage(p, acc)

    def paint(self):
        if not self.popup:
            return
        ops = []
        img = glass.render(self.bg, self.items, self.L, self.state(), ops)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfigure(self.canvas_img, image=self.photo)
        draw_text(self.canvas, ops, img)

    def hit(self, x, y):
        best = None
        for kind, box, col, acc in self.items:
            if kind in ("row", "ring", "door", "add", "auto") and box[0] <= x < box[2] and box[1] <= y < box[3]:
                key = acc["key"] if isinstance(acc, dict) else None
                if best is None or kind in ("ring", "door"):
                    best = (kind, col["name"], key)
        return best

    def set_hover(self, h):
        if h == self.hover:
            return
        self.hover = h
        self.canvas.configure(cursor="hand2" if h else "arrow")
        self.tip_hide()
        if h and h[0] == "ring":
            self.tip_show(h, usage_text((self.usage.get((h[1], h[2])) or (0, None))[1]))
        elif h and h[0] == "door":
            self.tip_show(h, "Clique de novo para remover" if self.armed == (h[1], h[2]) else "Remover conta")
        elif h and h[0] == "auto":
            self.tip_show(h, "Troca automática por limite: " +
                          ("ativada. Clique para desativar." if self.monitors[h[1]].enabled() else "desativada. Clique para ativar."))
        self.paint()

    def on_motion(self, e):
        self.set_hover(self.hit(e.x, e.y))

    def on_click(self, e):
        h = self.hit(e.x, e.y)
        if not h:
            return
        if h[0] == "auto":
            self.toggle_autoresume(h[1])
            return
        if self.busy or self.auto_busy():
            return
        kind, pname, key = h
        p = self.prov(pname)
        if kind == "add":
            self.add(p)
        elif kind == "door":
            self.remove(p, next(a for a in p.idx["accounts"] if a["key"] == key))
        elif kind in ("row", "ring"):
            self.switch(p, key)

    def close_if_unfocused(self):
        if self.popup and not self.busy and self.popup.focus_get() is None:
            self.close()

    def close(self):
        self.tip_hide()
        self.armed = self.hover = None
        if self.popup:
            self.popup.destroy()
            self.popup = None

    # ---- tooltip
    def tip_show(self, h, text):
        box = next(b for k, b, c, a in self.items
                   if k == h[0] and c["name"] == h[1]
                   and ((isinstance(a, dict) and a["key"] == h[2]) or (k == "auto" and h[2] is None)))
        px, py = self.popup_pos
        tmp = glass.tooltip(text, (0, 0, 1, 1), self.scale, [])
        tw, th = tmp.size
        x = px + box[2] + int(8 * self.scale)
        if x + tw > self.root.winfo_screenwidth():
            x = px + box[0] - tw - int(8 * self.scale)
        y = py + (box[1] + box[3]) // 2 - th // 2
        ops = []
        img = glass.tooltip(text, (x, y, x + tw, y + th), self.scale, ops)
        t = self.tip = tk.Toplevel(self.root, bg=KEY_HEX)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-transparentcolor", KEY_HEX)
        t.geometry(f"{tw}x{th}+{x}+{y}")
        self.tip_photo = ImageTk.PhotoImage(img)
        c = tk.Canvas(t, width=tw, height=th, bd=0, highlightthickness=0, bg=KEY_HEX)
        c.pack()
        c.create_image(0, 0, anchor="nw", image=self.tip_photo)
        draw_text(c, ops, img)

    def tip_hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    # ---- usage
    def fetch_usage(self, p, acc):
        uid = (p.name, acc["key"])
        c = self.usage.get(uid)
        if c and time.time() - c[0] < 60:
            return

        def work():
            try:
                with codex_autoresume.OPERATION_LOCK:
                    if p.mod is claude_core and acc.get("email") in ("?", "faça login no app"):
                        p.enrich(acc)
                        self.call(lambda: self.popup and not self.busy and self.show(True))
                    res = p.mod.usage(p.idx, acc["key"])
            except Exception as ex:  # noqa: BLE001
                res = ex
            self.usage[(p.name, acc["key"])] = (time.time(), res)
            self.call(self.schedule_reorder)
        self.usage[uid] = (time.time(), None)
        threading.Thread(target=work, daemon=True).start()

    # ---- actions
    def run_bg(self, work, done):
        if self.busy or self.auto_busy():
            return
        self.busy = True

        def go():
            try:
                with codex_autoresume.OPERATION_LOCK:
                    res, err = work(), None
            except Exception as ex:  # noqa: BLE001
                res, err = None, ex
            self.call(done, res, err)
        threading.Thread(target=go, daemon=True).start()

    def switch(self, p, key):
        self.close()
        if key == p.idx.get("active") and (p.mod is codex_core or claude_core.running_dir()):
            return
        if p.mod is claude_core:
            self.tray.notify("Fechando o Claude e abrindo a outra conta…", "Trocando conta")
        self.run_bg(lambda: p.mod.switch(p.idx, key), lambda c, e: self.after_switch(p, c, e))

    def after_switch(self, p, changed, err):
        self.busy = False
        if err:
            messagebox.showerror("Switch IA Account", str(err))
        p.reload()
        self.tray.title = self.tooltip()
        a = p.active()
        if changed and a:
            extra = " Se estiver no Cursor, rode 'Developer: Reload Window'." if p.mod is codex_core else ""
            self.tray.notify(f"{p.name}: agora usando {a['email']}.{extra}", "Conta trocada")

    def add(self, p):
        self.close()
        msg = ("Faça login no navegador que vai abrir." if p.mod is codex_core
               else "O Claude vai abrir num perfil novo: faça login nele.")
        self.tray.notify(msg, f"Adicionar conta {p.name}")
        self.run_bg(lambda: p.mod.add_account(p.idx), lambda i, e: self.after_add(p, i, e))

    def after_add(self, p, info, err):
        self.busy = False
        if err:
            messagebox.showerror("Switch IA Account", str(err))
            return
        p.reload()
        if p.mod is codex_core:
            self.tray.notify(f"{info['email']} ({info['plan']}) adicionada.", "Conta adicionada")

    def remove(self, p, acc):
        # 1st click arms the door (red) for 3s; 2nd click asks for final confirmation
        uid = (p.name, acc["key"])
        if self.armed != uid:
            self.armed = uid
            self.hover = None
            self.set_hover(("door", p.name, acc["key"]))

            def disarm():
                if self.armed == uid:
                    self.armed = None
                    self.paint()
            self.root.after(3000, disarm)
            return
        self.armed = None
        self.tip_hide()
        self.busy = True
        ok = messagebox.askyesno("Remover conta", f"Remover {acc['name']} ({acc['email']}) do {p.name}?\n"
                                 "O histórico de conversas não é afetado.", parent=self.popup)
        self.busy = False
        if not ok:
            return
        try:
            with codex_autoresume.OPERATION_LOCK:
                p.mod.remove_account(p.idx, acc["key"])
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Switch IA Account", str(ex))
        self.show(keep_pos=True)

    def quit(self):
        for monitor in self.monitors.values():
            monitor.stop()
        self.tray.stop()
        self.root.destroy()

    def run(self):
        threading.Thread(target=self.tray.run, daemon=True).start()
        self.root.mainloop()


if __name__ == "__main__":
    # pythonw has no console: keep a log so a crash leaves a trace
    LOG = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SwitchIAAccount.log")
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    print(time.strftime("\n--- start %Y-%m-%d %H:%M:%S"))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    import faulthandler
    faulthandler.enable(sys.stderr)                  # native crashes also leave a trace in the log
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "CodexSwitcherSingleton")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("ja estava aberto: saindo")
        sys.exit(0)
    # At logon the taskbar may not exist yet; an icon added then is silently lost.
    # Wait for the tray window, then give Explorer a few more seconds to settle.
    user32 = ctypes.windll.user32
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    uptime_s = ctypes.windll.kernel32.GetTickCount64() / 1000
    for _ in range(120):
        if user32.FindWindowW("Shell_TrayWnd", None):
            break
        time.sleep(1)
    if uptime_s < 300:
        time.sleep(15)
    print(time.strftime("bandeja pronta %H:%M:%S"))
    import traceback
    try:
        app = App()
        app.root.report_callback_exception = lambda *a: traceback.print_exception(*a)
        app.run()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise
    print(time.strftime("--- exit %H:%M:%S"))
