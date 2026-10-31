#!/usr/bin/env python3
"""Regenerate brand-derived assets from the master image.

Master:  docs/assets/brand-org.png  (square source artwork)

Derived assets (sizes/formats follow the existing committed files):
  - frontend/public/brand.png   800x800 PNG (palette, optimized)
        used by README.md and the in-app About dialog (src="/brand.png")
  - frontend/public/favicon.ico multi-resolution ICO
        sizes: 16, 24, 32, 48, 64, 128, 256
        referenced by frontend/index.html <link rel="icon">

Run from the repo root:
    python3 docs/assets/generate_brand_assets.py
"""

from pathlib import Path

from PIL import Image

# Resolve repo root relative to this script (docs/assets/ -> repo root).
ROOT = Path(__file__).resolve().parents[2]

MASTER = ROOT / "docs" / "assets" / "brand-org.png"
BRAND_PNG = ROOT / "frontend" / "public" / "brand.png"
FAVICON_ICO = ROOT / "frontend" / "public" / "favicon.ico"

BRAND_SIZE = (800, 800)
FAVICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    src = Image.open(MASTER)

    # brand.png: 800x800, palette PNG to match the existing committed format.
    brand = src.convert("RGB").resize(BRAND_SIZE, Image.LANCZOS)
    brand_p = brand.convert("P", palette=Image.ADAPTIVE, colors=256)
    brand_p.save(BRAND_PNG, format="PNG", optimize=True)
    print(f"wrote {BRAND_PNG.relative_to(ROOT)} {BRAND_SIZE} (palette)")

    # favicon.ico: single multi-resolution ICO holding every size.
    favicon = src.convert("RGBA").resize(
        (max(FAVICON_SIZES), max(FAVICON_SIZES)), Image.LANCZOS
    )
    favicon.save(
        FAVICON_ICO,
        format="ICO",
        sizes=[(s, s) for s in FAVICON_SIZES],
    )
    print(
        f"wrote {FAVICON_ICO.relative_to(ROOT)} "
        f"sizes={FAVICON_SIZES}"
    )


if __name__ == "__main__":
    main()
