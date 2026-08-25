"""Runtime window-size density: the app's one responsive mechanism.

Deliberately small and generic-free: a mode is computed purely from the
*current* content-area width/height (never a resolution or platform check),
and each mode carries a fixed bundle of layout numbers. MainWindow computes
the mode on resize and applies it; each tab exposes its own
``apply_responsive_mode()`` for the parts a shared mechanism can't reach
(per-layout margins/spacing aren't stylesheet-able, and structural reflow is
inherently tab-specific) — see main_window.py's docstring on
``_recompute_responsive_mode`` for how the two halves fit together.

Thresholds are deliberately expressed as width/height comparisons, not a
list of resolutions — a keyboard eating vertical space, a window dragged
narrower, or a genuinely small Raspberry Pi panel all just look like "the
content area got smaller than N px," and are handled identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResponsiveMode(Enum):
    """Three density tiers — enough range to matter, few enough to reason about."""

    NORMAL = "normal"
    COMPACT = "compact"
    ULTRA_COMPACT = "ultra_compact"


# Width/height thresholds, chosen by inspecting what the busiest rows in this
# app actually need (see passive_capture.py's Setup/Auto/Filters rows and
# profile_tab.py's toolbar rows) rather than picked to match any specific
# device:
#
# - COMPACT_*: below this, at least one existing row's natural width (or the
#   stack of fixed-height rows above a table) starts eating into space a
#   data table should get instead. 1000x650 comfortably covers a maximized
#   1024x600 Pi panel while leaving a normal laptop window in NORMAL mode.
# - ULTRA_COMPACT_WIDTH/HEIGHT: both must be tight (an 800x480-class panel)
#   before the deepest tier kicks in.
# - ULTRA_COMPACT_SEVERE_HEIGHT: a standalone height-only escape hatch for
#   the on-screen-keyboard case — width is usually untouched when a keyboard
#   appears (it eats height, not width), so requiring *both* dimensions to
#   be tight would under-react to exactly the scenario this task cares most
#   about. Any viewport this short goes straight to ULTRA_COMPACT regardless
#   of width.
COMPACT_WIDTH_THRESHOLD = 1000
COMPACT_HEIGHT_THRESHOLD = 650
ULTRA_COMPACT_WIDTH_THRESHOLD = 820
ULTRA_COMPACT_HEIGHT_THRESHOLD = 480
ULTRA_COMPACT_SEVERE_HEIGHT_THRESHOLD = 380


def compute_mode(width: int, height: int) -> ResponsiveMode:
    """Return the density mode for a content area of `width` x `height`."""
    if height < ULTRA_COMPACT_SEVERE_HEIGHT_THRESHOLD or (
        width < ULTRA_COMPACT_WIDTH_THRESHOLD and height < ULTRA_COMPACT_HEIGHT_THRESHOLD
    ):
        return ResponsiveMode.ULTRA_COMPACT
    if width < COMPACT_WIDTH_THRESHOLD or height < COMPACT_HEIGHT_THRESHOLD:
        return ResponsiveMode.COMPACT
    return ResponsiveMode.NORMAL


@dataclass(frozen=True)
class ResponsiveMetrics:
    """One density tier's worth of layout numbers.

    Every field here is something Qt layouts/QSS can apply uniformly and
    reversibly — nothing here rebuilds or reparents widgets (that's
    per-tab structural reflow, handled separately in each
    apply_responsive_mode()).
    """

    # Root/group layout spacing. Priorities 1-2 from the task's responsive
    # priority list: margins first, then inter-widget spacing.
    layout_margin: int
    layout_spacing: int
    group_margin_top: int
    group_padding: tuple[int, int, int, int]

    # Control chrome. Priorities 3-4: padding, then control height.
    control_min_height: int
    control_padding: str  # QSS shorthand, e.g. "2px 12px"
    field_min_height: int  # QLineEdit/QComboBox/QPlainTextEdit

    # Tab bar / table header chrome — same idea, applied to navigation
    # rather than form controls.
    tab_min_height: int
    tab_min_width: int
    tab_padding: str
    header_min_height: int
    table_row_height: int

    # Priority 9 (last resort): font size, kept modest — see MIN_FONT_PT.
    base_font_pt: float


# A floor under every mode's base_font_pt: small enough to gain real space,
# never so small the UI stops being readable at arm's length on a Pi touch
# panel.
MIN_FONT_PT = 8.5

METRICS: dict[ResponsiveMode, ResponsiveMetrics] = {
    # NORMAL reproduces the numbers this app already used before density
    # became mode-aware, so a desktop window in NORMAL mode is pixel-for-
    # pixel what it always was.
    ResponsiveMode.NORMAL: ResponsiveMetrics(
        layout_margin=10,
        layout_spacing=8,
        group_margin_top=12,
        group_padding=(13, 9, 9, 9),
        control_min_height=30,
        control_padding="2px 12px",
        field_min_height=28,
        tab_min_height=38,
        tab_min_width=130,
        tab_padding="0 12px",
        header_min_height=28,
        table_row_height=24,
        base_font_pt=10.0,
    ),
    ResponsiveMode.COMPACT: ResponsiveMetrics(
        layout_margin=6,
        layout_spacing=5,
        group_margin_top=8,
        group_padding=(9, 6, 6, 6),
        control_min_height=26,
        control_padding="1px 9px",
        field_min_height=24,
        tab_min_height=32,
        tab_min_width=100,
        tab_padding="0 8px",
        header_min_height=24,
        table_row_height=22,
        base_font_pt=9.3,
    ),
    ResponsiveMode.ULTRA_COMPACT: ResponsiveMetrics(
        layout_margin=4,
        layout_spacing=3,
        group_margin_top=6,
        group_padding=(6, 4, 4, 4),
        # Control/field heights stop shrinking here (same as COMPACT) —
        # this is a touch panel, and 26/24px is already a modest floor for
        # a tap target; further space comes from margins/spacing/font
        # instead, per the task's stated priority order.
        control_min_height=26,
        control_padding="1px 7px",
        field_min_height=24,
        tab_min_height=30,
        tab_min_width=84,
        tab_padding="0 6px",
        header_min_height=22,
        table_row_height=20,
        base_font_pt=MIN_FONT_PT,
    ),
}


def apply_layout_spacing(layout, spacing: int) -> None:
    """Recursively set `spacing` on `layout` and every layout reachable from it.

    Reaches nested layouts added via addLayout() *and* layouts owned by
    child widgets (e.g. a QGroupBox's own internal QGridLayout, or a
    QStackedWidget's pages) — the two ways this app's tabs actually nest
    layouts — so one call from a tab's own root layout densifies every row
    in it without each tab having to name and wire up every nested row
    individually.

    Deliberately spacing-only, not margins: a nested layout's margins are
    frequently set to (0, 0, 0, 0) on purpose (e.g. a QStackedWidget page
    matching its parent's own padding instead of doubling it), and blindly
    overwriting that would visually break those pages. Each tab sets its
    own single root layout's margins directly instead.
    """
    if layout is None:
        return
    layout.setSpacing(spacing)
    for index in range(layout.count()):
        item = layout.itemAt(index)
        nested_layout = item.layout()
        if nested_layout is not None:
            apply_layout_spacing(nested_layout, spacing)
            continue
        widget = item.widget()
        if widget is not None:
            apply_layout_spacing(widget.layout(), spacing)
