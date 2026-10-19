"""Fish Design System tokens。所有页面只从这里取色/间距/字号。"""

from __future__ import annotations

COLORS = {
    "bg": "#F7F8FB",
    "surface": "#FFFFFF",
    "elevated": "#F1F4F9",
    "border": "#E4E8F0",
    "primary": "#4168DC",
    "primary_soft": "#EDF2FF",
    "text": "#202B40",
    "secondary": "#66758A",
    "muted": "#66758A",  # §5.6: retain readable secondary text on light surfaces.
    "success": "#23826B",
    "warning": "#A87523",
    "danger": "#C05166",
    "white": "#FFFFFF",
}

SPACE = {
    "page": 28,
    "section": 20,
    "card": 20,
    "gap": 16,
    "small": 8,
    "tiny": 4,
}

RADIUS = {
    "card": 12,
    "control": 8,
    "pill": 999,
}

TYPE = {
    "page": 24,
    "section": 17,
    "body": 14,
    "secondary": 13,
    "caption": 12,
    "brand": 21,
}

HEIGHT = {
    "button": 36,
    "nav": 44,
    "input": 36,
}

STATE_COLORS = {
    "good": COLORS["success"],
    "warn": COLORS["warning"],
    "bad": COLORS["danger"],
    "info": COLORS["primary"],
    "neutral": COLORS["secondary"],
}
