"""Pristine, asset-free device-frame compositor.

Takes a raw device screenshot and renders it inside a clean phone mockup
(rounded screen, thin bezel, Dynamic Island / notch / hole-punch, side
buttons, soft drop shadow) on a configurable background.

No external mockup PNGs required -- the frame is drawn programmatically and
supersampled for crisp anti-aliased edges, so it stays sharp at any width and
matches whatever screenshot resolution the cloud device returns.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

# Supersample factor: render everything at SS x target size, then downscale
# with LANCZOS for clean rounded corners and edges.
SS = 2

# --- Geometry, all expressed as fractions of the (supersampled) screen width ---
BEZEL_FRAC = 0.034           # thickness of the bezel ring around the screen
SCREEN_RADIUS_FRAC = 0.135   # corner radius of the screen glass
MARGIN_FRAC = 0.12           # padding (per side) between device and canvas edge
EDGE_WIDTH_FRAC = 0.004      # rim-highlight outline thickness

# Notch / island / camera, as fractions of screen width (w) unless noted.
ISLAND_W_FRAC = 0.30
ISLAND_H_FRAC = 0.083
ISLAND_TOP_FRAC = 0.022      # offset from top of screen, as fraction of screen *height*
NOTCH_W_FRAC = 0.44
NOTCH_H_FRAC = 0.058
HOLE_R_FRAC = 0.020          # android hole-punch radius

# Side buttons, as fractions of device height.
BTN_POWER_TOP_FRAC = 0.26
BTN_POWER_LEN_FRAC = 0.12
BTN_VOL_TOP_FRAC = 0.22
BTN_VOL_LEN_FRAC = 0.075
BTN_VOL_GAP_FRAC = 0.02

# Shadow.
SHADOW_BLUR_FRAC = 0.035     # of device width
SHADOW_OFFSET_FRAC = 0.020   # of device height
SHADOW_ALPHA = 78

COLOR_SCHEMES = {
    # bezel = body color, edge = thin rim highlight, button = side-button color,
    # island = notch/island/hole fill.
    "black": {
        "bezel": (12, 12, 14, 255),
        "edge": (52, 52, 58, 255),
        "button": (26, 26, 30, 255),
        "island": (3, 3, 4, 255),
    },
    "graphite": {
        "bezel": (38, 39, 43, 255),
        "edge": (90, 91, 98, 255),
        "button": (28, 29, 33, 255),
        "island": (8, 8, 10, 255),
    },
    "silver": {
        "bezel": (216, 217, 221, 255),
        "edge": (255, 255, 255, 255),
        "button": (186, 187, 193, 255),
        "island": (18, 18, 20, 255),
    },
    "white": {
        "bezel": (244, 244, 247, 255),
        "edge": (255, 255, 255, 255),
        "button": (208, 208, 214, 255),
        "island": (18, 18, 20, 255),
    },
}

BACKGROUND_PRESETS = {
    "transparent": None,
    "light": ((250, 250, 252), (231, 231, 238)),
    "auto": ((250, 250, 252), (231, 231, 238)),
    "dark": ((20, 20, 26), (9, 9, 13)),
    "midnight": ((28, 24, 51), (14, 12, 26)),  # Revyl indigo-ish
    "white": ((255, 255, 255), (255, 255, 255)),
}


def _hex_to_rgb(value: str):
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _rounded_mask(size, radius):
    """A white-on-black 'L' mask with rounded corners (for putalpha)."""
    w, h = size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    return mask


def _vertical_gradient(size, top_rgb, bottom_rgb):
    w, h = size
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        grad.putpixel(
            (0, y),
            tuple(round(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * t) for i in range(3)),
        )
    return grad.resize((w, h)).convert("RGBA")


def _make_background(size, background):
    """Return an RGBA canvas for the given background spec.

    background may be:
      - "transparent"
      - a named preset ("light"/"dark"/"midnight"/...)
      - a "#rrggbb" solid color
      - a dict {"gradient": ["#top", "#bottom"]}
    """
    w, h = size
    if isinstance(background, dict) and "gradient" in background:
        top, bottom = background["gradient"][0], background["gradient"][1]
        return _vertical_gradient(size, _hex_to_rgb(top), _hex_to_rgb(bottom))

    if isinstance(background, str) and background.startswith("#"):
        rgb = _hex_to_rgb(background)
        return Image.new("RGBA", size, rgb + (255,))

    preset = BACKGROUND_PRESETS.get(background, BACKGROUND_PRESETS["light"])
    if preset is None:  # transparent
        return Image.new("RGBA", size, (0, 0, 0, 0))
    return _vertical_gradient(size, preset[0], preset[1])


def build_device_frame(
    screenshot,
    *,
    style="iphone-pro",
    color="black",
    background="light",
    shadow=True,
    screen_width=440,
    buttons=True,
):
    """Composite `screenshot` (a PIL.Image) inside a clean device frame.

    Returns an RGBA PIL.Image. Output size is deterministic for a given
    screenshot resolution + options, so every frame in a flow lines up.
    """
    scheme = COLOR_SCHEMES.get(color, COLOR_SCHEMES["black"])

    # --- scale the screenshot to the (supersampled) target screen width ---
    sw = int(round(screen_width * SS))
    src = screenshot.convert("RGBA")
    ratio = sw / src.width
    sh = int(round(src.height * ratio))
    screen = src.resize((sw, sh), Image.LANCZOS)

    bezel = max(2, int(round(BEZEL_FRAC * sw)))
    screen_radius = int(round(SCREEN_RADIUS_FRAC * sw))
    outer_radius = screen_radius + bezel

    # Round the screen's own corners so the glass meets the bezel cleanly.
    screen.putalpha(_rounded_mask((sw, sh), screen_radius))

    dev_w = sw + 2 * bezel
    dev_h = sh + 2 * bezel

    # --- device body ---
    body = Image.new("RGBA", (dev_w, dev_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.rounded_rectangle(
        (0, 0, dev_w - 1, dev_h - 1), radius=outer_radius, fill=scheme["bezel"]
    )
    # Subtle rim highlight for a metallic edge.
    edge_w = max(1, int(round(EDGE_WIDTH_FRAC * sw)))
    bd.rounded_rectangle(
        (edge_w // 2, edge_w // 2, dev_w - 1 - edge_w // 2, dev_h - 1 - edge_w // 2),
        radius=outer_radius - edge_w // 2,
        outline=scheme["edge"],
        width=edge_w,
    )

    # Place the screen.
    body.alpha_composite(screen, (bezel, bezel))

    # --- camera cutouts (drawn on top of the screen) ---
    cx = dev_w // 2
    if style in ("iphone-pro", "iphone"):
        if style == "iphone-pro":
            iw = int(round(ISLAND_W_FRAC * sw))
            ih = int(round(ISLAND_H_FRAC * sw))
        else:  # legacy notch
            iw = int(round(NOTCH_W_FRAC * sw))
            ih = int(round(NOTCH_H_FRAC * sw))
        top = bezel + int(round(ISLAND_TOP_FRAC * sh))
        bd.rounded_rectangle(
            (cx - iw // 2, top, cx + iw // 2, top + ih),
            radius=ih // 2,
            fill=scheme["island"],
        )
    elif style == "android":
        r = int(round(HOLE_R_FRAC * sw))
        top = bezel + int(round(ISLAND_TOP_FRAC * sh)) + r
        bd.ellipse((cx - r, top - r, cx + r, top + r), fill=scheme["island"])

    # --- side buttons ---
    if buttons:
        btn_w = max(2, int(round(bezel * 0.5)))
        # power (right)
        p_top = int(round(BTN_POWER_TOP_FRAC * dev_h))
        p_len = int(round(BTN_POWER_LEN_FRAC * dev_h))
        bd.rounded_rectangle(
            (dev_w - 1, p_top, dev_w - 1 + btn_w, p_top + p_len),
            radius=btn_w,
            fill=scheme["button"],
        )
        # volume up/down + ringer (left)
        v_top = int(round(BTN_VOL_TOP_FRAC * dev_h))
        v_len = int(round(BTN_VOL_LEN_FRAC * dev_h))
        gap = int(round(BTN_VOL_GAP_FRAC * dev_h))
        for i in range(2):
            y0 = v_top + i * (v_len + gap)
            bd.rounded_rectangle(
                (-btn_w, y0, 0, y0 + v_len), radius=btn_w, fill=scheme["button"]
            )

    # --- canvas with background + shadow ---
    margin = int(round(MARGIN_FRAC * dev_w))
    canvas_w = dev_w + 2 * margin
    canvas_h = dev_h + 2 * margin
    canvas = _make_background((canvas_w, canvas_h), background)

    if shadow:
        sh_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh_layer)
        offset = int(round(SHADOW_OFFSET_FRAC * dev_h))
        sd.rounded_rectangle(
            (margin, margin + offset, margin + dev_w, margin + dev_h + offset),
            radius=outer_radius,
            fill=(0, 0, 0, SHADOW_ALPHA),
        )
        blur = max(1, int(round(SHADOW_BLUR_FRAC * dev_w)))
        sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(blur))
        canvas.alpha_composite(sh_layer)

    canvas.alpha_composite(body, (margin, margin))

    # Downscale from supersampled space.
    final = canvas.resize((canvas_w // SS, canvas_h // SS), Image.LANCZOS)
    return final


def flatten(image, matte=(255, 255, 255)):
    """Flatten an RGBA frame onto a solid matte for opaque encoders (gif/mp4)."""
    if image.mode != "RGBA":
        return image.convert("RGB")
    bg = Image.new("RGB", image.size, matte)
    bg.paste(image, mask=image.split()[3])
    return bg
