"""Switch IA Account: tray icon + split Claude/Codex account menu in dark liquid glass."""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_core  # noqa: E402
import codex_core  # noqa: E402
import glass  # noqa: E402

UI_SCALE = 0.5  # menu size relative to the original design
KEY_HEX = "#%02x%02x%02x" % glass.KEY


class Provider:
    def __init__(self, name, mod, accent):
        self.name, self.mod, self.accent = name, mod, accent
        self.idx = {"active": None, "accounts": []}

    def reload(self):
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
        self.tray = pystray.Icon("switch-ia-account", tray_image(), self.tooltip(),
                                 menu=pystray.Menu(
                                     pystray.MenuItem("Contas", lambda: self.call(self.show), default=True),
                                     pystray.MenuItem("Abrir Codex", lambda: codex_core.open_codex()),
                                     pystray.MenuItem("Sair", lambda: self.call(self.quit))))

    # ---- helpers
    def call(self, fn, *a):
        self.root.after(0, fn, *a)

    def tooltip(self):
        return "\n".join(f"{p.name}: {(p.active() or {}).get('email', '—')}" for p in self.providers)[:120]

    def prov(self, name):
        return next(p for p in self.providers if p.name == name)

    # ---- popup
    def columns(self):
        cols = []
        for p in self.providers:
            groups = {}
            for acc in p.idx["accounts"]:
                groups.setdefault(acc["email"], []).append(acc)
            cols.append({"name": p.name, "accent": p.accent, "groups": list(groups.items())})
        return cols

    def state(self):
        return {"hover": self.hover, "armed": self.armed, "initials": initials,
                "active": {p.name: p.idx.get("active") for p in self.providers},
                "usage": {k: ring_left(v[1]) for k, v in self.usage.items()}}

    def show(self, keep_pos=False):
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
        self.bg = glass.backdrop((pos[0], pos[1], pos[0] + w, pos[1] + h), (w, h), self.scale)

        pop = self.popup = tk.Toplevel(self.root, bg=KEY_HEX)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.attributes("-transparentcolor", KEY_HEX)
        pop.geometry(f"{w}x{h}+{pos[0]}+{pos[1]}")
        self.canvas = tk.Label(pop, bd=0, bg=KEY_HEX, cursor="arrow")
        self.canvas.pack()
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
        img = glass.render(self.bg, self.items, self.L, self.state())
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.configure(image=self.photo)

    def hit(self, x, y):
        best = None
        for kind, box, col, acc in self.items:
            if kind in ("row", "ring", "door", "add") and box[0] <= x < box[2] and box[1] <= y < box[3]:
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
        self.paint()

    def on_motion(self, e):
        self.set_hover(self.hit(e.x, e.y))

    def on_click(self, e):
        h = self.hit(e.x, e.y)
        if not h:
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
                   if k == h[0] and c["name"] == h[1] and isinstance(a, dict) and a["key"] == h[2])
        px, py = self.popup_pos
        tmp = glass.tooltip(text, (0, 0, 1, 1), self.scale)
        tw, th = tmp.size
        x = px + box[2] + int(8 * self.scale)
        if x + tw > self.root.winfo_screenwidth():
            x = px + box[0] - tw - int(8 * self.scale)
        y = py + (box[1] + box[3]) // 2 - th // 2
        img = glass.tooltip(text, (x, y, x + tw, y + th), self.scale)
        t = self.tip = tk.Toplevel(self.root, bg=KEY_HEX)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-transparentcolor", KEY_HEX)
        t.geometry(f"{tw}x{th}+{x}+{y}")
        self.tip_photo = ImageTk.PhotoImage(img)
        tk.Label(t, image=self.tip_photo, bd=0, bg=KEY_HEX).pack()

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
                if p.mod is claude_core and acc.get("email") in ("?", "faça login no app"):
                    p.enrich(acc)
                    self.call(lambda: self.popup and not self.busy and self.show(True))
                res = p.mod.usage(p.idx, acc["key"])
            except Exception as ex:  # noqa: BLE001
                res = ex
            self.usage[(p.name, acc["key"])] = (time.time(), res)
            self.call(self.paint)
        self.usage[uid] = (time.time(), None)
        threading.Thread(target=work, daemon=True).start()

    # ---- actions
    def run_bg(self, work, done):
        self.busy = True

        def go():
            try:
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
            p.mod.remove_account(p.idx, acc["key"])
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Switch IA Account", str(ex))
        self.show(keep_pos=True)

    def quit(self):
        self.tray.stop()
        self.root.destroy()

    def run(self):
        threading.Thread(target=self.tray.run, daemon=True).start()
        self.root.mainloop()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "CodexSwitcherSingleton")
    if ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)
    App().run()
