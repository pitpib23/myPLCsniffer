# Architecture — a first-time walkthrough

This is a walkthrough for someone opening this project cold, working from the
top (what the app is) down to the details (how one byte on the wire becomes a
table row).

## What this app actually is

It's a **passive Modbus RTU sniffer** — a desktop tool that listens in on an
existing serial bus between industrial equipment (say, a PLC polling a
multimeter) and shows you what's being said, without ever injecting anything
itself. That "receive-only" promise isn't just a UI label — the app is
structured so the code path that would let it *send* on the wire simply
doesn't exist. It's built with **PySide6** (Python bindings for the Qt GUI
toolkit).

## The shape of the codebase

```text
main.py                    9 lines — just boots the app
plcsniffer/
  config.py               constants + two small settings dataclasses
  exceptions.py           one exception type used everywhere instead of raw ValueError
  validation.py           pure functions: "is this a valid baud rate/port/parity?"
  modbus.py               the protocol brain: CRC, decoding, background serial-reading threads
  capture.py              the safe wrapper the UI is allowed to talk to
  logging_config.py       structured JSON logging to a rotating file
  app.py                  builds the QApplication + main window
  ui/
    main_window.py        the 4-tab shell + the app's whole visual theme (one QSS string)
    passive_capture.py    Tab 1 — capture setup, packet table, filters
    packet_inspector.py   Tab 2 — read-only packet detail view
    log_viewer.py         Tab 3 — tails the log file
    profile_tab.py        Tab 4 — named register profiles + their own sniffing
    style.py              tiny shared dict of status-label colors
```

Nothing exotic here — it's a straightforward layered design:
**config → validation → protocol logic → a service that owns threads → UI
widgets that talk to that service.** Each layer only knows about the one
below it.

## The core engine, from the bottom up

- **`config.py`** has zero logic — just numbers (default baud rates, minimum
  window sizes, table widths) and two `@dataclass`es (`SerialSettings`,
  `AutoDetectionSettings`) that describe *what a capture configuration looks
  like*, without doing anything with it.

- **`validation.py`** is a pile of small pure functions — `validate_port`,
  `validate_baudrate`, `validate_parity`, etc. Each either returns a
  cleaned-up value or raises `ConfigurationError`. Nothing here touches Qt or
  serial hardware; it's just "is this input sane."

- **`modbus.py`** is the biggest, most important file and it has two distinct
  jobs bundled together:
  1. **Pure decoding logic** — `modbus_crc()`, `crc_is_valid()`, and
     `ModbusRTUDecoder`, a class that takes a raw byte string, checks its
     CRC, matches it against a previously-seen request (so it knows "this is
     the response to that"), and produces one `CapturedModbusFrame` — a
     frozen dataclass that's the single object the *entire rest of the app*
     passes around to mean "one decoded packet."
  2. **Background workers** — `PassiveSerialReaderThread`, a `QThread`
     subclass that actually opens the OS serial port and loops `read()`ing
     bytes forever, feeding them through the decoder, and
     `PassiveAutoDetectThread`, a subclass of that which additionally cycles
     through different baud/parity/stop-bit combinations until it stumbles
     onto valid traffic (this is what Tab 1's "Auto mode" uses).

- **`capture.py`** has one class, `PassiveCaptureService`, and it exists for
  one reason: **the UI is never allowed to construct or manage a `QThread`
  directly.** Every tab that wants to sniff creates one of these instead. It
  knows how to start a worker, refuses to start a second one while one's
  already running, relays the worker's signals up to the UI, and can
  gracefully `stop()`/`wait()` on shutdown without ever calling the
  dangerous `terminate()`.

## The four tabs

- **Tab 1 (`passive_capture.py`, the biggest tab)** — pick a port, choose
  Config mode (exact settings) or Auto mode (check off which baud
  rates/parities/stop-bits to try), hit Start. Captured packets land in a
  big filterable table. You can pause the live feed, clear it, copy rows,
  export CSV, or package the whole capture into a "profile" to hand off to
  Tab 4.

- **Tab 2 (`packet_inspector.py`)** — purely passive. It owns no capture
  logic at all; it just gets handed one `CapturedModbusFrame` (via
  double-clicking a row on Tab 1) and renders it — a short summary plus an
  optional full byte-by-byte table.

- **Tab 3 (`log_viewer.py`)** — the simplest tab by far. No buttons, no
  state. It just polls the app's own log file on a timer and shows the tail
  of it.

- **Tab 4 (`profile_tab.py`, the other complex tab)** — here you build named
  "profiles": a slave ID + serial config, plus a hand-picked list of
  specific registers (with friendly names, a function-code dropdown,
  multiplier/unit, notes). Each profile can *also* run its own independent
  Start/Stop sniffing session — a second, separate `PassiveCaptureService`
  instance — that live-updates only the registers you've defined for
  whichever profile is currently open.

## How a byte on the wire actually becomes a table row

This is the part that ties everything together — worth tracing once end to
end:

1. You click **Start** on Tab 1 → `start_monitor()` builds a
   `SerialSettings` object → hands it to
   `PassiveCaptureService.start_configured()`.
2. The service creates a `PassiveSerialReaderThread`, wires up its signals,
   and calls `.start()` — this actually spins up a new OS thread.
3. Inside that thread, `run()` opens the real serial port and loops: read
   whatever bytes are available, append to a buffer, hand the buffer to
   `ModbusRTUDecoder.split_frames()`, which pulls out any complete,
   CRC-valid frames and leaves partial ones in the buffer for next time.
4. Each complete frame gets `.decode()`'d into a `CapturedModbusFrame` and
   **emitted as a Qt signal** (`frame_received`) — this is the one moment
   background-thread code hands data back to the GUI. Qt automatically and
   safely queues that signal onto the main thread; the background thread
   never touches a widget directly.
5. `PassiveCaptureService` relays the signal; `PassiveSniffingWidget._on_frame()`
   adds a row to the table *and* re-emits it as `frame_observed`.
6. `MainWindow` has already wired that signal to `ProfileTab.set_latest_frame()`
   — so if you have a profile open on Tab 4 whose slave ID matches, its
   register rows update live too, entirely independent of whether Tab 4's
   *own* Start button is running.

## Two ideas worth remembering

- **The concurrency model is entirely signals-and-slots.** There's no manual
  locking anywhere in the UI code — background threads only ever communicate
  by emitting a signal, and Qt guarantees that gets marshaled safely onto
  the GUI thread before any slot runs.
- **There are deliberately two independent capture "engines" running side by
  side** — Tab 1's and Tab 4's own — each its own `PassiveCaptureService`
  instance. Starting one never requires or blocks the other; the only
  real-world limit is that two of them can't open the *same* serial port at
  once (the OS itself rejects that).

See [TESTING_WITHOUT_HARDWARE.md](TESTING_WITHOUT_HARDWARE.md) for how to
exercise all of this using nothing but your laptop.
