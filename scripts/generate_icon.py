"""Gera desktop/resources/icon.ico (e icon.png) a partir da geometria do
icone "Pulso" (mesma paleta de desktop/theme.py). Roda uma vez, sob demanda -
nao faz parte do app em si.
"""
import os

from PIL import Image, ImageDraw

SCALE = 8
SIZE = 256 * SCALE

BG = (0x12, 0x12, 0x12, 255)
BORDER = (0x2a, 0x2a, 0x2a, 255)
GREEN = (0x00, 0xff, 0x00, 255)
AMBER = (0xff, 0xaa, 0x00, 255)

def s(v: float) -> float:
    return v * SCALE


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [s(10), s(10), s(246), s(246)], radius=s(52),
        fill=BG, outline=BORDER, width=round(s(4)),
    )

    points = [
        (40, 150), (84, 150), (96, 128), (108, 178), (124, 58),
        (140, 196), (156, 150), (168, 150), (188, 132), (204, 150), (216, 150),
    ]
    points = [(s(x), s(y)) for x, y in points]
    stroke_w = round(s(12))
    draw.line(points, fill=GREEN, width=stroke_w, joint="curve")
    r = stroke_w / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=GREEN)

    cx, cy, cr = s(124), s(58), s(9)
    ow = round(s(3))
    draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=AMBER, outline=BG, width=ow)

    out_dir = os.path.join(os.path.dirname(__file__), "..", "desktop", "resources")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    base = img.resize((1024, 1024), Image.LANCZOS)
    base.save(os.path.join(out_dir, "icon.png"))

    sizes = [16, 24, 32, 48, 64, 128, 256]
    base.save(
        os.path.join(out_dir, "icon.ico"),
        sizes=[(sz, sz) for sz in sizes],
    )
    print("Gerado:", out_dir)


if __name__ == "__main__":
    main()
