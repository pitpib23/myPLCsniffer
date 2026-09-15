"""One input/axis owner for nested Lite pages; QScroller supplies inertia."""
from __future__ import annotations

from time import monotonic

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSlider, QApplication, QHeaderView, QScroller,
    QScrollerProperties, QWidget,
)
from shiboken6 import isValid


class LiteScrollOwner(QObject):
    """Route native touch and mouse drags without nested gesture recognizers.

    Only installed by the Lite shell. Normal mouse presses/releases still
    reach controls; a drag cancels the press just as Qt's scroller recognizer
    does. Accepted native touch uses that same mouse tap path without asking
    the platform to synthesize a second competing gesture.
    """

    @classmethod
    def install(cls, window, pages):
        existing = getattr(window, '_lite_scroll_owner', None)
        if existing is None:
            existing = cls(window, pages)
            window._lite_scroll_owner = existing
        return existing

    def __init__(self, window, pages):
        super().__init__(window)
        self._window = window
        self._pages = tuple(pages)
        self._receiver = None
        self._candidates = []
        self._owner = None
        self._axis = None
        self._native = False
        self._last_tap = None
        self._double_tap = False
        self._sending = False
        self._scrollers = []
        for page in self._pages:
            # Child viewports may consume touch before it reaches the page.
            # No scroller is created on content-fitted register/summary tables.
            for area in (page, *page.findChildren(QAbstractScrollArea)):
                area.viewport().setAttribute(Qt.WA_AcceptTouchEvents)
        QApplication.instance().installEventFilter(self)

    def _areas_at(self, receiver):
        areas = []
        widget = receiver
        while widget is not None:
            if isinstance(widget, QAbstractSlider):
                return []  # Keep scrollbar thumbs and sliders native.
            if isinstance(widget, QAbstractScrollArea) and not isinstance(widget, QHeaderView):
                areas.append(widget)
            if widget in self._pages:
                return areas
            widget = widget.parentWidget()
        return []  # Includes popup menus/combos and other windows.

    def _mouse(self, kind, position):
        receiver = self._receiver
        if receiver is None or not isValid(receiver):
            return
        local = receiver.mapFromGlobal(position)
        event = QMouseEvent(kind, local, position, Qt.LeftButton,
                            Qt.NoButton if kind == QEvent.MouseButtonRelease else Qt.LeftButton,
                            Qt.NoModifier)
        self._sending = True
        try:
            QApplication.sendEvent(receiver, event)
        finally:
            self._sending = False

    def _begin(self, receiver, position, native):
        candidates = self._areas_at(receiver)
        if not candidates:
            return False
        self._stop_scrollers()
        self._receiver = receiver
        self._candidates = candidates
        self._origin = QPointF(position)
        self._started = self._time()
        self._native = native
        self._axis = None
        self._owner = None
        self._double_tap = False
        if native:
            previous = self._last_tap
            hints = QApplication.styleHints()
            self._double_tap = (
                previous is not None and previous[0] is receiver
                and self._started - previous[2] <= hints.mouseDoubleClickInterval()
                and (position - previous[1]).manhattanLength() <= hints.mouseDoubleClickDistance()
            )
            self._mouse(QEvent.MouseButtonDblClick if self._double_tap else QEvent.MouseButtonPress, position)
        self._last_tap = None
        return True

    @staticmethod
    def _time():
        return int(monotonic() * 1000)

    def _position(self, position):
        # Global displacement avoids feedback as the page moves under a finger.
        delta = position - self._origin
        return self._local_origin + (
            QPointF(delta.x(), 0) if self._axis == 'h' else QPointF(0, delta.y())
        )

    def _move(self, position):
        if self._axis is None:
            delta = position - self._origin
            if delta.manhattanLength() < QApplication.startDragDistance():
                return False
            self._axis = 'h' if abs(delta.x()) > abs(delta.y()) else 'v'
            # Cancel any button press/item-view drag before taking over.
            self._mouse(QEvent.MouseButtonRelease, QPointF(-2147483648, -2147483648))
            for area in self._candidates:
                if not isValid(area) or not area.isVisible() or not area.isEnabled():
                    continue
                bar = area.horizontalScrollBar() if self._axis == 'h' else area.verticalScrollBar()
                policy = area.horizontalScrollBarPolicy() if self._axis == 'h' else area.verticalScrollBarPolicy()
                if policy == Qt.ScrollBarAlwaysOff or bar.maximum() <= bar.minimum():
                    continue
                self._owner = QScroller.scroller(area.viewport())
                if self._owner not in self._scrollers:
                    self._scrollers.append(self._owner)
                properties = self._owner.scrollerProperties()
                # The Qt threshold was already checked above. Disable bounce
                # so a boundary remains stationary without involving ancestors.
                properties.setScrollMetric(QScrollerProperties.DragStartDistance, 0.0)
                for metric in (QScrollerProperties.HorizontalOvershootPolicy,
                               QScrollerProperties.VerticalOvershootPolicy):
                    properties.setScrollMetric(metric, QScrollerProperties.OvershootAlwaysOff)
                self._owner.setScrollerProperties(properties)
                self._local_origin = area.viewport().mapFromGlobal(self._origin)
                self._owner.handleInput(QScroller.InputPress, self._local_origin, self._started)
                break
        if self._owner is not None and isValid(self._owner):
            self._owner.handleInput(QScroller.InputMove, self._position(position), self._time())
        return True  # Axis/owner (including no eligible owner) stays fixed.

    def _end(self, position):
        dragged = self._axis is not None
        if self._owner is not None and isValid(self._owner):
            self._owner.handleInput(QScroller.InputRelease, self._position(position), self._time())
        elif self._native and not dragged:
            if not self._double_tap:
                self._last_tap = (self._receiver, QPointF(position), self._time())
            self._mouse(QEvent.MouseButtonRelease, position)
        self._receiver = None
        self._candidates = []
        self._owner = None
        self._axis = None
        self._native = False
        return dragged

    def _stop_scrollers(self):
        self._scrollers = [s for s in self._scrollers if isValid(s)]
        for scroller in self._scrollers:
            scroller.stop()

    def _cancel(self):
        self._mouse(QEvent.MouseButtonRelease, QPointF(-2147483648, -2147483648))
        self._stop_scrollers()
        self._receiver = None
        self._candidates = []
        self._owner = None
        self._axis = None
        self._native = False
        self._last_tap = None

    def eventFilter(self, watched, event):
        if self._sending:
            return False
        kind = event.type()
        if kind == QEvent.WindowDeactivate and watched is self._window:
            self._cancel()
        elif kind == QEvent.Hide and (watched is self._window or watched in self._pages):
            self._cancel()
        elif kind == QEvent.EnabledChange and watched in self._candidates and not watched.isEnabled():
            self._cancel()
        elif kind == QEvent.Wheel and isinstance(watched, QWidget) and self._areas_at(watched):
            self._stop_scrollers()
        elif kind == QEvent.TouchCancel and self._native:
            self._cancel()
            event.accept()
            return True
        elif kind in (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd):
            if kind == QEvent.TouchBegin:
                if not isinstance(watched, QWidget) or len(event.points()) != 1:
                    return False
                position = event.points()[0].globalPosition()
                receiver = watched.childAt(watched.mapFromGlobal(position).toPoint()) or watched
                if not self._begin(receiver, position, True):
                    return False
            elif not self._native:
                return False
            elif len(event.points()) != 1:
                self._cancel()
            elif kind == QEvent.TouchUpdate:
                self._move(event.points()[0].globalPosition())
            else:
                self._end(event.points()[0].globalPosition())
            event.accept()
            return True
        elif kind in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
            if self._native:
                # Some platform drivers supply mouse events alongside touch.
                return event.source() != Qt.MouseEventNotSynthesized
            if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self._receiver is None and isinstance(watched, QWidget):
                    self._begin(watched, event.globalPosition(), False)
            elif self._receiver is not None:
                if kind == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                    return self._move(event.globalPosition())
                if kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                    return self._end(event.globalPosition())
        return False
