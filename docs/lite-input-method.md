# Lite input-method investigation

## Status

The application-side guard is implemented. **The keyboard bug is not accepted
as fixed on this device.** Its installed PySide6/Qt 6.11.2 Wayland backend has
QTBUG-146211, which bypasses widget eligibility when enabling text-input-v3.
Physical zero-flash acceptance remains outstanding.

## Architecture

- `plcsniffer/app.py`: `app.run(['main.py', '--lite'])` constructs QApplication
  and the Lite shell. There was no explicit keyboard implementation or guard.
- `plcsniffer/ui/main_window.py`: `_build_theme(METRICS[mode])` supplies the
  production QSS, including Segoe UI text and focus borders. The diagnostic
  used Fusion on Wayland, with Segoe UI requested at 8.5pt, the normal Qt
  palette, and the responsive production stylesheet. The keyboard change
  adds no palette, font, stylesheet, or dynamic-property overrides.
- `passive_capture.py`: editable port/baud combos, selector-only combos,
  read-only checkable-combo editors, auto-listening spin box, and a message
  table with `NoEditTriggers`. Free-text search is absent in Lite.
- `profile_tab.py`: dynamically read-only profile name, editable sniff-port
  combo, profile list, and register table with dynamic edit triggers and
  per-item editable flags. Register text editors are created by delegates;
  function-code/format/byte-order delegates create non-editable combos.
  No persistent editors were found in production code. Profile search is
  absent in Lite.
- `packet_inspector.py`: display-only byte and detail tables.
- `log_viewer.py`: read-only QPlainTextEdit in Full mode. Lite has no Logging
  tab; tests exercise equivalent read-only text controls under the Lite theme.
- `lite_scroll.py`: existing application event filter routes native touches
  and synthesizes mouse events. This file is unchanged.

## Evidence and root cause

Investigation performed on 2026-09-15 in the existing checkout. The running
desktop processes identify the Control Centre keyboard as Squeekboard and
the compositor as labwc. An unrestricted diagnostic connected to the actual
Wayland desktop, launched via `app.run(... --lite)`, and asserted the exact
production theme. Offscreen results were not used as physical acceptance.

Temporary diagnostics recorded monotonic timestamps, targets, widgetAt hits,
focusWidget, focusObject, focusWindow, widget ImEnabled attributes and queries,
read-only state, table edit triggers, and Qt visibility. No text-input values
were required to classify a target. Both native QTest touch and mouse events
were exercised. No serial workers ran; profile storage was isolated.

Observed sequences before the change:

1. Port line editor tap: focusWidget/focusObject become **QComboBox**, rather
   than its QLineEdit. Both advertise editable input.
2. Refresh button tap: MouseButtonPress -> focusObjectChanged -> FocusOut ->
   FocusIn -> focusChanged -> application mouse event filter. Button queries
   return false, but the Wayland text-input session remains enabled.
3. Populated message-table tap: focus becomes QTableWidget, editTriggers stay
   NoEditTriggers, no delegate editor is created, and ImEnabled queries are
   false. Qt can set WA_InputMethodEnabled true on the table despite this.
4. Baud arrow: focus first becomes the editable QComboBox, then focusWidget
   becomes the popup QListView while focusObject can remain the QComboBox.
   This is an actual stale input target, distinct from normal button/table
   focus transitions. Closing the popup restores combo focus.
5. Native table input includes TouchBegin -> the existing scroll owner's
   synthesized MouseButtonPress. A focusChanged-only fix is too late to
   govern the original interaction.

In a representative post-guard trace, at 3525ms the port editor's event query
and backend acceptance were both true. At 3710ms on Refresh they were both
false; at 4041ms on the message table both were false. Nevertheless the
Wayland trace still contained one enable on surface enter and no disable
during these transitions. Qt's isVisible() stayed false even for text entry;
text-input-v3 provides no reliable visible-panel measurement here.

The exact Qt **v6.11.2** source explains that protocol result:
`QWaylandTextInputv3::enableSurface()` and `disableSurface()` are empty;
`zwp_text_input_v3_enter()` unconditionally calls `enable()`. Therefore
changing focus, rejecting ImEnabled, calling update, or eventually hiding
the panel cannot by themselves meet the zero-flash requirement on this build.

The root cause is corroborated by upstream
[QTBUG-146211's fix](https://github.com/qt/qtbase/commit/8f376c89b21fe554e0b26c497025bce32943a4f7),
which tracks entered and eligible surfaces separately. Compare the
[installed release source](https://github.com/qt/qtbase/blob/v6.11.2/src/plugins/platforms/wayland/qwaylandtextinputv3.cpp).
Reading the moving 6.11 branch alone would miss this installed-release defect.
The package index checked during this task lists 6.11.2 as the newest PySide6;
the available 6.10.3 release has the same empty methods. The compositor only
advertised text-input-v3, so selecting a different protocol was not a viable
local fallback.

These findings disprove stale editor focus as the sole explanation for normal
button/table activation. A corrected backend must produce disable/commit
when non-text targets are selected, and no enable for those interactions.

## Target classification

| Target and current state | Text keyboard permission |
| --- | --- |
| Enabled, writable QLineEdit, QTextEdit, QPlainTextEdit | Allowed after intentional text interaction |
| Disabled or read-only text | Denied; selection/copy remain native |
| Editable port/baud/sniff combo's actual line editor | Allowed |
| Combo frame, dropdown arrow, popup list | Denied |
| Checkable combo's read-only display/editor and popup | Denied |
| Writable spin-box line editor | Allowed |
| Spin-box step controls | Denied |
| Message/byte/detail tables, lists, table/page viewports | Denied |
| Register table in view mode, non-editable cells | Denied |
| Editable register cell with no active editor | Denied |
| Actual text delegate editor for an editable cell | Allowed on activation; eligibility rechecked dynamically |
| Selector-only combo delegate | Denied |
| Buttons, labels, display controls | Denied |

## Implementation and regression protection

`LiteInputMethodGuard` in `plcsniffer/ui/lite_input.py` is installed once per
Lite window, after the scroll owner, and observes application events within
that window (including owned popups). Full windows are outside its scope.

It records the actual editor selected by an interaction. Compound controls
are normalized only where required; mouse delivery already identifies its
recipient, while native touch may identify an enclosing viewport. Repeating
childAt hit tests during mouse propagation caused a PySide wrapper lifetime
regression for disabled tables in the draft and was removed.

An always-active input-method query veto prevents focus changes from granting
text permission to buttons, tables, combo arrows, or stale editors. It also
rejects automatic profile-name focus caused by clicking an edit-mode button,
until the user interacts with the name field. Real delegate editors and their
current item flags are checked separately from table viewports.

Non-text interactions revoke the previous permission, clear an editable focus
that would otherwise persist, and update ImEnabled synchronously. Genuine
text interactions retain focus, selection, and normal editing. The guard
returns false for every pointer/touch event. Only a denied InputMethodQuery
is handled directly. No hide/show loop, focusChanged handler, timers,
per-widget click handlers, global NoFocus policy, or scrolling edits were added.

## Verification

The behavior tests in `tests/test_lite_input.py` launch the production entry
point with `--lite`, isolate profile storage and serial-port discovery, and
test K1-K13, dynamic read-only/item flags, Full-mode exclusion, and repeated
installation/running-state transitions. They observe ImEnabled during focus
transitions and pointer delivery, not merely helper return values or hide calls.

Commands (use the repository's virtual environment):

```sh
QT_QPA_PLATFORM=offscreen ./venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_lite_input.py tests/test_lite_mode.py
./venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_lite_input.py
./venv/bin/python -B -m tests.test_lite_touch --lite
```

Run UI suites separately on this Pi to avoid distorting flick/inertia timing.
The historical Full-mode WA_AcceptTouchEvents assertion in `test_lite_touch`
already failed before this task; no scrolling test or source was rewritten
to hide it. Final run counts are recorded in the task handoff.

## Remaining acceptance

The application guard is only one part of the resolution. Use a Qt runtime
containing the upstream correction above, preferably in a separate Lite
environment to preserve Full's runtime. No existing environment was upgraded,
downgraded, or patched during this task.

After that, repeat the production-themed Wayland trace and the real touch
sequence: editable port/name -> button -> populated table -> table drag ->
outer-page drag -> read-only control -> editable field. Also check combo
arrows/popups and spin steps/editor separately, before capture and after
repeated Start/Stop. Text entry must visibly open the keyboard; every other
interaction must produce no new activation or one-frame flash. Use slow-motion
video if available. Neither automated queries nor the current always-false
Qt visibility property establish physical acceptance.
