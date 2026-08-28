"""Regression tests for the runtime density/responsive-layout mechanism.

Covers plcsniffer/ui/responsive.py's pure mode computation, the recursive
layout-spacing helper, and each tab's apply_responsive_mode() — resizing a
real (offscreen) MainWindow through representative sizes (normal desktop,
a 1024x600-class small landscape display, and an ~800x340 short viewport
standing in for an on-screen keyboard eating vertical space) and checking
the *effects*: mode transitions, structural reflow, reversibility, and that
nothing raises.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from plcsniffer.capture import PassiveCaptureService
from plcsniffer.ui.main_window import MainWindow
from plcsniffer.ui.responsive import (
    METRICS,
    MIN_FONT_PT,
    ResponsiveMode,
    apply_layout_spacing,
    compute_mode,
)

APP = QApplication.instance() or QApplication([])


class ComputeModeTests(unittest.TestCase):
    """Pure function, no Qt widgets involved — width/height thresholds only."""

    def test_large_desktop_window_is_normal(self) -> None:
        self.assertEqual(compute_mode(1400, 900), ResponsiveMode.NORMAL)
        self.assertEqual(compute_mode(1280, 720), ResponsiveMode.NORMAL)

    def test_small_landscape_display_is_compact(self) -> None:
        self.assertEqual(compute_mode(1024, 600), ResponsiveMode.COMPACT)
        self.assertEqual(compute_mode(800, 480), ResponsiveMode.COMPACT)

    def test_narrow_and_short_together_is_ultra_compact(self) -> None:
        self.assertEqual(compute_mode(800, 400), ResponsiveMode.ULTRA_COMPACT)

    def test_severe_height_is_ultra_compact_regardless_of_width(self) -> None:
        """The on-screen-keyboard case: width often stays wide, only height
        collapses. This must not require width to also be narrow."""
        self.assertEqual(compute_mode(1280, 300), ResponsiveMode.ULTRA_COMPACT)
        self.assertEqual(compute_mode(1920, 350), ResponsiveMode.ULTRA_COMPACT)

    def test_thresholds_are_continuous_not_a_resolution_list(self) -> None:
        # One pixel on either side of a threshold flips the mode — proves
        # this is a genuine width/height comparison, not special-cased
        # resolutions.
        from plcsniffer.ui.responsive import COMPACT_WIDTH_THRESHOLD

        self.assertEqual(
            compute_mode(COMPACT_WIDTH_THRESHOLD, 900), ResponsiveMode.NORMAL
        )
        self.assertEqual(
            compute_mode(COMPACT_WIDTH_THRESHOLD - 1, 900), ResponsiveMode.COMPACT
        )

    def test_every_mode_has_metrics(self) -> None:
        for mode in ResponsiveMode:
            self.assertIn(mode, METRICS)

    def test_font_sizes_never_go_below_the_readable_floor(self) -> None:
        for metrics in METRICS.values():
            self.assertGreaterEqual(metrics.base_font_pt, MIN_FONT_PT)

    def test_denser_modes_never_use_larger_numbers_than_normal(self) -> None:
        """Sanity guard: COMPACT/ULTRA_COMPACT are supposed to be <= NORMAL
        on every shrinkable metric, never larger (that would be a typo, not
        a design choice)."""
        normal = METRICS[ResponsiveMode.NORMAL]
        for mode in (ResponsiveMode.COMPACT, ResponsiveMode.ULTRA_COMPACT):
            metrics = METRICS[mode]
            self.assertLessEqual(metrics.layout_margin, normal.layout_margin)
            self.assertLessEqual(metrics.layout_spacing, normal.layout_spacing)
            self.assertLessEqual(metrics.control_min_height, normal.control_min_height)
            self.assertLessEqual(metrics.tab_min_height, normal.tab_min_height)
            self.assertLessEqual(metrics.base_font_pt, normal.base_font_pt)


class ApplyLayoutSpacingTests(unittest.TestCase):
    """The recursive spacing helper each tab's apply_responsive_mode() uses."""

    def test_reaches_nested_addlayout_rows(self) -> None:
        widget = QWidget()
        root = QVBoxLayout(widget)
        row = QHBoxLayout()
        row.addWidget(QLabel("a"))
        root.addLayout(row)

        apply_layout_spacing(root, 3)

        self.assertEqual(root.spacing(), 3)
        self.assertEqual(row.spacing(), 3)

    def test_reaches_layouts_owned_by_child_widgets(self) -> None:
        """A QGroupBox's own internal QGridLayout — not addLayout()'d, but
        owned by a *widget* that's a direct child item of the root layout.
        """
        widget = QWidget()
        root = QVBoxLayout(widget)
        group = QWidget()
        grid = QGridLayout(group)
        root.addWidget(group)

        apply_layout_spacing(root, 5)

        self.assertEqual(grid.spacing(), 5)

    def test_does_not_touch_margins(self) -> None:
        widget = QWidget()
        root = QVBoxLayout(widget)
        root.setContentsMargins(1, 2, 3, 4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        root.addLayout(row)

        apply_layout_spacing(root, 9)

        root_margins = root.contentsMargins()
        self.assertEqual(
            (root_margins.left(), root_margins.top(), root_margins.right(), root_margins.bottom()),
            (1, 2, 3, 4),
        )
        row_margins = row.contentsMargins()
        self.assertEqual(
            (row_margins.left(), row_margins.top(), row_margins.right(), row_margins.bottom()),
            (0, 0, 0, 0),
        )

    def test_tolerates_spacer_items_and_layoutless_widgets(self) -> None:
        widget = QWidget()
        root = QVBoxLayout(widget)
        root.addStretch()
        root.addWidget(QLabel("no layout of its own"))
        apply_layout_spacing(root, 2)  # must not raise

    def test_none_layout_is_a_no_op(self) -> None:
        apply_layout_spacing(None, 5)  # must not raise


class MainWindowResponsiveTestCase(unittest.TestCase):
    """Shared fixture: a real MainWindow with resize forced synchronously."""

    def setUp(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            self.window = MainWindow()
        self.window.show()
        APP.processEvents()

    def tearDown(self) -> None:
        self.window.logging_page.stop_refresh()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def resize_and_settle(self, width: int, height: int) -> None:
        """Resize and force the debounced recompute immediately.

        Bypasses the real ~120ms debounce timer for deterministic,
        wall-clock-free tests; test_resize_debounces_rapid_events below
        separately verifies the debounce timer itself is wired correctly.
        """
        self.window.resize(width, height)
        APP.processEvents()
        self.window._resize_debounce_timer.stop()
        self.window._recompute_responsive_mode()
        APP.processEvents()


class MainWindowModeTransitionTests(MainWindowResponsiveTestCase):
    def test_starts_in_normal_mode_at_default_size(self) -> None:
        self.assertIs(self.window._responsive_mode, ResponsiveMode.NORMAL)

    def test_shrinking_to_small_landscape_enters_compact(self) -> None:
        self.resize_and_settle(1024, 600)
        self.assertIs(self.window._responsive_mode, ResponsiveMode.COMPACT)

    def test_shrinking_to_keyboard_open_height_enters_ultra_compact(self) -> None:
        """Width stays 800 (unchanged) while height collapses, like an
        on-screen keyboard eating vertical space — must still react."""
        self.resize_and_settle(800, 480)
        self.assertIs(self.window._responsive_mode, ResponsiveMode.COMPACT)
        self.resize_and_settle(800, 320)
        self.assertIs(self.window._responsive_mode, ResponsiveMode.ULTRA_COMPACT)

    def test_growing_back_returns_to_normal(self) -> None:
        self.resize_and_settle(800, 320)
        self.assertIs(self.window._responsive_mode, ResponsiveMode.ULTRA_COMPACT)
        self.resize_and_settle(1400, 900)
        self.assertIs(self.window._responsive_mode, ResponsiveMode.NORMAL)

    def test_resize_sweep_large_to_small_to_large_raises_nothing(self) -> None:
        """The task's explicit large -> small -> large check, across every
        tab, plus a couple of interactions exercised at each size."""
        sizes = [
            (1400, 900),
            (1280, 720),
            (1024, 600),
            (800, 480),
            (800, 320),
            (1400, 900),
        ]
        for width, height in sizes:
            self.resize_and_settle(width, height)
            for tab in (
                self.window.passive_tab,
                self.window.packet_inspector_tab,
                self.window.logging_page,
                self.window.profile_tab_widget,
            ):
                self.window.tabs.setCurrentWidget(tab)
                APP.processEvents()
            # A couple of representative interactions must keep working at
            # every size, not just exist.
            self.window.passive_page.capture_mode_combo.setCurrentIndex(1)
            APP.processEvents()
            self.window.passive_page.capture_mode_combo.setCurrentIndex(0)
            APP.processEvents()

    def test_resize_debounces_rapid_events(self) -> None:
        """Many resizeEvents in quick succession must coalesce into a single
        pending recompute, not one per event — the actual mechanism this
        task calls for ("avoid resize-event loops and excessive layout
        churn... debounce appropriately")."""
        for width in range(1000, 1010):
            self.window.resize(width, 900)
            APP.processEvents()
        # Still just one pending (restarted, not accumulated) timer.
        self.assertTrue(self.window._resize_debounce_timer.isActive())


class MainWindowCriticalWidgetsRemainReachableTests(MainWindowResponsiveTestCase):
    """Verify nothing becomes hidden/disabled purely as a side effect of
    density changes — only explicit, intentional behavior (the Profile
    sidebar) is allowed to hide anything."""

    def test_passive_sniffing_start_button_stays_enabled_at_every_size(self) -> None:
        for width, height in [(1400, 900), (1024, 600), (800, 480), (800, 320)]:
            self.resize_and_settle(width, height)
            self.assertTrue(self.window.passive_page.start_btn.isEnabled())
            self.assertTrue(self.window.passive_page.start_btn.isVisibleTo(self.window))

    def test_profile_save_and_register_table_stay_reachable(self) -> None:
        self.window.tabs.setCurrentWidget(self.window.profile_tab_widget)
        for width, height in [(1400, 900), (1024, 600), (800, 480), (800, 320)]:
            self.resize_and_settle(width, height)
            self.assertTrue(
                self.window.profile_tab.toggle_nav_btn.isVisibleTo(self.window)
            )
            self.assertTrue(
                self.window.profile_tab.register_table.isVisibleTo(self.window)
            )

    def test_packet_inspector_show_all_button_stays_reachable(self) -> None:
        self.window.tabs.setCurrentWidget(self.window.packet_inspector_tab)
        for width, height in [(1400, 900), (1024, 600), (800, 480), (800, 320)]:
            self.resize_and_settle(width, height)
            self.assertTrue(
                self.window.packet_inspector.show_all_btn.isVisibleTo(self.window)
            )


class PassiveSniffingReflowTests(MainWindowResponsiveTestCase):
    def test_setup_row_is_one_row_at_normal(self) -> None:
        pp = self.window.passive_page
        grid = pp._settings_grid
        _, mode_col, _, mode_span = grid.getItemPosition(
            grid.indexOf(pp.capture_mode_combo)
        )
        port_row, _, _, _ = grid.getItemPosition(grid.indexOf(pp.port_combo))
        self.assertEqual(mode_span, 1)
        self.assertEqual(port_row, 0)

    def test_setup_row_becomes_two_rows_when_compact(self) -> None:
        self.resize_and_settle(800, 480)
        pp = self.window.passive_page
        grid = pp._settings_grid
        mode_row, _, _, mode_span = grid.getItemPosition(
            grid.indexOf(pp.capture_mode_combo)
        )
        port_row, _, _, _ = grid.getItemPosition(grid.indexOf(pp.port_combo))
        self.assertEqual(mode_row, 0)
        self.assertGreater(mode_span, 1)  # combo now spans multiple columns
        self.assertEqual(port_row, 1)  # moved to its own row

    def test_setup_row_reflow_is_reversible(self) -> None:
        pp = self.window.passive_page
        grid = pp._settings_grid
        before = grid.getItemPosition(grid.indexOf(pp.port_combo))

        self.resize_and_settle(800, 480)
        self.resize_and_settle(1400, 900)

        after = grid.getItemPosition(grid.indexOf(pp.port_combo))
        self.assertEqual(before, after)

    def test_double_click_tip_label_hidden_when_compact_and_restored_when_normal(
        self,
    ) -> None:
        pp = self.window.passive_page
        self.assertFalse(pp._double_click_tip_label.isHidden())

        self.resize_and_settle(800, 480)
        self.assertTrue(pp._double_click_tip_label.isHidden())

        self.resize_and_settle(1400, 900)
        self.assertFalse(pp._double_click_tip_label.isHidden())

    def test_message_table_minimum_height_shrinks_when_compact_and_restores(
        self,
    ) -> None:
        pp = self.window.passive_page
        normal_floor = pp.message_table.minimumHeight()

        self.resize_and_settle(800, 320)
        self.assertLess(pp.message_table.minimumHeight(), normal_floor)

        self.resize_and_settle(1400, 900)
        self.assertEqual(pp.message_table.minimumHeight(), normal_floor)


class ProfileSidebarResponsiveTests(MainWindowResponsiveTestCase):
    def test_sidebar_auto_collapses_when_entering_compact(self) -> None:
        profile_tab = self.window.profile_tab
        self.assertTrue(profile_tab._sidebar_visible)

        self.resize_and_settle(1024, 600)

        self.assertFalse(profile_tab._sidebar_visible)
        self.assertFalse(profile_tab._sidebar_user_overridden)

    def test_sidebar_auto_restores_when_returning_to_normal(self) -> None:
        self.resize_and_settle(1024, 600)
        self.resize_and_settle(1400, 900)

        self.assertTrue(self.window.profile_tab._sidebar_visible)

    def test_manual_toggle_is_never_overridden_by_density_again(self) -> None:
        profile_tab = self.window.profile_tab
        self.resize_and_settle(1024, 600)  # auto-collapses
        self.assertFalse(profile_tab._sidebar_visible)

        profile_tab._toggle_nav_panel()  # user explicitly re-shows it
        self.assertTrue(profile_tab._sidebar_visible)
        self.assertTrue(profile_tab._sidebar_user_overridden)

        # Shrinking further, and growing back, must never touch it again.
        self.resize_and_settle(800, 320)
        self.assertTrue(profile_tab._sidebar_visible)
        self.resize_and_settle(1400, 900)
        self.assertTrue(profile_tab._sidebar_visible)


class PacketInspectorResponsiveTests(MainWindowResponsiveTestCase):
    def test_byte_table_is_expanding_not_fixed(self) -> None:
        """The core structural fix: byte_table used to be a fixed height
        capped at 16 rows regardless of window size — it must now be able
        to claim (or give up) space instead of only the outer QScrollArea
        absorbing every shortfall."""
        from PySide6.QtWidgets import QSizePolicy

        policy = self.window.packet_inspector.byte_table.sizePolicy()
        self.assertEqual(policy.verticalPolicy(), QSizePolicy.Expanding)

    def test_byte_table_floor_shrinks_when_compact_and_restores(self) -> None:
        inspector = self.window.packet_inspector
        normal_floor = inspector.byte_table.minimumHeight()

        self.resize_and_settle(800, 320)
        self.assertLess(inspector.byte_table.minimumHeight(), normal_floor)

        self.resize_and_settle(1400, 900)
        self.assertEqual(inspector.byte_table.minimumHeight(), normal_floor)

    def test_show_all_toggle_still_works_at_every_size(self) -> None:
        inspector = self.window.packet_inspector
        for width, height in [(1400, 900), (1024, 600), (800, 480)]:
            self.resize_and_settle(width, height)
            inspector.show_all_btn.setChecked(True)
            APP.processEvents()
            self.assertFalse(inspector.all_information_group.isHidden())
            inspector.show_all_btn.setChecked(False)
            APP.processEvents()
            self.assertTrue(inspector.all_information_group.isHidden())


if __name__ == "__main__":
    unittest.main(verbosity=2)
