"""Input-method behavior with the production Lite theme.

Run: python -B -m pytest -q tests/test_lite_input.py
Qt input-method queries test eligibility, not physical keyboard visibility.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QInputMethodQueryEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDoubleSpinBox, QLineEdit, QPlainTextEdit,
    QPushButton, QStyle, QStyleOptionComboBox, QStyleOptionSpinBox,
    QTableWidgetItem, QTextEdit,
)

from plcsniffer import app
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.ui.lite_input import LiteInputMethodGuard
from plcsniffer.ui.main_window import MainWindow, _build_theme
from plcsniffer.ui.responsive import METRICS

APP = QApplication.instance() or QApplication([])
DEVICE = QTest.createTouchDevice()


def accepts(widget=None):
    widget = widget or APP.focusObject()
    if widget is None:
        return False
    query = QInputMethodQueryEvent(Qt.ImEnabled | Qt.ImHints)
    APP.sendEvent(widget, query)
    return bool(query.value(Qt.ImEnabled))


class DeliveryObserver(QObject):
    def __init__(self):
        super().__init__()
        self.samples = []

    def eventFilter(self, target, event):
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                            QEvent.TouchBegin, QEvent.TouchEnd):
            self.samples.append(accepts())
        return False


class LiteInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mock in (
            patch('plcsniffer.ui.profile_tab.application_data_directory', return_value=Path(self.temp.name)),
            patch.object(PassiveCaptureService, 'available_ports', return_value=[]),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.observer = DeliveryObserver()
        APP.installEventFilter(self.observer)
        self.addCleanup(APP.removeEventFilter, self.observer)
        create = app.create_main_window

        def capture(*args, **kwargs):
            application, self.window = create(*args, **kwargs)
            return application, self.window

        with patch.object(app, 'create_main_window', side_effect=capture), patch.object(QApplication, 'exec', return_value=0):
            self.assertEqual(app.run(['main.py', '--lite']), 0)
        self.window.showNormal()
        self.window.resize(800, 480)
        self.assertTrue(QTest.qWaitForWindowExposed(self.window))
        self.window.activateWindow()
        APP.setActiveWindow(self.window)
        QTest.qWait(180)
        if APP.platformName() == 'wayland':
            self.assertTrue(QTest.qWaitForWindowActive(self.window))
        self.addCleanup(self.close)
        self.page = self.window.passive_page
        self.editor = self.page.port_combo.lineEdit()
        self.assertEqual(self.window.styleSheet(), _build_theme(METRICS[self.window._responsive_mode]))

    def close(self):
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)

    def tap(self, widget, point=None, native=False):
        point = point or widget.rect().center()
        if native:
            QTest.touchEvent(widget, DEVICE).press(0, point, widget).commit()
            QTest.touchEvent(widget, DEVICE).release(0, point, widget).commit()
        else:
            QTest.mouseClick(widget, Qt.LeftButton, pos=point)
        QTest.qWait(25)

    def edit(self):
        self.window.tabs.setCurrentIndex(0)
        self.editor = self.page.port_combo.lineEdit()
        self.window.passive_tab.ensureWidgetVisible(self.editor)
        self.tap(self.editor)
        self.assertTrue(accepts())

    def deny(self, action):
        self.observer.samples.clear()
        focus_samples = []
        slot = lambda obj: focus_samples.append(accepts(obj)) if obj is not None else None
        APP.focusObjectChanged.connect(slot)
        try:
            action()
        finally:
            APP.focusObjectChanged.disconnect(slot)
        self.assertFalse(accepts())
        self.assertNotIn(True, focus_samples)
        self.assertNotIn(True, self.observer.samples)

    def fixture(self, widget):
        widget.setParent(self.page)
        widget.setGeometry(10, 10, 240, 90)
        widget.show()
        widget.raise_()
        return widget

    def test_k1_editable_text_preserves_focus_and_typing(self):
        self.edit()
        QTest.keyClicks(self.editor, 'TEST')
        self.assertIn('TEST', self.editor.text())
        self.tap(self.editor)
        self.assertTrue(accepts())
        self.assertIs(APP.focusWidget(), self.page.port_combo)

    def test_k2_dynamic_readonly_profile_name(self):
        self.window.tabs.setCurrentIndex(2)
        page = self.window.profile_tab
        page.add_profile()
        self.window.profile_tab_widget.ensureWidgetVisible(page.profile_name_edit)
        self.tap(page.profile_name_edit)
        self.assertTrue(accepts())
        page._set_profile_controls_enabled(False)
        self.deny(lambda: self.tap(page.profile_name_edit))

    def test_k3_button_click_and_stale_no_focus_button(self):
        for native in (False, True):
            self.edit()
            with patch.object(self.page, 'refresh_ports'):
                clicks = []
                self.page.refresh_btn.clicked.connect(lambda: clicks.append(True))
                self.deny(lambda: self.tap(self.page.refresh_btn, native=native))
                self.assertTrue(clicks)
        self.edit()
        button = self.fixture(QPushButton('Action'))
        button.setFocusPolicy(Qt.NoFocus)
        self.deny(lambda: self.tap(button))
        self.assertIsNot(APP.focusWidget(), self.page.port_combo)

    def populate(self):
        table = self.page.message_table
        table.setRowCount(80)
        for row in range(80):
            table.setItem(row, 0, QTableWidgetItem(str(row)))
        self.window.passive_tab.ensureWidgetVisible(table)
        QTest.qWait(25)
        return table

    def test_k4_table_selection_never_accepts_input(self):
        table = self.populate()
        for native in (False, True):
            self.edit()
            self.deny(lambda: self.tap(table.viewport(), QPoint(30, 15), native))
            self.assertTrue(table.selectionModel().hasSelection())
            self.assertFalse(table.findChildren(QLineEdit))

    def drag(self, widget, native=False):
        origin = widget.mapToGlobal(QPoint(2, 65))
        if native:
            QTest.touchEvent(widget, DEVICE).press(0, QPoint(2, 65), widget).commit()
        else:
            QTest.mousePress(widget, Qt.LeftButton, pos=QPoint(2, 65))
        for delta in range(10, 101, 10):
            local = widget.mapFromGlobal(origin + QPoint(0, delta))
            if native:
                QTest.touchEvent(widget, DEVICE).move(0, local, widget).commit()
            else:
                QTest.mouseMove(widget, local)
            QTest.qWait(20)
            self.assertFalse(accepts())
        if native:
            QTest.touchEvent(widget, DEVICE).release(0, local, widget).commit()
        else:
            QTest.mouseRelease(widget, Qt.LeftButton, pos=local)
        QTest.qWait(25)

    def test_k5_table_and_page_drag_preserve_scroll_owner(self):
        table = self.populate()
        outer = self.window.passive_tab
        for native in (False, True):
            self.edit()
            outer.verticalScrollBar().setValue(outer.verticalScrollBar().maximum())
            table.verticalScrollBar().setValue(400)
            before = table.verticalScrollBar().value()
            self.deny(lambda: self.drag(table.viewport(), native))
            self.assertLess(table.verticalScrollBar().value(), before)
            self.edit()
            outer.verticalScrollBar().setValue(outer.verticalScrollBar().maximum())
            before = outer.verticalScrollBar().value()
            self.deny(lambda: self.drag(outer.viewport(), native))
            self.assertLess(outer.verticalScrollBar().value(), before)

    def test_k6_combo_editor_can_type(self):
        editor = self.page.baud_combo.lineEdit()
        self.tap(editor)
        self.assertTrue(accepts())
        editor.selectAll()
        QTest.keyClicks(editor, '38400')
        self.assertEqual(self.page.baud_combo.currentText(), '38400')

    def test_k7_combo_arrow_and_popup_selection(self):
        combo = self.page.baud_combo
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        point = combo.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, combo).center()
        self.edit()
        self.deny(lambda: self.tap(combo, point))
        self.assertTrue(combo.view().isVisible())
        self.deny(lambda: QTest.keyClick(combo.view(), Qt.Key_Home))
        self.deny(lambda: QTest.keyClick(combo.view(), Qt.Key_Down))
        self.deny(lambda: QTest.keyClick(combo.view(), Qt.Key_Return))
        self.assertEqual(combo.currentIndex(), 1)
        self.tap(combo.lineEdit())
        self.assertTrue(accepts())

    @unittest.skipIf(APP.platformName() == 'wayland',
                     'QTest pointer warp unsupported by this Wayland compositor; physical tap pending')
    def test_k7_popup_mouse_selection(self):
        combo = self.page.baud_combo
        combo.showPopup()
        index = combo.model().index(1, 0)
        combo.view().scrollTo(index)
        QTest.qWait(25)
        point = combo.view().visualRect(index).center()
        QTest.mouseMove(combo.view().viewport(), point)
        self.deny(lambda: self.tap(combo.view().viewport(), point))
        self.assertEqual(combo.currentIndex(), 1)

    def test_k8_k9_spin_editor_and_step_buttons(self):
        self.page.capture_mode_combo.setCurrentIndex(1)
        for spin in (self.page.auto_window_spin, self.fixture(QDoubleSpinBox())):
            editor = spin.findChild(QLineEdit)
            self.tap(editor)
            self.assertTrue(accepts())
            before = spin.value()
            option = QStyleOptionSpinBox()
            spin.initStyleOption(option)
            point = spin.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxUp, spin).center()
            self.deny(lambda: self.tap(spin, point))
            self.assertGreater(spin.value(), before)

    def test_k10_readonly_text_selection_copy(self):
        for cls in (QPlainTextEdit, QTextEdit):
            view = self.fixture(cls())
            view.setPlainText('Copy this detail')
            view.setReadOnly(True)
            self.edit()
            self.deny(lambda: self.tap(view.viewport()))
            view.selectAll()
            view.copy()
            self.assertEqual(APP.clipboard().text(), 'Copy this detail')
            view.hide()

    def test_k11_k12_register_view_and_real_delegate(self):
        self.window.tabs.setCurrentIndex(2)
        page = self.window.profile_tab
        page.add_profile()
        page.add_register()
        page._set_profile_controls_enabled(False)
        table = page.register_table
        self.window.profile_tab_widget.ensureWidgetVisible(table)
        point = table.visualItemRect(table.item(0, 0)).center()
        self.deny(lambda: self.tap(table.viewport(), point))
        self.assertFalse(table.findChildren(QLineEdit))
        page._set_profile_controls_enabled(True)
        self.deny(lambda: self.tap(table.viewport(), point))
        QTest.mouseDClick(table.viewport(), Qt.LeftButton, pos=point)
        QTest.qWait(25)
        editor = APP.focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        self.assertTrue(accepts())
        item = table.item(0, 0)
        flags = item.flags()
        item.setFlags(flags & ~Qt.ItemIsEditable)
        self.assertFalse(accepts(editor))
        item.setFlags(flags)
        self.assertTrue(accepts(editor))
        editor.selectAll()
        QTest.keyClicks(editor, 'Test register')
        QTest.keyClick(editor, Qt.Key_Return)
        QTest.qWait(30)
        self.assertEqual(table.item(0, 0).text(), 'Test register')

    def test_k13_repeated_sequence_and_install(self):
        table = self.populate()
        guard = self.window._lite_input_guard
        for _ in range(3):
            self.assertIs(LiteInputMethodGuard.install(self.window), guard)
            self.edit()
            self.deny(lambda: self.tap(self.page.refresh_btn))
            self.deny(lambda: self.tap(table.viewport(), QPoint(30, 15)))
            self.deny(lambda: self.drag(self.window.passive_tab.viewport()))
            self.edit()
            self.deny(lambda: self.tap(table.viewport(), QPoint(30, 15)))
        self.assertEqual(len(self.window.findChildren(LiteInputMethodGuard)), 1)
        for _ in range(3):
            with patch.object(self.page.capture_service, '_worker', object()):
                self.page._on_running_changed(True)
                self.assertFalse(accepts(self.page.port_combo))
            self.page._on_running_changed(False)
            self.edit()
            self.assertIs(self.window._lite_input_guard, guard)

    def test_selector_only_checkable_combo(self):
        self.page.capture_mode_combo.setCurrentIndex(1)
        combo = self.page.auto_baud_combo
        self.tap(combo.lineEdit())
        self.assertFalse(accepts(combo))
        combo.showPopup()
        view = combo.view()
        point = view.visualRect(combo.model().index(0, 0)).center()
        self.deny(lambda: self.tap(view.viewport(), point))
        self.assertTrue(combo.checked_values())
        combo.hidePopup()

    def test_full_mode_native_queries_and_focus_are_unchanged(self):
        full = MainWindow(lite=False)
        try:
            full.show()
            self.assertTrue(QTest.qWaitForWindowExposed(full))
            full.activateWindow()
            APP.setActiveWindow(full)
            QTest.qWait(180)
            if APP.platformName() == 'wayland':
                self.assertTrue(QTest.qWaitForWindowActive(full))
            self.assertFalse(hasattr(full, '_lite_input_guard'))
            editor = full.passive_page.port_combo.lineEdit()
            editor.setFocus()
            self.assertTrue(accepts())
            self.assertTrue(full.logging_page.log_view.isReadOnly())
            self.assertFalse(accepts(full.logging_page.log_view))
            self.assertEqual(full.tabs.count(), 4)
        finally:
            full.close()
            full.deleteLater()
            APP.sendPostedEvents(None, QEvent.DeferredDelete)
