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

import logging
import serial


# Marlin settings/query commands the TFT sends from its menus that Klipper has
# no equivalent for. Acked quietly instead of showing an "Unknown command"
# error toast on the screen.
QUIET_ACK = frozenset(
    'M22 M33 M36 M43 M92 M98 M125 M141 M154 M200 M201 M203 M205 M206 M207 '
    'M208 M209 M211 M218 M291 M292 M300 M301 M304 M306 M355 M376 M408 M409 '
    'M500 M501 M502 M503 M552 M564 M569 M593 M665 M666 M710 M876 M906 M913 '
    'M914 M928'.split())

# M280 P0 S<angle> servo angles the TFT BLTouch menu uses -> BLTOUCH_DEBUG.
BLTOUCH_ANGLES = {10: 'pin_down', 60: 'touch_mode', 90: 'pin_up',
                  120: 'reset', 130: 'self_test'}


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

        self.tft = None
        self.fd_handle = None
        self.rx_buf = b''
        self.cmd_queue = []
        self.processing = False
        self.output_hooked = False

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
        logging.debug("TFTBridge ->TFT: %r", data)

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
            logging.exception("TFTBridge: read failed")
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
        logging.debug("TFTBridge TFT->: %s", text)

        # --- local status: answered immediately, never queued ---
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

    def _translate(self, cmd, text):
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
            return 'RESUME'
        if cmd == 'M25':
            return 'PAUSE'
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
            action = BLTOUCH_ANGLES.get(int(angle)) if angle is not None                 else None
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
            heater = 'heater_bed' if self._param(text, 'E') == -1.                 else 'extruder'
            return 'PID_CALIBRATE HEATER=%s TARGET=%d' % (heater, target)
        if cmd == 'M900':
            k = self._param(text, 'K')
            if k is None:
                return ''
            return 'SET_PRESSURE_ADVANCE ADVANCE=%.4f' % k
        return None

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
        body = 'T:%.1f /%.1f B:%.1f /%.1f @:0 B@:0' % (t, tt, b, bt)
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
            elif state == 'complete' and prev in ('printing', 'paused'):
                self._send('Done printing file\r\n')
                logging.info("TFTBridge: print complete -> Done printing file")
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
            'FIRMWARE_NAME:Marlin 2.1.2 (Klipper bridge) '
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
