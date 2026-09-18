"""Intent-based input-method eligibility for the Lite window."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPersistentModelIndex, Qt
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QLineEdit,
    QPlainTextEdit, QTextEdit, QWidget,
)
from shiboken6 import isValid


class LiteInputMethodGuard(QObject):
    """Keep keyboard focus and text-input permission separate.

    Qt can change focus before widget event filters run. The query veto is
    therefore always active, including while a combo popup changes focus.
    Pointer events are never consumed; the existing scroll owner handles them.
    """

    @classmethod
    def install(cls, window):
        guard = getattr(window, '_lite_input_guard', None)
        if guard is None:
            guard = cls(window)
            window._lite_input_guard = guard
        return guard

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._editor = None
        self._delegate_request = None
        QApplication.instance().installEventFilter(self)

    def _contains(self, widget):
        while isinstance(widget, QWidget):
            if widget is self._window:
                return True
            widget = widget.parentWidget()
        return False

    @staticmethod
    def _text_editor(widget):
        if not isinstance(widget, QWidget) or not isValid(widget):
            return None
        if isinstance(widget, QComboBox):
            widget = widget.lineEdit() if widget.isEditable() else None
        elif isinstance(widget, QAbstractSpinBox):
            if widget.isReadOnly():
                return None
            widget = widget.findChild(QLineEdit)
        if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
            if widget.isEnabled() and not widget.isReadOnly():
                return widget
        return None

    def _target_editor(self, target):
        # A compound control's frame/arrow is distinct from its line editor.
        if isinstance(target, (QComboBox, QAbstractSpinBox)):
            return None
        parent = target.parentWidget()
        if isinstance(parent, (QPlainTextEdit, QTextEdit)) and target is parent.viewport():
            target = parent
        return self._text_editor(target)

    def _accepts(self, widget):
        editor = self._text_editor(widget)
        if editor is None or not editor.isVisible():
            return False
        parent = editor.parentWidget()
        while parent is not None and parent is not self._window:
            if isinstance(parent, QAbstractItemView):
                index = parent.indexAt(editor.mapTo(parent.viewport(), editor.rect().center()))
                if (not index.isValid() or not index.flags() & Qt.ItemIsEditable
                        or (parent.editTriggers() == QAbstractItemView.NoEditTriggers
                            and not parent.isPersistentEditorOpen(index))):
                    return False
                break
            parent = parent.parentWidget()
        if editor is self._editor:
            return True
        if self._delegate_request is not None:
            view, index = self._delegate_request
            if (isValid(view) and index.isValid()
                    and view.currentIndex() == index
                    and view.state() == QAbstractItemView.EditingState
                    and view.viewport().isAncestorOf(editor)
                    and index.flags() & Qt.ItemIsEditable):
                self._editor = editor
                return True
        return False

    def _begin(self, target, kind, position):
        self._editor = self._target_editor(target)
        self._delegate_request = None
        parent = target.parentWidget()
        if (kind == QEvent.MouseButtonDblClick and isinstance(parent, QAbstractItemView)
                and target is parent.viewport()
                and parent.editTriggers() & QAbstractItemView.DoubleClicked):
            index = parent.indexAt(target.mapFromGlobal(position).toPoint())
            if index.isValid() and index.flags() & Qt.ItemIsEditable:
                self._delegate_request = (parent, QPersistentModelIndex(index))
        focus = QApplication.focusWidget()
        if self._editor is None and self._contains(focus) and self._text_editor(focus) is not None:
            focus.clearFocus()
        # Refresh backend acceptance even if the focus object did not change.
        # Qt 6.11.2's separate text-input-v3 defect still needs QTBUG-146211.
        QApplication.inputMethod().update(Qt.ImEnabled)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.InputMethodQuery:
            if (self._contains(watched) and event.queries() & Qt.ImEnabled
                    and not self._accepts(watched)):
                event.setValue(Qt.ImEnabled, False)
                event.accept()
                return True  # Only the eligibility query, never input events.
        elif kind in (QEvent.TouchBegin, QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if kind == QEvent.TouchBegin:
                if len(event.points()) != 1:
                    return False
                position = event.points()[0].globalPosition()
            else:
                if event.button() != Qt.LeftButton:
                    return False
                position = event.globalPosition()
            if isinstance(watched, QWidget):
                # Mouse delivery already identifies the recipient, including
                # propagation from disabled children. Only touch needs a hit test.
                target = watched
                if kind == QEvent.TouchBegin:
                    target = watched.childAt(watched.mapFromGlobal(position).toPoint()) or watched
            elif isinstance(watched, QWindow):
                target = QApplication.widgetAt(position.toPoint())
            else:
                return False
            if self._contains(target):
                self._begin(target, kind, position)
        elif kind == QEvent.KeyPress and self._contains(watched):
            if (isinstance(watched, QAbstractItemView) and event.key() == Qt.Key_F2
                    and watched.editTriggers() & QAbstractItemView.EditKeyPressed):
                self._delegate_request = (watched, QPersistentModelIndex(watched.currentIndex()))
            elif event.text() and self._text_editor(watched) is not None:
                self._editor = self._text_editor(watched)
                QApplication.inputMethod().update(Qt.ImEnabled)
        return False
