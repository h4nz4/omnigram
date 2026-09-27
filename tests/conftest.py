import os

import pytest


@pytest.fixture
def qapp():
    """A headless QApplication for the few tests that need real widgets."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
