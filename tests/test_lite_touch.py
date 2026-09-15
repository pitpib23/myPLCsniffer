"""Behavior tests through app.run(--lite), with the real production theme.

Run: python -m tests.test_lite_touch --lite -v
No offscreen platform or alternate stylesheet is selected by this module.
Serial workers are replaced at the service boundary, never started on hardware.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QScroller, QTableWidgetItem, QWidget
from shiboken6 import getCppPointer

from plcsniffer import app
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.modbus import CapturedModbusFrame
from plcsniffer.ui.lite_scroll import LiteScrollOwner
from plcsniffer.ui.main_window import MainWindow, _build_theme
from plcsniffer.ui.responsive import METRICS


APP = QApplication.instance() or QApplication([])
DEVICE = QTest.createTouchDevice()


class LiteTouchTests(unittest.TestCase):
    def setUp(self):
        if '--lite' not in sys.argv:
            self.fail('Run this suite with --lite')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ports = patch.object(PassiveCaptureService, 'available_ports', return_value=[])
        self.ports.start()
        self.addCleanup(self.ports.stop)
        self.data = patch('plcsniffer.ui.profile_tab.application_data_directory', return_value=Path(self.temp.name))
        self.data.start()
        self.addCleanup(self.data.stop)
        create = app.create_main_window
        def capture(*args, **kwargs):
            application, self.window = create(*args, **kwargs)
            return application, self.window
        with patch.object(app, 'create_main_window', capture), patch.object(QApplication, 'exec', return_value=0):
            self.assertEqual(app.run(['main.py', '--lite']), 0)
        self.addCleanup(self.close)
        self.window.showNormal()
        self.window.resize(800, 480)
        QTest.qWait(180)
        self.assertTrue(self.window._lite)
        self.assertEqual(self.window.styleSheet(), _build_theme(METRICS[self.window._responsive_mode]))
        self.qt_errors = []
        hook = patch.object(sys, 'excepthook', lambda kind, value, tb: self.qt_errors.append(str(value)))
        hook.start()
        self.addCleanup(hook.stop)
        self.parent = self.window.passive_tab
        self.table = self.window.passive_page.message_table
        self.route = self.window._lite_scroll_owner
        self.synthesis = APP.testAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents)
        self.addCleanup(APP.setAttribute, Qt.AA_SynthesizeMouseForUnhandledTouchEvents, self.synthesis)
        APP.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, False)

    def close(self):
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)

    def tearDown(self):
        self.assertEqual(self.qt_errors, [], 'Exception in a Qt event callback')

    def prepare(self, rows=0, child_value=400):
        self.route._cancel()
        self.table.setRowCount(rows)
        QTest.qWait(35)
        self.parent.verticalScrollBar().setValue(self.parent.verticalScrollBar().maximum())
        self.table.verticalScrollBar().setValue(child_value)
        QTest.qWait(35)

    def press(self, widget, native=False, start=QPoint(100, 65)):
        self.widget, self.native = widget, native
        self.origin = widget.mapToGlobal(start)
        self.delta = QPoint()
        if native:
            QTest.touchEvent(widget, DEVICE).press(0, start, widget).commit()
        else:
            QTest.mousePress(widget, Qt.LeftButton, pos=start)

    def move(self, delta, delay=20):
        self.delta = delta
        local = self.widget.mapFromGlobal(self.origin + delta)
        if self.native:
            QTest.touchEvent(self.widget, DEVICE).move(0, local, self.widget).commit()
        else:
            QTest.mouseMove(self.widget, local)
        QTest.qWait(delay)

    def release(self):
        local = self.widget.mapFromGlobal(self.origin + self.delta)
        if self.native:
            QTest.touchEvent(self.widget, DEVICE).release(0, local, self.widget).commit()
        else:
            QTest.mouseRelease(self.widget, Qt.LeftButton, pos=local)
        QTest.qWait(25)

    def drag(self, widget, native=False, horizontal=False, start=QPoint(100, 65)):
        self.press(widget, native, start)
        for distance in range(10, 101, 10):
            self.move(QPoint(distance, 0) if horizontal else QPoint(0, distance))
        self.release()

    def test_s1_outer_page_before_start(self):
        for native in (False, True):
            with self.subTest(native=native):
                self.prepare()
                initial = self.parent.verticalScrollBar().value()
                self.assertGreater(initial, 0)
                self.drag(self.parent.viewport(), native, start=QPoint(2, 65))
                self.assertLess(self.parent.verticalScrollBar().value(), initial)

    def test_s2_empty_child_uses_parent_with_native_synthesis_on_and_off(self):
        for native, synthesis in ((False, False), (True, False), (True, True)):
            with self.subTest(native=native, synthesis=synthesis):
                APP.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, synthesis)
                self.prepare()
                initial = self.parent.verticalScrollBar().value()
                self.assertEqual(self.table.verticalScrollBar().maximum(), 0)
                self.drag(self.table.viewport(), native)
                self.assertLess(self.parent.verticalScrollBar().value(), initial)
                self.assertEqual(self.table.verticalScrollBar().value(), 0)

    def start_capture(self):
        page = self.window.passive_page
        service = page.capture_service
        def started(*args):
            service._worker = object()
            service.running_changed.emit(True)
        def stopped():
            service._worker = None
            service.running_changed.emit(False)
        page.port_combo.setEditText('TEST')
        with patch.object(service, 'start_configured', side_effect=started), patch.object(service, 'start_automatic', side_effect=started):
            page.start_monitor()
        self.addCleanup(setattr, service, '_worker', None)
        return patch.object(service, 'stop', side_effect=stopped)

    def test_s3_start_and_received_frames_leave_only_child_moving(self):
        model = getCppPointer(self.table.model())
        viewport = getCppPointer(self.table.viewport())
        self.start_capture()
        page = self.window.passive_page
        for index in range(80):
            page._on_frame(CapturedModbusFrame(
                timestamp=float(index), direction='Master to Slave', slave_id=1,
                function_code=3, function_name='Read Holding Registers',
                frame_type='Request', address=index, quantity=1, values=(),
                raw=b'\x01\x03\x00\x00\x00\x01\x84\x0a', description='test',
            ))
        for native in (False, True):
            self.prepare(80)
            parent = self.parent.verticalScrollBar().value()
            child = self.table.verticalScrollBar().value()
            self.drag(self.table.viewport(), native)
            self.assertLess(self.table.verticalScrollBar().value(), child)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
        self.assertEqual(getCppPointer(self.table.model()), model)
        self.assertEqual(getCppPointer(self.table.viewport()), viewport)

    def test_s4_boundary_and_reversal_never_handoff(self):
        for native in (False, True):
            self.prepare(80, child_value=25)
            parent = self.parent.verticalScrollBar().value()
            self.press(self.table.viewport(), native)
            for distance in range(10, 151, 10):
                self.move(QPoint(0, distance))
                self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.assertEqual(self.table.verticalScrollBar().value(), 0)
            self.move(QPoint(0, 70))
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.release()
            QTest.qWait(180)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)

    def test_s5_horizontal_child_owns_axis(self):
        self.window.passive_page.setMinimumWidth(1100)
        for native in (False, True):
            self.prepare(80)
            self.table.setColumnWidth(7, 1200)
            QTest.qWait(30)
            self.table.horizontalScrollBar().setValue(300)
            self.parent.horizontalScrollBar().setValue(200)
            parent = self.parent.verticalScrollBar().value()
            child_v = self.table.verticalScrollBar().value()
            self.assertGreater(self.table.horizontalScrollBar().maximum(), 300)
            self.assertGreater(self.parent.horizontalScrollBar().maximum(), 200)
            self.drag(self.table.viewport(), native, horizontal=True, start=QPoint(300, 65))
            self.assertLess(self.table.horizontalScrollBar().value(), 300)
            self.assertEqual(self.table.verticalScrollBar().value(), child_v)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.assertEqual(self.parent.horizontalScrollBar().value(), 200)

    def test_s6_horizontal_only_child_does_not_trap_vertical(self):
        self.table.setColumnWidth(7, 1200)
        for native in (False, True):
            self.prepare()
            self.table.horizontalScrollBar().setValue(200)
            self.assertGreater(self.table.horizontalScrollBar().maximum(), 0)
            initial = self.parent.verticalScrollBar().value()
            self.drag(self.table.viewport(), native)
            self.assertLess(self.parent.verticalScrollBar().value(), initial)
            self.assertEqual(self.table.horizontalScrollBar().value(), 200)

    def test_s6_vertical_only_child_does_not_trap_horizontal(self):
        self.window.passive_page.setMinimumWidth(1100)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for native in (False, True):
            self.prepare(80)
            self.parent.horizontalScrollBar().setValue(200)
            self.assertGreater(self.parent.horizontalScrollBar().maximum(), 0)
            self.drag(self.table.viewport(), native, horizontal=True, start=QPoint(300, 65))
            self.assertLess(self.parent.horizontalScrollBar().value(), 200)
            self.assertEqual(self.table.verticalScrollBar().value(), 400)

    def test_s7_tap_and_subthreshold_motion_select(self):
        for native in (False, True):
            self.prepare(40, child_value=0)
            self.table.setItem(1, 0, QTableWidgetItem('select me'))
            self.table.clearSelection()
            point = self.table.visualItemRect(self.table.item(1, 0)).center()
            parent = self.parent.verticalScrollBar().value()
            self.press(self.table.viewport(), native, point)
            self.move(QPoint(1, 1))
            self.release()
            self.assertTrue(self.table.item(1, 0).isSelected())
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.assertEqual(self.table.verticalScrollBar().value(), 0)

    def test_s8_flick_inertia_stays_in_child(self):
        for native in (False, True):
            self.prepare(150, child_value=1200)
            parent = self.parent.verticalScrollBar().value()
            self.press(self.table.viewport(), native)
            for distance in range(10, 101, 10):
                self.move(QPoint(0, distance), delay=10)
            self.release()
            scroller = QScroller.scroller(self.table.viewport())
            self.assertEqual(scroller.state(), QScroller.Scrolling)
            released = self.table.verticalScrollBar().value()
            QTest.qWait(120)
            self.assertLess(self.table.verticalScrollBar().value(), released)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)

    def test_s9_register_remains_parent_scrolled(self):
        self.window.tabs.setCurrentIndex(2)
        self.parent = self.window.profile_tab_widget
        self.table = self.window.profile_tab.register_table
        self.table.setRowCount(30)
        self.window.profile_tab._fit_register_table_to_content()
        for native in (False, True):
            self.prepare(30)
            self.assertEqual(self.table.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(self.table.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(self.table.minimumHeight(), self.table.maximumHeight())
            self.assertEqual(self.table.minimumWidth(), self.table.maximumWidth())
            before = self.parent.verticalScrollBar().value()
            # Bottom of the fully expanded table is visible at the page bottom.
            self.press(self.table.viewport(), native, QPoint(100, self.table.viewport().height() - 160))
            for distance in range(10, 101, 10):
                self.move(QPoint(0, distance))
            self.release()
            self.assertLess(self.parent.verticalScrollBar().value(), before)
            self.assertFalse(QScroller.hasScroller(self.table.viewport()))

    def test_s10_repeated_start_stop_and_install(self):
        viewport = getCppPointer(self.table.viewport())
        model = getCppPointer(self.table.model())
        for _ in range(3):
            stop = self.start_capture()
            self.prepare(80)
            self.drag(self.table.viewport())
            self.assertIs(LiteScrollOwner.install(self.window, self.route._pages), self.route)
            with stop:
                self.window.passive_page.stop_monitor()
            self.prepare()
            before = self.parent.verticalScrollBar().value()
            self.drag(self.table.viewport(), True)
            self.assertLess(self.parent.verticalScrollBar().value(), before)
            self.assertIsNone(self.route._receiver)
            self.assertEqual(getCppPointer(self.table.viewport()), viewport)
            self.assertEqual(getCppPointer(self.table.model()), model)

    def test_byte_table_both_axes_and_zero_range(self):
        self.window.tabs.setCurrentIndex(1)
        self.window.packet_inspector.all_information_group.show()
        self.parent = self.window.packet_inspector_tab
        self.table = self.window.packet_inspector.byte_table
        self.table.setColumnWidth(4, 1000)
        for native in (False, True):
            self.prepare(80)
            parent = self.parent.verticalScrollBar().value()
            self.drag(self.table.viewport(), native)
            self.assertLess(self.table.verticalScrollBar().value(), 400)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.route._cancel()
            self.table.horizontalScrollBar().setValue(200)
            self.drag(self.table.viewport(), native, horizontal=True)
            self.assertLess(self.table.horizontalScrollBar().value(), 200)
            self.assertEqual(self.parent.verticalScrollBar().value(), parent)
            self.prepare()
            parent = self.parent.verticalScrollBar().value()
            self.drag(self.table.viewport(), native)
            self.assertLess(self.parent.verticalScrollBar().value(), parent)

    def test_profile_list_scroll_and_tap(self):
        tab = self.window.profile_tab
        self.window.tabs.setCurrentIndex(2)
        tab._set_sidebar_visible(True, user_initiated=True)
        table = tab.profile_list
        table.addItems([f'Profile {i}' for i in range(80)])
        QTest.qWait(100)
        parent = self.window.profile_tab_widget
        parent.ensureWidgetVisible(table)
        for native in (False, True):
            self.route._cancel()
            table.verticalScrollBar().setValue(400)
            before = parent.verticalScrollBar().value()
            self.drag(table.viewport(), native)
            self.assertLess(table.verticalScrollBar().value(), 400)
            self.assertEqual(parent.verticalScrollBar().value(), before)
            self.route._cancel()
            table.verticalScrollBar().setValue(0)
            self.press(table.viewport(), native, table.visualItemRect(table.item(1)).center())
            self.release()
            self.assertEqual(table.currentRow(), 1)

    def test_axis_does_not_switch_after_diagonal_threshold(self):
        self.prepare(80)
        self.table.setColumnWidth(7, 1200)
        QTest.qWait(30)
        self.table.horizontalScrollBar().setValue(200)
        self.press(self.table.viewport(), True)
        self.move(QPoint(3, 20))
        self.move(QPoint(90, 30))
        self.move(QPoint(150, 50))
        self.release()
        self.assertEqual(self.table.horizontalScrollBar().value(), 200)
        self.assertLess(self.table.verticalScrollBar().value(), 400)

    def test_wheel_and_scrollbar_still_work(self):
        self.prepare(80)
        viewport = self.table.viewport()
        position = QPointF(100, 100)
        wheel = QWheelEvent(position, viewport.mapToGlobal(position), QPoint(), QPoint(0, -120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(viewport, wheel)
        self.assertGreater(self.table.verticalScrollBar().value(), 400)
        bar = self.table.verticalScrollBar()
        before = bar.value()
        QTest.mouseClick(bar, Qt.LeftButton, pos=QPoint(bar.width() // 2, bar.height() - 5))
        self.assertGreater(bar.value(), before)

    def test_replaced_viewport_gets_current_owner(self):
        self.table.setViewport(QWidget())
        self.prepare(80)
        parent = self.parent.verticalScrollBar().value()
        self.drag(self.table.viewport(), True)
        self.assertLess(self.table.verticalScrollBar().value(), 400)
        self.assertEqual(self.parent.verticalScrollBar().value(), parent)
        self.assertIs(QScroller.scroller(self.table.viewport()).target(), self.table.viewport())

    def test_cancel_and_tab_change_clear_owner(self):
        self.prepare(80)
        self.press(self.table.viewport(), True)
        self.move(QPoint(0, 40))
        QApplication.sendEvent(self.table.viewport(), QEvent(QEvent.TouchCancel))
        self.assertIsNone(self.route._receiver)
        self.assertEqual(QScroller.scroller(self.table.viewport()).state(), QScroller.Inactive)
        self.release()
        self.press(self.table.viewport())
        self.move(QPoint(0, 40))
        self.window.tabs.setCurrentIndex(1)
        self.assertIsNone(self.route._receiver)

    def test_disabled_child_is_ineligible_and_disabling_owner_cancels(self):
        self.prepare(80)
        self.press(self.table.viewport(), True)
        self.move(QPoint(0, 40))
        self.table.setEnabled(False)
        self.assertIsNone(self.route._receiver)
        self.assertEqual(QScroller.scroller(self.table.viewport()).state(), QScroller.Inactive)
        self.release()
        self.parent.verticalScrollBar().setValue(self.parent.verticalScrollBar().maximum())
        before = self.parent.verticalScrollBar().value()
        self.drag(self.table.viewport(), True)
        self.assertLess(self.parent.verticalScrollBar().value(), before)

    def test_native_tap_clicks_once_and_drag_cancels_button(self):
        button = QPushButton('Touch test', self.window.passive_page)
        button.setGeometry(20, 410, 160, 50)
        button.show()
        clicks = []
        button.clicked.connect(lambda: clicks.append(True))
        self.prepare()
        self.press(button, True, QPoint(40, 20))
        self.release()
        self.assertEqual(clicks, [True])
        self.prepare()
        before = self.parent.verticalScrollBar().value()
        self.drag(button, True, start=QPoint(40, 20))
        self.assertEqual(clicks, [True])
        self.assertFalse(button.isDown())
        self.assertLess(self.parent.verticalScrollBar().value(), before)

    def test_native_double_tap_keeps_table_double_click_action(self):
        self.prepare(2, child_value=0)
        self.table.setItem(1, 0, QTableWidgetItem('double tap'))
        double_clicks = []
        self.table.cellDoubleClicked.connect(lambda row, column: double_clicks.append((row, column)))
        point = self.table.visualItemRect(self.table.item(1, 0)).center()
        for _ in range(2):
            self.press(self.table.viewport(), True, point)
            self.release()
        self.assertEqual(double_clicks, [(1, 0)])

    def test_synthesized_mouse_during_touch_does_not_start_second_drag(self):
        self.prepare(80)
        parent = self.parent.verticalScrollBar().value()
        self.press(self.table.viewport(), True)
        self.move(QPoint(0, 30))
        viewport = self.parent.viewport()
        point = QPointF(20, 40)
        for kind, delta in ((QEvent.MouseButtonPress, 0), (QEvent.MouseMove, 80), (QEvent.MouseButtonRelease, 80)):
            position = point + QPointF(0, delta)
            event = QMouseEvent(kind, position, viewport.mapTo(self.window, position), viewport.mapToGlobal(position),
                                Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton,
                                Qt.NoButton if kind == QEvent.MouseButtonRelease else Qt.LeftButton,
                                Qt.NoModifier, Qt.MouseEventSynthesizedBySystem)
            QApplication.sendEvent(viewport, event)
        self.move(QPoint(0, 90))
        self.release()
        self.assertLess(self.table.verticalScrollBar().value(), 400)
        self.assertEqual(self.parent.verticalScrollBar().value(), parent)

    def test_full_negative_control_has_no_input_owner_or_touch_changes(self):
        # Full construction is a regression control inside this --lite test
        # process, never a substitute for launching/testing the Lite app.
        with patch.object(LiteScrollOwner, 'install') as install:
            full = MainWindow(lite=False)
        try:
            QTest.qWait(180)  # Let the existing Full-only label timers finish.
            install.assert_not_called()
            self.assertFalse(hasattr(full, '_lite_scroll_owner'))
            for area in (full.passive_tab, full.packet_inspector_tab, full.profile_tab_widget,
                         full.passive_page.message_table, full.packet_inspector.byte_table,
                         full.profile_tab.profile_list, full.profile_tab.register_table):
                self.assertFalse(QScroller.hasScroller(area.viewport()))
                self.assertFalse(area.viewport().testAttribute(Qt.WA_AcceptTouchEvents))
            self.assertEqual(full.packet_inspector.byte_table.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(full.tabs.count(), 4)
        finally:
            full.close()
            full.deleteLater()
            APP.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == '__main__':
    if '--lite' not in sys.argv:
        raise SystemExit('This suite requires --lite')
    unittest.main(argv=[arg for arg in sys.argv if arg != '--lite'],
                  testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
