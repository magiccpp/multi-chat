"""Draw the MultiChat icon (three overlapping chat dots, as in the app) into a multi-size .ico. Needs Pillow."""

import sys

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, color in enumerate(("#2563eb", "#d97706", "#059669")):
        x, y = 16 + i * 64, 72 + (i % 2) * 32
        d.ellipse((x, y, x + 104, y + 104), fill=color)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "multichat.ico"
    draw(256).save(out, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", out)
