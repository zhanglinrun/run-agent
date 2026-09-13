"""Accessible terminal palettes for Run Agent's Textual interface."""

from textual.theme import Theme

RUN_DARK = Theme(
    name="run-dark",
    primary="#DFA75C",
    secondary="#76BEB4",
    accent="#DFA75C",
    foreground="#E6E3DD",
    background="#171A1D",
    surface="#202529",
    panel="#272D32",
    warning="#EDBD72",
    error="#F28D8D",
    success="#8AC9A1",
    dark=True,
)
RUN_LIGHT = Theme(
    name="run-light",
    primary="#875113",
    secondary="#256B65",
    accent="#875113",
    foreground="#262B30",
    background="#FAF9F6",
    surface="#F0EFEB",
    panel="#E7E6E1",
    warning="#8B5600",
    error="#B62632",
    success="#226E40",
    dark=False,
)
RUN_HIGH_CONTRAST = Theme(
    name="run-high-contrast",
    primary="#FFD580",
    secondary="#8AE9DB",
    accent="#FFD580",
    foreground="#FFFFFF",
    background="#000000",
    surface="#101010",
    panel="#1C1C1C",
    warning="#FFDB70",
    error="#FF9696",
    success="#90EDAE",
    dark=True,
    text_alpha=1,
)
