"""集中 QSS。页面不得写随机 style。"""

from __future__ import annotations

from .tokens import COLORS, HEIGHT, RADIUS, TYPE
from pathlib import Path
CHECKMARK = (Path(__file__).resolve().parent.parent / 'assets/checkmark.svg').as_posix()

QSS = f"""
QWidget {{
    background: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: "Microsoft YaHei UI", "Segoe UI Variable", "Segoe UI";
    font-size: {TYPE['body']}px;
}}
QMainWindow, QDialog, QStackedWidget, QScrollArea {{ background: {COLORS['bg']}; }}
QFrame#Sidebar {{
    background: {COLORS['surface']};
    border-right: 1px solid {COLORS['border']};
}}
QListWidget#NavList {{
    background: transparent;
    border: none;
    outline: 0;
    padding: 4px 8px;
}}
QListWidget#NavList::item {{
    color: {COLORS['secondary']};
    border-radius: {RADIUS['control']}px;
    padding: 8px 10px;
    margin: 1px 0;
}}
QListWidget#NavList::item:selected {{
    color: {COLORS['text']};
    background: {COLORS['elevated']};
}}
QListWidget#NavList::item:hover {{ color: {COLORS['text']}; }}
QFrame#Card {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['card']}px;
}}
QFrame#Elevated {{
    background: {COLORS['elevated']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['card']}px;
}}
QLabel {{ background: transparent; }}
QFrame#Divider {{ background: {COLORS['border']}; max-height: 1px; }}
QLabel#PageTitle {{
    font-size: {TYPE['page']}px;
    font-weight: 700;
    color: {COLORS['text']};
}}
QLabel#PageSubtitle {{ color: {COLORS['secondary']}; font-size: {TYPE['secondary']}px; }}
QLabel#SectionTitle {{ font-size: {TYPE['section']}px; font-weight: 650; }}
QLabel#CardTitle {{
    color: {COLORS['secondary']};
    font-size: {TYPE['caption']}px;
    font-weight: 600;
}}
QLabel#Muted {{ color: {COLORS['secondary']}; }}
QLabel#Caption {{ color: {COLORS['muted']}; font-size: {TYPE['caption']}px; }}
QLabel#Value {{ color: {COLORS['text']}; }}
QLabel#StatValue {{ font-size: 22px; font-weight: 700; color: {COLORS['text']}; }}
QLabel#BrandName {{ font-size: {TYPE['brand']}px; font-weight: 750; color: {COLORS['text']}; }}
QLabel#BrandSub {{ color: {COLORS['secondary']}; font-size: {TYPE['caption']}px; }}
QLabel#StatusPill {{
    border-radius: {RADIUS['pill']}px;
    padding: 3px 9px;
    font-size: {TYPE['caption']}px;
    font-weight: 600;
}}
QLabel#StatusPill[state="good"] {{ color: {COLORS['success']}; background: rgba(92, 207, 146, 0.12); }}
QLabel#StatusPill[state="warn"] {{ color: {COLORS['warning']}; background: rgba(244, 196, 106, 0.12); }}
QLabel#StatusPill[state="bad"] {{ color: {COLORS['danger']}; background: rgba(240, 119, 119, 0.12); }}
QLabel#StatusPill[state="info"] {{ color: {COLORS['primary']}; background: rgba(90, 169, 255, 0.12); }}
QLabel#StatusPill[state="neutral"] {{ color: {COLORS['secondary']}; background: rgba(147, 164, 184, 0.08); }}
QPushButton {{
    background: {COLORS['elevated']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    padding: 6px 12px;
    min-height: {HEIGHT['button'] - 14}px;
}}
QPushButton:hover {{ border-color: {COLORS['primary']}; }}
QPushButton:pressed {{ background: #E8EDF8; }}
QPushButton:disabled {{ color: {COLORS['muted']}; border-color: {COLORS['border']}; }}
QPushButton#PrimaryButton {{
    background: {COLORS['primary']};
    color: #FFFFFF;
    border: none;
    font-weight: 700;
}}
QPushButton#DangerButton {{ color: {COLORS['danger']}; }}
QToolButton {{
    background: transparent;
    color: {COLORS['secondary']};
    border: none;
    padding: 4px 6px;
    text-align: left;
}}
QToolButton:hover {{ color: {COLORS['text']}; }}
QToolButton:checked {{ color: {COLORS['primary']}; }}
QTabWidget::pane {{ border: 1px solid {COLORS['border']}; border-radius: {RADIUS['card']}px; background: {COLORS['surface']}; }}
QTabBar::tab {{
    background: transparent;
    color: {COLORS['secondary']};
    padding: 8px 14px;
    border: none;
}}
QTabBar::tab:selected {{ color: {COLORS['text']}; border-bottom: 2px solid {COLORS['primary']}; }}
QTableWidget, QTreeWidget {{
    background: {COLORS['surface']};
    alternate-background-color: {COLORS['elevated']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    gridline-color: {COLORS['border']};
    selection-background-color: #E5ECFF;
    selection-color: {COLORS['text']};
}}
QHeaderView::section {{
    background: {COLORS['surface']};
    color: {COLORS['secondary']};
    border: none;
    border-bottom: 1px solid {COLORS['border']};
    padding: 7px;
    font-weight: 600;
}}
QPlainTextEdit, QTextEdit {{
    background: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    padding: 8px;
    selection-background-color: #DCE6FF;
}}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C9D2E1; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QFrame#MessageIn {{
    background: {COLORS['elevated']};
    border: 1px solid {COLORS['border']};
    border-radius: 12px;
}}
QFrame#MessageOut {{
    background: #EAF0FF;
    border: 1px solid #D8E1F8;
    border-radius: 12px;
}}
QFrame#MessageIn[state="bad"], QFrame#MessageOut[state="bad"] {{ border-color: #6B3A42; }}
QLabel#BubbleText {{ color: {COLORS['text']}; font-size: 13px; }}
QLabel#BubbleMeta {{ color: {COLORS['secondary']}; font-size: 10px; }}
QLabel#BubbleMeta[state="bad"] {{ color: {COLORS['danger']}; }}
QLabel#BubbleMeta[state="warn"] {{ color: {COLORS['warning']}; }}
"""

QSS += f"""
QLineEdit, QComboBox, QSpinBox, QDateEdit, QDateTimeEdit, QTimeEdit {{ background: white; color: {COLORS['text']};
    border: 1px solid {COLORS['border']}; border-radius: 7px; padding: 7px 10px; min-height: 20px; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {COLORS['primary']}; }}
QComboBox QAbstractItemView {{ background: white; color: {COLORS['text']}; selection-background-color: #EAF0FF; }}
QCheckBox {{ spacing: 8px; background: transparent; padding: 3px 0; }}
QCheckBox::indicator {{ width: 17px; height: 17px; }}
QCheckBox::indicator:unchecked {{ border: 1px solid #BFCADD; border-radius: 4px; background: white; }}
QCheckBox::indicator:checked {{ border: 1px solid {COLORS['primary']}; border-radius: 4px; background: {COLORS['primary']}; image: url("{CHECKMARK}"); }}
QPushButton#PrimaryButton:disabled {{ background: #DDE4F8; color: #8898BC; }}
QProgressBar {{ border: none; background: #EAF0FF; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {COLORS['primary']}; border-radius: 3px; }}
QListWidget#RoleList {{ background: transparent; border: none; outline: 0; }}
QListWidget#RoleList::item {{ padding: 16px 12px; border-radius: 8px; margin-bottom: 8px; }}
QListWidget#RoleList::item:selected {{ background: #EAF0FF; color: {COLORS['primary']}; }}
QListWidget#NavList::item:selected {{ background: #EAF0FF; color: {COLORS['primary']}; border-left: 3px solid {COLORS['primary']}; }}
QLabel#HeroTitle {{ font-size: 28px; font-weight: 700; }}
QLabel#HeroText {{ font-size: 16px; line-height: 24px; color: {COLORS['secondary']}; }}
QLabel#CardTitle {{ letter-spacing: 1px; }}
QToolTip {{ background: white; color: {COLORS['text']}; border: 1px solid {COLORS['border']}; padding: 6px; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QListWidget#ConversationList {{ background: white; border: none; outline: 0; }}
QListWidget#ConversationList::item {{ padding: 12px 8px; border-radius: 6px; margin-bottom: 3px; }}
QListWidget#ConversationList::item:selected {{ background: #EAF0FF; color: {COLORS['primary']}; }}
QFrame#MessageIn[searchMatch="true"], QFrame#MessageOut[searchMatch="true"] {{ border: 2px solid {COLORS['primary']}; }}
"""
