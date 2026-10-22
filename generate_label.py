#!/usr/bin/env python3
"""Generate a 40x30mm thermal printer label from Spoolman spool data."""

import argparse
import base64
import io
import math
import os
import re
import sys
import tomllib

import qrcode
import requests
from PIL import Image, ImageDraw, ImageFont

# Label dimensions at 203 DPI (40x30mm)
LABEL_W = 320
LABEL_H = 240
DPI = 203

# Layout coordinates
BRAND_X = 10
BRAND_Y = 8
BRAND_MAX_W = 302
BRAND_FONT_MAX = 36
BRAND_FONT_MIN = 18

BAR_X = 10
BAR_Y = 51
BAR_W = 302
BAR_H = 34
BAR_TEXT_PAD = 5

COLOR_X = 12
COLOR_Y = 90
COLOR_FONT_SIZE = 20

HEX_X = 12
HEX_Y = 112
HEX_FONT_SIZE = 14

PROP_X_LABEL = 12
PROP_X_VALUE = 80
PROP_Y_START = 140
PROP_LINE_H = 24
PROP_FONT_SIZE = 18

QR_X = 200
QR_Y = 120
QR_SIZE = 112

# AML (Labelife) output: 800x600 = 2.5x scale
AML_SCALE = 2.5
AML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<LPAPI version="1.6">
    <labelName>{label_name}</labelName>
    <paperName>Custom Label</paperName>
    <isPrintHorizontal>0</isPrintHorizontal>
    <labelHeight>30.000</labelHeight>
    <labelWidth>40.000</labelWidth>
    <validBoundsX>1</validBoundsX>
    <validBoundsY>1</validBoundsY>
    <validBoundsWidth>38</validBoundsWidth>
    <validBoundsHeight>28</validBoundsHeight>
    <paperType>0</paperType>
    <paperBackground>#ffffff</paperBackground>
    <paperForeground>#000000</paperForeground>
    <DisplaySize_mm>40.00mm * 30.00mm</DisplaySize_mm>
    <DisplaySize_in>1.575inch * 1.181inch</DisplaySize_in>
    <isRotate180>0</isRotate180>
    <isBannerMode>0</isBannerMode>
    <flagType>0</flagType>
    <isCustomSize>0</isCustomSize>
    <leftBlank>0</leftBlank>
    <rightBlank>0</rightBlank>
    <upBlank>0</upBlank>
    <downBlank>0</downBlank>
    <typeName>White-4030</typeName>
    <showDisplayMm>40.0 * 30.0 mm</showDisplayMm>
    <showDisplayIn>1.57 * 1.18 in</showDisplayIn>
    <isRibbonLabel>0</isRibbonLabel>
    <isRedBlack>0</isRedBlack>
    <isBorderRedBlack>0</isBorderRedBlack>
    <hasTail>0</hasTail>
    <tailDirection>0</tailDirection>
    <tailLength>0</tailLength>
    <contents>
        <WdPage>
            <masksToBoundsType>0</masksToBoundsType>
            <borderDisplay>0</borderDisplay>
            <isAutoHeight>0</isAutoHeight>
            <lineType>0</lineType>
            <borderWidth>1</borderWidth>
            <borderColor>#000000</borderColor>
            <lockMovement>0</lockMovement>
            <contents>
                <Image>
                    <lineType>0</lineType>
                    <content>{content}</content>
                    <height>30.000</height>
                    <width>40.000</width>
                    <y>0.000</y>
                    <x>0.000</x>
                    <orientation>0.000000</orientation>
                    <lockMovement>0</lockMovement>
                    <borderDisplay>0</borderDisplay>
                    <borderHeight>0.7055555582046509</borderHeight>
                    <borderColor>#000000</borderColor>
                    <id>2150144943</id>
                    <objectId>2153023841</objectId>
                    <imageEffect>0</imageEffect>
                    <antiColor>0</antiColor>
                    <isRatioScale>1</isRatioScale>
                    <imageType>0</imageType>
                    <isMirror>0</isMirror>
                    <isRedBlack>0</isRedBlack>
                </Image>
            </contents>
            <columnCount>0</columnCount>
        </WdPage>
    </contents>
    <squareBackingPaper>0</squareBackingPaper>
</LPAPI>
"""


def load_font(bold=False, size=16):
    """Load a font with fallback chain: Helvetica (macOS) → DejaVu Sans → Arial → default."""
    candidates = []
    if bold:
        candidates = [
            ("/System/Library/Fonts/Helvetica.ttc", 1),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", None),
            ("C:\\Windows\\Fonts\\arialbd.ttf", None),
        ]
    else:
        candidates = [
            ("/System/Library/Fonts/Helvetica.ttc", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None),
            ("C:\\Windows\\Fonts\\arial.ttf", None),
        ]

    for path, index in candidates:
        if os.path.exists(path):
            try:
                if index is not None:
                    return ImageFont.truetype(path, size, index=index)
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue

    print(f"Warning: No TrueType font found, using bitmap fallback", file=sys.stderr)
    return ImageFont.load_default()


def auto_shrink_font(draw, text, bold, max_width, start_size, min_size):
    """Return a font sized to fit text within max_width."""
    for size in range(start_size, min_size - 1, -1):
        font = load_font(bold=bold, size=size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return load_font(bold=bold, size=min_size)


def fetch_spool(base_url, spool_id):
    """Fetch spool data from Spoolman API."""
    url = f"{base_url}/api/v1/spool/{spool_id}"
    try:
        resp = requests.get(url, timeout=10)
    except requests.ConnectionError:
        print(f"Error: Cannot connect to Spoolman at {base_url}", file=sys.stderr)
        sys.exit(1)
    except requests.Timeout:
        print(f"Error: Request to Spoolman timed out", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 404:
        print(f"Error: Spool {spool_id} not found", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        print(f"Error: Spoolman returned status {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    return resp.json()


def generate_qr(content, size):
    """Generate a QR code image."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=1,
        box_size=10,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("L")
    return img.resize((size, size), Image.NEAREST)


def sanitize_filename(name):
    """Sanitize a string for use in a filename."""
    name = name.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9_\-]", "", name)


def format_range(value):
    """Format an integer_range extra field value (e.g. [230, 250] → '230-250')."""
    if isinstance(value, list) and len(value) == 2:
        return f"{value[0]}-{value[1]}"
    s = str(value).strip('"')
    m = re.match(r"\[(\d+),\s*(\d+)\]", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return s


LOGO_BASE_URL = "https://raw.githubusercontent.com/MarksMakerSpace/filament-profiles/main/logos/label"
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")


def brand_to_slug(name):
    """Convert a brand name to a logo filename slug."""
    slug = name.lower().replace(" ", "-").replace("_", "-")
    return re.sub(r"[^a-z0-9\-]", "", slug)


def fetch_logo(slug):
    """Fetch a brand logo, using local cache or downloading from GitHub."""
    os.makedirs(LOGO_DIR, exist_ok=True)
    cached = os.path.join(LOGO_DIR, f"{slug}.png")
    if os.path.exists(cached):
        try:
            return Image.open(cached)
        except (OSError, IOError):
            pass

    url = f"{LOGO_BASE_URL}/{slug}.png"
    try:
        resp = requests.get(url, timeout=10)
    except (requests.ConnectionError, requests.Timeout):
        return None
    if resp.status_code != 200:
        return None

    with open(cached, "wb") as f:
        f.write(resp.content)
    try:
        return Image.open(cached)
    except (OSError, IOError):
        return None


def load_config():
    """Load config.toml from the script directory if it exists."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def generate_label(spool_data, base_url, spool_id, scale=1.0, use_logo=False):
    """Generate the label image from spool data."""
    s = scale
    filament = spool_data.get("filament", {})
    vendor = filament.get("vendor", {}) or {}
    extra = filament.get("extra", {}) or {}

    brand = vendor.get("name") or "Unknown"
    material = filament.get("material") or "Unknown"
    name = filament.get("name")
    color_hex = filament.get("color_hex") or "000000"
    color_hex = color_hex.lstrip("#")

    nozzle_temp = extra.get("nozzle_temp")
    bed_temp = extra.get("bed_temp")
    drying_temp = extra.get("drying_temperature")
    drying_time = extra.get("drying_time")
    printing_speed = extra.get("printing_speed")

    # Compose material bar text
    bar_text = f"{material}/{name}" if name else material

    # Create image
    img = Image.new("RGB", (int(LABEL_W * s), int(LABEL_H * s)), "white")
    draw = ImageDraw.Draw(img)

    # -- Brand name / logo --
    logo_used = False
    if use_logo and brand != "Unknown":
        slug = brand_to_slug(brand)
        logo_img = fetch_logo(slug)
        if logo_img is not None:
            max_w = int(BRAND_MAX_W * s)
            max_h = int(40 * s)
            logo_img = logo_img.convert("RGBA")
            lw, lh = logo_img.size
            ratio = min(max_w / lw, max_h / lh)
            if ratio < 1:
                logo_img = logo_img.resize(
                    (int(lw * ratio), int(lh * ratio)), Image.LANCZOS,
                )
            img.paste(logo_img, (int(BRAND_X * s), int(BRAND_Y * s)), logo_img)
            logo_used = True

    if not logo_used:
        brand_font = auto_shrink_font(
            draw, brand, bold=True,
            max_width=int(BRAND_MAX_W * s),
            start_size=int(BRAND_FONT_MAX * s),
            min_size=int(BRAND_FONT_MIN * s),
        )
        draw.text((int(BRAND_X * s), int(BRAND_Y * s)), brand, fill="black", font=brand_font)

    # -- Material bar --
    draw.rectangle(
        [int(BAR_X * s), int(BAR_Y * s),
         int((BAR_X + BAR_W) * s), int((BAR_Y + BAR_H) * s)],
        fill="black",
    )
    bar_interior_w = int((BAR_W - BAR_TEXT_PAD * 2) * s)
    bar_font = auto_shrink_font(
        draw, bar_text, bold=True,
        max_width=bar_interior_w, start_size=int(22 * s), min_size=int(10 * s),
    )
    bar_text_bbox = draw.textbbox((0, 0), bar_text, font=bar_font)
    bar_text_h = bar_text_bbox[3] - bar_text_bbox[1]
    bar_h = int(BAR_H * s)
    bar_text_y = int(BAR_Y * s) + (bar_h - bar_text_h) // 2 - bar_text_bbox[1]
    draw.text(
        (int((BAR_X + BAR_TEXT_PAD) * s), bar_text_y),
        bar_text, fill="white", font=bar_font,
    )

    # -- Hex code below color name --
    hex_font = load_font(bold=False, size=int(HEX_FONT_SIZE * s))
    hex_text = f"#{color_hex.upper()}"
    draw.text((int(HEX_X * s), int(HEX_Y * s)), hex_text, fill="black", font=hex_font)

    # -- Color name --
    color_name = name or material
    color_font = load_font(bold=False, size=int(COLOR_FONT_SIZE * s))
    draw.text((int(COLOR_X * s), int(COLOR_Y * s)), color_name, fill="black", font=color_font)

    # -- QR code --
    qr_content = f"{base_url}/spool/show/{spool_id}"
    qr_img = generate_qr(qr_content, int(QR_SIZE * s))
    img.paste(qr_img, (int(QR_X * s), int(QR_Y * s)))

    # -- Properties --
    prop_font = load_font(bold=False, size=int(PROP_FONT_SIZE * s))
    props = []
    if nozzle_temp:
        props.append(("Nozzle:", f"{format_range(nozzle_temp)}\u00b0C"))
    if bed_temp:
        props.append(("Bed:", f"{format_range(bed_temp)}\u00b0C"))
    if printing_speed:
        props.append(("Speed:", f"{format_range(printing_speed)} mm/s"))
    if drying_temp and drying_time:
        props.append(("Drying:", f"{format_range(drying_temp)}\u00b0C / {drying_time}h"))
    elif drying_temp:
        props.append(("Drying:", f"{format_range(drying_temp)}\u00b0C"))
    elif drying_time:
        props.append(("Drying:", f"{drying_time}h"))

    for i, (label, value) in enumerate(props):
        y = int((PROP_Y_START + i * PROP_LINE_H) * s)
        draw.text((int(PROP_X_LABEL * s), y), label, fill="black", font=prop_font)
        draw.text((int(PROP_X_VALUE * s), y), value, fill="black", font=prop_font)

    return img, brand, material, name


# ---------------------------------------------------------------------------
# Hardware labels (screws, bolts, nuts, washers, inserts, standoffs)
# ---------------------------------------------------------------------------

# Layout below the bar. Icons sit in a row on the left, properties below them,
# and the optional QR code takes the same spot as on the filament label.
HW_ICON_Y = 92
HW_ICON_H = 50
HW_SIDE_X = 12
HW_SIDE_W = 104
HW_TOP_X = 126
HW_PROP_X_LABEL = 12
HW_PROP_X_VALUE = 92
HW_PROP_Y_START = 150
HW_PROP_LINE_H = 21
HW_PROP_FONT_SIZE = 16
HW_PROP_MAX_LINES = 4
HW_COL_RIGHT = 192        # right edge of the text column when a QR is shown

# Without a QR code the icons move to the right column and get bigger.
HW_BIG_SIDE_BOX = (194, 94, 118, 62)
HW_BIG_TOP_BOX = (222, 162, 66, 66)
HW_BIG_SINGLE_BOX = (204, 104, 104, 104)
HW_NOQR_PROP_Y_START = 96
HW_NOQR_PROP_MAX_LINES = 6

HW_TYPES = {
    "screw": "Screw",
    "bolt": "Bolt",
    "nut": "Nut",
    "washer": "Washer",
    "insert": "Heat-set insert",
    "standoff": "Standoff",
}

# Head shapes as seen from the side. Keys are canonical names.
HW_HEADS = {
    "cap": "Socket cap",
    "button": "Button head",
    "pan": "Pan head",
    "countersunk": "Countersunk",
    "cheese": "Cheese head",
    "hex": "Hex head",
    "flange": "Flange head",
}
HW_HEAD_ALIASES = {
    "socket": "cap", "shcs": "cap", "socket cap": "cap",
    "dome": "button", "bhcs": "button", "round": "button",
    "sunken": "countersunk", "csk": "countersunk", "flat": "countersunk",
    "fhcs": "countersunk", "flat head": "countersunk",
    "hexagon": "hex",
}

# Drive recesses as seen from the top.
HW_DRIVES = {
    "phillips": "Phillips",
    "pozidriv": "Pozidriv",
    "slotted": "Slotted",
    "hex": "Hex",
    "torx": "Torx",
    "square": "Square",
    "external": "External hex",
}
HW_DRIVE_ALIASES = {
    "philips": "phillips", "ph": "phillips", "cross": "phillips",
    "pz": "pozidriv", "pozi": "pozidriv",
    "flat": "slotted", "slot": "slotted", "straight": "slotted",
    "allen": "hex", "socket": "hex", "inbus": "hex", "hex socket": "hex",
    "torq": "torx", "torck": "torx", "tx": "torx", "star": "torx",
    "robertson": "square",
}

HW_VARIANTS = {
    "nut": {"hex": "Hex nut", "nyloc": "Nyloc nut", "flange": "Flange nut",
            "square": "Square nut", "cap": "Cap nut"},
    "washer": {"flat": "Washer", "spring": "Spring washer",
               "fender": "Fender washer"},
    "standoff": {"male-female": "Standoff M-F", "female-female": "Standoff F-F",
                 "male-male": "Standoff M-M"},
}
HW_VARIANT_ALIASES = {
    "locknut": "nyloc", "lock": "nyloc", "acorn": "cap", "dome": "cap",
    "split": "spring", "lock washer": "spring", "penny": "fender",
    "mf": "male-female", "ff": "female-female", "mm": "male-male",
}


def _canon(value, aliases):
    """Map a user-supplied name onto a canonical key via aliases (lowercased)."""
    if not value:
        return None
    key = str(value).strip().lower()
    key = aliases.get(key, key)
    return key


def normalize_hardware(item):
    """Fill in defaults and canonical names for a hardware item dict."""
    hw = {k: v for k, v in item.items() if v not in (None, "")}
    hw["type"] = str(hw.get("type", "screw")).strip().lower()
    if hw["type"] == "bolt" and "head" not in hw:
        hw["head"] = "hex"
    if hw["type"] in ("screw", "bolt"):
        hw["head"] = _canon(hw.get("head"), HW_HEAD_ALIASES) or "cap"
        drive = _canon(hw.get("drive"), HW_DRIVE_ALIASES)
        if drive is None:
            drive = "external" if hw["head"] in ("hex", "flange") else "hex"
        hw["drive"] = drive
    else:
        hw.pop("head", None)
        hw.pop("drive", None)
    default_variant = {"nut": "hex", "washer": "flat", "standoff": "male-female"}
    if hw["type"] in default_variant:
        hw["variant"] = (
            _canon(hw.get("variant"), HW_VARIANT_ALIASES)
            or default_variant[hw["type"]]
        )
    for key in ("length", "pitch", "diameter"):
        if isinstance(hw.get(key), str):
            hw[key] = re.sub(r"\s*mm$", "", hw[key].strip(), flags=re.IGNORECASE)
    size = str(hw.get("size", "")).strip()
    if re.fullmatch(r"\d+(\.\d+)?", size):
        size = f"M{size}"
    hw["size"] = size.upper() if size.lower().startswith("m") else size
    return hw


def _fmt_num(value):
    """Format 12.0 as '12' and 5.7 as '5.7'."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def hardware_title(hw):
    """Big text at the top: 'M3 × 12', or just 'M3' for nuts and washers."""
    size = hw.get("size") or "?"
    length = hw.get("length")
    if length and hw["type"] not in ("nut", "washer"):
        return f"{size} × {_fmt_num(length)}"
    return size


def hardware_description(hw):
    """Text for the black bar, e.g. 'Countersunk screw' or 'Nyloc nut'."""
    if hw.get("name"):
        return hw["name"]
    t = hw["type"]
    if t in ("screw", "bolt"):
        head = HW_HEADS.get(hw["head"], hw["head"].title())
        return f"{head} {t}"
    if t in HW_VARIANTS:
        variants = HW_VARIANTS[t]
        return variants.get(hw["variant"], f"{hw['variant'].title()} {t}")
    return HW_TYPES.get(t, t.title())


def hardware_properties(hw):
    """Label/value rows shown under the icons."""
    props = []
    if hw["type"] in ("screw", "bolt"):
        drive = HW_DRIVES.get(hw["drive"], hw["drive"].title())
        if hw.get("drive_size"):
            size = _fmt_num(hw["drive_size"])
            if re.fullmatch(r"[\d.]+", size):
                size += " mm"
            drive = f"{drive} {size}"
        props.append(("Drive:", drive))
    if hw.get("pitch"):
        props.append(("Pitch:", f"{_fmt_num(hw['pitch'])} mm"))
    if hw.get("diameter"):
        props.append(("OD:", f"{_fmt_num(hw['diameter'])} mm"))
    if hw.get("material"):
        props.append(("Material:", str(hw["material"])))
    if hw.get("finish"):
        props.append(("Finish:", str(hw["finish"])))
    if hw.get("standard"):
        props.append(("Std:", str(hw["standard"])))
    if hw.get("note"):
        props.append(("Note:", str(hw["note"])))
    return props


def _hexagon(cx, cy, r, rotation=0.0):
    """Points of a regular hexagon (flat top when rotation=0)."""
    return [
        (cx + r * math.cos(math.radians(rotation + 60 * i)),
         cy + r * math.sin(math.radians(rotation + 60 * i)))
        for i in range(6)
    ]


def _draw_threads(draw, x0, y0, x1, y1, lw, pitch):
    """Diagonal thread lines across a horizontal shank."""
    x = x0 + pitch * 0.6
    while x < x1 - pitch * 0.3:
        draw.line([(x, y0), (x - pitch * 0.6, y1)], fill="black", width=max(1, lw // 2 + 1))
        x += pitch


def draw_side_profile(draw, box, hw, lw):
    """Side view of a screw, bolt, insert or standoff, head on the left."""
    x, y, w, h = box
    cy = y + h / 2
    t = hw["type"]

    if t == "insert":
        # Knurled cylinder with a top lip
        bw, bh = w * 0.8, h * 0.8
        bx = x + (w - bw) / 2
        draw.rectangle([bx, cy - bh / 2, bx + bw * 0.12, cy + bh / 2], outline="black", width=lw)
        kx0, kx1 = bx + bw * 0.12, bx + bw
        kh = bh * 0.82
        draw.rectangle([kx0, cy - kh / 2, kx1, cy + kh / 2], outline="black", width=lw)
        step = kh / 2.2
        xx = kx0
        while xx < kx1 - 1:
            nx = min(xx + step, kx1)
            draw.line([(xx, cy - kh / 2), (nx, cy + kh / 2)], fill="black", width=max(1, lw // 2 + 1))
            draw.line([(xx, cy + kh / 2), (nx, cy - kh / 2)], fill="black", width=max(1, lw // 2 + 1))
            xx += step
        return

    if t == "standoff":
        variant = hw.get("variant", "male-female")
        stud_w = w * 0.18
        left_male = variant == "male-male"
        right_male = variant in ("male-female", "male-male")
        bx0 = x + (stud_w if left_male else 0)
        bx1 = x + w - (stud_w if right_male else 0)
        bh = h * 0.7
        draw.rectangle([bx0, cy - bh / 2, bx1, cy + bh / 2], outline="black", width=lw)
        draw.line([(bx0, cy - bh / 4), (bx1, cy - bh / 4)], fill="black", width=lw)
        draw.line([(bx0, cy + bh / 4), (bx1, cy + bh / 4)], fill="black", width=lw)
        sd = h * 0.3
        if right_male:
            draw.rectangle([bx1, cy - sd / 2, x + w, cy + sd / 2], outline="black", width=lw)
            _draw_threads(draw, bx1, cy - sd / 2, x + w, cy + sd / 2, lw, sd * 0.55)
        if left_male:
            draw.rectangle([x, cy - sd / 2, bx0, cy + sd / 2], outline="black", width=lw)
            _draw_threads(draw, x, cy - sd / 2, bx0, cy + sd / 2, lw, sd * 0.55)
        return

    # Screws and bolts
    head = hw.get("head", "cap")
    d = h * 0.36                     # shank diameter
    hh = h * 0.9                     # head height (perpendicular to axis)
    head_w = {
        "cap": w * 0.24, "button": w * 0.16, "pan": w * 0.18,
        "countersunk": w * 0.17, "cheese": w * 0.2, "hex": w * 0.18,
        "flange": w * 0.22,
    }.get(head, w * 0.2)
    hx0, hx1 = x, x + head_w
    top, bot = cy - hh / 2, cy + hh / 2

    if head == "countersunk":
        draw.polygon(
            [(hx0, top), (hx0 + head_w * 0.15, top), (hx1, cy - d / 2),
             (hx1, cy + d / 2), (hx0 + head_w * 0.15, bot), (hx0, bot)],
            outline="black", width=lw,
        )
    elif head == "button":
        draw.chord([hx0, top, hx0 + head_w * 2, bot], 90, 270, outline="black", width=lw)
        draw.line([(hx1, top + hh * 0.06), (hx1, bot - hh * 0.06)], fill="black", width=lw)
    elif head == "pan":
        draw.rounded_rectangle([hx0, top + hh * 0.08, hx1, bot - hh * 0.08],
                               radius=head_w * 0.45, outline="black", width=lw)
    elif head == "cap":
        draw.rounded_rectangle([hx0, top + hh * 0.1, hx1, bot - hh * 0.1],
                               radius=head_w * 0.12, outline="black", width=lw)
        # knurl hint
        for i in range(1, 4):
            ky = top + hh * 0.1 + (hh * 0.8) * i / 4
            draw.line([(hx0 + head_w * 0.2, ky), (hx1 - head_w * 0.2, ky)],
                      fill="black", width=max(1, lw // 2))
    elif head in ("hex", "flange"):
        hx_end = hx1 - (head_w * 0.25 if head == "flange" else 0)
        draw.rectangle([hx0, top + hh * 0.06, hx_end, bot - hh * 0.06], outline="black", width=lw)
        draw.line([(hx0, cy - hh * 0.2), (hx_end, cy - hh * 0.2)], fill="black", width=lw)
        draw.line([(hx0, cy + hh * 0.2), (hx_end, cy + hh * 0.2)], fill="black", width=lw)
        if head == "flange":
            draw.rectangle([hx_end, top, hx1, bot], outline="black", width=lw)
    else:  # cheese and anything unknown
        draw.rectangle([hx0, top + hh * 0.15, hx1, bot - hh * 0.15], outline="black", width=lw)

    # Shank: bolts get a plain section before the thread, screws are fully threaded
    sx0, sx1 = hx1, x + w
    draw.rectangle([sx0, cy - d / 2, sx1, cy + d / 2], outline="black", width=lw)
    thread_start = sx0 + (sx1 - sx0) * (0.4 if hw["type"] == "bolt" else 0.0)
    if thread_start > sx0:
        draw.line([(thread_start, cy - d / 2), (thread_start, cy + d / 2)], fill="black", width=lw)
    _draw_threads(draw, thread_start, cy - d / 2, sx1, cy + d / 2, lw, d * 0.55)


def draw_top_view(draw, box, hw, lw):
    """Top view: drive recess for screws/bolts, outline for nuts/washers/inserts."""
    x, y, w, h = box
    s = min(w, h)
    cx, cy = x + w / 2, y + h / 2
    r = s / 2 - lw
    t = hw["type"]

    if t == "nut":
        variant = hw.get("variant", "hex")
        if variant == "square":
            q = r * 0.82
            draw.rectangle([cx - q, cy - q, cx + q, cy + q], outline="black", width=lw)
        else:
            if variant == "flange":
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=lw)
                hr = r * 0.8
            else:
                hr = r
            draw.polygon(_hexagon(cx, cy, hr, 30), outline="black", width=lw)
        hole = r * 0.42
        draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], outline="black", width=lw)
        if variant == "nyloc":
            ring = r * 0.62
            draw.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], outline="black", width=lw)
        elif variant == "cap":
            draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill="black")
        return

    if t == "washer":
        variant = hw.get("variant", "flat")
        hole = r * (0.3 if variant == "fender" else 0.5)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=lw)
        draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], outline="black", width=lw)
        if variant == "spring":
            # Gap in the ring
            draw.polygon([(cx + hole * 0.9, cy - lw), (cx + r * 1.02, cy - lw * 3),
                          (cx + r * 1.02, cy + lw), (cx + hole * 0.9, cy + lw * 3)], fill="white")
            draw.line([(cx + hole, cy - lw * 2), (cx + r, cy - lw * 4)], fill="black", width=lw)
            draw.line([(cx + hole, cy + lw * 2), (cx + r, cy)], fill="black", width=lw)
        return

    if t in ("insert", "standoff"):
        if t == "standoff":
            draw.polygon(_hexagon(cx, cy, r, 30), outline="black", width=lw)
        else:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=lw)
        hole = r * 0.45
        draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], outline="black", width=lw)
        return

    # Screws and bolts: head outline + drive recess
    drive = hw.get("drive", "hex")
    if drive == "external":
        draw.polygon(_hexagon(cx, cy, r, 30), outline="black", width=lw)
        inner = r * 0.78
        draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], outline="black", width=max(1, lw // 2))
        return
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=lw)

    arm = r * 0.6
    thick = r * 0.2
    if drive in ("phillips", "pozidriv"):
        draw.rectangle([cx - arm, cy - thick / 2, cx + arm, cy + thick / 2], fill="black")
        draw.rectangle([cx - thick / 2, cy - arm, cx + thick / 2, cy + arm], fill="black")
        if drive == "pozidriv":
            d = arm * 0.55
            for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                draw.line([(cx + sx * thick * 0.6, cy + sy * thick * 0.6),
                           (cx + sx * d, cy + sy * d)], fill="black", width=max(1, lw // 2))
    elif drive == "slotted":
        draw.rectangle([cx - r * 0.75, cy - thick / 2, cx + r * 0.75, cy + thick / 2], fill="black")
    elif drive == "hex":
        draw.polygon(_hexagon(cx, cy, r * 0.42, 0), fill="black")
    elif drive == "torx":
        pts = []
        for i in range(120):
            a = 2 * math.pi * i / 120
            rr = r * (0.38 + 0.1 * math.cos(6 * a))
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        draw.polygon(pts, fill="black")
    elif drive == "square":
        q = r * 0.3
        draw.rectangle([cx - q, cy - q, cx + q, cy + q], fill="black")


def generate_hardware_label(hw, scale=1.0):
    """Generate a label image for a hardware item (already normalized)."""
    s = scale
    title = hardware_title(hw)
    bar_text = hardware_description(hw)
    props = hardware_properties(hw)
    qr_url = hw.get("url")
    t = hw["type"]

    img = Image.new("RGB", (int(LABEL_W * s), int(LABEL_H * s)), "white")
    draw = ImageDraw.Draw(img)

    # -- Title (size × length) --
    title_font = auto_shrink_font(
        draw, title, bold=True,
        max_width=int(BRAND_MAX_W * s),
        start_size=int(BRAND_FONT_MAX * s),
        min_size=int(BRAND_FONT_MIN * s),
    )
    draw.text((int(BRAND_X * s), int(BRAND_Y * s)), title, fill="black", font=title_font)

    # -- Description bar --
    draw.rectangle(
        [int(BAR_X * s), int(BAR_Y * s),
         int((BAR_X + BAR_W) * s), int((BAR_Y + BAR_H) * s)],
        fill="black",
    )
    bar_font = auto_shrink_font(
        draw, bar_text, bold=True,
        max_width=int((BAR_W - BAR_TEXT_PAD * 2) * s),
        start_size=int(22 * s), min_size=int(10 * s),
    )
    bbox = draw.textbbox((0, 0), bar_text, font=bar_font)
    bar_text_y = int(BAR_Y * s) + (int(BAR_H * s) - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((int((BAR_X + BAR_TEXT_PAD) * s), bar_text_y), bar_text, fill="white", font=bar_font)

    # -- Icons --
    lw = max(1, int(2 * s))
    has_side = t in ("screw", "bolt", "insert", "standoff")
    has_top = t in ("screw", "bolt", "nut", "washer")

    def sbox(box):
        return tuple(v * s for v in box)

    if qr_url:
        if has_side:
            draw_side_profile(draw, sbox((HW_SIDE_X, HW_ICON_Y, HW_SIDE_W, HW_ICON_H)), hw, lw)
        if has_top:
            top_x = HW_TOP_X if has_side else HW_SIDE_X
            draw_top_view(draw, sbox((top_x, HW_ICON_Y, HW_ICON_H, HW_ICON_H)), hw, lw)
        prop_y, max_lines, col_right = HW_PROP_Y_START, HW_PROP_MAX_LINES, HW_COL_RIGHT
        qr_img = generate_qr(qr_url, int(QR_SIZE * s))
        img.paste(qr_img, (int(QR_X * s), int(QR_Y * s)))
    else:
        if has_side and has_top:
            draw_side_profile(draw, sbox(HW_BIG_SIDE_BOX), hw, lw)
            draw_top_view(draw, sbox(HW_BIG_TOP_BOX), hw, lw)
        elif has_side:
            x, y, w, h = HW_BIG_SIDE_BOX
            draw_side_profile(draw, sbox((x, y + 30, w, h)), hw, lw)
        else:
            draw_top_view(draw, sbox(HW_BIG_SINGLE_BOX), hw, lw)
        prop_y, max_lines, col_right = HW_NOQR_PROP_Y_START, HW_NOQR_PROP_MAX_LINES, HW_COL_RIGHT

    # -- Properties --
    label_font = load_font(bold=False, size=int(HW_PROP_FONT_SIZE * s))
    value_w = int((col_right - HW_PROP_X_VALUE) * s)
    for i, (label, value) in enumerate(props[:max_lines]):
        y = int((prop_y + i * HW_PROP_LINE_H) * s)
        value_font = auto_shrink_font(
            draw, value, bold=False, max_width=value_w,
            start_size=int(HW_PROP_FONT_SIZE * s), min_size=int(11 * s),
        )
        draw.text((int(HW_PROP_X_LABEL * s), y), label, fill="black", font=label_font)
        draw.text((int(HW_PROP_X_VALUE * s), y), value, fill="black", font=value_font)

    return img, title, bar_text


# Homebox custom field names (case-insensitive; spaces, dashes and underscores
# are treated the same) mapped onto hardware keys.
HB_FIELD_MAP = {
    "type": "type",
    "size": "size", "thread": "size",
    "length": "length",
    "head": "head", "head_type": "head",
    "drive": "drive", "drive_type": "drive",
    "drive_size": "drive_size",
    "variant": "variant",
    "pitch": "pitch",
    "diameter": "diameter", "od": "diameter", "outer_diameter": "diameter",
    "material": "material", "grade": "material",
    "finish": "finish",
    "standard": "standard", "din": "standard", "iso": "standard",
    "supplier_url": "supplier_url", "source_url": "supplier_url", "url": "supplier_url",
    "label_text": "name",
    "label_note": "note",
}


def _hb_key(name):
    return re.sub(r"[\s\-]+", "_", str(name).strip().lower())


def homebox_session(base_url, token=None, username=None, password=None):
    """Return a requests session authenticated against Homebox.

    Prefers an API key (Homebox >= 0.27, Profile → API keys); falls back to
    logging in with username and password.
    """
    session = requests.Session()
    if not token:
        if not (username and password):
            print("Error: Set HOMEBOX_TOKEN (API key) or HOMEBOX_USERNAME/HOMEBOX_PASSWORD",
                  file=sys.stderr)
            sys.exit(1)
        resp = _homebox_request(session, "post", f"{base_url}/api/v1/users/login", json={
            "username": username, "password": password, "stayLoggedIn": False,
        })
        if resp.status_code != 200:
            print(f"Error: Homebox login failed (status {resp.status_code})", file=sys.stderr)
            sys.exit(1)
        token = resp.json().get("token", "")
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    session.headers["Authorization"] = token
    return session


def _homebox_request(session, method, url, **kwargs):
    try:
        return session.request(method, url, timeout=10, **kwargs)
    except requests.ConnectionError:
        print(f"Error: Cannot connect to Homebox at {url}", file=sys.stderr)
        sys.exit(1)
    except requests.Timeout:
        print("Error: Request to Homebox timed out", file=sys.stderr)
        sys.exit(1)


def _homebox_get(session, base_url, path):
    resp = _homebox_request(session, "get", f"{base_url}/api/v1{path}")
    if resp.status_code == 401:
        print("Error: Homebox rejected the credentials", file=sys.stderr)
        sys.exit(1)
    return resp


def fetch_homebox_item(session, base_url, ref):
    """Fetch an item by UUID or asset ID (e.g. '000-042' or '42')."""
    if re.fullmatch(r"[\d\-]+", ref):
        resp = _homebox_get(session, base_url, f"/assets/{ref}")
        hits = resp.json().get("items", []) if resp.status_code == 200 else []
        if not hits:
            print(f"Error: No Homebox item with asset ID {ref}", file=sys.stderr)
            sys.exit(1)
        ref = hits[0]["id"]

    # Homebox 0.27 renamed items to entities; older versions only have /items.
    for path in (f"/entities/{ref}", f"/items/{ref}"):
        resp = _homebox_get(session, base_url, path)
        if resp.status_code == 200:
            return resp.json()
    print(f"Error: Homebox item {ref} not found (status {resp.status_code})", file=sys.stderr)
    sys.exit(1)


def list_homebox_items(session, base_url, tag=None):
    """Print Homebox items, optionally only those with the given tag."""
    params = {"pageSize": 500, "orderBy": "assetId"}
    if tag:
        tag_id = None
        for path in ("/tags", "/labels"):
            resp = _homebox_get(session, base_url, path)
            if resp.status_code == 200:
                tag_id = next((t["id"] for t in resp.json()
                               if t.get("name", "").lower() == tag.lower()), None)
                break
        if tag_id is None:
            print(f"Error: Homebox tag '{tag}' not found", file=sys.stderr)
            sys.exit(1)
        params["tags"] = tag_id
        params["labels"] = tag_id

    resp = _homebox_request(session, "get", f"{base_url}/api/v1/entities", params=params)
    if resp.status_code == 404:
        resp = _homebox_request(session, "get", f"{base_url}/api/v1/items", params=params)
    if resp.status_code != 200:
        print(f"Error: Homebox returned status {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    items = resp.json().get("items", [])
    if not items:
        print("No items found.")
        return
    for item in items:
        asset = item.get("assetId") or ""
        print(f"  {asset:>8}  {item.get('name', '')}")


def homebox_to_hardware(entity):
    """Turn a Homebox item into a hardware dict, using custom fields first and
    falling back to tags and the item name (e.g. 'M3x12 socket cap screw')."""
    hw = {}
    for field in entity.get("fields") or []:
        key = HB_FIELD_MAP.get(_hb_key(field.get("name", "")))
        if not key:
            continue
        if field.get("type") == "number":
            value = field.get("numberValue")
        elif field.get("type") == "boolean":
            continue
        else:
            value = (field.get("textValue") or "").strip()
        if value not in (None, ""):
            hw[key] = value

    name = entity.get("name") or ""
    lname = name.lower()
    if "type" not in hw:
        tags = [t.get("name", "").lower() for t in
                (entity.get("tags") or entity.get("labels") or [])]
        for t in HW_TYPES:
            if t in tags or f"{t}s" in tags or re.search(rf"\b{t}s?\b", lname):
                hw["type"] = t
                break
    if "size" not in hw:
        m = re.search(r"\bM(\d+(?:\.\d+)?)", name, re.IGNORECASE)
        if m:
            hw["size"] = f"M{m.group(1)}"
    if "length" not in hw:
        m = re.search(r"\bM\d+(?:\.\d+)?\s*[x×]\s*(\d+(?:\.\d+)?)", name, re.IGNORECASE)
        if m:
            hw["length"] = m.group(1)
    if "head" not in hw:
        for alias in [*HW_HEADS, *HW_HEAD_ALIASES]:
            if re.search(rf"\b{alias}\b", lname):
                hw["head"] = alias
                break

    hw["id"] = entity.get("assetId") or entity.get("id")
    hw["homebox_id"] = entity.get("id")
    hw["supplier"] = entity.get("purchaseFrom") or None
    return hw


def hardware_filename(hw, title, bar_text):
    """Build the default output filename for a hardware label."""
    if hw.get("id"):
        return f"hw_{sanitize_filename(str(hw['id']))}"
    return "hw_" + sanitize_filename(f"{title.replace(chr(0xd7), 'x')}_{bar_text}")


def default_filename(spool_id, brand, material, name):
    """Build the default output filename."""
    parts = [
        "spool",
        str(spool_id),
        sanitize_filename(brand),
        sanitize_filename(material),
    ]
    if name:
        parts.append(sanitize_filename(name))
    return "_".join(parts)


def generate_aml(img, label_name):
    """Wrap a label image in Labelife AML format."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    content = base64.b64encode(buf.getvalue()).decode("ascii")
    return AML_TEMPLATE.format(label_name=label_name, content=content)


def fetch_all_spools(base_url):
    """Fetch all spools from Spoolman API."""
    url = f"{base_url}/api/v1/spool"
    try:
        resp = requests.get(url, timeout=10)
    except requests.ConnectionError:
        print(f"Error: Cannot connect to Spoolman at {base_url}", file=sys.stderr)
        sys.exit(1)
    except requests.Timeout:
        print(f"Error: Request to Spoolman timed out", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: Spoolman returned status {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    return resp.json()


def list_spools(base_url):
    """List all spools from Spoolman."""
    spools = fetch_all_spools(base_url)
    if not spools:
        print("No spools found.")
        return

    for spool in spools:
        spool_id = spool.get("id")
        filament = spool.get("filament", {})
        vendor = filament.get("vendor", {}) or {}
        brand = vendor.get("name") or "Unknown"
        material = filament.get("material") or "Unknown"
        name = filament.get("name") or ""
        color_hex = filament.get("color_hex") or ""
        remaining = spool.get("remaining_weight")

        label = f"{brand} {material}"
        if name:
            label += f"/{name}"
        if color_hex:
            label += f" (#{color_hex.lstrip('#').upper()})"
        if remaining is not None:
            label += f" - {remaining:.0f}g remaining"

        print(f"  {spool_id:>4}  {label}")


def main():
    config = load_config()
    default_url = (
        os.environ.get("SPOOLMAN_URL")
        or config.get("spoolman_url")
        or "http://localhost:7912"
    )
    default_aml = config.get("output_format", "png").lower() == "aml"
    output_dir = config.get("output_dir")

    parser = argparse.ArgumentParser(
        description="Generate a thermal printer label from Spoolman spool data.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List all spools from Spoolman")
    list_parser.add_argument(
        "--url", "-u",
        default=default_url,
        help="Spoolman base URL (default: SPOOLMAN_URL env or http://localhost:7912)",
    )

    # generate subcommand
    gen_parser = subparsers.add_parser("generate", help="Generate a label for a spool")
    gen_parser.add_argument("spool_id", type=int, help="Spoolman spool ID")
    gen_parser.add_argument(
        "--url", "-u",
        default=default_url,
        help="Spoolman base URL (default: SPOOLMAN_URL env or http://localhost:7912)",
    )
    gen_parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output filename (default: auto-generated from spool data)",
    )
    gen_parser.add_argument(
        "--aml",
        action="store_true",
        default=default_aml,
        help="Output in Labelife AML format instead of PNG",
    )
    gen_parser.add_argument(
        "--png",
        action="store_true",
        default=False,
        help="Output in PNG format (overrides --aml and config)",
    )
    gen_parser.add_argument(
        "--logo",
        action="store_true",
        default=config.get("use_logo", False),
        help="Use manufacturer logo instead of text brand name",
    )

    # hardware subcommand
    hw_parser = subparsers.add_parser(
        "hardware", aliases=["hw"],
        help="Generate a label for hardware (screws, nuts, ...) from Homebox or flags",
    )
    hw_parser.add_argument(
        "item", nargs="?",
        help="Homebox item: asset ID (e.g. 000-042) or UUID. Omit to use flags only.",
    )
    hw_parser.add_argument("--list", action="store_true", help="List Homebox items and exit")
    hw_parser.add_argument(
        "--tag", default=config.get("homebox_tag"),
        help="Only list Homebox items with this tag (default: homebox_tag in config)",
    )
    hw_parser.add_argument(
        "--homebox-url",
        default=os.environ.get("HOMEBOX_URL") or config.get("homebox_url") or "http://localhost:7745",
        help="Homebox base URL (default: HOMEBOX_URL env, config, or http://localhost:7745)",
    )
    hw_parser.add_argument(
        "--qr", choices=["homebox", "supplier", "none"],
        default=config.get("hardware_qr", "homebox"),
        help="What the QR code links to (default: homebox item page)",
    )
    hw_parser.add_argument("--qr-url", help="Encode this URL in the QR code instead")
    for flag, help_text in (
        ("--type", "screw, bolt, nut, washer, insert, standoff"),
        ("--size", "Thread size, e.g. M3"),
        ("--length", "Length in mm"),
        ("--head", "cap, button, pan, countersunk, cheese, hex, flange"),
        ("--drive", "hex, torx, phillips, pozidriv, slotted, square"),
        ("--drive-size", "e.g. 2.5 or T20"),
        ("--variant", "Nut/washer/standoff variant, e.g. nyloc, spring, male-female"),
        ("--pitch", "Thread pitch in mm (for fine threads)"),
        ("--diameter", "Outer diameter in mm (washers, inserts)"),
        ("--material", "e.g. A2 stainless, 12.9 steel"),
        ("--finish", "e.g. black oxide, zinc"),
        ("--standard", "e.g. DIN 912"),
        ("--name", "Override the text in the black bar"),
        ("--note", "Short extra line on the label"),
    ):
        hw_parser.add_argument(flag, help=help_text)
    hw_parser.add_argument("--output", "-o", default=None, help="Output filename")
    hw_parser.add_argument("--aml", action="store_true", default=default_aml,
                           help="Output in Labelife AML format instead of PNG")
    hw_parser.add_argument("--png", action="store_true", default=False,
                           help="Output in PNG format (overrides --aml and config)")

    # Show help when no arguments provided
    if len(sys.argv) < 2:
        parser.print_help()
        sys.exit(0)

    # Support bare spool ID for backward compatibility
    # (e.g. `generate_label.py 8` instead of `generate_label.py generate 8`)
    if sys.argv[1].isdigit():
        sys.argv.insert(1, "generate")

    args = parser.parse_args()

    if args.command == "list":
        base_url = args.url.rstrip("/")
        list_spools(base_url)
        return

    if args.command in ("hardware", "hw"):
        run_hardware(args, output_dir)
        return

    base_url = args.url.rstrip("/")
    spool_data = fetch_spool(base_url, args.spool_id)

    img, brand, material, name = generate_label(
        spool_data, base_url, args.spool_id,
        scale=AML_SCALE, use_logo=args.logo,
    )
    save_label(
        img, args.output or default_filename(args.spool_id, brand, material, name),
        explicit=bool(args.output), output_dir=output_dir, use_aml=args.aml and not args.png,
    )


def run_hardware(args, output_dir):
    """Handle the `hardware` subcommand."""
    hb_url = args.homebox_url.rstrip("/")
    hw = {}
    if args.item or args.list:
        session = homebox_session(
            hb_url,
            token=os.environ.get("HOMEBOX_TOKEN"),
            username=os.environ.get("HOMEBOX_USERNAME"),
            password=os.environ.get("HOMEBOX_PASSWORD"),
        )
        if args.list:
            list_homebox_items(session, hb_url, args.tag)
            return
        hw = homebox_to_hardware(fetch_homebox_item(session, hb_url, args.item))

    # Flags override whatever Homebox returned
    for key in ("type", "size", "length", "head", "drive", "drive_size", "variant",
                "pitch", "diameter", "material", "finish", "standard", "name", "note"):
        value = getattr(args, key)
        if value is not None:
            hw[key] = value
    if not hw.get("size"):
        print("Error: No size found; set a Size field in Homebox or pass --size", file=sys.stderr)
        sys.exit(1)

    if args.qr_url:
        hw["url"] = args.qr_url
    elif args.qr == "homebox" and hw.get("homebox_id"):
        hw["url"] = f"{hb_url}/item/{hw['homebox_id']}"
    elif args.qr == "supplier" and hw.get("supplier_url"):
        hw["url"] = hw["supplier_url"]

    hw = normalize_hardware(hw)
    img, title, bar_text = generate_hardware_label(hw, scale=AML_SCALE)
    save_label(
        img, args.output or hardware_filename(hw, title, bar_text),
        explicit=bool(args.output), output_dir=output_dir, use_aml=args.aml and not args.png,
    )


def save_label(img, basename, explicit, output_dir, use_aml):
    """Write a label image as PNG or AML."""
    img = img.convert("RGBA")
    # Prepend output_dir when set and no explicit --output path given
    if output_dir and not explicit:
        os.makedirs(output_dir, exist_ok=True)
        basename = os.path.join(output_dir, basename)

    if use_aml:
        output = basename if basename.endswith(".aml") else basename + ".aml"
        label_name = os.path.splitext(os.path.basename(output))[0]
        aml_content = generate_aml(img, label_name)
        with open(output, "w", encoding="utf-8") as f:
            f.write(aml_content)
        print(f"AML label saved to {output}")
    else:
        output = basename if basename.endswith(".png") else basename + ".png"
        img.save(output)
        print(f"Label saved to {output}")

if __name__ == "__main__":
    main()
