"""One real default-photo desktop turn; never uses the QQ transport."""
import json
import os
import re
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "harness")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fish_desktop.services import FishServices, tcp_open
from fish_desktop.conversation import ChatPage
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from fish_desktop.theme import QSS


def main():
    out = ROOT / "artifacts/round1"
    out.mkdir(parents=True, exist_ok=True)
    render_only = "--render-only" in sys.argv
    if not render_only and tcp_open("127.0.0.1", 3080):
        raise RuntimeError("研究角色已在运行，验收不会替换正在使用的服务")
    image = Image.new("RGB", (440, 260), "white")
    draw = ImageDraw.Draw(image)
    draw.text((22, 20), "ROUND1 6215", font=ImageFont.truetype(r"C:\Windows\Fonts\consolab.ttf", 44), fill="black")
    draw.rectangle((25, 95, 180, 235), fill="#2266dd")
    photo = out / "default-photo-fixture.png"
    image.save(photo)
    app = QApplication.instance() or QApplication([])
    for font_path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf"):
        QFontDatabase.addApplicationFont(font_path)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(QSS)
    services = FishServices(ROOT)
    page = ChatPage(services)
    page.roles.setCurrentIndex(1)
    page.resize(1200, 800)
    page.show()
    if render_only:
        prior = json.loads((out / "default-photo-check.json").read_text(encoding="utf-8"))
        page.cid = prior["conversation_id"]
        page.refresh()
        QTest.qWait(250)
        page.grab().save(str(out / "default-photo-chat.png"))
        page.close()
        app.processEvents()
        print("Rendered existing verified conversation")
        return 0
    page._new()
    page.add_files([str(photo)])
    page.editor.setPlainText("这是主人要求的验收。请直接理解照片，严格完成三项：1.抄录全部字母数字；2.说明色块颜色；3.用‘已逐项核对’结束。不要调用工具，不要转移话题。")
    try:
        QTest.mouseClick(page.send_btn, Qt.LeftButton)
        for _ in range(800):
            QTest.qWait(250)
            if not page.loading:
                break
        rows = services.chat("research").snapshot(page.cid)["messages"]
        answer = next((row["text"] for row in reversed(rows) if row["direction"] == "outbound"), "")
        result = {"ok": bool("6215" in answer and "蓝" in answer and "已逐项核对" in answer),
                  "answer": answer, "input_has_photo": bool(rows and rows[0]["segments"]),
                  "operation": "选择照片后点击普通发送按钮", "qq_messages_sent": 0,
                  "conversation_id": page.cid}
        QTest.qWait(100)
        page.grab().save(str(out / "default-photo-chat.png"))
        (out / "default-photo-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    finally:
        services.chat("research").cancel()
        services.runtime.stop("research")
        page.close()
        app.processEvents()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__, re.sub(r"https?://\S+", "<url>", str(exc)))
        raise SystemExit(1)
