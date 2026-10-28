"""Tray icon: a small pink Furby whose antenna bubble and eyes show connection state."""

from PIL import Image, ImageDraw

# Drawn at a large size, then downscaled for smooth edges.
_CANVAS = 256

_PINK = (242, 128, 186, 255)
_PINK_DARK = (196, 72, 138, 255)
_PINK_LIGHT = (255, 196, 225, 255)
_BEAK = (255, 170, 40, 255)
_EYE_WHITE = (255, 255, 255, 255)
_PUPIL = (40, 60, 120, 255)
_OUTLINE = (90, 30, 70, 255)
_BUBBLE_ON = (60, 220, 90, 255)
_BUBBLE_OFF = (150, 150, 150, 255)


def furby_icon(connected: bool, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (_CANVAS, _CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Ears behind the head
    d.polygon([(70, 120), (8, 50), (112, 92)], fill=_PINK_DARK, outline=_OUTLINE, width=6)
    d.polygon([(186, 120), (248, 50), (144, 92)], fill=_PINK_DARK, outline=_OUTLINE, width=6)

    # Antenna stalk + bubble; bubble is oversized so it stays visible at 16px
    d.rectangle((120, 60, 136, 90), fill=_PINK_DARK)
    bubble = _BUBBLE_ON if connected else _BUBBLE_OFF
    d.ellipse((92, 4, 164, 76), fill=bubble, outline=_OUTLINE, width=6)

    # Head
    d.ellipse((36, 78, 220, 252), fill=_PINK, outline=_OUTLINE, width=6)
    d.ellipse((84, 176, 172, 244), fill=_PINK_LIGHT)

    # Eyes
    for x0 in (64, 132):
        box = (x0, 112, x0 + 60, 178)
        if connected:
            d.ellipse(box, fill=_EYE_WHITE, outline=_OUTLINE, width=5)
            d.ellipse((x0 + 17, 128, x0 + 45, 160), fill=_PUPIL)
        else:
            d.arc(box, start=20, end=160, fill=_OUTLINE, width=12)

    # Beak
    d.polygon([(110, 180), (146, 180), (128, 206)], fill=_BEAK, outline=_OUTLINE, width=4)

    return img.resize((size, size), Image.Resampling.LANCZOS)
