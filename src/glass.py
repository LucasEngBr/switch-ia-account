"""Dark "liquid glass" renderer (iOS-style) for the tray menu, drawn with Pillow.

The window shows one image: the blurred screen behind it, tinted, with glass cards,
specular highlights and anti-aliased shapes on top. Hit regions are returned so the
Tk window can map mouse events back to actions.
"""
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageGrab

SS = 3  # supersampling for shapes
KEY = (1, 2, 3)  # transparent key colour outside the rounded window

FG = (245, 245, 247)
MUTED = (235, 235, 245, 150)
FAINT = (235, 235, 245, 92)
RED = (255, 99, 92)
GREEN, AMBER = (52, 199, 89), (255, 179, 64)

_fonts = {}
TEXT_SCALE = 1.18  # text a bit larger than the layout grid (icons keep their size)


def fit(d, text, f, maxw):
    """Cut text with '…' so it never runs past maxw pixels."""
    if maxw <= 0:
        return ""
    if d.textlength(text, font=f) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(text[:mid].rstrip() + "…", font=f) <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else "…"


def font(size, weight="regular"):
    if weight != "icon":
        size = max(7, int(round(size * TEXT_SCALE)))
    k = (size, weight)
    if k not in _fonts:
        names = {"regular": ["SegUIVar.ttf", "segoeui.ttf"], "semibold": ["seguisb.ttf", "segoeuib.ttf"],
                 "bold": ["segoeuib.ttf"], "icon": ["SegoeIcons.ttf", "segmdl2.ttf"]}[weight]
        for n in names:
            try:
                _fonts[k] = ImageFont.truetype(n, size)
                break
            except OSError:
                continue
        else:
            _fonts[k] = ImageFont.load_default()
    return _fonts[k]


class Layout:
    def __init__(self, s=1.0):
        self.s = s
        self.pad, self.gap, self.col_w = self.u(12), self.u(10), self.u(304)
        self.head_h, self.email_h, self.row_h = self.u(40), self.u(26), self.u(46)
        self.sep_h, self.add_h, self.inner = self.u(11), self.u(42), self.u(8)
        self.radius, self.card_r, self.pill_r = self.u(28), self.u(20), self.u(14)

    def u(self, v):
        return int(round(v * self.s))


def build(columns, s=1.0):
    """columns: [{'name','accent','groups':[(email,[acc..])],'active':key}] -> (size, items)."""
    L = Layout(s)
    items, heights = [], []
    for ci, col in enumerate(columns):
        x0 = L.pad + ci * (L.col_w + L.gap)
        y = L.pad + L.inner
        items.append(("head", (x0, y, x0 + L.col_w, y + L.head_h), col, None))
        y += L.head_h
        for gi, (email, accs) in enumerate(col["groups"]):
            if gi:
                items.append(("sep", (x0, y, x0 + L.col_w, y + L.sep_h), col, None))
                y += L.sep_h
            items.append(("email", (x0, y, x0 + L.col_w, y + L.email_h), col, email))
            y += L.email_h
            for acc in accs:
                r = (x0 + L.inner, y, x0 + L.col_w - L.inner, y + L.row_h)
                items.append(("row", r, col, acc))
                cx = r[2] - L.u(20)
                items.append(("door", (cx - L.u(15), y, cx + L.u(15), y + L.row_h), col, acc))
                rx = r[2] - L.u(84)
                items.append(("ring", (rx - L.u(15), y, rx + L.u(15), y + L.row_h), col, acc))
                y += L.row_h
        if col["groups"]:
            items.append(("sep", (x0, y, x0 + L.col_w, y + L.sep_h), col, None))
            y += L.sep_h
        items.append(("add", (x0 + L.inner, y, x0 + L.col_w - L.inner, y + L.add_h), col, None))
        y += L.add_h + L.inner
        heights.append(y)
    w = L.pad * 2 + len(columns) * L.col_w + (len(columns) - 1) * L.gap
    h = max(heights) + L.pad
    return (w, h), items, L


def grab(box):
    try:
        return ImageGrab.grab(box, all_screens=True).convert("RGB")
    except Exception:  # noqa: BLE001
        return Image.new("RGB", (box[2] - box[0], box[3] - box[1]), (30, 30, 34))


class Backdrop:
    """What is behind the window, with a margin so the lens can bend in content from outside."""

    def __init__(self, img, margin, s):
        self.m, self.s = margin, s
        img = img.filter(ImageFilter.GaussianBlur(1.6 * s))      # Liquid Glass is clear, not frosted
        img = ImageEnhance.Color(img).enhance(1.35)
        self.src = np.asarray(img, dtype=np.float32) / 255.0
        self.base = None


def backdrop(box, size, s=1.0):
    m = int(40 * s)
    img = grab((box[0] - m, box[1] - m, box[0] + size[0] + m, box[1] + size[1] + m))
    img = img.resize((size[0] + 2 * m, size[1] + 2 * m))
    return Backdrop(img, m, s)


# ---------- lens optics
LIGHT = np.array([-0.55, -0.83])  # light from the top-left


def _sdf(w, h, r):
    """Signed distance to a rounded rect of size w x h (negative inside) and its outward normal."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    px, py = xs + 0.5 - w / 2, ys + 0.5 - h / 2
    qx, qy = np.abs(px) - (w / 2 - r), np.abs(py) - (h / 2 - r)
    ox, oy = np.maximum(qx, 0), np.maximum(qy, 0)
    d = np.hypot(ox, oy) + np.minimum(np.maximum(qx, qy), 0) - r
    ln = np.hypot(ox, oy) + 1e-6
    corner = (qx > 0) & (qy > 0)
    nx = np.where(corner, ox / ln, np.where(qx > qy, 1.0, 0.0)) * np.sign(px)
    ny = np.where(corner, oy / ln, np.where(qx > qy, 0.0, 1.0)) * np.sign(py)
    return d, nx, ny


def _sample(src, x, y):
    h, w = src.shape[:2]
    x = np.clip(x, 0, w - 1.001)
    y = np.clip(y, 0, h - 1.001)
    x0, y0 = x.astype(np.int32), y.astype(np.int32)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    a, b = src[y0, x0], src[y0, x0 + 1]
    c, d = src[y0 + 1, x0], src[y0 + 1, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def lens(src, off, box, r, s, bezel=18, depth=1.0, dark=0.5, spec=1.0, chroma=1.0):
    """Liquid Glass slab over `box`: refraction at the bezel, chromatic fringe, dark tint, specular rim.

    src: float RGB array to sample from; off: (dx, dy) of output coords inside src.
    Returns (rgb float array, alpha mask float array) for the box.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    d, nx, ny = _sdf(w, h, r)
    b = bezel * s
    t = np.clip(-d, 0, None)                      # depth inside the edge
    xb = np.clip(1 - t / b, 0, 1)                  # 1 at the rim, 0 past the bezel
    bend = (xb ** 2.2) * b * depth * 1.4        # convex lens: strongest at the rim, fades inward
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    gx, gy = xs + x0 + off[0], ys + y0 + off[1]
    out = np.empty((h, w, 3), np.float32)
    for ch, k in enumerate((1.0, 1.0 + 0.10 * chroma, 1.0 + 0.20 * chroma)):   # R, G, B bend differently
        out[..., ch] = _sample(src[..., ch:ch + 1], gx + nx * bend * k, gy + ny * bend * k)[..., 0]
    # dark, luminance-adaptive tint: bright backgrounds get pulled down more
    lum = out @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    keep = (1 - dark) - 0.18 * dark * lum
    out = out * keep[..., None] + np.array([0.03, 0.03, 0.04], np.float32) * dark
    # specular: thin bright rim facing the light, softer on the opposite side, faint inner glow
    face = nx * LIGHT[0] + ny * LIGHT[1]
    rim = np.exp(-((t - 0.6 * s) / (0.9 * s)) ** 2)
    glow = np.exp(-t / (b * 0.55))
    hi = spec * (rim * (0.30 + 0.70 * np.clip(face, 0, 1) ** 1.5 + 0.35 * np.clip(-face, 0, 1) ** 2)
                 + glow * 0.07 * np.clip(face, 0, 1))
    out = out + hi[..., None] * (1 - out) * 0.9
    alpha = np.clip(0.5 - d, 0, 1)                 # 1px anti-aliased edge
    return np.clip(out, 0, 1), alpha


def _rr(d, box, r, **kw):
    d.rounded_rectangle([v * SS for v in box], r * SS, **kw)


def _vgrad(size, top, bottom):
    g = Image.new("RGBA", (1, 256))
    for i in range(256):
        t = i / 255
        g.putpixel((0, i), tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(4)))
    return g.resize(size)


def _glass_card(base, box, r, fill_a=16, edge_a=52):
    """Translucent card with a light top rim fading down (liquid glass edge)."""
    w, h = box[2] - box[0], box[3] - box[1]
    big = (w * SS, h * SS)
    mask = Image.new("L", big, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), r * SS, fill=255)
    body = _vgrad(big, (255, 255, 255, fill_a + 10), (255, 255, 255, fill_a - 6))
    body.putalpha(ImageChops.multiply(body.getchannel("A"), mask))
    rim = Image.new("L", big, 0)
    ImageDraw.Draw(rim).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), r * SS, outline=255, width=SS)
    rim_grad = _vgrad(big, (255, 255, 255, edge_a + 40), (255, 255, 255, max(edge_a - 30, 8)))
    rim_layer = Image.new("RGBA", big, (255, 255, 255, 0))
    rim_layer.putalpha(ImageChops.multiply(rim_grad.getchannel("A"), rim))
    card = Image.alpha_composite(body, rim_layer).resize((w, h), Image.LANCZOS)
    base.alpha_composite(card, (box[0], box[1]))


def _ring(size, left, s):
    big = size * SS
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    wd = max(2, int(2.6 * s)) * SS
    m = wd // 2 + SS
    d.ellipse((m, m, big - m, big - m), outline=(255, 255, 255, 40), width=wd)
    if left is not None and left > 0:
        c = GREEN if left > 50 else AMBER if left > 20 else RED
        if left >= 100:
            d.ellipse((m, m, big - m, big - m), outline=c + (255,), width=wd)
        else:
            d.arc((m, m, big - m, big - m), -90, -90 + 3.6 * left, fill=c + (255,), width=wd)
    return im.resize((size, size), Image.LANCZOS)


def _avatar(size, color, text, s):
    big = size * SS
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    c = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    g = _vgrad((big, big), tuple(min(255, v + 40) for v in c) + (255,), tuple(int(v * 0.8) for v in c) + (255,))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    im.paste(g, (0, 0), mask)
    hl = Image.new("L", (big, big), 0)
    ImageDraw.Draw(hl).ellipse((big * 0.18, big * 0.04, big * 0.82, big * 0.48), fill=60)
    im.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (big, big), 255)] * 3,
                                            ImageChops.multiply(hl, mask))))
    ImageDraw.Draw(im).ellipse((0, 0, big - 1, big - 1), outline=(255, 255, 255, 70), width=SS)
    im = im.resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    d.text((size / 2, size / 2 + s * 0.5), text, font=font(int(11 * s), "semibold"), fill=FG, anchor="mm")
    return im


def render(bg, items, L, state):
    """state: hover=(kind,id), active={colname:key}, usage={(col,key):left|None|'err'},
    armed=(col,key)|None, initials=fn, label=fn."""
    s = L.s
    w, h = bg.src.shape[1] - 2 * bg.m, bg.src.shape[0] - 2 * bg.m
    if bg.base is None:
        # 1) the window itself is one thick dark glass slab bending the desktop at its rim
        arr, alpha = lens(bg.src, (bg.m, bg.m), (0, 0, w, h), L.radius, s,
                          bezel=34, depth=1.0, dark=0.46, spec=1.0, chroma=1.0)
        # 2) each column is a thinner slab on top, refracting the slab below
        cols = {}
        for kind, box, col, _ in items:
            b = cols.setdefault(col["name"], list(box))
            b[0], b[1], b[2], b[3] = min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3])
        for b in cols.values():
            cb = (b[0], L.pad, b[2], h - L.pad)
            sub, a = lens(arr, (0, 0), cb, L.card_r, s, bezel=18, depth=0.9, dark=0.14, spec=0.75, chroma=0.6)
            reg = arr[cb[1]:cb[3], cb[0]:cb[2]]
            reg[:] = reg * (1 - a[..., None]) + sub * a[..., None]
        bg.base, bg.alpha = arr, alpha

    arr = bg.base.copy()
    hover = state.get("hover")
    for kind, box, col, acc in items:
        key = acc["key"] if isinstance(acc, dict) else None
        hid = (kind, col["name"], key)
        hov = hover and hover[1:] == hid[1:] and (hover[0] in ("row", "ring", "door") if kind == "row"
                                                   else hover[0] == kind)
        active = kind == "row" and state["active"].get(col["name"]) == key
        if kind in ("row", "add") and (hov or active):
            # 3) hovered / selected rows are a small glass capsule, lighter than the card
            sub, a = lens(arr, (0, 0), box, L.pill_r, s, bezel=10, depth=0.75,
                          dark=-0.10 if hov else -0.04, spec=0.9 if hov else 0.6, chroma=0.5)
            reg = arr[box[1]:box[3], box[0]:box[2]]
            reg[:] = reg * (1 - a[..., None]) + sub * a[..., None]
    img = Image.fromarray((arr * 255).astype(np.uint8), "RGB").convert("RGBA")

    over = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    for kind, box, col, acc in items:
        key = acc["key"] if isinstance(acc, dict) else None
        if kind == "sep":
            y = (box[1] + box[3]) / 2
            od.line(((box[0] + L.u(18)) * SS, y * SS, (box[2] - L.u(18)) * SS, y * SS),
                    fill=(255, 255, 255, 26), width=SS)
        elif kind == "door" and state.get("armed") == (col["name"], key):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            rr = L.u(13)
            od.ellipse(((cx - rr) * SS, (cy - rr) * SS, (cx + rr) * SS, (cy + rr) * SS),
                       fill=RED + (48,), outline=RED + (120,), width=SS)
    img.alpha_composite(over.resize((w, h), Image.LANCZOS))

    d = ImageDraw.Draw(img)
    s = L.s
    for kind, box, col, acc in items:
        key = acc["key"] if isinstance(acc, dict) else None
        cy = (box[1] + box[3]) / 2
        if kind == "head":
            dot = L.u(4)
            cx = box[0] + L.u(22)
            d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=col["accent"])
            fh = font(int(15 * s), "semibold")
            d.text((cx + L.u(12), cy), fit(d, col["name"], fh, box[2] - cx - L.u(28)), font=fh, fill=FG, anchor="lm")
        elif kind == "email":
            fe = font(int(11.5 * s))
            d.text((box[0] + L.u(20), cy), fit(d, acc, fe, box[2] - box[0] - L.u(40)), font=fe, fill=MUTED,
                   anchor="lm")
        elif kind == "row":
            a = L.u(30)
            img.alpha_composite(_avatar(a, acc["color"], state["initials"](acc), s),
                                (box[0] + L.u(10), int(cy - a / 2)))
            fn, fp = font(int(15.5 * s), "semibold"), font(int(12 * s))
            tx = box[0] + L.u(50)
            avail = box[2] - L.u(84) - L.u(16) - tx          # stop before the usage ring
            gap = L.u(7)
            plan = acc.get("plan") or ""
            nw, pw = d.textlength(acc["name"], font=fn), d.textlength(plan, font=fp) if plan else 0
            if nw + (gap + pw if plan else 0) > avail:
                name_max = max(avail * 0.6, avail - gap - pw) if plan else avail
                name = fit(d, acc["name"], fn, name_max)
                nw = d.textlength(name, font=fn)
                plan = fit(d, plan, fp, avail - nw - gap) if plan else ""
            else:
                name = acc["name"]
            d.text((tx, cy), name, font=fn, fill=FG, anchor="lm")
            if plan and plan != "…":
                d.text((tx + nw + gap, cy), plan, font=fp, fill=MUTED, anchor="lm")
            if state["active"].get(col["name"]) == key:
                d.text((box[2] - L.u(52), cy), "", font=font(int(13 * s), "icon"), fill=FG, anchor="mm")
        elif kind == "ring":
            rs = L.u(22)
            left = state["usage"].get((col["name"], key))
            ring = _ring(rs, left if isinstance(left, int) else None, s)
            cx = (box[0] + box[2]) / 2
            img.alpha_composite(ring, (int(cx - rs / 2), int(cy - rs / 2)))
            if left == "err":
                d.text((cx, cy), "!", font=font(int(11 * s), "bold"), fill=MUTED, anchor="mm")
        elif kind == "door":
            armed = state.get("armed") == (col["name"], key)
            hov = hover == ("door", col["name"], key)
            fill = RED if armed else (FG if hov else MUTED)
            d.text(((box[0] + box[2]) / 2, cy), "", font=font(int(14 * s), "icon"), fill=fill, anchor="mm")
        elif kind == "add":
            d.text((box[0] + L.u(22), cy), "", font=font(int(12 * s), "icon"), fill=FG, anchor="mm")
            d.text((box[0] + L.u(42), cy), "Adicionar conta", font=font(int(13 * s)), fill=FG, anchor="lm")

    # rounded window: outside the outer radius becomes the transparent key colour
    mask = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), L.radius * SS, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    out = Image.new("RGB", (w, h), KEY)
    out.paste(img.convert("RGB"), (0, 0), mask.point(lambda v: 255 if v > 110 else 0))
    return out


def tooltip(text, origin_box, s=1.0):
    """Small glass bubble; returns (image, size)."""
    f = font(int(12.5 * s))
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bw, bh = tmp.multiline_textbbox((0, 0), text, font=f, spacing=int(5 * s))[2:]
    pw, ph = int(14 * s), int(10 * s)
    size = (bw + pw * 2, bh + ph * 2 + int(2 * s))
    if origin_box[2] - origin_box[0] < 4:
        return Image.new("RGB", size, KEY)  # size probe only
    bg = backdrop(origin_box, size, s)
    r = min(int(14 * s), size[1] // 2)
    arr, alpha = lens(bg.src, (bg.m, bg.m), (0, 0, size[0], size[1]), r, s,
                      bezel=12, depth=1.0, dark=0.55, spec=1.0, chroma=0.8)
    img = Image.fromarray((arr * 255).astype(np.uint8), "RGB")
    ImageDraw.Draw(img).multiline_text((pw, ph), text, font=f, fill=FG, spacing=int(5 * s))
    mask = Image.fromarray((alpha > 0.43).astype(np.uint8) * 255, "L")
    out = Image.new("RGB", size, KEY)
    out.paste(img, (0, 0), mask)
    return out
