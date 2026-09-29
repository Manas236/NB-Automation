"""
Newsband Instagram post generator.

Usage (single post):
    python make_post.py --image photo.jpg --headline "NMMC creates 170 artificial ponds for Ganesh idol immersion"

Usage (batch from CSV with columns: image,headline[,out]):
    python make_post.py --batch posts.csv

Output goes to ./output/<slug>.png unless --out is given.
"""

import argparse
import csv
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
ASSET_DIR = os.path.join(BASE_DIR, "assets")
OUT_DIR = os.path.join(BASE_DIR, "output")

# ---------------------------------------------------------------- layout ----
W, H = 1080, 1440                       # 4:5 Instagram portrait
RED = (196, 0, 0)
WHITE = (255, 255, 255)
BLACK = (20, 20, 20)
YELLOW = (255, 210, 0)
LOGO_RED = (200, 0, 0)

IMG_BOX = (20, 20, 1060, 1020)          # photo area (x0, y0, x1, y1)
IMG_RADII = (0, 120, 0, 0)              # corner radii: TL, TR, BR, BL

LOGO_BOX = (722, 1034, 1040, 1096)      # white pill for the logo
DOT_Y = 1068                            # dotted line centre y
DOT_X0, DOT_X1 = 60, 700
DOT_R, DOT_GAP = 3, 12

HEAD_X = 60
HEAD_MAX_W = 960
HEAD_TOP, HEAD_BOTTOM = 1110, 1345      # headline is vertically centred here
HEAD_MAX_SIZE = 76                      # start big, shrink until it fits...
HEAD_MIN_SIZE = 40
HEAD_MAX_LINES = 3                      # ...in at most this many lines
HEAD_LINE_SPACING = 1.2

FOOT_ICON_BOX = (62, 1355, 126, 1419)   # globe icon
FOOT_TEXT_X = 140
FOOT_TEXT_Y = 1387                      # vertical centre of footer text
FOOT_SIZE = 29
FOOTER_TEXT = "For the complete story, click the link in the bio."


def font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


# ------------------------------------------------------------ background ----
BG_PATH = os.path.join(ASSET_DIR, "BG.jpg")


def make_background():
    """assets/BG.jpg as-is (white top where the photo sits, textured red below)."""
    if not os.path.exists(BG_PATH):
        return Image.new("RGB", (W, H), RED)
    return Image.open(BG_PATH).convert("RGB").resize((W, H), Image.LANCZOS)


# ----------------------------------------------------------------- photo ----
def fit_cover(img, box_w, box_h, focus="center"):
    """Scale + crop image to fill box_w x box_h."""
    img = img.convert("RGB")
    scale = max(box_w / img.width, box_h / img.height)
    new = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    if focus == "top":
        top = 0
    elif focus == "bottom":
        top = new.height - box_h
    else:
        top = (new.height - box_h) // 2
    left = (new.width - box_w) // 2
    return new.crop((left, top, left + box_w, top + box_h))


def corner_mask(size, radii):
    """Mask with individually rounded corners (TL, TR, BR, BL)."""
    w, h = size
    mask = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(mask)
    tl, tr, br, bl = radii
    corners = (
        (tl, (tl, tl), (0, 0, tl, tl)),
        (tr, (w - tr, tr), (w - tr, 0, w, tr)),
        (br, (w - br, h - br), (w - br, h - br, w, h)),
        (bl, (bl, h - bl), (0, h - bl, bl, h)),
    )
    for r, (cx, cy), box in corners:
        if r <= 0:
            continue
        d.rectangle(box, fill=0)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return mask


def paste_photo(canvas, photo_path, focus):
    x0, y0, x1, y1 = IMG_BOX
    bw, bh = x1 - x0, y1 - y0
    photo = fit_cover(Image.open(photo_path), bw, bh, focus)
    canvas.paste(photo, (x0, y0), corner_mask((bw, bh), IMG_RADII))


# ------------------------------------------------------------------ logo ----
def draw_logo(canvas):
    d = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = LOGO_BOX
    d.rounded_rectangle(LOGO_BOX, radius=(y1 - y0) // 2, fill=WHITE)

    custom = os.path.join(ASSET_DIR, "logo.png")
    if os.path.exists(custom):
        # drop your real logo in assets/logo.png and it will be used here
        logo = Image.open(custom).convert("RGBA")
        pad = 4
        max_w, max_h = (x1 - x0) - 2 * pad, (y1 - y0) - 2 * pad
        s = min(max_w / logo.width, max_h / logo.height)
        logo = logo.resize((round(logo.width * s), round(logo.height * s)), Image.LANCZOS)
        lx = (x0 + x1 - logo.width) // 2
        ly = (y0 + y1 - logo.height) // 2
        canvas.paste(logo, (lx, ly), logo)
        return

    # fallback: typeset "newsband" with the red "b"
    f = font("PlayfairDisplay-Regular.ttf", 54)
    parts = [("news", BLACK), ("b", LOGO_RED), ("and", BLACK)]
    total = sum(d.textlength(t, font=f) for t, _ in parts)
    x = (x0 + x1 - total) / 2
    cy = (y0 + y1) / 2 - 2
    for text, color in parts:
        d.text((x, cy), text, font=f, fill=color, anchor="lm")
        x += d.textlength(text, font=f)


def draw_dots(canvas):
    d = ImageDraw.Draw(canvas)
    x = DOT_X0
    while x <= DOT_X1:
        d.ellipse((x - DOT_R, DOT_Y - DOT_R, x + DOT_R, DOT_Y + DOT_R), fill=YELLOW)
        x += DOT_GAP


# -------------------------------------------------------------- headline ----
def wrap(text, f, max_w, d):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=f) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def smart_quotes(text):
    """Straight quotes -> curly, matching the house style."""
    text = re.sub(r"(^|[\s(\[])'", r"\1‘", text)
    text = text.replace("'", "’")
    text = re.sub(r'(^|[\s(\[])"', r"\1“", text)
    return text.replace('"', "”")


def draw_headline(canvas, headline):
    headline = smart_quotes(headline.strip())
    d = ImageDraw.Draw(canvas)
    avail_h = HEAD_BOTTOM - HEAD_TOP
    size = HEAD_MAX_SIZE
    while True:
        f = font("Poppins-Bold.ttf", size)
        lines = wrap(headline, f, HEAD_MAX_W, d)
        line_h = size * HEAD_LINE_SPACING
        block_h = line_h * len(lines)
        fits = len(lines) <= HEAD_MAX_LINES and block_h <= avail_h             and all(d.textlength(ln, font=f) <= HEAD_MAX_W for ln in lines)
        if fits or size <= HEAD_MIN_SIZE:
            break
        size -= 2
    y = HEAD_TOP + (avail_h - block_h) / 2
    for line in lines:
        d.text((HEAD_X, y + line_h / 2), line, font=f, fill=WHITE, anchor="lm")
        y += line_h


# ---------------------------------------------------------------- footer ----
def draw_globe(canvas):
    x0, y0, x1, y1 = FOOT_ICON_BOX
    # draw at 4x then downsample for smooth lines
    s = 4
    w, h = (x1 - x0) * s, (y1 - y0) * s
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw = 3 * s
    pad = 3 * s
    d.ellipse((pad, pad, w - pad, h - pad), outline=WHITE, width=lw)
    cx = w / 2
    # meridian ellipse
    d.ellipse((cx - w * 0.19, pad, cx + w * 0.19, h - pad), outline=WHITE, width=lw)
    # equator + latitude lines
    d.line((pad, h / 2, w - pad, h / 2), fill=WHITE, width=lw)
    d.line((w * 0.16, h * 0.3, w * 0.84, h * 0.3), fill=WHITE, width=lw)
    d.line((w * 0.16, h * 0.7, w * 0.84, h * 0.7), fill=WHITE, width=lw)
    # small link/chain badge bottom-right, like the originals
    bx0, by0 = w * 0.58, h * 0.62
    bx1, by1 = w * 0.98, h * 1.0
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=6 * s, fill=RED)
    cy = (by0 + by1) / 2
    r = (by1 - by0) * 0.24
    d.rounded_rectangle((bx0 + 5 * s, cy - r, bx0 + 21 * s, cy + r), radius=r, outline=WHITE, width=2 * s)
    d.rounded_rectangle((bx1 - 21 * s, cy - r, bx1 - 5 * s, cy + r), radius=r, outline=WHITE, width=2 * s)
    d.line((bx0 + 13 * s, cy, bx1 - 13 * s, cy), fill=WHITE, width=2 * s)
    layer = layer.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    canvas.paste(layer, (x0, y0), layer)


GLOBE_PATH = os.path.join(ASSET_DIR, "globe.png")


def paste_globe_asset(canvas):
    """Use assets/globe.png if it exists. A flat red-background icon is keyed
    onto the texture by turning its whiteness into alpha. Returns False if absent."""
    if not os.path.exists(GLOBE_PATH):
        return False
    icon = Image.open(GLOBE_PATH).convert("RGBA")
    if icon.getchannel("A").getextrema()[0] == 255:      # no transparency at all
        r, g, b, _ = icon.split()
        from PIL import ImageChops
        alpha = ImageChops.darker(ImageChops.darker(r, g), b)  # min(R,G,B): white->255, red->0
        icon = Image.merge("RGBA", (Image.new("L", icon.size, 255),) * 3 + (alpha,))
    x0, y0, x1, y1 = FOOT_ICON_BOX
    icon = icon.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    canvas.paste(icon, (x0, y0), icon)
    return True


def draw_footer(canvas):
    if not paste_globe_asset(canvas):
        draw_globe(canvas)
    d = ImageDraw.Draw(canvas)
    f = font("Poppins-Regular.ttf", FOOT_SIZE)
    d.text((FOOT_TEXT_X, FOOT_TEXT_Y), FOOTER_TEXT, font=f, fill=WHITE, anchor="lm")
    tw = d.textlength(FOOTER_TEXT, font=f)
    uy = FOOT_TEXT_Y + FOOT_SIZE * 0.42
    d.line((FOOT_TEXT_X, uy, FOOT_TEXT_X + tw, uy), fill=WHITE, width=2)


# ------------------------------------------------------------------ main ----
def slugify(text, n=60):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "post"


def prune_dir(directory, keep=15, trigger=30):
    """Retention policy: once `directory` holds `trigger`+ files, delete the
    oldest ones down to `keep`, so at least the `keep` most recent are always
    around. Ignores dotfiles/subdirectories."""
    if not os.path.isdir(directory):
        return
    files = [os.path.join(directory, f) for f in os.listdir(directory)
             if not f.startswith(".") and os.path.isfile(os.path.join(directory, f))]
    if len(files) < trigger:
        return
    files.sort(key=os.path.getmtime)  # oldest first
    for f in files[:len(files) - keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def make_post(image_path, headline, out_path=None, focus="center"):
    canvas = make_background()
    paste_photo(canvas, image_path, focus)
    draw_logo(canvas)
    draw_dots(canvas)
    draw_headline(canvas, headline)
    draw_footer(canvas)

    if not out_path:
        os.makedirs(OUT_DIR, exist_ok=True)
        out_path = os.path.join(OUT_DIR, slugify(headline) + ".png")
        canvas.save(out_path, quality=95)
        prune_dir(OUT_DIR)
        return out_path
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    canvas.save(out_path, quality=95)
    return out_path


def main():
    p = argparse.ArgumentParser(description="Generate a Newsband-style Instagram post.")
    p.add_argument("--image", "-i", help="path to the photo")
    p.add_argument("--headline", "-t", help="headline text")
    p.add_argument("--out", "-o", help="output file (default: output/<slug>.png)")
    p.add_argument("--focus", choices=["top", "center", "bottom"], default="center",
                   help="which part of the photo to keep when cropping")
    p.add_argument("--batch", "-b", help="CSV file with columns: image,headline[,out][,focus]")
    args = p.parse_args()

    jobs = []
    if args.batch:
        with open(args.batch, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if not row.get("image") or not row.get("headline"):
                    continue
                jobs.append((row["image"], row["headline"], row.get("out") or None,
                             row.get("focus") or "center"))
    elif args.image and args.headline:
        jobs.append((args.image, args.headline, args.out, args.focus))
    else:
        p.error("give --image and --headline, or --batch file.csv")

    for image, headline, out, focus in jobs:
        try:
            path = make_post(image, headline, out, focus)
            print(f"OK   {path}")
        except Exception as e:  # keep going on batch errors
            print(f"FAIL {image!r}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
