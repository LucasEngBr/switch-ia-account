"""Render the menu with fake accounts over a synthetic wallpaper into docs/ (no real login or screen is read)."""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import glass  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
S = 1.5

COLS = [
    {"name": "Claude", "accent": "#d97757", "groups": [
        ("ana@empresa.com", [{"key": "c1", "name": "Ana", "email": "ana@empresa.com", "plan": "Max", "color": "#d97757"}]),
        ("ana.pessoal@gmail.com", [{"key": "c2", "name": "Ana", "email": "ana.pessoal@gmail.com", "plan": "Pro", "color": "#7c5cff"}]),
        ("studio@exemplo.dev", [{"key": "c3", "name": "Estúdio", "email": "studio@exemplo.dev", "plan": "Team", "color": "#2f7de1"}])]},
    {"name": "Codex", "accent": "#10a37f", "groups": [
        ("ana@empresa.com", [{"key": "x1", "name": "Ana", "email": "ana@empresa.com", "plan": "Team", "color": "#1f9d6b"},
                             {"key": "x2", "name": "Ana", "email": "ana@empresa.com", "plan": "Plus", "color": "#e08a1e"}]),
        ("bruno@exemplo.dev", [{"key": "x3", "name": "Bruno", "email": "bruno@exemplo.dev", "plan": "Plus", "color": "#c2408f"}]),
        ("labs@exemplo.dev", [{"key": "x4", "name": "Labs", "email": "labs@exemplo.dev", "plan": "Pro", "color": "#4b9fa8"}])]},
]
USAGE = {("Claude", "c1"): 72, ("Claude", "c2"): 0, ("Claude", "c3"): 35,
         ("Codex", "x1"): 76, ("Codex", "x2"): 100, ("Codex", "x3"): 12, ("Codex", "x4"): 100}


def wallpaper(size):
    w, h = size
    im = Image.new("RGB", size, (10, 12, 22))
    d = ImageDraw.Draw(im)
    for (cx, cy, r, c) in [(0.18, 0.25, 0.42, (217, 119, 87)), (0.78, 0.2, 0.38, (88, 86, 214)),
                           (0.62, 0.85, 0.45, (16, 163, 127)), (0.1, 0.95, 0.3, (47, 125, 225))]:
        d.ellipse(((cx - r) * w, (cy - r) * w, (cx + r) * w, (cy + r) * w), fill=c)
    im = im.filter(ImageFilter.GaussianBlur(w / 12))
    # crisp details so the lens bending at the rims is visible
    d = ImageDraw.Draw(im)
    step = int(46 * S)
    for x in range(0, w, step):
        d.line((x, 0, x, h), fill=(255, 255, 255), width=1)
    for y in range(0, h, step):
        d.line((0, y, w, y), fill=(255, 255, 255), width=1)
    for i, (x, y, r) in enumerate([(0.32, 0.18, 60), (0.7, 0.62, 80), (0.15, 0.7, 46), (0.9, 0.3, 54)]):
        d.ellipse((x * w - r * S, y * h - r * S, x * w + r * S, y * h + r * S),
                  fill=[(255, 214, 102), (255, 255, 255), (255, 120, 160), (120, 220, 255)][i])
    return im


(w, h), items, L = glass.build(COLS, S)
canvas_w, canvas_h = w + int(260 * S), h + int(80 * S)
pad = int(60 * S)
wall_full = wallpaper((canvas_w + 2 * pad, canvas_h + 2 * pad))
wall = wall_full.crop((pad, pad, pad + canvas_w, pad + canvas_h))
ox, oy = int(40 * S), int(40 * S)


def fake_backdrop(box, size, s=1.0):
    m = int(40 * s)
    img = wall_full.crop((box[0] - m + pad, box[1] - m + pad, box[0] + size[0] + m + pad, box[1] + size[1] + m + pad))
    return glass.Backdrop(img, m, s)


glass.backdrop = fake_backdrop

state = {"hover": ("row", "Codex", "x3"), "armed": None, "initials": lambda a: a["name"][0].upper(),
         "active": {"Claude": "c1", "Codex": "x1"}, "usage": USAGE}
menu = glass.render(glass.backdrop((ox, oy, ox + w, oy + h), (w, h)), items, L, state)


def place(img, x, y, base):
    """Paste like the real window does: the key colour is see-through."""
    mask = Image.frombytes("L", img.size, bytes(0 if p == glass.KEY else 255 for p in img.get_flattened_data()))
    base.paste(img, (x, y), mask)


out = wall.copy()
place(menu, ox, oy, out)
out.crop((ox - 20, oy - 20, ox + w + 20, oy + h + 20)).save(os.path.join(DOCS, "menu.png"))

ring = next(b for k, b, c, a in items if k == "ring" and isinstance(a, dict) and a["key"] == "x3")
text = "Sessão (5h): 12% restantes\nSemanal: 40% restantes"
tx = ox + ring[2] + int(8 * S)
tmp = glass.tooltip(text, (0, 0, 1, 1), S)
ty = oy + (ring[1] + ring[3]) // 2 - tmp.size[1] // 2
tip = glass.tooltip(text, (tx, ty, tx + tmp.size[0], ty + tmp.size[1]), S)
place(tip, tx, ty, out)
out.crop((ox - 20, oy - 20, max(ox + w, tx + tip.size[0]) + 30, oy + h + 20)).save(
    os.path.join(DOCS, "menu-tooltip.png"))

import importlib.machinery, importlib.util  # noqa: E401,E402
loader = importlib.machinery.SourceFileLoader("sw", os.path.join(ROOT, "src", "switcher.pyw"))
spec = importlib.util.spec_from_loader("sw", loader)
sw = importlib.util.module_from_spec(spec)
loader.exec_module(sw)
icon = sw.tray_image().resize((256, 256), Image.LANCZOS)
bg = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
ImageDraw.Draw(bg).rounded_rectangle((0, 0, 255, 255), 56, fill="#1e1e1e")
bg.alpha_composite(icon.resize((168, 168), Image.LANCZOS), (44, 40))
bg.save(os.path.join(DOCS, "icon.png"))
bg.save(os.path.join(ROOT, "src", "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print("ok")
