"""Generate QuantEcho's icon + every image asset the Windows/MSIX/Store build needs.

    python packaging/make_icons.py

Writes to packaging/assets/ (and quantecho/assets/icon.png for the Tk window icon).
Design: dark rounded tile, a cyan "echo" ring (Q-like) and a rising price line with a signal dot.
Drawn at 1024 px and downsampled, so every size is crisp. Requires Pillow (dev only).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "assets"
APP_ICON = ROOT.parent / "quantecho" / "assets" / "icon.png"

BG_TOP, BG_BOT = (22, 28, 38), (10, 13, 18)
CYAN, GREEN, WHITE = (88, 166, 255), (63, 185, 80), (230, 237, 243)


def master(size: int = 1024) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # vertical gradient tile with rounded corners
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        gd.line([(0, y), (s, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT, strict=True)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # echo ring (the "Q") + a fainter outer echo
    c, r = s * 0.47, s * 0.30
    d.ellipse([c - r * 1.22, c - r * 1.22, c + r * 1.22, c + r * 1.22], outline=CYAN + (70,), width=int(s * 0.022))
    d.ellipse([c - r, c - r, c + r, c + r], outline=CYAN + (255,), width=int(s * 0.055))
    # Q tail
    d.line([(c + r * 0.55, c + r * 0.55), (s * 0.84, s * 0.84)], fill=CYAN + (255,), width=int(s * 0.07))

    # rising price line inside the ring, with soft glow
    pts = [(0.25, 0.60), (0.36, 0.52), (0.45, 0.56), (0.55, 0.42), (0.63, 0.46), (0.71, 0.33)]
    pts = [(x * s, y * s) for x, y in pts]
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=GREEN + (160,), width=int(s * 0.07), joint="curve")
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.02)))
    d = ImageDraw.Draw(img)
    d.line(pts, fill=GREEN + (255,), width=int(s * 0.04), joint="curve")
    x, y = pts[-1]
    rr = s * 0.04
    d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=WHITE + (255,))
    return img


def fit(img: Image.Image, w: int, h: int, scale: float = 1.0, pad_bg: bool = False) -> Image.Image:
    """Centre the square icon on a w x h canvas at `scale` of the short side."""
    side = int(min(w, h) * scale)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0) if not pad_bg else BG_BOT + (255,))
    canvas.alpha_composite(img.resize((side, side), Image.LANCZOS), ((w - side) // 2, (h - side) // 2))
    return canvas


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    APP_ICON.parent.mkdir(parents=True, exist_ok=True)
    m = master()
    m.save(OUT / "icon-1024.png")
    m.resize((256, 256), Image.LANCZOS).save(APP_ICON)
    m.save(OUT / "quantecho.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # MSIX visual assets (base + scale-200 for HiDPI). Square44 gets unplated target sizes for the taskbar.
    for scale in (100, 200):
        k = scale / 100
        suffix = "" if scale == 100 else f".scale-{scale}"
        fit(m, int(44 * k), int(44 * k)).save(OUT / f"Square44x44Logo{suffix}.png")
        fit(m, int(150 * k), int(150 * k), 0.8).save(OUT / f"Square150x150Logo{suffix}.png")
        fit(m, int(310 * k), int(150 * k), 0.8, pad_bg=True).save(OUT / f"Wide310x150Logo{suffix}.png")
        fit(m, int(50 * k), int(50 * k)).save(OUT / f"StoreLogo{suffix}.png")
    for t in (16, 24, 32, 48, 256):
        fit(m, t, t).save(OUT / f"Square44x44Logo.targetsize-{t}_altform-unplated.png")
    # Partner Center store listing logo (1:1, 300x300 minimum)
    fit(m, 300, 300).save(OUT / "StoreListing-300.png")
    print(f"wrote {len(list(OUT.glob('*')))} files to {OUT} and {APP_ICON}")


if __name__ == "__main__":
    main()
