import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from omnigram import telegram
from omnigram.window import STYLE, MainWindow


def main():
    telegram.start()
    app = QApplication(sys.argv)
    app.setOrganizationName("Omnigram")
    app.setApplicationName("Omnigram")
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
