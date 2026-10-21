"""Shared presentation tokens and UI style registration.

This module owns the single source of truth for the design tokens (colours,
fonts, style names) used by the dashboard and its dialogs. Importing it has
no side effects: no Tk root is created and no styles are registered until
``configure_app_styles`` is called with a real ``ttk.Style``. The Qt
``configure_qt_styles`` adapter applies the same semantic tokens while
consumers move in later migration sections; the Tk registration API is
transitional, not a second long-term UI implementation.
"""

from typing import Any

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

Font = tuple[Any, ...]

# The first block is the semantic contract. The legacy names below remain in
# the public map because dialogs and external test seams still consume them.
COLOR_ROLES: dict[str, str] = {
    "base": "#F4F7FB",
    "surface": "#FFFFFF",
    "line": "#E4E7EC",
    "ink": "#172033",
    "ink_2": "#667085",
    "ink_3": "#98A2B3",
    "accent": "#4F46E5",
    "accent_ink": "#FFFFFF",
    "success": "#16803C",
    "warning": "#B45309",
    "danger": "#B42318",
    "disabled": "#D0D5DD",
    "focus": "#4F46E5",
    "selection": "#E0E7FF",
}

COLORS: dict[str, str] = {
    **COLOR_ROLES,
    "background": COLOR_ROLES["base"],
    "card": COLOR_ROLES["surface"],
    "text": COLOR_ROLES["ink"],
    "secondary": COLOR_ROLES["ink_2"],
    "accent": COLOR_ROLES["accent"],
    "accent_active": "#4338CA",
    "border": COLOR_ROLES["line"],
    "success": COLOR_ROLES["success"],
    "warning": COLOR_ROLES["warning"],
    "danger": COLOR_ROLES["danger"],
    "danger_active": "#912018",
    "disabled": COLOR_ROLES["disabled"],
    "primary_disabled": "#A5B4FC",
    "primary_disabled_text": "#EEF2FF",
    "bar_trough": "#E8ECF5",
    "card_bar_trough": "#EEF2F6",
    "button_bg": "#E3E4E8",
    "button_bg_active": "#D4D8E0",
    "muted_text": COLOR_ROLES["ink_3"],
    "graph_grid": "#D9E0EC",
    "graph_empty_border": "#CBD5E1",
}

SPACING: dict[str, int] = {
    "page_x": 30,
    "page_y": 26,
    "section_gap": 14,
    "row_gap": 10,
    "control_gap": 12,
    "card_pad_x": 18,
    "card_pad_y": 16,
    "section_pad_x": 18,
    "section_pad_y": 14,
    "dialog_pad_x": 24,
    "dialog_pad_y": 22,
    "button_gap": 8,
    "footer_gap": 16,
    "scrollbar_gutter": 6,
    "caption_gap": 2,
    "header_desc_gap": 6,
    "header_actions_gap": 20,
    "heading_desc_gap": 4,
    "section_body_top": 10,
    "nav_button_gap": 16,
}

CONTROL: dict[str, int] = {
    "button_pad_x": 12,
    "button_pad_y": 7,
    "primary_button_pad_x": 16,
    "primary_button_pad_y": 9,
    "spinbox_pad": 4,
    "card_border_width": 1,
}

LAYOUT: dict[str, int] = {
    "dashboard_description_wrap": 520,
    "dashboard_status_wrap": 280,
    "card_grid_gap": 7,
    "card_row_gap": 14,
    "dashboard_header_wrap": 680,
    "page_shell_wrap": 620,
    "navigation_card_wrap": 560,
    "section_description_wrap": 360,
    "fit_wrap_margin": 36,
    "fit_wrap_max": 560,
}

GRAPH: dict[str, int] = {
    "height": 108,
    "width": 520,
    "max_points": 240,
}

FONTS: dict[str, Font] = {
    "ui": ("Helvetica",),
    "title": ("Helvetica", 24, "bold"),
    "section": ("Helvetica", 14, "bold"),
    "node": ("Helvetica", 9, "bold"),
    "body": ("Helvetica", 10),
    "button": ("Helvetica", 11, "bold"),
    "danger_button": ("Helvetica", 10, "bold"),
    "status": ("Helvetica", 10, "bold"),
    "card_overline": ("Helvetica", 10, "bold"),
    "card_headline": ("Helvetica", 22, "bold"),
    "card_subtitle": ("Helvetica", 10),
    "card_metric": ("Helvetica", 9),
    "dialog_heading": ("Helvetica", 20, "bold"),
    "info_heading": ("Helvetica", 22, "bold"),
    "info_subtitle": ("Helvetica", 11),
    "info_headline": ("Helvetica", 20, "bold"),
    "dialog_description": ("Helvetica", 10),
    "detail_section": ("Helvetica", 11, "bold"),
    "detail_row": ("Helvetica", 10),
    "empty_detail": ("Helvetica", 11),
    "status_text": ("Helvetica", 10),
}

TYPOGRAPHY: dict[str, Font] = {
    "page_title": FONTS["title"],
    "section_title": FONTS["section"],
    "card_overline": FONTS["card_overline"],
    "primary_metric": FONTS["card_headline"],
    "body": FONTS["body"],
    "caption": FONTS["card_subtitle"],
    "action": FONTS["button"],
    "dialog_title": FONTS["dialog_heading"],
}

STYLE_APP_FRAME = "App.TFrame"
STYLE_TITLE = "Title.TLabel"
STYLE_SECTION = "Section.TLabel"
STYLE_DESCRIPTION = "Description.TLabel"
STYLE_NODE = "Node.TLabel"
STYLE_HEALTHY = "Healthy.TLabel"
STYLE_HEALTH_WARNING = "HealthWarning.TLabel"
STYLE_PRIMARY_BUTTON = "Primary.TButton"
STYLE_NEUTRAL_BUTTON = "Neutral.TButton"
STYLE_DANGER_BUTTON = "Danger.TButton"
STYLE_DANGER_CHECKBUTTON = "Danger.TCheckbutton"
STYLE_STATUS_READY = "Ready.Status.TLabel"
STYLE_STATUS_BUSY = "Busy.Status.TLabel"
STYLE_SPINBOX = "App.TSpinbox"
STYLE_CHECKBUTTON = "App.TCheckbutton"
STYLE_BAR_ANALYSIS = "Analysis.Horizontal.TProgressbar"
STYLE_BAR_COMPLETE = "Complete.Horizontal.TProgressbar"
STYLE_BAR_CARD = "Card.Horizontal.TProgressbar"

DEFAULT_APPEARANCE = "indigo"

NODE_COLORS: dict[str, str] = {
    "indigo": "#4F46E5",
    "emerald": "#047857",
    "rose": "#E11D48",
    "amber": "#B45309",
    "sky": "#0369A1",
    "violet": "#7C3AED",
    "teal": "#0F766E",
    "slate": "#475569",
}

ACCENT_THEMES: dict[str, dict[str, str]] = {
    "indigo": {
        "accent": "#4F46E5",
        "accent_active": "#4338CA",
        "primary_disabled": "#A5B4FC",
        "primary_disabled_text": "#EEF2FF",
    },
    "emerald": {
        "accent": "#047857",
        "accent_active": "#065F46",
        "primary_disabled": "#A7F3D0",
        "primary_disabled_text": "#ECFDF5",
    },
    "rose": {
        "accent": "#E11D48",
        "accent_active": "#BE123C",
        "primary_disabled": "#FDA4AF",
        "primary_disabled_text": "#FFF1F2",
    },
    "amber": {
        "accent": "#B45309",
        "accent_active": "#92400E",
        "primary_disabled": "#FCD34D",
        "primary_disabled_text": "#FFFBEB",
    },
    "sky": {
        "accent": "#0369A1",
        "accent_active": "#075985",
        "primary_disabled": "#7DD3FC",
        "primary_disabled_text": "#F0F9FF",
    },
}


def accent_theme_colors(
    theme: str,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a full colour token map with one accent theme applied.

    Unknown themes fall back to the default appearance, so a stale persisted
    choice can never break style registration.
    """

    tokens = ACCENT_THEMES.get(theme, ACCENT_THEMES[DEFAULT_APPEARANCE])
    colors = dict(COLORS if base is None else base)
    colors.update(tokens)
    colors["focus"] = colors["accent"]
    colors.setdefault("accent_ink", COLOR_ROLES["accent_ink"])
    return colors


def configure_app_styles(
    style: Any,
    colors: dict[str, str] | None = None,
    fonts: dict[str, Font] | None = None,
) -> None:
    """Register every ttk style used by the dashboard and its dialogs.

    ``style`` is a ``ttk.Style`` bound to the live application root. The
    optional token dictionaries default to this module's ``COLORS`` and
    ``FONTS`` so callers can override them for tests without touching the
    shared token source.
    """

    c = COLORS if colors is None else colors
    f = FONTS if fonts is None else fonts

    style.configure(
        STYLE_APP_FRAME,
        background=c["background"],
    )
    style.configure(
        STYLE_TITLE,
        background=c["background"],
        foreground=c["text"],
        font=f["title"],
    )
    style.configure(
        STYLE_SECTION,
        background=c["background"],
        foreground=c["text"],
        font=f["section"],
    )
    style.configure(
        STYLE_DESCRIPTION,
        background=c["background"],
        foreground=c["secondary"],
        font=f["body"],
    )
    style.configure(
        STYLE_NODE,
        background=c["background"],
        foreground=c["secondary"],
        font=f["node"],
    )
    style.configure(
        STYLE_HEALTHY,
        background=c["background"],
        foreground=c["success"],
        font=f["body"],
    )
    style.configure(
        STYLE_HEALTH_WARNING,
        background=c["background"],
        foreground=c["warning"],
        font=f["body"],
    )
    style.configure(
        STYLE_PRIMARY_BUTTON,
        background=c["accent"],
        foreground=c["accent_ink"],
        font=f["button"],
        padding=(
            CONTROL["primary_button_pad_x"],
            CONTROL["primary_button_pad_y"],
        ),
        borderwidth=0,
        focusthickness=2,
        focuscolor=c["accent"],
    )
    style.map(
        STYLE_PRIMARY_BUTTON,
        background=[
            ("active", c["accent_active"]),
            ("disabled", c["primary_disabled"]),
        ],
        foreground=[("disabled", c["primary_disabled_text"])],
    )
    style.configure(
        STYLE_DANGER_BUTTON,
        background=c["danger"],
        foreground=c["accent_ink"],
        font=f["danger_button"],
        padding=(CONTROL["button_pad_x"], CONTROL["button_pad_y"]),
        borderwidth=0,
        focusthickness=2,
        focuscolor=c["danger"],
    )
    style.map(
        STYLE_DANGER_BUTTON,
        background=[("active", c["danger_active"]), ("disabled", c["disabled"])],
    )
    style.configure(
        STYLE_NEUTRAL_BUTTON,
        background=c["button_bg"],
        foreground=c["text"],
        font=f["danger_button"],
        padding=(CONTROL["button_pad_x"], CONTROL["button_pad_y"]),
        borderwidth=0,
        focusthickness=2,
        focuscolor=c["accent"],
        bordercolor=c["border"],
    )
    style.map(
        STYLE_NEUTRAL_BUTTON,
        background=[("active", c["button_bg_active"]), ("disabled", c["disabled"])],
        foreground=[("disabled", c["muted_text"])],
    )
    style.configure(
        STYLE_STATUS_READY,
        background=c["background"],
        foreground=c["success"],
        font=f["status"],
    )
    style.configure(
        STYLE_STATUS_BUSY,
        background=c["background"],
        foreground=c["warning"],
        font=f["status"],
    )
    style.configure(
        STYLE_BAR_ANALYSIS,
        background=c["accent"],
        troughcolor=c["bar_trough"],
        borderwidth=0,
        lightcolor=c["accent"],
        darkcolor=c["accent"],
    )
    style.configure(
        STYLE_SPINBOX,
        background=c["card"],
        foreground=c["text"],
        fieldbackground=c["card"],
        bordercolor=c["border"],
        lightcolor=c["border"],
        darkcolor=c["border"],
        arrowcolor=c["secondary"],
        padding=CONTROL["spinbox_pad"],
        font=f["body"],
    )
    style.map(
        STYLE_SPINBOX,
        foreground=[("disabled", c["disabled"])],
        fieldbackground=[("disabled", c["background"])],
    )
    style.configure(
        STYLE_CHECKBUTTON,
        background=c["card"],
        foreground=c["text"],
        font=f["body"],
    )
    style.map(
        STYLE_CHECKBUTTON,
        background=[("active", c["card"])],
        foreground=[("disabled", c["disabled"])],
    )
    style.configure(
        STYLE_DANGER_CHECKBUTTON,
        background=c["card"],
        foreground=c["danger"],
        font=f["body"],
    )
    style.map(
        STYLE_DANGER_CHECKBUTTON,
        background=[("active", c["card"])],
        foreground=[("disabled", c["disabled"])],
    )
    style.configure(
        STYLE_BAR_COMPLETE,
        background=c["success"],
        troughcolor=c["bar_trough"],
        borderwidth=0,
        lightcolor=c["success"],
        darkcolor=c["success"],
    )
    style.configure(
        STYLE_BAR_CARD,
        background=c["accent"],
        troughcolor=c["card_bar_trough"],
        borderwidth=0,
        lightcolor=c["accent"],
        darkcolor=c["accent"],
    )


def configure_qt_styles(
    widget: Any,
    *,
    colors: dict[str, str] | None = None,
    fonts: dict[str, Font] | None = None,
) -> None:
    """Apply the shared semantic tokens to a Qt widget subtree.

    This Qt API is introduced while Tk consumers are migrated in later
    sections. Widgets opt into button roles with ``setProperty('role', ...)``.
    """
    c = COLORS if colors is None else colors
    f = FONTS if fonts is None else fonts
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c["background"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["card"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["selection"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c["text"]))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)
    app = QApplication.instance()
    if isinstance(app, QApplication):
        app.setFont(_qt_font(f["ui"]))
    widget.setStyleSheet(
        "QWidget { color: "
        + c["text"]
        + "; background-color: "
        + c["background"]
        + "; }"
        "QGroupBox { border: 1px solid "
        + c["border"]
        + "; border-radius: 4px; margin-top: 8px; padding: 12px 8px 8px; }"
        " QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; font-family: '"
        + str(f["section"][0])
        + "'; font-size: "
        + f"{f['section'][1]}pt; font-weight: 700;"
        " }"
        " QListWidget { border: 1px solid "
        + c["border"]
        + "; border-radius: 4px; background-color: "
        + c["card"]
        + "; padding: 2px; }"
        " QListWidget::item { padding: 6px; }"
        " QListWidget::item:selected { background-color: "
        + c["selection"]
        + "; color: "
        + c["text"]
        + "; }"
        " QPushButton { border: 1px solid "
        + c["border"]
        + "; border-radius: 4px; background-color: "
        + c["card"]
        + "; color: "
        + c["text"]
        + "; padding: "
        + f"{CONTROL['button_pad_y']}px {CONTROL['button_pad_x']}px;"
        " } QPushButton:hover { background-color: "
        + c["button_bg"]
        + "; } QPushButton:pressed { background-color: "
        + c["button_bg_active"]
        + "; }"
        " QPushButton[role='primary'] { background-color: "
        + c["accent"]
        + "; color: "
        + c["accent_ink"]
        + "; padding: "
        + f"{CONTROL['primary_button_pad_y']}px {CONTROL['primary_button_pad_x']}px;"
        " } QPushButton[role='primary']:hover { background-color: "
        + c["accent_active"]
        + "; } QPushButton[role='neutral'] { background-color: "
        + c["button_bg"]
        + "; color: "
        + c["text"]
        + "; } QPushButton[role='danger'] { background-color: "
        + c["danger"]
        + "; color: "
        + c["accent_ink"]
        + "; } QPushButton[role='danger']:hover { background-color: "
        + c["danger_active"]
        + "; }"
        " QPushButton:focus, QCheckBox:focus, QSpinBox:focus { outline: 2px solid "
        + c["focus"]
        + "; outline-offset: 2px; border-color: "
        + c["focus"]
        + "; }"
        " QLabel[role='status'] { background-color: "
        + c["card"]
        + "; border-left: 3px solid "
        + c["accent"]
        + "; border-radius: 4px; padding: 5px 8px; }"
        " QLabel[role='description'] { color: " + c["secondary"] + "; }"
        " QLabel[role='success'] { color: "
        + c["success"]
        + "; } QLabel[role='warning'] { color: "
        + c["warning"]
        + "; } QLabel[role='danger'] { color: "
        + c["danger"]
        + "; }"
    )
    widget.setStyleSheet(widget.styleSheet() + _qt_typography_styles(f))


def _qt_font(spec: Font) -> QFont:
    font = QFont(str(spec[0]))
    for part in spec[1:]:
        if isinstance(part, int):
            font.setPointSize(part)
        elif part == "bold":
            font.setBold(True)
    return font


def _qt_typography_styles(fonts: dict[str, Font]) -> str:
    roles = {
        "title": ("QLabel[role='title']", "title"),
        "section": ("QLabel[role='section']", "section"),
        "description": ("QLabel[role='description']", "body"),
        "status": ("QLabel[role='status']", "status"),
        "action": ("QPushButton", "button"),
    }
    rules: list[str] = []
    for selector, token in roles.values():
        spec = fonts[token]
        family = str(spec[0]).replace("'", "")
        size = next((part for part in spec[1:] if isinstance(part, int)), None)
        weight = "700" if "bold" in spec else "400"
        declarations = [f"font-family: '{family}'", f"font-weight: {weight}"]
        if size is not None:
            declarations.append(f"font-size: {size}pt")
        rules.append(f"{selector} {{ {'; '.join(declarations)}; }}")
    return " ".join(rules)
