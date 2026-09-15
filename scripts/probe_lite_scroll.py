"""Production-themed gesture evidence; run from repo root with --lite."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QEvent, QPoint, QTimer, Qt
from PySide6.QtGui import QInputDevice
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScroller
from shiboken6 import getCppPointer
from plcsniffer import app
from plcsniffer.capture import PassiveCaptureService


class Trace(QObject):
    def __init__(self, surfaces):
        super().__init__()
        self.surfaces = surfaces
        self.events = []

    def eventFilter(self, obj, event):
        if event.type() in (
            QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd,
            QEvent.ScrollPrepare, QEvent.Scroll,
        ) and obj in self.surfaces:
            self.events.append([self.surfaces[obj], event.type().name])
        return False


def state(area):
    return dict(viewport=getCppPointer(area.viewport())[0], model=getCppPointer(area.model())[0] if hasattr(area, 'model') else None,
                rows=area.rowCount() if hasattr(area, 'rowCount') else None,
                visible=area.isVisible(), enabled=area.isEnabled(),
                geometry=area.geometry().getRect(), viewport_geometry=area.viewport().geometry().getRect(),
                h=[area.horizontalScrollBar().minimum(), area.horizontalScrollBar().maximum(), area.horizontalScrollBar().value()],
                v=[area.verticalScrollBar().minimum(), area.verticalScrollBar().maximum(), area.verticalScrollBar().value()],
                policies=[area.horizontalScrollBarPolicy().name, area.verticalScrollBarPolicy().name],
                scroller=QScroller.hasScroller(area.viewport()),
                gesture=int(QScroller.grabbedGesture(area.viewport())))


def observe(window):
    window.showNormal()
    window.resize(800, 480)
    QTest.qWait(250)
    areas = dict(sniff=window.passive_tab, message=window.passive_page.message_table,
                 inspect=window.packet_inspector_tab, byte=window.packet_inspector.byte_table,
                 profile=window.profile_tab_widget, profiles=window.profile_tab.profile_list,
                 register=window.profile_tab.register_table)
    trace = Trace({a.viewport(): name for name, a in areas.items()})
    QApplication.instance().installEventFilter(trace)
    output = dict(platform=QApplication.platformName(), style=QApplication.style().objectName(),
                  touch_to_mouse=QApplication.testAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents),
                  devices=[str(d.type()) for d in QInputDevice.devices()], theme=window._responsive_mode.name)
    for index, prefix in enumerate(('sniff', 'inspect', 'profile')):
        window.tabs.setCurrentIndex(index)
        QTest.qWait(150)
        output[prefix] = {n: state(a) for n, a in areas.items() if a.isVisible() or n == 'byte'}
    window.tabs.setCurrentIndex(0)
    parent, child = areas['sniff'], areas['message']
    device = QTest.createTouchDevice()
    results = []
    for populated in (False, True):
        if populated:
            page = window.passive_page
            with patch.object(page.capture_service, 'start_configured') as start, patch.object(page.capture_service, 'start_automatic') as auto:
                page.port_combo.setEditText('TEST')
                page.start_monitor()
                page._on_running_changed(True)
                output['start_called'] = start.called or auto.called
            child.setRowCount(80)
        QTest.qWait(150)
        for native, synthesis in ((False, True), (True, True), (True, False)):
            QApplication.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, synthesis)
            for area in (parent, child):
                QScroller.scroller(area.viewport()).stop()
                area.verticalScrollBar().setValue(0)
            parent.ensureWidgetVisible(child)
            child.verticalScrollBar().setValue(400 if populated else 0)
            QTest.qWait(100)
            before = {n: state(a) for n, a in [('parent', parent), ('child', child)]}
            trace.events.clear()
            vp = child.viewport()
            start = QPoint(100, 60)
            if native:
                QTest.touchEvent(vp, device).press(0, start, vp).commit()
            else:
                QTest.mousePress(vp, Qt.LeftButton, pos=start)
            for dy in range(10, 101, 10):
                point = start + QPoint(0, dy)
                if native:
                    QTest.touchEvent(vp, device).move(0, point, vp).commit()
                else:
                    QTest.mouseMove(vp, point, 20)
                QTest.qWait(20)
            if native:
                QTest.touchEvent(vp, device).release(0, point, vp).commit()
            else:
                QTest.mouseRelease(vp, Qt.LeftButton, pos=point)
            QTest.qWait(350)
            results.append(dict(populated=populated, native=native, synthesis=synthesis, before=before,
                                after={n: state(a) for n, a in [('parent', parent), ('child', child)]}, events=trace.events[:]))
    output['drags'] = results
    print(json.dumps(output, indent=2))
    QApplication.instance().removeEventFilter(trace)
    window.close()
    QApplication.quit()


if __name__ == '__main__':
    if '--lite' not in sys.argv:
        raise SystemExit('This probe requires --lite')
    create = app.create_main_window
    def hooked(*args, **kwargs):
        application, window = create(*args, **kwargs)
        assert window._lite
        def run_probe():
            try:
                observe(window)
            except Exception:
                traceback.print_exc()
                application.exit(1)
        QTimer.singleShot(0, run_probe)
        return application, window
    with patch.object(app, 'create_main_window', hooked), patch.object(PassiveCaptureService, 'available_ports', return_value=[]):
        raise SystemExit(app.run(sys.argv))
