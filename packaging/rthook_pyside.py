import os
import sys

base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
paths = [os.path.join(base, "PySide6"), os.path.join(base, "shiboken6"), base]
for path in paths:
    if os.path.isdir(path):
        try:
            os.add_dll_directory(path)
        except Exception:
            pass
os.environ["PATH"] = os.pathsep.join(paths + [os.environ.get("PATH", "")])
