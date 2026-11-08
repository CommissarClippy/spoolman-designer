# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python tool that generates 40x30mm thermal printer labels for 3D printing filament spools. It fetches spool/filament data from a [Spoolman](https://github.com/Donkie/Spoolman) instance API and renders a label with brand name, material, color info, properties (nozzle/bed temp, drying info), and a QR code linking back to the spool page.

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# List all spools from Spoolman
python generate_label.py list

# Generate a label (PNG)
python generate_label.py generate <spool_id> --png

# Generate a label (Labelife AML format for M110 printer)
python generate_label.py generate <spool_id> --aml

# Use manufacturer logo instead of text brand name
python generate_label.py generate <spool_id> --logo --png

# Custom output path
python generate_label.py generate <spool_id> -o my_label
```

Bare spool IDs work for backward compatibility: `python generate_label.py 8` is equivalent to `python generate_label.py generate 8`.

The `--url` flag defaults to `SPOOLMAN_URL` env var, then `config.toml`, then `http://localhost:7912`.

## Configuration

Optional `config.toml` in project root (gitignored). Priority: CLI flags > env vars > config.toml > defaults.

```toml
spoolman_url = "http://10.0.1.9:7912"
output_format = "aml"   # "aml" or "png"
output_dir = "output"
use_logo = true         # use manufacturer logo instead of text brand name
```

## Architecture

Everything lives in `generate_label.py`. Key concepts:

- **Label rendering**: Uses Pillow to draw onto a 320x240px base image (203 DPI), always rendered at 2.5x scale (800x600). All layout is defined via pixel-coordinate constants at the top of the file.
- **AML output**: For the Labelife/Phomemo M110 printer app. The label image is rendered at 2.5x scale (800x600), base64-encoded, and embedded in an XML template (`AML_TEMPLATE`). Output files go to `output/`.
- **Spoolman API**: Fetches `/api/v1/spool/{id}` which returns spool data with nested `filament`, `vendor`, and `extra` fields. Custom extra fields on filaments:

  | Key                 | Type          | Unit |
  | ------------------- | ------------- | ---- |
  | `nozzle_temp`       | integer_range | °C   |
  | `bed_temp`          | integer_range | °C   |
  | `drying_temperature`| integer_range | °C   |
  | `drying_time`       | integer       | h    |
  | `printing_speed`    | integer_range | mm/s |
  | `bambu_filament_id` | text          | —    |
  | `bambu_setting_id`  | text          | —    |
  | `bambu_filament_type`| text         | —    |
- **Font loading**: Cross-platform fallback chain (Helvetica on macOS, DejaVu Sans on Linux, Arial on Windows).
- **Auto-shrink**: Brand name and material bar text auto-shrink to fit their bounding boxes.
- **Hardware labels**: `hardware` (alias `hw`) subcommand renders labels for screws, bolts, nuts, washers, inserts and standoffs on the same 40x30mm layout. Data comes from Homebox (`/api/v1/entities/{id}`, falling back to `/items/{id}` for pre-0.27 versions) via custom fields listed in `HB_FIELD_MAP` (see README), or from CLI flags. Icons are drawn with Pillow primitives in `draw_side_profile` / `draw_top_view`. Auth: `HOMEBOX_TOKEN` env (API key) or `HOMEBOX_USERNAME`/`HOMEBOX_PASSWORD`. Config keys: `homebox_url`, `homebox_tag`, `hardware_qr` (`homebox`/`supplier`/`none`).
- **Logo support**: `--logo` flag replaces text brand name with manufacturer logo. Logos are fetched from [filament-profiles](https://github.com/MarksMakerSpace/filament-profiles) and cached locally in `logos/` (gitignored).

## Environment

- Python 3.14 via Homebrew, virtualenv in `.venv/`
- Dependencies: Pillow, qrcode, requests
