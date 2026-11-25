"""Render the menu with fake accounts to docs/ (no real login is read)."""
import ctypes
import importlib.machinery
import importlib.util
import os
import sys
import time

from PIL import Image, ImageDraw, ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DOCS = os.path.join(ROOT, "docs")
sys.path.insert(0, SRC)
ctypes.windll.shcore.SetProcessDpiAwareness(1)

loader = importlib.machinery.SourceFileLoader("sw", os.path.join(SRC, "switcher.pyw"))
spec = importlib.util.spec_from_loader("sw", loader)
sw = importlib.util.module_from_spec(spec)
loader.exec_module(sw)

FAKE = {
    "Claude": {"active": "c1", "accounts": [
        {"key": "c1", "name": "Ana", "email": "ana@empresa.com", "plan": "Max", "color": "#d97757"},
        {"key": "c2", "name": "Ana", "email": "ana.pessoal@gmail.com", "plan": "Pro", "color": "#7c5cff"},
        {"key": "c3", "name": "Estúdio", "email": "studio@exemplo.dev", "plan": "Team", "color": "#2f7de1"}]},
    "Codex": {"active": "x1", "accounts": [
        {"key": "x1", "name": "Ana", "email": "ana@empresa.com", "plan": "Team", "color": "#1f9d6b"},
        {"key": "x2", "name": "Ana", "email": "ana@empresa.com", "plan": "Plus", "color": "#e08a1e"},
        {"key": "x3", "name": "Bruno", "email": "bruno@exemplo.dev", "plan": "Plus", "color": "#c2408f"},
        {"key": "x4", "name": "Labs", "email": "labs@exemplo.dev", "plan": "Pro", "color": "#4b9fa8"}]},
}
USAGE = {"c1": (72, 61), "c2": (100, 0), "c3": (35, 80), "x1": (76, 56), "x2": (100, 58), "x3": (12, 40), "x4": (100, 90)}

for P in (sw.Provider,):
    P.reload = lambda self: setattr(self, "idx", FAKE[self.name]) or self.idx
sw.App.fetch_usage = lambda self, p, acc: (self.usage.__setitem__(
    (p.name, acc["key"]), (time.time(), {"session": (USAGE[acc["key"]][0], 7200),
                                          "weekly": (USAGE[acc["key"]][1], 190000)})),
    self.draw_ring((p.name, acc["key"])))
sw.pystray.Icon.notify = lambda *a, **k: None

app = sw.App()
app.close_if_unfocused = lambda: None


def snap():
    p = app.popup
    p.geometry("+120+120")
    p.update()
    x, y, w, h = p.winfo_rootx(), p.winfo_rooty(), p.winfo_width(), p.winfo_height()
    ImageGrab.grab((x, y, x + w, y + h)).save(os.path.join(DOCS, "menu.png"))
    ring = app.rings[("Codex", "x3")]
    # tooltip drawn with PIL on a clean canvas, so nothing behind the window leaks in
    from PIL import ImageFont
    rx, ry = ring.winfo_rootx() - x, ring.winfo_rooty() - y
    menu_img = ImageGrab.grab((x, y, x + w, y + h))
    text = app.usage_text(("Codex", "x3"))
    f = ImageFont.truetype("segoeui.ttf", 15)
    d0 = ImageDraw.Draw(menu_img)
    bw, bh = d0.multiline_textbbox((0, 0), text, font=f, spacing=4)[2:]
    canvas = Image.new("RGB", (w + bw + 40, h), "#0f0f0f")
    canvas.paste(menu_img, (0, 0))
    d = ImageDraw.Draw(canvas)
    bx, by = rx + 30, ry - 6
    d.rectangle((bx, by, bx + bw + 22, by + bh + 16), fill="#262626", outline="#333333")
    d.multiline_text((bx + 11, by + 6), text, font=f, fill="#ececec", spacing=4)
    canvas.save(os.path.join(DOCS, "menu-tooltip.png"))
    app.root.destroy()


app.root.after(300, app.show)
app.root.after(2500, snap)
app.root.mainloop()

# app icon
icon = sw.tray_image().resize((256, 256), Image.LANCZOS)
bg = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
ImageDraw.Draw(bg).rounded_rectangle((0, 0, 255, 255), 56, fill="#1e1e1e")
bg.alpha_composite(icon.resize((168, 168), Image.LANCZOS), (44, 40))
bg.save(os.path.join(DOCS, "icon.png"))
bg.save(os.path.join(ROOT, "src", "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print("ok")
