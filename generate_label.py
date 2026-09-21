#!/usr/bin/env python3
"""Generate a 40x30mm thermal printer label from Spoolman spool data."""

import argparse
import base64
import io
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
        lo, hi = value
    else:
        s = str(value).strip('"')
        m = re.match(r"\[(\d+),\s*(\d+)\]", s)
        if not m:
            return s
        lo, hi = m.groups()
    return str(lo) if str(lo) == str(hi) else f"{lo}-{hi}"


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
    bar_text = material

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
            # Scale up or down so the logo fills the brand area
            ratio = min(max_w / lw, max_h / lh)
            logo_img = logo_img.resize(
                (max(1, int(lw * ratio)), max(1, int(lh * ratio))), Image.LANCZOS,
            )
            logo_y = int(BRAND_Y * s) + (max_h - logo_img.height) // 2
            img.paste(logo_img, (int(BRAND_X * s), logo_y), logo_img)
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
    color_font = auto_shrink_font(
        draw, color_name, bold=False,
        max_width=int((LABEL_W - COLOR_X * 2) * s),
        start_size=int(COLOR_FONT_SIZE * s), min_size=int(12 * s),
    )
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
    if drying_temp:
        props.append(("Drying:", f"{format_range(drying_temp)}\u00b0C"))
    if drying_time:
        props.append(("Time:", f"{format_range(drying_time)}h"))

    for i, (label, value) in enumerate(props):
        y = int((PROP_Y_START + i * PROP_LINE_H) * s)
        draw.text((int(PROP_X_LABEL * s), y), label, fill="black", font=prop_font)
        draw.text((int(PROP_X_VALUE * s), y), value, fill="black", font=prop_font)

    return img, brand, material, name


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

    base_url = args.url.rstrip("/")
    spool_data = fetch_spool(base_url, args.spool_id)

    img, brand, material, name = generate_label(
        spool_data, base_url, args.spool_id,
        scale=AML_SCALE, use_logo=args.logo,
    )
    img = img.convert("RGBA")
    basename = args.output or default_filename(args.spool_id, brand, material, name)

    # Prepend output_dir when set and no explicit --output path given
    if output_dir and not args.output:
        os.makedirs(output_dir, exist_ok=True)
        basename = os.path.join(output_dir, basename)

    use_aml = args.aml and not args.png
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
