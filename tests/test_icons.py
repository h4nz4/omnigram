"""The bundled Lucide icons: every name the code asks for has its SVG, and it renders in the theme colour
(Lucide draws with currentColor, which would otherwise come out black on the dark UI)."""
import re
from pathlib import Path

from omnigram import icons

SOURCES = Path(icons.__file__).parent


def used_names() -> set[str]:
    """Names in icons.get("…") calls, plus the sidebar's Lucide names (passed to icons.get as variables)."""
    from omnigram.window import CATEGORIES
    names = {name for _, items in CATEGORIES for _, _, name, _ in items}
    for f in SOURCES.glob("*.py"):
        names |= set(re.findall(r'icons\.get\("([a-z0-9-]+)"', f.read_text("utf-8")))
    # nav entries and button tables: ("Text", "icon-name", …) tuples right before a handler
    window = (SOURCES / "window.py").read_text("utf-8") + (SOURCES / "dialogs.py").read_text("utf-8") + \
        (SOURCES / "mailing_dialogs.py").read_text("utf-8")
    names |= set(re.findall(r'\("[^"]+", "([a-z0-9]+(?:-[a-z0-9]+)*)", (?:"[a-z]*", )?(?:self|lambda)', window))
    return names


def test_every_icon_the_app_uses_is_bundled():
    names = used_names()
    assert len(names) > 50  # the sidebar alone has ~50; a regex that stopped matching must not pass silently
    missing = sorted(n for n in names if not (icons.DIR / f"{n}.svg").exists())
    assert not missing, f"add these from the same Lucide release: {missing}"


def test_the_lucide_licence_ships_with_the_icons():
    assert "ISC License" in (icons.DIR / "LICENSE").read_text("utf-8")


def test_icons_render_in_the_theme_colour(qapp):
    from PySide6.QtGui import QColor

    image = icons.get("flame").pixmap(48, 48).toImage()
    drawn = [QColor(image.pixel(x, y)) for x in range(48) for y in range(48) if image.pixelColor(x, y).alpha() == 255]
    assert drawn, "nothing rendered"
    assert all(c.name() == icons.COLOR for c in drawn)
