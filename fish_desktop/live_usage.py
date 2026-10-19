"""Live usage settings without stopping the desktop, QQ, or native runtime."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
import dependencies


def main():
    from PySide6.QtWidgets import QApplication
    from .services import FishServices
    from .system_settings import OwnerPage
    from .theme.qss import QSS
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(QSS)
    services = FishServices()
    page = OwnerPage(services)
    page.setWindowTitle('大肥鱼 Fish · 用量设置（实时生效）')
    page.resize(760, 880)
    page.show()
    page.refresh()
    # The main desktop owns the shared services. Closing this settings window
    # must not call services.shutdown() or stop the existing QQ processes.
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
