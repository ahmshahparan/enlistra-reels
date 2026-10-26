"""Leaf & Ridge channel art: layered mountain ridges at dusk with one amber leaf as the sun."""
import math, random
from PIL import Image, ImageDraw, ImageFilter
from make_brand import font, LORA, INTER_SEMI, INTER_MED

SS = 2
SKY_TOP, SKY_BOT = (34, 49, 58), (92, 112, 112)
RIDGES = [(120, 140, 134), (86, 108, 104), (58, 78, 76), (36, 52, 52), (22, 33, 34)]
AMBER, CREAM = (217, 142, 58), (241, 233, 214)


def sky(w, h):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(SKY_TOP[i] * (1 - t) + SKY_BOT[i] * t) for i in range(3)))
    return img


def ridge(d, w, h, base, amp, color, seed, step=6):
    rnd = random.Random(seed)
    pts, y = [], base
    phase = [rnd.uniform(0, 6.28) for _ in range(3)]
    for x in range(0, w + step, step):
        y = base - amp * (0.55 * math.sin(x / w * 2.1 * math.pi + phase[0]) +
                          0.3 * math.sin(x / w * 5.3 * math.pi + phase[1]) +
                          0.15 * math.sin(x / w * 11.7 * math.pi + phase[2]))
        pts.append((x, y))
    d.polygon(pts + [(w, h), (0, h)], fill=color)


def leaf(d, cx, cy, r, color, vein, angle=-35):
    a = math.radians(angle)
    def rot(x, y):
        return (cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a))
    pts = []
    for i in range(80):
        t = i / 79 * math.pi
        pts.append(rot(-r + 2 * r * i / 79, -r * 0.42 * math.sin(t) ** 1.3))
    for i in range(80):
        t = i / 79 * math.pi
        pts.append(rot(r - 2 * r * i / 79, r * 0.42 * math.sin(t) ** 1.3))
    d.polygon(pts, fill=color)
    wv = max(2, int(r * 0.05))
    d.line([rot(-r * 1.15, 0), rot(r * 0.95, 0)], fill=vein, width=wv)       # midrib and stem
    for k in (-0.5, -0.1, 0.3):                                             # side veins
        d.line([rot(r * k, 0), rot(r * (k + 0.32), -r * 0.26)], fill=vein, width=max(1, wv - 1))
        d.line([rot(r * k, 0), rot(r * (k + 0.32), r * 0.26)], fill=vein, width=max(1, wv - 1))


def scene(w, h, sun=(0.5, 0.42), sun_r=0.11, ridge_base=0.52):
    img = sky(w, h)
    glow = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(glow)
    sx, sy, R = w * sun[0], h * sun[1], min(w, h) * sun_r
    gd.ellipse((sx - R * 2.6, sy - R * 2.6, sx + R * 2.6, sy + R * 2.6), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(R))
    img = Image.composite(Image.new("RGB", (w, h), (226, 170, 104)), img, glow)
    d = ImageDraw.Draw(img)
    leaf(d, sx, sy, R, AMBER, (176, 104, 36))
    for i, c in enumerate(RIDGES):
        ridge(d, w, h, h * (ridge_base + i * 0.085), h * (0.07 - i * 0.008), c, seed=i + 3)
    return img


def avatar(out):
    S = 800 * SS
    img = scene(S, S, sun=(0.5, 0.36), sun_r=0.19, ridge_base=0.56)
    img.resize((800, 800), Image.LANCZOS).save(out)


def banner(out):
    W, H = 2560 * SS, 1440 * SS
    img = scene(W, H, sun=(0.5, 0.345), sun_r=0.036, ridge_base=0.665)
    d = ImageDraw.Draw(img)
    title, tag, small = font(LORA, 132 * SS), font(INTER_SEMI, 50 * SS), font(INTER_MED, 34 * SS)
    for text, f, y, col in (("Leaf & Ridge", title, 0.375, CREAM), ("Slow nature. Real places.", tag, 0.515, AMBER),
                            ("Original nature footage · Calm music · New videos every week", small, 0.575, (200, 208, 200))):
        tw = d.textlength(text, font=f)
        d.text(((W - tw) / 2, H * y), text, font=f, fill=col)
    img.resize((2560, 1440), Image.LANCZOS).save(out, quality=95)


if __name__ == "__main__":
    avatar("leafridge_avatar.png")
    banner("leafridge_banner.jpg")
    a = Image.open("leafridge_avatar.png").resize((300, 300))
    m = Image.new("L", (300, 300), 0); ImageDraw.Draw(m).ellipse((0, 0, 299, 299), fill=255)
    b = Image.open("leafridge_banner.jpg")
    prev = Image.new("RGB", (1280, 1080), "white")
    prev.paste(b.resize((1280, 720)), (0, 0))
    ImageDraw.Draw(prev).rectangle((253, 254, 1026, 465), outline=(255, 0, 0), width=2)
    prev.paste(a, (40, 750), m)
    prev.save("leafridge_preview.jpg")
    print("ok")
