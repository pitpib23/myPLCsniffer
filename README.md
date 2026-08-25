# myPLCsniffer — Passive Modbus RTU Sniffer

`myPLCsniffer` is a receive-only PySide6 desktop application for observing an
existing Modbus RTU serial bus. The visible application contains only passive
capture, packet inspection, and logging workflows. It never sends a Modbus
request or response.

> [!IMPORTANT]
> The passive workers never call `serial.write()`. A USB-RS485 adapter is still
> electrically connected to the bus, so use an isolated receive-capable adapter
> or a dedicated high-impedance industrial tap on production equipment.

## Application tabs

### 1. Passive Sniffing

Select a COM port and one setup mode:

- **Config mode** listens with the baud rate, parity, and stop-bit settings you
  enter.
- **Auto mode** opens the selected port once, cycles common serial formats on
  that handle, and locks onto the first structurally plausible CRC-valid Modbus
  RTU packet. It then continues capturing on the same open handle. Detection is
  receive-only and may require several passes on a quiet bus.

The large packet table includes timestamps, inferred direction, unit ID,
function, frame type, address, count, and complete RTU bytes. Filters can
be combined. Search updates are debounced to keep the interface
responsive with large captures. Paused packets are queued and can be copied,
cleared, or exported to CSV. On a small display the Setup row and the
Pause/Clear/Save-as-Profile row reflow onto two lines each and the
"double-click to inspect" hint is dropped — see
[Responsive layout](#responsive-layout--small-screens) below; scrolling only
kicks in if a window genuinely can't fit even that.

Direction is inferred from request/response order because ordinary two-wire
RS-485 has no separate direction channel. A response observed before its request
can therefore appear as unmatched.

### 2. Packet Inspector

Double-click a packet in Tab 1 to open it here. The important view includes
timing, direction, unit ID, function, address, quantity, decoder status,
CRC values, and the raw frame — not the decoded register values themselves.
**Show All Packet Information** adds exact timestamps, PDU/data bytes,
exception details, matched-request timing, and a byte-by-byte table (each
byte's hex/decimal/binary value and its role, e.g. "Start address high
byte" — still not a combined decoded reading). The byte table expands or
shrinks to fill whatever space is actually available (with its own internal
scrollbar for whatever doesn't fit) rather than always reserving room for a
fixed row count; the important-information summary above it always keeps
its own natural size.

### 3. Logging

The Logging tab shows the exact active file and refreshes when it changes.
Logging uses `TimedRotatingFileHandler` at local midnight with five dated backup
files in addition to the current file. Development runs use
`logs/plcsniffer.log`; packaged builds use the current user's application-data
directory.

### 4. Profile

Named register profiles map raw Modbus addresses to meaningful PLC values.
Each row records a name, function code, **Format**, **Byte Order**,
register address, multiplier, unit, description, and its own live
**Timestamp**/**Status** (not sniffed yet, waiting for data, updated, or
stopped). Long descriptions that don't fit the column are still available
as a hover tooltip. Profiles open read-only; **Edit Profile** unlocks the
form and register table for changes, and **Save Profile** or **Discard
Changes** commits or reverts them. Profiles persist to
`plcsniffer/profile.json`. A capture session on Tab 1 can also build a new
profile directly from the packets it observed, landing here unlocked for
review before saving (every register it detects starts as plain 16-bit
Unsigned; combine a span into a wider reading afterwards if needed, as
below).

**Format** controls how many consecutive registers a row reads and how the
combined bytes are interpreted. The eight options match what the major
Modbus/SCADA tools expose (Kepware KEPServerEX, Modbus Poll, Ignition's
Advanced Modbus module) — deliberately not more: e.g. an 80-bit "long
double" appears in generic C type references but in none of those tools'
Modbus type lists, since it has no defined wire format and isn't IEEE-754:

- **16-bit Unsigned** (default) / **16-bit Signed** each use only this row's
  own address.
- **32-bit Unsigned / Signed / Float** each combine this address with the
  next one (2 registers).
- **64-bit Unsigned / Signed / Float** each combine this address with the
  next three (4 registers).
- Every register in the span must arrive in the *same* response to be
  decoded; a poll that only covers part of the span leaves the reading
  unchanged rather than showing a half-updated value. Only meaningful for
  register-based functions (Read Holding/Input Registers, Write
  Single/Multiple Register); coil-type functions carry single-bit values
  and are never combined.

**Byte Order** (next to Format) picks which official-standard byte order to
decode with — vendors disagree on this, so if a decoded value looks
garbled, try another option. Which options are offered depends on Format's
register span: 16-bit only has **Big-Endian**/**Little-Endian**; 32-bit adds
**Word-Swapped**/**Byte-Swapped**; 64-bit adds **Double-Word Swapped** and
its own **Byte-and-Word Swapped** (the same underlying swap as 32-bit's
Byte-Swapped, just under the name vendors use at that width). Hover an
option — in the open dropdown, or the cell itself when closed — to see its
example letter mapping (e.g. Word-Swapped shows "C D A B"). Switching Format
to a width where the current Byte Order no longer exists resets it to
Big-Endian.

The **Register** column shows the raw, zero-based starting address you
type in (e.g. "48"), or a range (e.g. "48-49") once Format spans more than
one register — only the starting number is ever edited, the range is
display-only. Hover over it to see the equivalent Modicon reference (e.g.
"Address: 40049-40050"). If a profile already has separate 16-bit rows for
what's actually one wider value, reconfigure one row's Format/Byte Order
and remove the now-redundant row(s) for the other address(es).

The profile list on the left is a collapsible sidebar: **« Hide Profiles**
gives the summary and register table the full tab width, and the same
button (now **» Show Profiles**) brings it back at its previous width. On a
small/short window the sidebar auto-collapses to give the register table
more room; the moment you use the button yourself, that manual choice is
respected from then on and the sidebar stops auto-collapsing or
auto-restoring — see [Responsive layout](#responsive-layout--small-screens).

**Passive sniffing on this tab is independent of Tab 1.** Pick a serial
port in this tab's own toolbar and click **Start Passive Sniffing** to open
a second, receive-only connection using the *selected profile's own* slave
ID, baud rate, parity, and stop bits — Tab 1 does not need to be running.
Only addresses already listed in the profile are ever matched or written to
the table (matched by slave ID, register address, and function code); the
table is a live view of that profile's configuration, not a log of every
frame seen on the bus. Editing, adding, removing, or switching profiles is
locked while sniffing is active and restored when it stops, since changing
the active profile mid-capture would leave the worker matching frames
against settings it was never opened with.

## Responsive layout / small screens

The window adapts to whatever size it actually is at runtime — including an
on-screen keyboard shrinking the usable height on a Raspberry Pi — rather
than being tuned for one resolution. `plcsniffer/ui/responsive.py` computes
one of three density tiers purely from the current window's width/height
(never a resolution or platform check): **Normal**, **Compact**, and
**Ultra-compact**. `MainWindow` recomputes the tier on every resize
(debounced ~120ms so a drag-resize doesn't trigger dozens of recomputes) and
applies it two ways:

- Regenerating and reapplying the app's one QSS theme with tier-specific
  margins/padding/control heights/font size — this reaches every widget in
  one `setStyleSheet()` call.
- Calling each tab's own `apply_responsive_mode()` for what a stylesheet
  can't do: per-layout spacing (via the small recursive
  `apply_layout_spacing()` helper, which reaches every nested row/grid —
  including ones owned by a `QGroupBox`'s own layout — from one call on a
  tab's root layout) and structural reflow. On Tab 1, the Setup row and the
  Pause/Clear/Save-as-Profile row each split from one row into two below the
  Compact threshold (and back to one row above it); a purely informational
  hint label is dropped in Compact/Ultra-compact rather than reflowed. On
  Tab 4, the profile sidebar auto-collapses/restores the same way, unless
  you've ever toggled it yourself — see the Profile section above.

Data tables (the capture table, the register table, the packet inspector's
byte table) use `QSizePolicy.Expanding` with a stretch factor rather than a
fixed or capped height, so they claim whatever space the chrome above them
doesn't need; their minimum-height floors are themselves smaller in
Compact/Ultra-compact, rather than one fixed floor regardless of window
size. `QScrollArea` remains around each tab as a last-resort fallback for a
window that genuinely can't fit even the densified layout — not as the
primary way small screens are handled.

## Architecture

The application uses one shallow package and a single root launcher:

```text
main.py                         desktop entry point
plcsniffer/
  app.py                       application bootstrap
  capture.py                   serial-port discovery and capture lifecycle
  config.py                    immutable defaults and settings
  exceptions.py                application errors
  logging_config.py            rotating file logging
  modbus.py                    RTU decoding and receive-only workers
  validation.py                input validation
  profile.json                 saved register profiles
  ui/
    main_window.py             four-tab shell
    passive_capture.py         capture controls and packet table
    packet_inspector.py        decoded packet details
    log_viewer.py              live application log
    profile_tab.py             named register profiles
assets/                         application icons
tests/                          regression tests
```

The UI depends directly on the small capture and Modbus modules. There are no
compatibility shims, speculative protocol abstractions, or legacy active-scanning
layers.

## Wiring

Connect the monitoring adapter in parallel with the existing bus:

```text
Existing RS-485 A  -------------------- A on monitoring adapter
Existing RS-485 B  -------------------- B on monitoring adapter
Existing signal GND ------------------- GND (when appropriate)
```

Keep the monitoring stub short. Do not add another 120-ohm terminator unless the
monitor is at a bus end and the existing termination design requires it.

## Installation and launch

Python 3.9 or newer is required. PySide6 provides the UI and pyserial handles receive-only serial access.

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -v
```

The suite covers CRC decoding, response matching, receive-only Auto mode,
single-handle cleanup, filters, paused queues, CSV export, packet inspection,
midnight log rotation, live log-file refresh (including the log-path label
eliding to the available width), the main window's small-screen minimum-size
floor, its close-event shutdown/warning path, Tab 1's Save as Profile
capture-to-profile flow, the Profile tab's own add/remove/edit/save/
discard/sidebar-toggle workflow and register-address range validation, its
multi-register Format/Byte Order combining (every official-standard byte
order at every width — including 64-bit's Double-Word Swapped — the
Format-dependent set of options offered and their example letter-mapping
tooltips, resetting Byte Order when a Format change makes it invalid,
unsigned/signed/float/64-bit, partial-span and coil-function guards, the
Register cell's range display, and backward compatibility with profiles
saved before Format existed), and the responsive density mechanism
(`tests/test_responsive.py` — mode-threshold computation, the recursive
layout-spacing helper, resizing a real MainWindow large → small → large with
mode transitions/reflow/sidebar-collapse all verified reversible, and that
critical controls on every tab stay reachable at every size).

## Troubleshooting

- **No COM port:** install the adapter driver and select **Refresh Ports**.
- **Port cannot open:** close other programs using that COM port.
- **No Config-mode packets:** verify traffic, serial format, polarity, grounding,
  and adapter receive LEDs.
- **Auto mode does not lock:** allow another pass or enter the known settings in
  Config mode; sparse traffic takes longer.
- **CRC errors:** check serial format, wiring, frame truncation, and signal
  integrity.
- **Unmatched responses:** begin capture before the next master request and
  check for lost bytes.
- **Diagnostic log:** open Tab 3 to see the active path and current contents.
- **Profile tab won't start sniffing:** select a profile that has at least
  one register, choose a port in that tab's own toolbar, and make sure the
  profile isn't currently open for editing (**Save Profile** or **Discard
  Changes** first).
- **Profile register never updates:** confirm the frame's slave ID matches
  the profile's Slave ID field, and that the register's address and
  function code (if set) match what's actually on the wire — only
  registers already listed in the profile are ever matched.
