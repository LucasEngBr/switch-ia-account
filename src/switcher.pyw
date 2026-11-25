"""Account switcher for Claude Desktop and Codex: tray icon + split account menu."""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_core  # noqa: E402
import codex_core  # noqa: E402

BG, CARD, HOVER, FG, MUTED, LINE = "#1e1e1e", "#262626", "#303030", "#ececec", "#9a9a9a", "#333333"
FONT = "Segoe UI"
ICON_FONT = "Segoe MDL2 Assets"
DOOR = ""  # SignOut glyph


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
        """Claude only: read name/email/plan from the profile's login."""
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
    d.ellipse((78, 14, 178, 114), fill="white")                 # head
    d.pieslice((18, 124, 238, 344), 180, 360, fill="white")     # shoulders
    d.rectangle((18, 232, 238, 240), fill="white")
    return img.resize((64, 64), Image.LANCZOS)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.providers = [Provider("Claude", claude_core, "#d97757"), Provider("Codex", codex_core, "#10a37f")]
        for p in self.providers:
            try:
                p.reload()
            except Exception:  # noqa: BLE001
                pass
        self.popup = None
        self.busy = False
        self.rings, self.usage, self.tip, self.armed = {}, {}, None, None
        self.tray = pystray.Icon("account-switcher", tray_image(), self.tooltip(),
                                 menu=pystray.Menu(
                                     pystray.MenuItem("Contas", lambda: self.call(self.show), default=True),
                                     pystray.MenuItem("Abrir Codex", lambda: codex_core.open_codex()),
                                     pystray.MenuItem("Sair", lambda: self.call(self.quit))))

    # ---- helpers
    def call(self, fn, *a):
        self.root.after(0, fn, *a)

    def tooltip(self):
        parts = []
        for p in self.providers:
            a = p.active()
            parts.append(f"{p.name}: {a['email'] if a else '—'}")
        return "\n".join(parts)[:120]

    def refresh_tray(self):
        self.tray.title = self.tooltip()

    # ---- popup
    def show(self):
        if self.popup:
            self.close()
            return
        for p in self.providers:
            try:
                p.reload()
            except Exception:  # noqa: BLE001
                pass
        self.refresh_tray()
        self.rings = {}
        pop = self.popup = tk.Toplevel(self.root, bg=LINE)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        outer = tk.Frame(pop, bg=BG)
        outer.pack(padx=1, pady=1)
        for i, p in enumerate(self.providers):
            if i:
                tk.Frame(outer, bg=LINE, width=1).pack(side="left", fill="y")
            col = tk.Frame(outer, bg=BG, padx=6, pady=6)
            col.pack(side="left", fill="both", anchor="n")
            self.column(col, p)

        pop.update_idletasks()
        x, y = pop.winfo_pointerxy()
        w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
        sw = pop.winfo_screenwidth()
        pop.geometry(f"+{max(8, min(x - w // 2, sw - w - 8))}+{max(y - h - 16, 8)}")
        pop.bind("<FocusOut>", lambda e: self.root.after(150, self.close_if_unfocused))
        pop.bind("<Escape>", lambda e: self.close())
        pop.focus_force()

    def column(self, col, p):
        head = tk.Frame(col, bg=BG)
        head.pack(fill="x", pady=(2, 6))
        tk.Label(head, text="●", bg=BG, fg=p.accent, font=(FONT, 9)).pack(side="left", padx=(10, 4))
        tk.Label(head, text=p.name, bg=BG, fg=FG, font=(FONT, 11, "bold")).pack(side="left")

        groups = {}
        for acc in p.idx["accounts"]:
            groups.setdefault(acc["email"], []).append(acc)
        for i, (email, accs) in enumerate(groups.items()):
            if i:
                tk.Frame(col, bg=LINE, height=1).pack(fill="x", pady=4)
            tk.Label(col, text="◎  " + email, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                     padx=10, pady=4).pack(fill="x")
            for acc in accs:
                self.row(col, p, acc)
        if p.idx["accounts"]:
            tk.Frame(col, bg=LINE, height=1).pack(fill="x", pady=4)
        self.action(col, "+   Adicionar conta", lambda: self.add(p))
        for acc in list(p.idx["accounts"]):
            self.fetch_usage(p, acc)

    def close_if_unfocused(self):
        if self.popup and not self.busy and self.popup.focus_get() is None:
            self.close()

    def close(self):
        self.tip_hide()
        self.armed = None
        self.rings = {}
        if self.popup:
            self.popup.destroy()
            self.popup = None

    def row(self, parent, p, acc):
        uid = (p.name, acc["key"])
        is_active = acc["key"] == p.idx.get("active")
        r = tk.Frame(parent, bg=BG, padx=8, pady=6, cursor="hand2")
        r.pack(fill="x")
        av = tk.Canvas(r, width=26, height=26, bg=BG, highlightthickness=0)
        av.create_oval(1, 1, 25, 25, fill=acc["color"], outline="")
        av.create_text(13, 13, text=initials(acc), fill="white", font=(FONT, 8, "bold"))
        av.pack(side="left")
        label = acc["name"] + (f"  ·  {acc['plan']}" if acc.get("plan") else "")
        t = tk.Label(r, text=label, bg=BG, fg=FG, font=(FONT, 10), anchor="w", padx=10, width=22)
        t.pack(side="left", fill="x", expand=True)
        door = tk.Label(r, text=DOOR, bg=BG, fg=MUTED, font=(ICON_FONT, 11), cursor="hand2", padx=4)
        door.pack(side="right")
        ck = tk.Label(r, text="✓" if is_active else " ", bg=BG, fg=FG, font=(FONT, 11), width=2)
        ck.pack(side="right")
        ring = tk.Canvas(r, width=22, height=22, bg=BG, highlightthickness=0)
        ring.pack(side="right", padx=(0, 4))
        self.rings[uid] = ring
        self.draw_ring(uid)
        widgets = (r, av, t, ck, ring, door)

        def paint(c):
            for wdg in widgets:
                wdg.configure(bg=c)
        for wdg in widgets:
            wdg.bind("<Enter>", lambda e: paint(HOVER), add="+")
            wdg.bind("<Leave>", lambda e: paint(BG), add="+")
        for wdg in (r, av, t, ck):
            wdg.bind("<Button-1>", lambda e, k=acc["key"]: self.switch(p, k))
        ring.bind("<Enter>", lambda e: self.tip_show(ring, self.usage_text(uid)), add="+")
        ring.bind("<Leave>", lambda e: self.tip_hide(), add="+")
        door.bind("<Enter>", lambda e: self.tip_show(door, "Remover conta"), add="+")
        door.bind("<Leave>", lambda e: self.tip_hide(), add="+")
        door.bind("<Button-1>", lambda e, a=acc, d=door: self.remove(p, a, d))

    # ---- usage meter
    def fetch_usage(self, p, acc):
        uid = (p.name, acc["key"])
        c = self.usage.get(uid)
        if c and time.time() - c[0] < 60:
            return

        def work():
            try:
                if p.mod is claude_core and acc.get("email") in ("?", "faça login no app"):
                    p.enrich(acc)
                    self.call(self.redraw)
                res = p.mod.usage(p.idx, acc["key"])
            except Exception as ex:  # noqa: BLE001
                res = ex
            self.usage[(p.name, acc["key"])] = (time.time(), res)
            self.call(self.draw_ring, (p.name, acc["key"]))
        self.usage[uid] = (time.time(), None)
        threading.Thread(target=work, daemon=True).start()

    def redraw(self):
        if self.popup and not self.busy:
            self.close()
            self.show()

    def draw_ring(self, uid):
        ring = self.rings.get(uid)
        if not ring or not ring.winfo_exists():
            return
        ring.delete("all")
        ring.create_oval(3, 3, 19, 19, outline="#444444", width=3)
        data = (self.usage.get(uid) or (0, None))[1]
        if not isinstance(data, dict):
            if isinstance(data, Exception):
                ring.create_text(11, 11, text="!", fill=MUTED, font=(FONT, 8, "bold"))
            return
        s, w = data.get("session"), data.get("weekly")
        left = 0 if (w and w[0] <= 0) else (s[0] if s else (w[0] if w else 0))
        color = "#4caf50" if left > 50 else "#e0a020" if left > 20 else "#e5533d"
        if left >= 100:
            ring.create_oval(3, 3, 19, 19, outline=color, width=3)
        elif left > 0:
            ring.create_arc(3, 3, 19, 19, start=90, extent=-3.6 * left, style="arc",
                            outline=color, width=3)

    def usage_text(self, uid):
        data = (self.usage.get(uid) or (0, None))[1]
        if data is None:
            return "Carregando limites…"
        if isinstance(data, Exception):
            return f"Não foi possível ler o limite:\n{data}"
        s, w = data.get("session"), data.get("weekly")
        fmt = codex_core.fmt_dur
        if w and w[0] <= 0:
            return f"Limite semanal esgotado\nRenova em {fmt(w[1])}"
        if s and s[0] <= 0:
            txt = f"Limite de sessão esgotado\nReseta em {fmt(s[1])}"
            return txt + (f"\nSemanal: {w[0]}% restantes" if w else "")
        lines = []
        if s:
            lines.append(f"Sessão (5h): {s[0]}% restantes")
        if w:
            lines.append(f"Semanal: {w[0]}% restantes")
        return "\n".join(lines) or "Sem dados de limite"

    def tip_show(self, widget, text):
        self.tip_hide()
        t = self.tip = tk.Toplevel(self.root, bg=LINE)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=text, bg=CARD, fg=FG, font=(FONT, 9), justify="left",
                 padx=10, pady=6).pack(padx=1, pady=1)
        t.update_idletasks()
        x = widget.winfo_rootx() + widget.winfo_width() + 6
        y = widget.winfo_rooty() - 4
        if x + t.winfo_reqwidth() > t.winfo_screenwidth():
            x = widget.winfo_rootx() - t.winfo_reqwidth() - 6
        t.geometry(f"+{x}+{y}")

    def tip_hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def action(self, parent, text, fn):
        b = tk.Label(parent, text=text, bg=BG, fg=FG, font=(FONT, 10), anchor="w", padx=12, pady=7,
                     cursor="hand2")
        b.pack(fill="x")
        b.bind("<Enter>", lambda e: b.configure(bg=HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=BG))
        b.bind("<Button-1>", lambda e: fn())

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
        if key == p.idx.get("active") and (p.mod is codex_core or claude_core.running_dir()):
            self.close()
            return
        self.close()
        if p.mod is claude_core:
            self.tray.notify("Fechando o Claude e abrindo a outra conta…", "Trocando conta")
        self.run_bg(lambda: p.mod.switch(p.idx, key), lambda c, e: self.after_switch(p, c, e))

    def after_switch(self, p, changed, err):
        self.busy = False
        if err:
            messagebox.showerror("Trocador de contas", str(err))
        p.reload()
        self.refresh_tray()
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
            messagebox.showerror("Trocador de contas", str(err))
            return
        p.reload()
        if p.mod is codex_core:
            self.tray.notify(f"{info['email']} ({info['plan']}) adicionada.", "Conta adicionada")

    def remove(self, p, acc, door):
        # 1st click arms the door (red) for 3s; 2nd click asks for final confirmation
        uid = (p.name, acc["key"])
        if self.armed != uid:
            self.armed = uid
            door.configure(fg="#e5533d")
            self.tip_show(door, "Clique de novo para remover")

            def disarm():
                if self.armed == uid:
                    self.armed = None
                    if door.winfo_exists():
                        door.configure(fg=MUTED)
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
            messagebox.showerror("Trocador de contas", str(ex))
        self.close()
        self.show()

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
