"""Channel artwork for Enlistra Reels and Enlistra Wild Facts (original designs)."""
import math
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SS = 2  # supersampling factor
LORA = "/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf"
INTER_BLACK = "/usr/share/fonts/opentype/inter/InterDisplay-Black.otf"
INTER_SEMI = "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"
INTER_MED = "/usr/share/fonts/opentype/inter/Inter-Medium.otf"


def font(path, size):
    try:
        f = ImageFont.truetype(path, size)
        if "Variable" in path:
            f.set_variation_by_name("Bold")
        return f
    except OSError:
        import glob
        alt = glob.glob("/usr/share/fonts/**/Lora*Bold*.ttf", recursive=True)
        return ImageFont.truetype(alt[0] if alt else "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", size)


def radial_bg(w, h, inner, outer, cx=0.5, cy=0.45, r=0.75):
    img = Image.new("RGB", (w, h), outer)
    px = img.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            d = math.hypot((x / w - cx) * (w / h), y / h - cy) / r
            t = max(0.0, min(1.0, d))
            c = tuple(int(inner[i] * (1 - t) + outer[i] * t) for i in range(3))
            px[x, y] = c
            if x + 1 < w: px[x + 1, y] = c
            if y + 1 < h:
                px[x, y + 1] = c
                if x + 1 < w: px[x + 1, y + 1] = c
    return img


# ---------------------------------------------------------------- marks
def reel_mark(d, cx, cy, r, cream=(243, 233, 210), accent=(242, 107, 29), bg=(28, 23, 20)):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=cream)
    d.ellipse((cx - r * 0.86, cy - r * 0.86, cx + r * 0.86, cy + r * 0.86), outline=bg, width=max(2, int(r * 0.035)))
    for k in range(5):  # five sprocket windows
        a = -math.pi / 2 + k * 2 * math.pi / 5
        hx, hy, hr = cx + math.cos(a) * r * 0.52, cy + math.sin(a) * r * 0.52, r * 0.2
        d.ellipse((hx - hr, hy - hr, hx + hr, hy + hr), fill=bg)
    hub = r * 0.2  # centre hub with a play triangle
    d.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=accent)
    t = hub * 0.55
    d.polygon([(cx - t * 0.6, cy - t), (cx - t * 0.6, cy + t), (cx + t, cy)], fill=cream)


def owl_mark(d, cx, cy, r, cream=(244, 236, 214), amber=(242, 164, 29), dark=(14, 46, 32), beak=(242, 107, 29)):
    # head: rounded body with two ear tufts
    d.polygon([(cx - r * 0.78, cy - r * 0.55), (cx - r * 0.55, cy - r * 1.02), (cx - r * 0.28, cy - r * 0.62)], fill=cream)
    d.polygon([(cx + r * 0.78, cy - r * 0.55), (cx + r * 0.55, cy - r * 1.02), (cx + r * 0.28, cy - r * 0.62)], fill=cream)
    d.rounded_rectangle((cx - r * 0.82, cy - r * 0.72, cx + r * 0.82, cy + r * 0.9), radius=r * 0.75, fill=cream)
    for s in (-1, 1):
        ex = cx + s * r * 0.38
        ey = cy - r * 0.12
        er = r * 0.34
        d.ellipse((ex - er, ey - er, ex + er, ey + er), fill=dark)
        ir = er * 0.72
        d.ellipse((ex - ir, ey - ir, ex + ir, ey + ir), fill=amber)
        pr = er * 0.36
        d.ellipse((ex - pr, ey - pr, ex + pr, ey + pr), fill=dark)
        gl = er * 0.13
        d.ellipse((ex - pr * 0.2 - gl, ey - pr * 0.55 - gl, ex - pr * 0.2 + gl, ey - pr * 0.55 + gl), fill=cream)
    d.polygon([(cx - r * 0.1, cy + r * 0.2), (cx + r * 0.1, cy + r * 0.2), (cx, cy + r * 0.42)], fill=beak)
    # chest feathers: three small chevrons
    for i, yy in enumerate((0.58, 0.7)):
        for xx in ((-0.18, 0.0, 0.18) if i == 0 else (-0.09, 0.09)):
            px, py, w = cx + xx * r, cy + yy * r, r * 0.08
            d.line([(px - w, py - w * 0.6), (px, py), (px + w, py - w * 0.6)], fill=dark, width=max(2, int(r * 0.03)))


# ---------------------------------------------------------------- avatars
def avatar(kind, out):
    S = 800 * SS
    if kind == "reels":
        img = radial_bg(S, S, (58, 44, 36), (20, 16, 14), r=0.8)
        d = ImageDraw.Draw(img)
        reel_mark(d, S / 2, S / 2, S * 0.33)
    else:
        img = radial_bg(S, S, (30, 92, 62), (10, 38, 26), r=0.8)
        d = ImageDraw.Draw(img)
        owl_mark(d, S / 2, S * 0.53, S * 0.3)
    img.resize((800, 800), Image.LANCZOS).save(out)


# ---------------------------------------------------------------- banners
def center_text(d, text, f, y, fill, W):
    w = d.textlength(text, font=f)
    d.text(((W - w) / 2, y), text, font=f, fill=fill)
    return w


def banner(kind, out):
    W, H = 2560 * SS, 1440 * SS
    if kind == "reels":
        img = radial_bg(W, H, (62, 46, 37), (18, 14, 12), cy=0.5, r=0.6)
        d = ImageDraw.Draw(img)
        # film strips top and bottom of the safe area
        for y0 in (int(H * 0.30), int(H * 0.66)):
            d.rectangle((0, y0, W, y0 + 46 * SS), fill=(12, 10, 9))
            for x in range(0, W, 64 * SS):
                d.rounded_rectangle((x + 14 * SS, y0 + 12 * SS, x + 44 * SS, y0 + 34 * SS), radius=5 * SS, fill=(70, 58, 50))
        cream, accent = (243, 233, 210), (242, 107, 29)
        cy = H * 0.5 - 28 * SS
        mark_r = 120 * SS
        title = font(LORA, 150 * SS)
        tag = font(INTER_SEMI, 52 * SS)
        small = font(INTER_MED, 36 * SS)
        tw = d.textlength("Enlistra Reels", font=title)
        total = mark_r * 2 + 50 * SS + tw
        x0 = (W - total) / 2
        reel_mark(d, x0 + mark_r, cy - 40 * SS, mark_r, cream=cream, accent=accent, bg=(28, 23, 20))
        d.text((x0 + mark_r * 2 + 50 * SS, cy - 150 * SS), "Enlistra Reels", font=title, fill=cream)
        center_text(d, "Silent comedy, cut for your feed", tag, cy + 95 * SS, accent, W)
        center_text(d, "Chaplin and friends · New Shorts every day · Public domain films, restored", small, cy + 168 * SS, (190, 176, 156), W)
    else:
        img = radial_bg(W, H, (34, 104, 70), (8, 34, 23), cy=0.5, r=0.6)
        d = ImageDraw.Draw(img)
        # soft leaf shapes in the corners
        leaf = Image.new("L", (W, H), 0)
        ld = ImageDraw.Draw(leaf)
        for (lx, ly, lr, ang) in ((0.12, 0.35, 260, 30), (0.88, 0.62, 300, -25), (0.2, 0.75, 200, -40), (0.82, 0.3, 220, 50)):
            cx, cy0, r = lx * W, ly * H, lr * SS
            pts = [(cx + math.cos(math.radians(ang)) * r * math.cos(t) - math.sin(math.radians(ang)) * r * 0.45 * math.sin(t),
                    cy0 + math.sin(math.radians(ang)) * r * math.cos(t) + math.cos(math.radians(ang)) * r * 0.45 * math.sin(t))
                   for t in [i * 2 * math.pi / 60 for i in range(60)]]
            ld.polygon(pts, fill=60)
        leaf = leaf.filter(ImageFilter.GaussianBlur(6 * SS))
        img = Image.composite(Image.new("RGB", (W, H), (70, 150, 100)), img, leaf)
        d = ImageDraw.Draw(img)
        cream, amber = (244, 236, 214), (242, 164, 29)
        cy = H * 0.5 - 28 * SS
        mark_r = 115 * SS
        title = font(INTER_BLACK, 150 * SS)
        tag = font(INTER_SEMI, 54 * SS)
        small = font(INTER_MED, 36 * SS)
        tw = d.textlength("Wild Facts", font=title)
        total = mark_r * 2 + 50 * SS + tw
        x0 = (W - total) / 2
        owl_mark(d, x0 + mark_r, cy - 30 * SS, mark_r)
        d.text((x0 + mark_r * 2 + 50 * SS, cy - 140 * SS), "Wild Facts", font=title, fill=cream)
        center_text(d, "Real wildlife. One true fact.", tag, cy + 95 * SS, amber, W)
        center_text(d, "New Shorts every day · Public domain nature footage · Facts you can check", small, cy + 168 * SS, (186, 214, 196), W)
    img.resize((2560, 1440), Image.LANCZOS).save(out, quality=95)


if __name__ == "__main__":
    avatar("reels", "reels_avatar.png")
    avatar("wild", "wild_avatar.png")
    banner("reels", "reels_banner.jpg")
    banner("wild", "wild_banner.jpg")
    # previews: avatar in a circle, banner safe area
    for n in ("reels", "wild"):
        a = Image.open(f"{n}_avatar.png").resize((300, 300))
        m = Image.new("L", (300, 300), 0); ImageDraw.Draw(m).ellipse((0, 0, 299, 299), fill=255)
        b = Image.open(f"{n}_banner.jpg")
        prev = Image.new("RGB", (1280, 1080), "white")
        prev.paste(b.resize((1280, 720)), (0, 0))
        ImageDraw.Draw(prev).rectangle((253, 254, 1026, 465), outline=(255, 0, 0), width=2)  # TV/desktop safe area
        prev.paste(a, (40, 750), m)
        prev.paste(b.crop((507, 508, 2053, 931)).resize((880, 241)), (380, 780))
        prev.save(f"{n}_preview.jpg")
    print("ok")
