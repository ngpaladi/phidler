"""Generate the Phidler application icon (packaging/appimage/phidler/phidler.png).

Draws an add–drop ring resonator — a ring between two bus waveguides — in the
app's own orange on the dark canvas colour, so the launcher icon echoes what the
tool actually renders. Committed as a PNG so the AppImage build needs no drawing
deps; re-run this to regenerate it:

    python packaging/appimage/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BG = (26, 29, 35, 255)          # dark charcoal, matching the canvas background
WG = (210, 105, 74, 255)        # the waveguide orange the app draws components in
OUT = Path(__file__).parent / "phidler" / "phidler.png"


def main() -> None:
    # Supersample 4× then downscale for smooth anti-aliased curves.
    s = 4
    img = Image.new("RGBA", (SIZE * s, SIZE * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    m = 10 * s  # outer margin
    d.rounded_rectangle([m, m, SIZE * s - m, SIZE * s - m], radius=44 * s, fill=BG)

    cx, cy = SIZE * s // 2, SIZE * s // 2
    ring_r = 58 * s
    wg_w = 15 * s
    gap = 7 * s

    # Two horizontal bus waveguides, tangent above and below the ring.
    bus_x0, bus_x1 = 40 * s, SIZE * s - 40 * s
    for yc in (cy - ring_r - gap - wg_w // 2, cy + ring_r + gap + wg_w // 2):
        d.rounded_rectangle(
            [bus_x0, yc - wg_w // 2, bus_x1, yc + wg_w // 2], radius=wg_w // 2, fill=WG
        )

    # The ring itself (an annulus of waveguide width).
    d.ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        outline=WG, width=wg_w,
    )

    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"wrote {OUT} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
