from furbyhack.icons import furby_icon


def test_icon_size_and_mode() -> None:
    img = furby_icon(True, 32)
    assert img.size == (32, 32)
    assert img.mode == "RGBA"


def test_bubble_color_reflects_state() -> None:
    # Center of the antenna bubble at 64px
    on = furby_icon(True, 64).getpixel((32, 10))
    off = furby_icon(False, 64).getpixel((32, 10))
    assert isinstance(on, tuple) and isinstance(off, tuple)
    r, g, b, _ = on
    assert g > r and g > b  # green
    r, g, b, _ = off
    assert abs(r - g) < 10 and abs(g - b) < 10  # gray
