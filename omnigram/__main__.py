import sys


def main():
    if sys.argv[1:2] == ["serve"]:  # the headless server: no Qt needed (see server.py)
        from omnigram import server
        server.main(sys.argv[2:])
        return
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:
        sys.exit("The desktop app needs its GUI: pip install 'omnigram[gui]'. On a server, run `omnigram serve`.")

    from omnigram import telegram
    from omnigram.window import STYLE, MainWindow

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
