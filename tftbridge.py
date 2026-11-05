# BigTreeTech TFT35 E3 V3 <-> Klipper compatibility bridge (in-process).
#
# Runs inside Klippy as an [tftbridge] extras module. It reads the TFT serial
# port directly through the Klipper reactor (no helper threads, no klippy.serial
# pty round-trip) and speaks a Marlin-compatible dialect back to the screen:
#
#   * status queries (M105/M114/M220/M221/M27) are answered locally from the
#     Klipper object model -> they are NEVER injected into the gcode queue, so
#     the TFT status loop no longer competes with print/motion traffic;
#   * M115 advertises a Marlin firmware name + AUTOREPORT_TEMP capability;
#   * M155 starts a local temperature auto-report timer, so temperatures keep
#     updating on screen even while a long command (G28, heating) is running;
#   * exactly ONE "ok" is emitted per command; long commands are kept alive
#     with periodic "echo:busy: processing" instead of a premature "ok"
#     (the old double-ok was the root cause of "ACK timed out");
#   * Marlin-only commands are translated to Klipper equivalents (G29, M420,
#     M851, M24/M25/M524); unsupported ones are acked quietly.
#
# Replaces the transparent oldhui-uk tftbridge.

import base64
import glob
import io
import logging
import os
import re
import serial
import struct
import subprocess
import threading
import time
import zlib


# Marlin settings/query commands the TFT sends from its menus that Klipper has
# no equivalent for. Acked quietly instead of showing an "Unknown command"
# error toast on the screen.
QUIET_ACK = frozenset(
    'M22 M33 M36 M43 M92 M98 M125 M141 M154 M200 M201 M203 M206 M208 '
    'M209 M211 M218 M291 M292 M300 M301 M304 M306 M355 M376 M408 M409 '
    'M501 M502 M503 M552 M564 M569 M593 M665 M666 M710 M876 M906 M913 '
    'M914 M928'.split())

# M280 P0 S<angle> servo angles the TFT BLTouch menu uses -> BLTOUCH_DEBUG.
BLTOUCH_ANGLES = {10: 'pin_down', 60: 'touch_mode', 90: 'pin_up',
                  120: 'reset', 130: 'self_test'}


class WifiBridgeManager:
    """Non-blocking Wi-Fi manager for M980-M982 RPC using nmcli in background threads."""
    def __init__(self, bridge):
        self.bridge = bridge
        self.reactor = bridge.reactor
        self.busy = False
        self.last_status = None

    def handle_m980_status(self, send_ok=True):
        if self.busy:
            if send_ok:
                self.bridge.send_line("ok")
            return
        self.busy = True
        threading.Thread(target=self._status_worker, args=(send_ok,), daemon=True).start()

    def _status_worker(self, send_ok=True):
        lines = []
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                text=True, timeout=5
            )
            ssid = ""
            for row in out.splitlines():
                if row.startswith("yes:"):
                    ssid = row.split(":", 1)[1].replace("\\:", ":")
                    break

            ip_out = subprocess.check_output(
                ["hostname", "-I"], text=True, timeout=3
            ).strip()
            ip_addr = ip_out.split()[0] if ip_out else "0.0.0.0"

            if ip_addr and ip_addr != "0.0.0.0":
                connected = "1"
                if not ssid:
                    ssid = "Ethernet"
            else:
                connected = "1" if ssid else "0"

            status_line = f"//bridge:wifi_status:{connected}|{ip_addr}|{ssid}"
            lines.append(status_line)
            lines.append(f"M118 P0 IP:{ip_addr}")
            self.last_status = status_line
        except Exception:
            lines.append("//bridge:wifi_status:0|0.0.0.0|")

        self.reactor.register_async_callback(lambda e: self._finish(lines, send_ok))

    def handle_m981_scan(self):
        if self.busy:
            self.bridge.send_line("ok")
            return
        self.busy = True
        self.bridge.start_busy_keepalive()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        lines = []
        try:
            cmd = [
                "nmcli", "-t", "-f", "SIGNAL,SECURITY,SSID",
                "dev", "wifi", "list", "--rescan", "yes"
            ]
            out = subprocess.check_output(cmd, text=True, timeout=12)
            seen = set()
            for row in out.splitlines():
                parts = row.split(":", 2)
                if len(parts) == 3:
                    sig = parts[0].strip()
                    sec = parts[1].strip()
                    ssid = parts[2].replace("\\:", ":").strip()
                    if ssid and ssid not in seen:
                        seen.add(ssid)
                        sec_flag = "1" if sec and sec != "--" else "0"
                        lines.append(f"//bridge:wifi_ap:{sig}|{sec_flag}|{ssid}")
                        if len(seen) >= 16:
                            break
        except Exception as exc:
            lines.append(f"//bridge:wifi_err:{str(exc)[:32]}")
        lines.append("//bridge:wifi_scan_done")
        self.reactor.register_async_callback(lambda e: self._finish(lines))

    def handle_m982_connect(self, params_str):
        if self.busy:
            self.bridge.send_line("ok")
            return
        hex_ssid = ""
        hex_pass = ""
        for token in params_str.split():
            if token.upper().startswith("S"):
                hex_ssid = token[1:]
            elif token.upper().startswith("P"):
                hex_pass = token[1:]
        try:
            ssid = bytes.fromhex(hex_ssid).decode("utf-8")
            password = bytes.fromhex(hex_pass).decode("utf-8") if hex_pass else ""
        except Exception:
            self.bridge.send_line("//bridge:wifi_conn:0|Invalid Hex")
            self.bridge.send_line("ok")
            return

        self.busy = True
        self.bridge.start_busy_keepalive()
        threading.Thread(
            target=self._connect_worker, args=(ssid, password), daemon=True
        ).start()

    def _connect_worker(self, ssid, password):
        lines = []
        try:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=25)
            ip_out = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).strip()
            ip_addr = ip_out.split()[0] if ip_out else "Connected"
            lines.append(f"//bridge:wifi_conn:1|{ip_addr}")
        except subprocess.CalledProcessError as exc:
            err_msg = (exc.output or "Connect failed").strip().splitlines()[-1][:36]
            lines.append(f"//bridge:wifi_conn:0|{err_msg}")
        except Exception as exc:
            lines.append(f"//bridge:wifi_conn:0|{str(exc)[:36]}")

        self.reactor.register_async_callback(lambda e: self._finish(lines))

    def _finish(self, lines, send_ok=True):
        self.bridge.stop_busy_keepalive()
        for line in lines:
            self.bridge.send_line(line)
        if send_ok:
            self.bridge.send_line("ok")
        self.busy = False


def decode_png_fallback(png_bytes):
    if not png_bytes or not png_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return None
    idx = 8
    width = height = bit_depth = color_type = 0
    idat_parts = []
    palette = None
    while idx < len(png_bytes):
        if idx + 8 > len(png_bytes):
            break
        length, chunk_type = struct.unpack('>I4s', png_bytes[idx:idx+8])
        idx += 8
        if idx + length > len(png_bytes):
            break
        chunk_data = png_bytes[idx:idx+length]
        idx += length + 4
        if chunk_type == b'IHDR':
            width, height, bit_depth, color_type = struct.unpack('>IIBB', chunk_data[:10])
        elif chunk_type == b'PLTE':
            palette = chunk_data
        elif chunk_type == b'IDAT':
            idat_parts.append(chunk_data)
        elif chunk_type == b'IEND':
            break

    if not idat_parts or width == 0 or height == 0:
        return None

    try:
        raw_data = zlib.decompress(b''.join(idat_parts))
    except Exception:
        return None

    bpp_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in bpp_map or bit_depth != 8:
        return None
    bpp = bpp_map[color_type]
    stride = width * bpp
    rows = []
    raw_idx = 0
    prev_row = bytearray(stride)
    for _ in range(height):
        if raw_idx >= len(raw_data):
            break
        filter_type = raw_data[raw_idx]
        raw_idx += 1
        scanline = bytearray(raw_data[raw_idx:raw_idx+stride])
        raw_idx += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for x in range(bpp, stride):
                scanline[x] = (scanline[x] + scanline[x - bpp]) & 0xFF
        elif filter_type == 2:
            for x in range(stride):
                scanline[x] = (scanline[x] + prev_row[x]) & 0xFF
        elif filter_type == 3:
            for x in range(stride):
                left = scanline[x - bpp] if x >= bpp else 0
                scanline[x] = (scanline[x] + ((left + prev_row[x]) >> 1)) & 0xFF
        elif filter_type == 4:
            for x in range(stride):
                a = scanline[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                c = prev_row[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                scanline[x] = (scanline[x] + pr) & 0xFF
        prev_row = scanline
        rows.append(scanline)
    return width, height, color_type, rows, palette

def render_70x70_rgb565(w, h, ctype, rows, palette=None):
    target_w, target_h = 70, 70
    lines = ['00460046']
    bg_r, bg_g, bg_b = 30, 30, 30
    for ty in range(target_h):
        row_hex = []
        sy = int(ty * h / target_h)
        scanline = rows[sy]
        for tx in range(target_w):
            sx = int(tx * w / target_w)
            if ctype == 6:
                r, g, b, a = scanline[sx*4:sx*4+4]
                if a == 0:
                    r, g, b = bg_r, bg_g, bg_b
                elif a < 255:
                    alpha = a / 255.0
                    r = int(r * alpha + bg_r * (1.0 - alpha))
                    g = int(g * alpha + bg_g * (1.0 - alpha))
                    b = int(b * alpha + bg_b * (1.0 - alpha))
            elif ctype == 2:
                r, g, b = scanline[sx*3:sx*3+3]
            elif ctype == 3 and palette:
                pidx = scanline[sx] * 3
                r, g, b = palette[pidx:pidx+3]
            elif ctype == 0:
                r = g = b = scanline[sx]
            else:
                r = g = b = 0
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            row_hex.append(f'{rgb565:04X}')
        lines.append(''.join(row_hex))
    return lines

def render_png_to_70x70(png_bytes):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
        resample = getattr(Image, 'Resampling', Image).LANCZOS if hasattr(Image, 'Resampling') else getattr(Image, 'ANTIALIAS', Image.BILINEAR)
        im = im.resize((70, 70), resample)
        pixels = list(im.getdata())
        lines = ['00460046']
        bg_r, bg_g, bg_b = 30, 30, 30
        for y in range(70):
            row_hex = []
            for x in range(70):
                r, g, b, a = pixels[y * 70 + x]
                if a == 0:
                    r, g, b = bg_r, bg_g, bg_b
                elif a < 255:
                    alpha = a / 255.0
                    r = int(r * alpha + bg_r * (1.0 - alpha))
                    g = int(g * alpha + bg_g * (1.0 - alpha))
                    b = int(b * alpha + bg_b * (1.0 - alpha))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                row_hex.append(f'{rgb565:04X}')
            lines.append(''.join(row_hex))
        return lines
    except Exception:
        pass
    decoded = decode_png_fallback(png_bytes)
    if decoded:
        w, h, ctype, rows, pal = decoded
        return render_70x70_rgb565(w, h, ctype, rows, pal)
    return None

def extract_gcode_png(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            in_b64 = False
            b64_lines = []
            best_png = None
            for idx, line in enumerate(f):
                if idx > 4000:
                    break
                s = line.strip()
                if not in_b64:
                    if s.startswith('; thumbnail begin'):
                        in_b64 = True
                        b64_lines = []
                else:
                    if s.startswith('; thumbnail end'):
                        in_b64 = False
                        try:
                            data = base64.b64decode(''.join(b64_lines))
                            if data.startswith(b'\x89PNG\r\n\x1a\n'):
                                best_png = data
                        except Exception:
                            pass
                    else:
                        if s.startswith(';'):
                            s = s[1:].strip()
                        b64_lines.append(s)
            return best_png
    except Exception:
        return None


class TftBridge:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        # Serial port to the TFT.
        self.tft_device = config.get('tft_device')
        self.tft_baud = config.getint('tft_baud', 115200)
        # Legacy options from the old transparent bridge - read so Klipper does
        # not reject them as "invalid" (unused by this implementation).
        config.getint('tft_timeout', 0)
        config.get('klipper_device', None)
        config.getint('klipper_baud', 0)
        config.getint('klipper_timeout', 0)

        # Tunables.
        self.busy_interval = config.getfloat('busy_interval', 2., above=0.)
        self.machine_type = config.get('machine_type', 'Ender-3')

        self.gcode = self.printer.lookup_object('gcode')

        self.wifi_manager = WifiBridgeManager(self)
        self.selected_vsd_file = None

        self.tft = None
        self.fd_handle = None
        self.rx_buf = b''
        self.cmd_queue = []
        self.processing = False
        self.output_hooked = False
        self.vsd_alias_to_real = {}
        self.vsd_real_to_alias = {}

        self.autoreport_interval = 0.
        self.autoreport_timer = self.reactor.register_timer(
            self._autoreport_event)
        self.busy_timer = self.reactor.register_timer(self._busy_event)
        # The USB serial adapter can show up after Klipper starts (boot order,
        # screen powered later), so a failed open is retried until it works.
        self.reconnect_timer = self.reactor.register_timer(
            self._reconnect_event)

        # Host print detection: the TFT only polls M27 (progress) once it has
        # been told a print is active via a "File opened:" notification.
        self.print_state = None
        self.printwatch_timer = self.reactor.register_timer(
            self._printwatch_event)

        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)
        logging.info("TFTBridge: configured for %s @ %d",
                     self.tft_device, self.tft_baud)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _handle_ready(self):
        self._open_tft()

    def _reconnect_event(self, eventtime):
        self._open_tft()
        return self.reactor.NEVER if self.tft is not None else eventtime + 5.

    def _open_tft(self):
        if self.tft is not None:
            return
        try:
            # timeout=0 -> non-blocking reads; the reactor wakes us up when
            # bytes are available on the fd.
            self.tft = serial.Serial(self.tft_device, self.tft_baud, timeout=0)
        except Exception:
            logging.warning("TFTBridge: cannot open %s, retrying in 5s",
                            self.tft_device)
            self.tft = None
            self.reactor.update_timer(self.reconnect_timer,
                                      self.reactor.monotonic() + 5.)
            return
        self.fd_handle = self.reactor.register_fd(self.tft.fileno(),
                                                  self._tft_readable)
        if not self.output_hooked:
            self.gcode.register_output_handler(self._relay_klipper_output)
            self.output_hooked = True
        self.print_state = None
        self.fan_sent = None
        self.msg_sent = ''
        self.progress_sent = None
        self.reactor.update_timer(self.printwatch_timer,
                                  self.reactor.monotonic() + 1.)
        self.wifi_manager.handle_m980_status(send_ok=False)
        logging.info("TFTBridge: ready, listening on %s", self.tft_device)

    def _handle_disconnect(self):
        self.reactor.update_timer(self.reconnect_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.busy_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.autoreport_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.printwatch_timer, self.reactor.NEVER)
        if self.fd_handle is not None:
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
        if self.tft is not None:
            try:
                self.tft.close()
            except Exception:
                pass
            self.tft = None

    # ------------------------------------------------------------------ #
    # TFT serial I/O
    # ------------------------------------------------------------------ #
    def _send(self, data):
        if self.tft is None:
            return
        if isinstance(data, str):
            data = data.encode('ascii', 'ignore')
        try:
            self.tft.write(data)
            self.tft.flush()
        except Exception:
            logging.exception("TFTBridge: write failed")
            return
        if not data.startswith(b'//bridge:thumb:'):
            logging.info("TFTBridge ->TFT: %r", data)

    def send_line(self, line):
        self._send(line + '\r\n')

    def start_busy_keepalive(self):
        self.reactor.update_timer(
            self.busy_timer,
            self.reactor.monotonic() + self.busy_interval)

    def stop_busy_keepalive(self):
        self.reactor.update_timer(self.busy_timer, self.reactor.NEVER)

    def _relay_klipper_output(self, msg):
        # Relay Klipper-originated messages (//, !!, data lines) to the TFT
        # terminal. Drop bare "ok" lines: this bridge emits exactly one ok per
        # command, so relaying Klipper's ok would desync the TFT ACK counter.
        if self.tft is None:
            return
        for line in msg.split('\n'):
            s = line.strip()
            if not s or s == 'ok' or s.startswith('ok '):
                continue
            self._send(s + '\r\n')

    def _tft_readable(self, eventtime):
        try:
            data = self.tft.read(256)
        except Exception:
            logging.exception("TFTBridge: read failed, disconnecting and scheduling reconnect")
            if self.fd_handle is not None:
                self.reactor.unregister_fd(self.fd_handle)
                self.fd_handle = None
            if self.tft is not None:
                try:
                    self.tft.close()
                except Exception:
                    pass
                self.tft = None
            self.reactor.update_timer(self.reconnect_timer,
                                      self.reactor.monotonic() + 1.)
            return
        if not data:
            return
        logging.debug("TFTBridge: fd read %r", data)
        self.rx_buf += data
        # Extract all complete lines now, before any forwarded command can
        # block the greenlet -> rx_buf is only ever touched between yields.
        lines = []
        while b'\n' in self.rx_buf:
            line, self.rx_buf = self.rx_buf.split(b'\n', 1)
            lines.append(line)
        if len(self.rx_buf) > 512:
            # Runaway garbage with no newline -> drop it.
            self.rx_buf = b''
        for line in lines:
            self._classify(line)
        self._pump()

    # ------------------------------------------------------------------ #
    # Command classification
    # ------------------------------------------------------------------ #
    def _classify(self, raw):
        try:
            text = raw.decode('ascii', 'ignore').strip()
        except Exception:
            return
        # Strip optional Marlin line number (N123 ...) and checksum (*42).
        if text[:1] in ('N', 'n') and ' ' in text:
            head, _, rest = text.partition(' ')
            if head[1:].isdigit():
                text = rest.strip()
        star = text.find('*')
        if star != -1:
            text = text[:star].strip()
        if not text:
            return
        cmd = text.split(' ', 1)[0].upper()
        logging.info("TFTBridge TFT->: %s", text)

        # --- local status / RPC / Virtual SD: answered immediately ---
        if cmd == 'M105':
            self._send(self._temp_report(with_ok=True))
            return
        if cmd == 'M114':
            self._send(self._position_report())
            return
        if cmd == 'M27':
            self._send(self._sd_report())
            return
        if cmd == 'M115':
            logging.info("TFTBridge: M115 capabilities query from TFT")
            self._send(self._m115_report())
            self.wifi_manager.handle_m980_status(send_ok=False)
            return
        if cmd in ('M524', 'CANCEL_PRINT'):
            self._handle_cancel()
            return
        if cmd == 'M155':
            self._set_autoreport(text)
            self._send('ok\r\n')
            return
        if cmd in ('M220', 'M221'):
            if self._param(text, 'S') is not None:
                # Setting -> Klipper supports M220/M221 natively; forward it.
                self.cmd_queue.append(text)
            else:
                # Query -> report current value (a bare M220/M221 forwarded to
                # Klipper would reset the factor to 100%).
                self._send(self._factor_report(cmd))
            return

        # --- Custom RPC block (M980-M989) ---
        if cmd == 'M980':
            self.wifi_manager.handle_m980_status()
            return
        if cmd == 'M981':
            self.wifi_manager.handle_m981_scan()
            return
        if cmd == 'M982':
            self.wifi_manager.handle_m982_connect(text)
            return
        if cmd == 'M985':
            self._handle_m985_macros()
            return
        if cmd == 'M986':
            self._handle_m986_params()
            return
        if cmd == 'M987':
            self._handle_m987_thumb(text)
            return

        # --- Virtual SD card handling (M20, M21, M23) ---
        if cmd == 'M20':
            self._handle_m20_files()
            return
        if cmd == 'M21':
            self._send('SD card ok\r\nok\r\n')
            return
        if cmd == 'M23':
            self._handle_m23_open(text)
            return

        # --- quietly acked: Klipper has no equivalent / no-op ---
        if cmd in QUIET_ACK:
            self._send('ok\r\n')
            return

        # --- Marlin -> Klipper translations ---
        translated = self._translate(cmd, text)
        if translated == '':
            self._send('ok\r\n')  # recognised, nothing to do in Klipper
            return
        if translated is not None:
            self.cmd_queue.append(translated)
            return

        # --- everything else: forward verbatim ---
        self.cmd_queue.append(text)

    def _handle_cancel(self):
        self.selected_vsd_file = None
        self.start_busy_keepalive()
        try:
            self.gcode.run_script('CANCEL_PRINT')
        except Exception as e:
            logging.warning("TFTBridge: CANCEL_PRINT failed: %s", e)
        finally:
            self.stop_busy_keepalive()
        self._send('//action:cancel\r\nNot SD printing\r\nok\r\n')
        self.print_state = 'standby'

    def _translate(self, cmd, text):
        if cmd == 'M999':
            return 'FIRMWARE_RESTART'
        if cmd in ('FIRMWARE_RESTART', 'RESTART'):
            return cmd
        if cmd == 'G29':
            return 'BED_MESH_CALIBRATE'
        if cmd == 'M420':
            if self._param(text, 'S') == 1.:
                return 'BED_MESH_PROFILE LOAD=default'
            return 'BED_MESH_CLEAR'
        if cmd == 'M851':
            z = self._param(text, 'Z')
            if z is None:
                return 'SET_GCODE_OFFSET Z=0'
            return 'SET_GCODE_OFFSET Z=%.3f MOVE=0' % z
        if cmd == 'M24':
            if self.selected_vsd_file:
                f = self.selected_vsd_file
                self.selected_vsd_file = None
                return 'SDCARD_PRINT_FILE FILENAME="%s"' % f
            return 'RESUME'
        if cmd == 'M25':
            return 'PAUSE'
        if cmd == 'M33':
            parts = text.split(None, 1)
            raw = parts[1].strip().strip('"').lstrip('/') if len(parts) > 1 else ''
            real = self.vsd_alias_to_real.get(raw.lower(), raw)
            self.send_line(f"/{real}")
            self.send_line("ok")
            return ''
        if cmd == 'M524':
            return 'CANCEL_PRINT'
        if cmd == 'M290':
            # Babystep menu: relative Z move, applied live.
            z = self._param(text, 'Z')
            if z is None:
                return ''
            return 'SET_GCODE_OFFSET Z_ADJUST=%.3f MOVE=1' % z
        if cmd == 'M280':
            # BLTouch menu. Other servos have no Klipper equivalent.
            angle = self._param(text, 'S')
            action = BLTOUCH_ANGLES.get(int(angle)) if angle is not None \
                else None
            if self._param(text, 'P') == 0. and action:
                return 'BLTOUCH_DEBUG COMMAND=%s' % action
            return ''
        if cmd == 'M401':
            # Plain M401 deploys; "M401 H"/"M401 S" are BLTouch HS-mode
            # queries/settings Klipper does not have.
            if len(text.split()) > 1:
                return ''
            return 'BLTOUCH_DEBUG COMMAND=pin_down'
        if cmd == 'M402':
            return 'BLTOUCH_DEBUG COMMAND=pin_up'
        if cmd == 'M48':
            return 'PROBE_ACCURACY'
        if cmd == 'M303':
            # PID autotune from the Tune menu: E<heater> (-1 = bed) S<target>.
            target = self._param(text, 'S')
            if target is None:
                return ''
            heater = 'heater_bed' if self._param(text, 'E') == -1. \
                else 'extruder'
            return 'PID_CALIBRATE HEATER=%s TARGET=%d' % (heater, target)
        if cmd == 'M900':
            k = self._param(text, 'K')
            if k is None:
                return ''
            return 'SET_PRESSURE_ADVANCE ADVANCE=%.4f' % k
        if cmd == 'M204':
            # Acceleration
            accel = self._param(text, 'P')
            if accel is None:
                accel = self._param(text, 'S')
            if accel is None:
                accel = self._param(text, 'T')
            if accel is not None:
                return 'SET_VELOCITY_LIMIT ACCEL=%d' % int(accel)
            return ''
        if cmd == 'M205':
            # Jerk / Square Corner Velocity
            x = self._param(text, 'X')
            if x is not None:
                return 'SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.2f' % x
            return ''
        if cmd == 'M207':
            # Firmware Retraction
            s = self._param(text, 'S')
            f = self._param(text, 'F')
            z = self._param(text, 'Z')
            parts = []
            if s is not None:
                parts.append('RETRACT_LENGTH=%.2f' % s)
            if f is not None:
                parts.append('RETRACT_SPEED=%.2f' % (f / 60.))
            if z is not None:
                parts.append('Z_HOP=%.2f' % z)
            return ('SET_RETRACTION ' + ' '.join(parts)) if parts else ''
        if cmd == 'M500':
            return 'SAVE_CONFIG'
        if cmd == 'RESTART':
            return 'RESTART'
        if cmd == 'FIRMWARE_RESTART':
            return 'FIRMWARE_RESTART'
        return None

    def _handle_m985_macros(self):
        macros = []
        try:
            for name, _ in self.printer.lookup_objects("gcode_macro"):
                parts = name.split()
                if len(parts) > 1 and not parts[1].startswith("_"):
                    macros.append(parts[1])
            macros.sort()
        except Exception:
            pass
        for m in macros:
            self.send_line(f"//bridge:macro:{m}")
        self.send_line("//bridge:macro_done")
        self.send_line("ok")

    def _handle_m986_params(self):
        now = self.reactor.monotonic()
        pa = 0.0
        accel = 0
        scv = 0.0
        zoff = 0.0
        try:
            ext = self.printer.lookup_object('extruder', None)
            if ext is not None:
                pa = float(ext.get_status(now).get('pressure_advance', 0.0) or 0.0)
        except Exception:
            pass
        try:
            th = self.printer.lookup_object('toolhead', None)
            if th is not None:
                st = th.get_status(now)
                accel = int(st.get('max_accel', 0) or 0)
                scv = float(st.get('square_corner_velocity', 0.0) or 0.0)
        except Exception:
            pass
        try:
            gm = self.printer.lookup_object('gcode_move', None)
            if gm is not None:
                st = gm.get_status(now)
                ho = st.get('homing_origin', [0., 0., 0., 0.])
                zoff = float(ho[2] if len(ho) > 2 else 0.0)
        except Exception:
            pass
        self.send_line(f"//bridge:klipper_params:PA={pa:.4f}|ACCEL={accel}|SCV={scv:.2f}|ZOFF={zoff:.3f}")
        self.send_line("ok")


    def _handle_m987_thumb(self, text):
        hex_filename = ""
        for token in text.split()[1:]:
            if token.upper().startswith("S"):
                hex_filename = token[1:]
                break
        try:
            filename = bytes.fromhex(hex_filename).decode("utf-8", errors="ignore")
        except Exception:
            filename = hex_filename
        filename = filename.lstrip('/\\').replace('\\', '/')
        filename = self.vsd_alias_to_real.get(filename.lower(), filename)
        vsd = self.printer.lookup_object('virtual_sdcard', None)
        lines = None
        if vsd is not None and hasattr(vsd, 'sdcard_dirname') and filename:
            filepath = os.path.join(vsd.sdcard_dirname, filename)
            sub_dir, fname = os.path.split(filename)
            base_name = os.path.splitext(fname)[0]
            thumbs_dir = os.path.join(vsd.sdcard_dirname, sub_dir, '.thumbs')

            png_bytes = None
            if os.path.isdir(thumbs_dir):
                candidates = [
                    os.path.join(thumbs_dir, f"{base_name}-300x300.png"),
                    os.path.join(thumbs_dir, f"{base_name}-48x48.png"),
                    os.path.join(thumbs_dir, f"{base_name}-32x32.png"),
                ]
                for cand in candidates:
                    if os.path.isfile(cand):
                        try:
                            with open(cand, 'rb') as pf:
                                png_bytes = pf.read()
                            break
                        except Exception:
                            pass
                if not png_bytes:
                    matches = glob.glob(os.path.join(thumbs_dir, f"{glob.escape(base_name)}*.png"))
                    if matches:
                        try:
                            with open(matches[0], 'rb') as pf:
                                png_bytes = pf.read()
                        except Exception:
                            pass

            if not png_bytes and os.path.isfile(filepath):
                png_bytes = extract_gcode_png(filepath)

            if png_bytes:
                lines = render_png_to_70x70(png_bytes)

            if not lines and os.path.isfile(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        in_thumb = False
                        count = 0
                        cura_lines = []
                        for line in f:
                            s = line.strip()
                            if not in_thumb:
                                if s.startswith(';00460046') or s.startswith(';simage:') or s.startswith(';gimage:'):
                                    in_thumb = True
                            if in_thumb:
                                if not s:
                                    continue
                                if count > 0 and len(s) < 20 and (s.startswith(';00') or s.startswith(';simage:') or s.startswith(';gimage:')):
                                    break
                                cura_lines.append(s.lstrip('; '))
                                count += 1
                                if count >= 71 or (s.endswith(';') and len(s) < 5 and count > 1):
                                    break
                        if cura_lines:
                            lines = cura_lines
                except Exception:
                    pass

        if lines:
            for l in lines:
                self.send_line(f"//bridge:thumb:{l}")
                time.sleep(0.002)
        self.send_line("//bridge:thumb_done")
        self.send_line("ok")

    def _handle_m20_files(self):
        vsd = self.printer.lookup_object('virtual_sdcard', None)
        self.send_line("Begin file list")
        self.vsd_alias_to_real.clear()
        self.vsd_real_to_alias.clear()
        if vsd is not None and hasattr(vsd, 'sdcard_dirname'):
            gcode_dir = vsd.sdcard_dirname
            if os.path.isdir(gcode_dir):
                idx = 1
                for root, dirs, files in os.walk(gcode_dir):
                    for file in sorted(files):
                        if file.lower().endswith(('.gcode', '.gco', '.g')):
                            full_p = os.path.join(root, file)
                            rel_p = os.path.relpath(full_p, gcode_dir).replace('\\', '/')
                            try:
                                fsize = os.path.getsize(full_p)
                            except Exception:
                                fsize = 0

                            base_no_ext, ext = os.path.splitext(file)
                            if ' ' in file or len(base_no_ext) > 8 or len(ext) > 4:
                                clean_name = re.sub(r'[^A-Za-z0-9]', '', base_no_ext).upper()[:4]
                                if not clean_name:
                                    clean_name = "MKS"
                                alias = f"{clean_name}{idx:04d}.GCO"
                            else:
                                alias = file.upper()

                            sub_dir = os.path.dirname(rel_p)
                            alias_rel = f"{sub_dir}/{alias}" if sub_dir else alias

                            self.vsd_alias_to_real[alias.lower()] = rel_p
                            self.vsd_alias_to_real[alias_rel.lower()] = rel_p
                            self.vsd_alias_to_real[rel_p.lower()] = rel_p
                            self.vsd_real_to_alias[rel_p.lower()] = alias_rel

                            self.send_line(f"{alias_rel} {fsize} /{rel_p}")
                            idx += 1
        self.send_line("End file list")
        self.send_line("ok")

    def _handle_m23_open(self, text):
        parts = text.split(None, 1)
        raw_name = parts[1].strip().strip('"').lstrip('/') if len(parts) > 1 else ''
        filename = self.vsd_alias_to_real.get(raw_name.lower(), raw_name)
        self.selected_vsd_file = filename
        fsize = 0
        vsd = self.printer.lookup_object('virtual_sdcard', None)
        if vsd is not None and hasattr(vsd, 'sdcard_dirname') and filename:
            filepath = os.path.join(vsd.sdcard_dirname, filename)
            if os.path.isfile(filepath):
                try:
                    fsize = os.path.getsize(filepath)
                except Exception:
                    fsize = 0
        self.send_line(f"File opened: {filename} Size: {fsize}")
        self.send_line("File selected")
        self.send_line("ok")

    # ------------------------------------------------------------------ #
    # Serialized command pump
    # ------------------------------------------------------------------ #
    def _pump(self):
        if self.processing or not self.cmd_queue:
            return
        self.processing = True
        try:
            while self.cmd_queue:
                cmd = self.cmd_queue.pop(0)
                # Keep the TFT ACK timer alive while the command runs.
                self.reactor.update_timer(
                    self.busy_timer,
                    self.reactor.monotonic() + self.busy_interval)
                try:
                    self.gcode.run_script(cmd)
                except Exception as e:
                    info = str(e).replace('\n', ' ').replace('\r', ' ')
                    logging.warning("TFTBridge: cmd %r failed: %s", cmd, info)
                    self._send('echo:%s\r\n' % info)
                self.reactor.update_timer(self.busy_timer, self.reactor.NEVER)
                self._send('ok\r\n')
        finally:
            self.processing = False

    def _busy_event(self, eventtime):
        self._send('echo:busy: processing\r\n')
        return eventtime + self.busy_interval

    # ------------------------------------------------------------------ #
    # Temperature auto-report (M155)
    # ------------------------------------------------------------------ #
    def _set_autoreport(self, text):
        interval = self._param(text, 'S')
        self.autoreport_interval = float(interval) if interval else 0.
        if self.autoreport_interval > 0.:
            self.reactor.update_timer(
                self.autoreport_timer,
                self.reactor.monotonic() + self.autoreport_interval)
        else:
            self.reactor.update_timer(self.autoreport_timer,
                                      self.reactor.NEVER)

    def _autoreport_event(self, eventtime):
        if self.autoreport_interval <= 0.:
            return self.reactor.NEVER
        self._send(self._temp_report(with_ok=False))
        return eventtime + self.autoreport_interval

    # ------------------------------------------------------------------ #
    # Object-model reports
    # ------------------------------------------------------------------ #
    def _heater(self, name):
        obj = self.printer.lookup_object(name, None)
        if obj is None:
            return (0., 0.)
        try:
            st = obj.get_status(self.reactor.monotonic())
            return (st.get('temperature', 0.) or 0.,
                    st.get('target', 0.) or 0.)
        except Exception:
            return (0., 0.)

    def _temp_report(self, with_ok):
        t, tt = self._heater('extruder')
        b, bt = self._heater('heater_bed')
        body = 'T:%.1f /%.1f B:%.1f /%.1f T0:%.1f /%.1f @:0 B@:0' % (t, tt, b, bt, t, tt)
        return ('ok %s\r\n' % body) if with_ok else ('%s\r\n' % body)

    def _position_report(self):
        gm = self.printer.lookup_object('gcode_move', None)
        x = y = z = e = 0.
        if gm is not None:
            try:
                p = gm.get_status(self.reactor.monotonic())['gcode_position']
                x, y, z, e = p[0], p[1], p[2], p[3]
            except Exception:
                pass
        return 'X:%.2f Y:%.2f Z:%.2f E:%.2f\r\nok\r\n' % (x, y, z, e)

    def _factor_report(self, cmd):
        gm = self.printer.lookup_object('gcode_move', None)
        pct = 100
        if gm is not None:
            try:
                st = gm.get_status(self.reactor.monotonic())
                key = 'speed_factor' if cmd == 'M220' else 'extrude_factor'
                pct = int(round(st[key] * 100.))
            except Exception:
                pass
        if cmd == 'M220':
            # Current BTT TFT firmware parses the feedrate via the "FR:"
            # keyword, while older firmware looked for "Feedrate". ack_seen()
            # scans the whole line and ack_value() reads the number right after
            # the matched keyword, so emitting both keeps both firmwares happy.
            body = 'Feedrate: %d%% FR:%d%%' % (pct, pct)
        else:
            # "Flow:" is recognised by all known firmware versions.
            body = 'Flow: %d%%' % pct
        return 'echo:%s\r\nok\r\n' % body

    def _print_info(self):
        now = self.reactor.monotonic()
        state, filename = 'standby', ''
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None:
            try:
                st = ps.get_status(now)
                state = st.get('state', 'standby')
                filename = st.get('filename', '') or ''
            except Exception:
                pass
        pos = size = 0
        vsd = self.printer.lookup_object('virtual_sdcard', None)
        if vsd is not None:
            try:
                vst = vsd.get_status(now)
                pos = int(vst.get('file_position', 0) or 0)
                size = int(vst.get('file_size', 0) or 0)
            except Exception:
                pass
        return state, filename, pos, size

    def _sd_report(self):
        state, filename, pos, size = self._print_info()
        if state in ('printing', 'paused') and size > 0:
            return 'SD printing byte %d/%d\r\nok\r\n' % (pos, size)
        if state == 'complete':
            return 'Done printing file\r\nok\r\n'
        return 'Not SD printing\r\nok\r\n'

    def _printwatch_event(self, eventtime):
        # Detect host-initiated print start/end and notify the TFT so it
        # enters/leaves its print screen. The TFT only starts polling M27
        # (progress) after it is told a print is active via "File opened:".
        if self.tft is None:
            return self.reactor.NEVER
        self._report_fan_and_message()
        state, filename, pos, size = self._print_info()
        self._report_progress(state, pos, size)
        if state != self.print_state:
            prev = self.print_state
            self.print_state = state
            if state == 'printing' and prev not in ('printing', 'paused'):
                name = filename if filename else 'print.gcode'
                self._send('File opened: %s Size: %d\r\n' % (name, size))
                logging.info("TFTBridge: print start -> File opened: %s "
                             "Size: %d", name, size)
            elif state in ('complete', 'cancelled', 'standby') and prev in ('printing', 'paused'):
                self.selected_vsd_file = None
                if state == 'complete':
                    self._send('Done printing file\r\n')
                    logging.info("TFTBridge: print complete -> Done printing file")
                else:
                    self._send('//action:cancel\r\nNot SD printing\r\n')
                    logging.info("TFTBridge: print %s -> //action:cancel Not SD printing", state)
        return eventtime + 1.

    def _report_fan_and_message(self):
        # The TFT only learns fan speed from an "M106 P0 S<n>" line, and shows
        # "echo:" lines as notifications, so push both when Klipper changes them.
        now = self.reactor.monotonic()
        fan = self.printer.lookup_object('fan', None)
        if fan is not None:
            try:
                pwm = int(round(fan.get_status(now)['speed'] * 255.))
            except Exception:
                pwm = None
            if pwm is not None and pwm != self.fan_sent:
                self.fan_sent = pwm
                self._send('M106 P0 S%d\r\n' % pwm)
        ds = self.printer.lookup_object('display_status', None)
        if ds is not None:
            try:
                msg = (ds.get_status(now)['message'] or '').replace('\n', ' ')
            except Exception:
                msg = ''
            if msg and msg != self.msg_sent:
                self._send('echo:%s\r\n' % msg)
            self.msg_sent = msg

    def _report_progress(self, state, pos, size):
        # Same host actions the BTT slicer post-processor emits via M118: they
        # fill the TFT's "time left" and "data left" tiles.
        if state != 'printing' or size <= 0:
            self.progress_sent = None
            return
        pct = int(100. * pos / size)
        # shortcut: time left is linear in file position, so it is rough on
        # prints with a slow start (first layers); upgrade to slicer M73 R.
        ps = self.printer.lookup_object('print_stats', None)
        try:
            elapsed = ps.get_status(self.reactor.monotonic())['print_duration']
        except Exception:
            return
        if pct < 1 or pos <= 0:
            return
        left = int(elapsed * (size - pos) / pos)
        key = (pct, left // 60)
        if key == self.progress_sent:
            return
        self.progress_sent = key
        self._send('//action:notification Data Left %d/100\r\n'
                   '//action:notification Time Left %02dh%02dm%02ds\r\n'
                   % (pct, left // 3600, left // 60 % 60, left % 60))

    def _m115_report(self):
        caps = [
            'FIRMWARE_NAME:Klipper (via TFT bridge) '
            'SOURCE_CODE_URL:github.com/bigtreetech/BIGTREETECH-TouchScreenFirmware '
            'PROTOCOL_VERSION:1.0 MACHINE_TYPE:%s EXTRUDER_COUNT:1 '
            'UUID:00000000-0000-0000-0000-000000000000' % self.machine_type,
            'Cap:EEPROM:1',
            'Cap:AUTOREPORT_TEMP:1',
            'Cap:AUTOREPORT_SD_STATUS:0',
            'Cap:SDCARD:1',
            'Cap:AUTOREPORT_POS:0',
            'Cap:AUTOLEVEL:1',
            'Cap:Z_PROBE:1',
            'Cap:LEVELING_DATA:1',
            'Cap:BABYSTEPPING:1',
            'Cap:SOFTWARE_POWER:0',
            'Cap:TOGGLE_LIGHTS:0',
            'Cap:EMERGENCY_PARSER:1',
            'Cap:CHAMBER_TEMPERATURE:0',
            'Cap:LONG_FILENAME:1',
            'ok',
        ]
        return '\r\n'.join(caps) + '\r\n'

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _param(self, text, letter):
        letter = letter.upper()
        for tok in text.split()[1:]:
            if tok[:1].upper() == letter:
                try:
                    return float(tok[1:])
                except ValueError:
                    return None
        return None


def load_config(config):
    return TftBridge(config)
