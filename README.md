# klipper-btt-tft-bridge

A high-performance **Klipper `extras` module** that runs **BigTreeTech and MKS TouchScreens** in full native **Touch Mode** against Klipper with complete bidirectional RPC support — including **virtual SD card file browsing, on-screen G-code thumbnail streaming, Wi-Fi provisioning, macro discovery, live tuning, and host print progress**.

Paired with the dedicated screen firmware fork: [**BIGTREETECH-TouchScreenFirmware-Klipper**](https://github.com/persano/BIGTREETECH-TouchScreenFirmware-Klipper).

---

## 📊 Why Use This Bridge? (Feature Comparison)

| Feature | Transparent Passthrough (socat / naive bridge) | **klipper-btt-tft-bridge (This Module)** |
| :--- | :--- | :--- |
| **Architecture** | External user daemon shuttling raw bytes | **In-process Klipper `extras`** running inside Klippy event reactor |
| **Serial Synchronization** | Double `ok` collisions cause fatal **`ACK timed out`** | **Strict single `ok` invariant**; status queries never enter gcode queue |
| **Long Commands (`G28`, Bed Mesh, Heating)** | Screen locks up / times out after 5 seconds | Emits **`echo:busy: processing`** keepalives; screen stays responsive |
| **Host-Started Print Tracking** | ❌ None (screen stays on idle menu) | ✅ Emits **`File opened:`** and live `M27` byte progress from `print_stats` |
| **Virtual SD Card File Browsing** | ❌ Empty list or broken short 8.3 aliases | ✅ **Full real filenames** from Mainsail/Fluidd with folder traversal (`M20 L`) |
| **G-code Thumbnail Streaming** | ❌ None (blank document icons only) | ✅ **Live 70x70 RGB565 thumbnail streaming** from Moonraker `.thumbs/` or G-code (`M987`) |
| **On-Screen Wi-Fi Management** | ❌ None | ✅ **Live Wi-Fi scanning (`M981`) and connection (`M982`)** via on-screen keyboard |
| **Klipper Macro Discovery** | ❌ Hardcoded Marlin buttons only | ✅ **Dynamic enumeration (`M985`)** of all public `[gcode_macro]` objects |
| **Klipper Live Parameter Tuning** | ❌ Marlin codes fail (`M900`, `M205` rejected) | ✅ **Pressure Advance, Square Corner Velocity, Accel, Retract (`M986`)** |
| **Host Control & Recovery** | ❌ None | ✅ Direct **`SAVE_CONFIG`**, **`RESTART`**, and **`FIRMWARE_RESTART`** (recovers MCU shutdown) |

---

## Key Capabilities

### 1. In-Process Single-Threaded Architecture
Runs inside the Klippy process using `reactor.register_fd` (same mechanism Klipper uses for microcontrollers). Status commands (`M105`, `M114`, `M220`, `M221`, `M27`) are answered instantly from Klipper's internal object model without going through the G-code execution queue.

### 2. Virtual SD Card File Manager (`M20 L`, `M21`, `M23`, `M24`)
- Reads files directly from Klipper's `virtual_sdcard` directory.
- Sends full long filenames (`Cap:LONG_FILENAME:1`) and file sizes.
- Supports subdirectories and clean folder navigation on the touchscreen.
- Tapping a file selects it via `M23` and prints via `M24` directly from the display.

### 3. High-Speed G-Code Thumbnail Streaming (`M987` & `//bridge:thumb:`)
- When the screen requests a thumbnail (`M987 S<hex_filename>`), the bridge locates the file in `virtual_sdcard`.
- Checks Moonraker `.thumbs/` (e.g. `300x300.png`, `48x48.png`, `32x32.png`), embedded G-code Base64 PNGs, or Cura image comments.
- Resizes to 70x70 RGB565 with alpha blending over a dark neutral background.
- Streams the pixel data to the screen via `//bridge:thumb:<row_hex>` lines.
- Paired with the screen firmware's dedicated SPI flash cache (`0x7C0000`), preview is instant on tap and persists across cancel.

### 4. On-Screen Wi-Fi Provisioning (`M980`–`M982`)
- `M980`: Returns current host Wi-Fi connection status, SSID, and IP address.
- `M981`: Scans nearby access points with signal strength and encryption type.
- `M982`: Connects to a selected SSID using the password entered on the TFT's ASCII keyboard (via NetworkManager or `wpa_supplicant`).

### 5. Dynamic Klipper Macro Enumeration (`M985`)
- Queries Klipper's config object for all defined `[gcode_macro]` objects (filtering out internal/hidden macros prefixed with `_`).
- Transmits macro names to the TFT, which displays them in a scrollable list with one-tap execution.

### 6. Klipper Parameter Tuning (`M986`)
Translates touchscreen live adjustments to native Klipper commands:
- **Pressure Advance** → `SET_PRESSURE_ADVANCE ADVANCE=...`
- **Square Corner Velocity** → `SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=...`
- **Max Acceleration** → `SET_VELOCITY_LIMIT ACCEL=...`
- **Firmware Retraction** → `SET_RETRACTION RETRACT_LENGTH=...`
- **Probe Z-Offset** → `SET_GCODE_OFFSET Z_ADJUST=... MOVE=1`

### 7. Bidirectional Print Progress & Host Actions
- Automatically notifies the TFT when a print starts from Mainsail/Fluidd.
- Streams live progress percentage, elapsed time, and remaining time (`Time Left ..`, `Data Left ..`).
- Forwards `M117` and `[display_status]` messages to the TFT status bar.
- Syncs part cooling fan speed gauge (`M106`).

---

## Topology & Wiring

```
BTT / MKS TFT (Touch Mode)  --UART/TTL (3.3V)-->  USB-TTL adapter (CH340/CP2102)  --USB-->  Host SBC (Raspberry Pi)
Printer Mainboard (SKR, etc.) --------------------------------------------------------USB-->  Host SBC (Raspberry Pi)
```

The TFT connects to a USB-TTL serial adapter plugged into the Raspberry Pi (or host SBC), **not** the mainboard TFT port.

> ⚠️ **Voltage Warning:** The TFT serial port operates at **3.3V TTL**. Do **NOT** connect RS-232 voltage (±12V); use a standard 3.3V USB-TTL adapter (CH340, CP2102, FT232R).

---

## Installation

### 1. Clone & Link
On your Raspberry Pi:
```bash
cd ~
git clone https://github.com/persano/klipper-btt-tft-bridge.git
cd klipper-btt-tft-bridge
./install.sh
```

### 2. Configure `printer.cfg`
Add the `[tftbridge]` section to your `printer.cfg`:
```ini
[tftbridge]
tft_device: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0   # Find via: ls /dev/serial/by-id/
tft_baud: 115200
# optional:
# busy_interval: 2.0      # Seconds between keepalive echoes
# machine_type: GeniusPro # Machine name reported in M115

[virtual_sdcard]
path: ~/printer_data/gcodes

[display_status]
[pause_resume]
[print_stats]
```

### 3. Screen Firmware Configuration (`config.ini`)
In your TFT's `config.ini` on the SD card:
```ini
serial_port:P1:6          # 115200 baud (must match tft_baud)
command_checksum:0
onboard_sd:1              # Enables virtual SD card mode and host print tracking
M27_always_active:1
M27_refresh_time:2
prog_source:0             # File-position based progress %
long_filename:1           # Enable long filename support
```

### 4. Restart Klipper
```bash
sudo systemctl restart klipper
```

---

## Updates via Moonraker (Mainsail / Fluidd Web UI)

Add this entry to your `moonraker.conf` so update notifications appear directly in Mainsail/Fluidd:

```ini
[update_manager klipper-btt-tft-bridge]
type: git_repo
path: ~/klipper-btt-tft-bridge
origin: https://github.com/persano/klipper-btt-tft-bridge.git
primary_branch: main
managed_services: klipper
```

---

## Paired Screen Firmware

For the best experience, use our matching firmware:
👉 [**BIGTREETECH-TouchScreenFirmware-Klipper**](https://github.com/persano/BIGTREETECH-TouchScreenFirmware-Klipper)

Features include:
- SD-less instant boot for Artillery Genius Pro / Sidewinder X2 (`PB15` hardware detect + NVIC fix).
- Dedicated 5-slot SPI flash thumbnail cache (`0x7C0000`) for zero-latency scroll and instant popup previews.
- Upfront flash sector erase preventing UART buffer overrun during thumbnail streaming.
- Word-wrapping print confirmation popup with model thumbnail preview.
- Turnkey SD packages for Artillery Genius Pro and other popular printers.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

