"""PyInstaller 入口；GUI 与 --backend 都走同一 Fish.exe。"""

from fish_desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
