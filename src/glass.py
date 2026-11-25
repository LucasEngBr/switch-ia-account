"""Dark "liquid glass" renderer (iOS-style) for the tray menu, drawn with Pillow.

The window shows one image: the blurred screen behind it, tinted, with glass cards,
specular highlights and anti-aliased shapes on top. Hit regions are returned so the
Tk window can map mouse events back to actions.
"""
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageGrab

SS = 3  # supersampling for shapes
KEY = (1, 2, 3)  # transparent key colour outside the rounded window

FG = (245, 245, 247)
MUTED = (235, 235, 245, 150)
FAINT = (235, 235, 245, 92)
RED = (255, 99, 92)
GREEN, AMBER = (52, 199, 89), (255, 179, 64)

_fonts = {}


def font(size, weight="regular"):
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


def backdrop(box, size):
    """Blurred, tinted, slightly saturated grab of what is behind the window."""
    try:
        bg = ImageGrab.grab(box, all_screens=True).convert("RGB").resize(size)
    except Exception:  # noqa: BLE001
        bg = Image.new("RGB", size, (30, 30, 34))
    bg = bg.filter(ImageFilter.GaussianBlur(max(size) / 22))
    bg = ImageEnhance.Color(bg).enhance(1.6)
    tint = Image.new("RGB", size, (14, 14, 18))
    return Image.blend(bg, tint, 0.62)


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
    w, h = bg.size
    img = bg.convert("RGBA")

    # outer specular sheen
    img.alpha_composite(_vgrad((w, h), (255, 255, 255, 18), (255, 255, 255, 0)))
    cols = {}
    for kind, box, col, _ in items:
        cols.setdefault(col["name"], [box[0], box[1], box[2], box[3]])
        b = cols[col["name"]]
        b[0], b[1], b[2], b[3] = min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3])
    for name, b in cols.items():
        _glass_card(img, (b[0], L.pad, b[2], h - L.pad), L.card_r, fill_a=14, edge_a=46)

    over = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    hover = state.get("hover")
    for kind, box, col, acc in items:
        key = acc["key"] if isinstance(acc, dict) else None
        hid = (kind, col["name"], key)
        if kind == "row":
            active = state["active"].get(col["name"]) == key
            hov = hover and hover[1:] == hid[1:] and hover[0] in ("row", "ring", "door")
            if hov or active:
                _rr(od, box, L.pill_r, fill=(255, 255, 255, 30 if hov else 16),
                    outline=(255, 255, 255, 46 if hov else 26), width=SS)
        elif kind == "add" and hover == hid:
            _rr(od, box, L.pill_r, fill=(255, 255, 255, 26), outline=(255, 255, 255, 40), width=SS)
        elif kind == "sep":
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
            d.text((cx + L.u(12), cy), col["name"], font=font(int(15 * s), "semibold"), fill=FG, anchor="lm")
        elif kind == "email":
            d.text((box[0] + L.u(20), cy), acc, font=font(int(11.5 * s)), fill=MUTED, anchor="lm")
        elif kind == "row":
            a = L.u(30)
            img.alpha_composite(_avatar(a, acc["color"], state["initials"](acc), s),
                                (box[0] + L.u(10), int(cy - a / 2)))
            d.text((box[0] + L.u(50), cy), acc["name"], font=font(int(13.5 * s), "semibold"), fill=FG, anchor="lm")
            nx = box[0] + L.u(50) + d.textlength(acc["name"], font=font(int(13.5 * s), "semibold"))
            if acc.get("plan"):
                d.text((nx + L.u(8), cy), acc["plan"], font=font(int(12 * s)), fill=MUTED, anchor="lm")
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
    rim = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(rim).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), L.radius * SS, outline=255,
                                          width=int(1.2 * SS))
    rim = rim.resize((w, h), Image.LANCZOS)
    edge = _vgrad((w, h), (255, 255, 255, 120), (255, 255, 255, 34))
    edge.putalpha(ImageChops.multiply(edge.getchannel("A"), rim))
    img.alpha_composite(edge)
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
    bg = backdrop(origin_box, size)
    L = Layout(s)
    L.radius = int(14 * s)
    img = bg.convert("RGBA")
    _glass_card(img, (0, 0, size[0], size[1]), L.radius, fill_a=26, edge_a=70)
    ImageDraw.Draw(img).multiline_text((pw, ph), text, font=f, fill=FG, spacing=int(5 * s))
    mask = Image.new("L", (size[0] * SS, size[1] * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] * SS - 1, size[1] * SS - 1), L.radius * SS, fill=255)
    mask = mask.resize(size, Image.LANCZOS).point(lambda v: 255 if v > 110 else 0)
    out = Image.new("RGB", size, KEY)
    out.paste(img.convert("RGB"), (0, 0), mask)
    return out
