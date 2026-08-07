# Trying the app with only a laptop

Since this is a receive-only sniffer, "trying" it doesn't need a real PLC —
just something feeding bytes into a serial port it can listen on. This
covers the two realistic ways to do that with nothing but your laptop. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces referenced below fit
together.

## Option A — quick sanity check, no GUI (2 minutes)

The test suite already proves the whole decode pipeline works, using a fake
in-memory serial port — no hardware, virtual or real:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -v
```

This confirms the logic is sound, but you won't see the actual app window or
watch packets appear live — for that, use Option B.

## Option B — see the real GUI working, with simulated traffic

The trick is a **virtual null-modem cable** — two virtual COM ports wired to
each other in software, so anything written to one comes out the other.
Then a small script plays "PLC polling a multimeter" on one end, while the
app listens on the other.

### 1. Install a virtual COM port pair

On Windows, the standard free tool is **com0com**. It creates a linked pair
like `COM10↔COM11`. Note: the original com0com driver isn't signed for
modern Windows by default — if the installer complains, grab one of the
signed forks (search "com0com signed driver") or enable test-signing mode.
Once installed, you'll have two new COM ports that always talk to each
other.

### 2. Run the traffic simulator on one end

[`scripts/simulate_multimeter.py`](scripts/simulate_multimeter.py) plays
back a realistic "master polls a multimeter every 2 seconds" pattern, with
the value actually changing each poll so you can see live updates:

```powershell
python scripts\simulate_multimeter.py COM10 --baud 9600
```

You'll see it printing each simulated request/response pair as it sends
them. It builds correct Modbus RTU frames (right CRC, right byte layout) —
verified directly against the app's own decoder, so it decodes cleanly, not
just superficially.

> **Note:** this script is a standalone test tool that writes to a port —
> it is deliberately **not** receive-only, unlike the sniffer app itself.
> Only ever point it at a virtual loopback port, never at a port wired to
> real equipment.

### 3. Open myPLCsniffer, go to Tab 1, and point it at the *other* side

- Setup mode: **Config mode**
- Serial port: `COM11`
- Baud rate: `9600` (must match the simulator)
- Click **Start Passive Sniffing**

You should immediately see request/response pairs from slave 1 landing in
the table, with the voltage/current values changing every 2 seconds.

### 4. Explore the rest of the app using this live data

- Double-click a row → opens **Packet Inspector** (Tab 2) with the full byte
  breakdown.
- Click **Save as Profile** → builds a profile from what you've captured,
  hands it to **Tab 4**.
- On Tab 4, try its *own* independent Start/Stop sniffing on `COM11` — the
  register rows update live, separate from Tab 1.
- Try **Auto mode** on Tab 1: check `9600` in the baud-rate picker (leave
  parity/stop-bits at their defaults), Start — it should lock onto the
  simulator's traffic within the configured listen window.

### Simulator options

```text
python scripts\simulate_multimeter.py <port> [--baud N] [--interval SECONDS]
```

- `--baud` — must match whatever you configure on the sniffer's Config-mode
  side, or whatever's checked in Auto mode's baud-rate picker. Default 9600.
- `--interval` — seconds between polls. Default 2.0, deliberately slow (like
  a real device) — useful for exercising Auto mode's window-growth behavior
  if you set the sniffer's "seconds per setting" lower than this.
