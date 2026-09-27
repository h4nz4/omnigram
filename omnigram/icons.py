"""Bundled Lucide icons (https://lucide.dev, release 1.48.0; ISC licence in icons/LICENSE), tinted for the theme.

Qt's platform theme icons differ per OS (Windows maps 118 of its 150 names, macOS and Linux their own
subsets), so the app ships one icon set and looks the same everywhere. Lucide draws with
stroke="currentColor", which would render black on the dark UI, so every icon is recoloured on load.

Adding an icon: copy <name>.svg from the same Lucide release into omnigram/icons/ and use icons.get("<name>").
tests/test_icons.py checks that every name the code uses has its file.
"""
from functools import cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

DIR = Path(__file__).with_name("icons")
COLOR = "#e4e6eb"  # the theme's text colour, see window.STYLE


@cache
def get(name: str, color: str = COLOR) -> QIcon:
    """The icon `name` in `color`, rendered at 16/32/48 px so it stays crisp at 1x-3x display scaling.
    Qt derives the greyed-out variant for disabled buttons itself. A missing file raises: tests catch it."""
    svg = (DIR / f"{name}.svg").read_text("utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    for size in (16, 32, 48):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
