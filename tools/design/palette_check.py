"""Chart palette validator.

A validated palette is only validated against the surface it was checked on,
so this reads the live values out of ``web/assets/css/viz.css`` and
``tokens.css`` rather than keeping its own copy. A checker that duplicates the
thing it checks is measuring a palette nobody ships.

Two questions per theme:

  * Does each mark clear 3:1 against its own surface? That is the WCAG 1.4.11
    floor for a graphical object, and a chart line is a graphical object.
  * Is every pair of marks separable? CIE76 dE with a floor of 15, evaluated
    under normal vision and under simulated protanopia and deuteranopia --
    because "these look different to me" and "these look different to the
    roughly 8% of men with a colour vision deficiency" are different
    questions, and only one of them is checkable.

Run it after touching any value in viz.css or any surface token in
tokens.css:

    py tools/design/palette_check.py

Exits non-zero on a failure, so it can be wired into CI the moment the design
stops moving.
"""

from __future__ import annotations

import itertools
import math
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VIZ = REPO_ROOT / "web" / "assets" / "css" / "viz.css"
TOKENS = REPO_ROOT / "web" / "assets" / "css" / "tokens.css"

CONTRAST_FLOOR = 3.0
DE_FLOOR = 15.0

MARKS = ("series", "marker", "critical")


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


def hexrgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    r, g, b = (_lin(c) for c in hexrgb(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def to_lab(colour: str) -> tuple[float, float, float]:
    r, g, b = (_lin(c) for c in hexrgb(colour))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: str, b: str) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(to_lab(a), to_lab(b), strict=True)))


def simulate(colour: str, kind: str) -> str:
    """Brettel-style approximation of protanopia / deuteranopia."""
    r, g, b = (_lin(c) for c in hexrgb(colour))
    long_ = 17.8824 * r + 43.5161 * g + 4.11935 * b
    med = 3.45565 * r + 27.1554 * g + 3.86714 * b
    short = 0.0299566 * r + 0.184309 * g + 1.46709 * b

    if kind == "protan":
        long_, med, short = 2.02344 * med - 2.52581 * short, med, short
    else:
        long_, med, short = long_, 0.494207 * long_ + 1.24827 * short, short

    out = []
    for channel in (
        0.080944 * long_ - 0.130504 * med + 0.116721 * short,
        -0.0102485 * long_ + 0.0540194 * med - 0.113615 * short,
        -0.000365294 * long_ - 0.00412163 * med + 0.693513 * short,
    ):
        c = max(0.0, min(1.0, channel))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
        out.append(format(round(max(0.0, min(1.0, c)) * 255), "02x"))
    return "#" + "".join(out)


# ---------------------------------------------------------------------------
# Reading the live CSS
# ---------------------------------------------------------------------------


def _block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    return match.group(1) if match else ""


def _var(block: str, name: str) -> str | None:
    match = re.search(r"--" + re.escape(name) + r":\s*([^;]+);", block)
    return match.group(1).strip() if match else None


def read_palette() -> dict[str, dict]:
    viz = VIZ.read_text(encoding="utf-8")
    tokens = TOKENS.read_text(encoding="utf-8")

    base = _block(viz, ".viz")
    dark = _block(viz, 'html[data-theme="dark"] .viz')

    surfaces = {
        "light": _var(_block(tokens, ":root"), "surface"),
        "dark": _var(_block(tokens, 'html[data-theme="dark"]'), "surface"),
    }

    palette: dict[str, dict] = {}
    for theme, block in (("light", base), ("dark", dark)):
        marks = {}
        for mark in MARKS:
            value = _var(block, "viz-" + mark) or _var(base, "viz-" + mark)
            if value and value.startswith("#"):
                marks[mark] = value
        surface = surfaces[theme]
        if surface and not surface.startswith("#"):
            surface = None
        palette[theme] = {"surface": surface or "#ffffff", "marks": marks}
    return palette


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def main() -> int:
    palette = read_palette()
    failures: list[str] = []

    for theme, spec in palette.items():
        print("-- " + theme + " on " + spec["surface"])
        for name, colour in spec["marks"].items():
            ratio = contrast(colour, spec["surface"])
            ok = ratio >= CONTRAST_FLOOR
            print(f"   {name:<16} {colour}  contrast {ratio:5.2f}:1  "
                  + ("ok" if ok else "FAIL"))
            if not ok:
                failures.append(theme + "/" + name + " contrast " + format(ratio, ".2f"))

        for (n1, c1), (n2, c2) in itertools.combinations(spec["marks"].items(), 2):
            readings = [("normal", delta_e(c1, c2))]
            for kind in ("protan", "deutan"):
                readings.append(
                    (kind, delta_e(simulate(c1, kind), simulate(c2, kind)))
                )
            worst = min(v for _, v in readings)
            ok = worst >= DE_FLOOR
            detail = "  ".join(k + " " + format(v, ".1f") for k, v in readings)
            print(f"   {n1 + '/' + n2:<16} {detail}   " + ("ok" if ok else "FAIL"))
            if not ok:
                failures.append(theme + "/" + n1 + "-" + n2 + " dE " + format(worst, ".1f"))
        print()

    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    print("every mark clears " + format(CONTRAST_FLOOR, ".1f")
          + ":1 and every pair clears dE " + format(DE_FLOOR, ".0f")
          + " under normal, protan and deutan vision")
    return 0


if __name__ == "__main__":
    sys.exit(main())
