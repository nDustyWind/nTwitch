"""Shared look & feel: black base, neutral greys, and one accent colour used sparingly."""

ACCENT = "#BABAFF"       # the nTwitch accent: active state, key hints, focus, primary action
ACCENT_DIM = "#8a8ad0"   # softer variant for hover states
BG = "#000000"           # app background: always black
SURFACE = "#000000"      # panes, cards, inputs: also black, separated by borders not fills
PANEL = "#1e1e1e"        # buttons, toasts, highlighted rows
BORDER_DIM = "#333333"   # inactive borders
TEXT = "#e6e6e6"
MUTED = "#8a8a8a"

OK = "#8ee6a8"
WARN = "#f5d08a"
ERR = "#ff8fa3"
REC = "#ff6b81"


def make_theme():
    """Build the Textual theme (imported lazily so stdlib-only code can use the constants)."""
    from textual.theme import Theme

    return Theme(
        name="ntwitch",
        primary=ACCENT,
        secondary=ACCENT_DIM,
        accent=ACCENT,
        warning=WARN,
        error=ERR,
        success=OK,
        foreground=TEXT,
        background=BG,
        surface=SURFACE,
        panel=PANEL,
        dark=True,
        variables={
            "border": BORDER_DIM,
            "border-blurred": BORDER_DIM,
            "input-cursor-background": ACCENT,
            "input-cursor-foreground": BG,
            "input-selection-background": f"{ACCENT} 35%",
            "footer-background": BG,
            "footer-key-foreground": ACCENT,
            "footer-key-background": "transparent",
            "footer-description-foreground": MUTED,
            "footer-item-background": "transparent",
            "block-cursor-background": PANEL,
            "block-cursor-foreground": ACCENT,
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": PANEL,
            "block-cursor-blurred-foreground": TEXT,
            "block-hover-background": PANEL,
            "scrollbar": BORDER_DIM,
            "scrollbar-hover": MUTED,
            "scrollbar-active": ACCENT,
        },
    )
