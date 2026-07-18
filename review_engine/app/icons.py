"""Emoji-free inline SVG icon set for the Review Engine UI (RAYAAAA-269).

The owner asked for the Review Engine ("RAYSERR Lens") to carry NO emoji. These
small, monochrome line icons replace the previous emoji glyphs in nav items, stat
tiles, and review-type cards. Each icon inherits its colour from ``currentColor``
so a caller can tint it via a wrapping ``color:`` style (the coloured chips do).

Kept UI-agnostic (no Streamlit import) so both the Streamlit views and tests can
import it. ``icon(name)`` returns an ``<svg>`` string ready to drop into an
``st.markdown(..., unsafe_allow_html=True)`` block.
"""
from __future__ import annotations

# viewBox is a constant 24x24; paths use stroke=currentColor so a wrapper's
# ``color`` tints them. ``fill=none`` keeps them as clean line icons.
_PATHS: dict[str, str] = {
    "document": "<path d='M6 2h8l4 4v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z'/>"
                "<path d='M14 2v4h4'/><path d='M8 13h8'/><path d='M8 17h5'/>",
    "clock": "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
    "check": "<path d='M20 6 9 17l-5-5'/>",
    "alert": "<path d='M12 3 2.5 20h19L12 3z'/><path d='M12 10v4'/><path d='M12 17.5h.01'/>",
    "scales": "<path d='M12 3v18'/><path d='M7 6l5-1 5 1'/><path d='M8 21h8'/>"
              "<path d='M7 6 4 13a3 3 0 0 0 6 0L7 6z'/><path d='M17 6l-3 7a3 3 0 0 0 6 0l-3-7z'/>",
    "users": "<circle cx='9' cy='8' r='3.2'/><path d='M3.5 20a5.5 5.5 0 0 1 11 0'/>"
             "<path d='M16 5.2a3.2 3.2 0 0 1 0 5.6'/><path d='M18.5 20a5.5 5.5 0 0 0-3-4.9'/>",
    "contract": "<path d='M6 2h8l4 4v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z'/>"
                "<path d='M14 2v4h4'/><path d='M8 12h6'/><path d='M8 16h4'/>",
    "book": "<path d='M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z'/>"
            "<path d='M4 20.5A2.5 2.5 0 0 1 6.5 18H19v3H6.5A2.5 2.5 0 0 1 4 20.5z'/>",
    "bolt": "<path d='M13 2 4 14h7l-1 8 9-12h-7l1-8z'/>",
    "plus": "<path d='M12 5v14'/><path d='M5 12h14'/>",
    "grid": "<rect x='3' y='3' width='7.5' height='7.5' rx='1.5'/><rect x='13.5' y='3' width='7.5' height='7.5' rx='1.5'/>"
            "<rect x='3' y='13.5' width='7.5' height='7.5' rx='1.5'/><rect x='13.5' y='13.5' width='7.5' height='7.5' rx='1.5'/>",
    "list": "<path d='M9 6h12'/><path d='M9 12h12'/><path d='M9 18h12'/>"
            "<path d='M4 6h.01'/><path d='M4 12h.01'/><path d='M4 18h.01'/>",
    "search": "<circle cx='11' cy='11' r='7'/><path d='m21 21-4.3-4.3'/>",
    "robot": "<rect x='4' y='8' width='16' height='12' rx='4'/><path d='M12 8V4.5'/>"
             "<circle cx='12' cy='3' r='1.4'/><circle cx='9' cy='14' r='1.3'/><circle cx='15' cy='14' r='1.3'/>"
             "<path d='M9.5 17.5h5'/><path d='M4 12H2.5'/><path d='M22 12h-1.5'/>",
}


def icon(name: str, size: float = 18) -> str:
    """Return an inline ``<svg>`` for ``name`` (see ``_PATHS``), tinted by the
    surrounding ``color``. Unknown names render nothing (empty string) so a caller
    never emits a broken glyph."""
    body = _PATHS.get(name)
    if body is None:
        return ""
    return (
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        f"stroke-linejoin='round' aria-hidden='true'>{body}</svg>"
    )


ICON_NAMES = frozenset(_PATHS)
