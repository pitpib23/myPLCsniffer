# Lite scrolling, phase 1

## Assessment and evidence before production edits

Entry: `main.py` -> `app.run()` consumes `--lite` -> `create_main_window(lite=True)`
-> `MainWindow`. All probes below invoke that entry path with `--lite`.

`MainWindow._make_scrollable` wraps Sniff, Inspect and Profile in two-axis
`QScrollArea`s. The nested controls are:

| Child | Parent | Policy / intended axes | Initial state at 800 x 480 |
| --- | --- | --- | --- |
| `PassiveSniffingWidget.message_table` (QTableWidget) | Sniff | both AsNeeded, per-pixel | 0 rows; H 0, V 0 |
| `PacketInspectorWidget.byte_table` (QTableWidget) | Inspect | both AsNeeded, per-pixel | hidden until details opened; 0 rows |
| `ProfileTab.profile_list` (QListWidget) | Profile | H AlwaysOff, V AsNeeded/per-pixel | sidebar initially hidden in compact layout |
| `ProfileTab.register_table` (QTableWidget) | Profile | both AlwaysOff; content fitted | 16 existing rows; H 0, V 0; no scroller |
| inspector summary/full-details tables | Inspect | both AlwaysOff; content fitted | parent-scrolled |

Before editing, outer viewports and the first three child viewports each call
`grabGesture(TouchGesture)` then `grabGesture(LeftMouseButtonGesture)`. There
is no nested guard, ownership helper or viewportEvent override. The only
application event filter is for checkable combo popup selection, unrelated to
these scroll surfaces. Qt's second grab replaces its first, as documented:
https://doc.qt.io/qt-6.8/qscroller.html#grabGesture

Start handlers (`start_monitor`, `start_sniffing`) start capture services;
running callbacks disable configuration/profile management controls. They do
not replace table models/viewports or install scrolling. Frames append rows;
inspecting packets populates tables and can show the byte group. Responsive
changes reflow layouts and QSS without replacing viewports.

Theme: QApplication uses the platform default style/palette/font. MainWindow
applies `_build_theme(METRICS[mode])` at construction and after debounced size
changes; tab methods apply layout/row geometry. QSS specifies Segoe UI, and
primary buttons use `role="primary"`. There is no separate Lite theme file.
The authoritative local probe used Windows/windowsvista, Qt 6.11.2, the exact
production QSS, COMPACT density and an 800 x 480 shown window. This does not
establish Linux font metrics or physical input behavior.

Command: `.venv/Scripts/python scripts/probe_lite_scroll.py --lite`.
Serial opening is mocked; Start is invoked, then 80 rows supplied. No PLC
traffic is needed. Sniff viewport 781 x 444; message viewport 767 x 287.
Parent V range 0..255; message V range 0..0 before Start and 0..1633 with rows.
Profile parent V range 0..373. Model/viewport C++ identities remain stable.

Same 100-pixel downward gesture, both widgets away from the relevant endpoint:

| State/input | Parent V before -> after | Child V before -> after |
| --- | --- | --- |
| Empty / mouse | 255 -> 0 | 0 -> 0 |
| Empty / native touch, Qt mouse synthesis on | 255 -> 0 | 0 -> 0 |
| Empty / native touch, synthesis off | 255 -> 255 | 0 -> 0 |
| Populated / mouse | 255 -> 0 | 400 -> 228 |
| Populated / native touch, synthesis on | 255 -> 0 | 400 -> 300 |
| Populated / native touch, synthesis off | 255 -> 255 | 400 -> 400 |

Event traces show ScrollPrepare and Scroll delivered to both parent and child
for a populated drag. Native touch without synthesis generates no scrolling.
The suspected empty-child guard bug is **not present** in this checkout on this
platform: the empty child allows the parent mouse path to scroll.

## SCROLL ROOT-CAUSE HYPOTHESIS

Independent nested mouse recognizers activate for the same propagated gesture,
moving both surfaces. Replacing the native recognizer with the mouse recognizer
also makes scrolling depend on mouse synthesis. Evidence is the movement/event
matrix above. Exclusive movement with the original recognizers, or native
scrolling with synthesis disabled, would disprove these mechanisms; neither
occurred in the probe. Start is not the initializer; content range changes
which competing recognizers can move.

## Smallest coherent design supported by this evidence

Replace the distributed grabs with one Lite-only input owner in MainWindow.
After Qt's drag threshold, choose the predominant axis and nearest eligible
scroll area (visible/enabled, policy permits it, nonzero range). Feed only that
viewport's QScroller via handleInput. Project motion onto that fixed axis;
release retains QScroller inertia on that owner. Native touch and mouse input
enter the same route, with accepted touch preventing Qt mouse synthesis.
Register/summary tables remain parent-scrolled. No Start setup or keyboard
changes are required. Full-mode branches retain their original behavior.

## Implementation

- `plcsniffer/ui/lite_scroll.py`: one application event filter scoped to the
  Lite pages. Ownership is selected once after `QApplication.startDragDistance`.
  The nearest eligible area owns the predominant axis; QScroller handles motion
  and inertia. Mouse wheel and scrollbar controls keep their native behavior.
  Taps use normal mouse press/release semantics; native double taps preserve the
  table double-click action using Qt's timing/distance settings. Cancellation,
  tab hiding, disable transitions and repeated initialization clear state.
- `main_window.py`: installs that owner inside the existing `if lite` branch.
- `passive_capture.py`, `packet_inspector.py`, `profile_tab.py`: remove the old
  independent gesture grabs. Existing per-pixel modes, policies and register
  content fitting are preserved. No Start handler or keyboard code changed.
- `tests/test_lite_mode.py`: replaces the misleading assertion that both gesture
  types were installed with the single-owner setup contract.
- `tests/test_lite_touch.py`: behavior coverage via `app.run(['main.py', '--lite'])`,
  the real application factory, shown UI and unchanged production QSS.

Final probe, same platform/geometry/gesture as the baseline:

| State/input | Parent V before -> after | Child V before -> after |
| --- | --- | --- |
| Empty / mouse | 255 -> 0 | 0 -> 0 |
| Empty / native touch, synthesis on | 255 -> 0 | 0 -> 0 |
| Empty / native touch, synthesis off | 255 -> 0 | 0 -> 0 |
| Populated / mouse | 255 -> 255 | 400 -> 222 |
| Populated / native touch, synthesis on | 255 -> 255 | 400 -> 245 |
| Populated / native touch, synthesis off | 255 -> 255 | 400 -> 300 |

Kinetic endpoints vary with event timing. The ownership result is consistent:
only the eligible child or its parent moves. The automated suite uses fixed
global finger positions as content moves; the diagnostic probe deliberately
retains the original local-coordinate sequence for before/after comparison.

## Verification (2026-09-15)

Runtime: isolated workspace `.venv`, Python 3.14.0, PySide6/Qt 6.11.2. The
machine's Python 3.9 standard library could not import unittest; it was not
modified. Dependencies came from the existing `requirements.txt`.

`python -B -m tests.test_lite_touch --lite`: **22 tests passed** on the Windows
Qt platform with production QSS. An additional run of the strengthened
horizontal-ownership test passed with both parent and child horizontal ranges
nonzero. Tests cover S1-S10, both native touch and mouse, synthesis on/off,
wrong-axis fallback, taps/double taps, inertia, boundary reversal, wheel/thumb
input, disabled controls, touch cancellation, tab changes, replaced viewport,
and duplicate synthesized mouse events during native touch.

The existing `test_lite_mode` suite: **27 tests passed**, entered through an
`app.run(--lite)` wrapper. The no-flag Full CLI test was excluded. Full widget
construction was used only as the requested regression control within the
Lite test process; Full itself was never used as a successful application run.
Existing Full layout/policy/scroller checks passed, and the new negative control
confirms no Lite owner or touch attributes are installed for Full construction.

`python -B scripts/probe_lite_scroll.py --lite`: passed through the real Qt
application event loop. It preserves production styling, mocks serial opening,
invokes Start, and reports pre/post gesture values and event recipients.
The probe is retained as a reproducible diagnostic; no debug logging was added
to production code. `git diff --check` passed.

## Physical acceptance: pending

No physical finger interaction or target Linux input trace was generated in
this session. Windows Qt advertises a touchscreen device, but that enumeration
does not prove a finger path. These results do not establish Linux Qt version,
font metrics, X11/Wayland delivery, or driver synthesis behavior. Serial worker
opening was mocked; actual device capture remains a manual acceptance step.

On the target, launch this checkout with **`python main.py --lite`**, using the
normal production environment and theme. Record Qt version and X11/Wayland
session type alongside the result. Optionally run the probe there with `--lite`
first; its synthetic gestures still do not count as physical acceptance.

| Physical check | Expected | Result |
| --- | --- | --- |
| Before Start: outer Sniff page and empty message table | page moves, no trapped drag | pending |
| Startup Inspect page | page works; byte group is initially hidden | pending |
| Profile page and register region | outer page moves; register has no internal scroller | pending |
| Populated message table after Start | table alone moves on each usable axis | pending |
| Inspect a packet, open complete byte information | byte table scrolls on each usable axis | pending |
| Child boundary, without releasing, then reverse | same child retains ownership | pending |
| Child without range on chosen axis | eligible parent moves | pending |
| Slow drag, fast flick, diagonal, repeated gestures | one fixed owner/axis; inertia stays there | pending |
| Quick tap and double tap | selection and inspection action still work | pending |
| Repeated Start/Stop and page changes | no lost scrolling or stale gesture | pending |

Phase 1 is **not physically accepted or complete** until those checks pass.
