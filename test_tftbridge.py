# Self-check for fan/M117 reporting: python test_tftbridge.py
import sys, types
sys.modules['serial'] = types.ModuleType('serial')
import tftbridge

class Obj:
    def __init__(self, st): self.st = st
    def get_status(self, t): return self.st

b = tftbridge.TftBridge.__new__(tftbridge.TftBridge)
sent = []
b._send = sent.append
b.reactor = types.SimpleNamespace(monotonic=lambda: 0.)
fan, ds = Obj({'speed': 0.5}), Obj({'message': 'Hello'})
b.printer = types.SimpleNamespace(
    lookup_object=lambda n, d=None: {'fan': fan, 'display_status': ds}.get(n, d))
b.fan_sent, b.msg_sent = None, ''
b._report_fan_and_message()
b._report_fan_and_message()  # unchanged -> nothing new
assert sent == ['M106 P0 S128\r\n', 'echo:Hello\r\n'], sent
fan.st['speed'] = 0
b._report_fan_and_message()
assert sent[-1] == 'M106 P0 S0\r\n', sent
print('ok')

# progress: 50% of the file after 600 s printing -> 600 s (10 min) left
ps = Obj({'print_duration': 600.})
b.printer.lookup_object = lambda n, d=None: {'print_stats': ps}.get(n, d)
b.progress_sent = None
sent.clear()
b._report_progress('printing', 50, 100)
b._report_progress('printing', 50, 100)  # unchanged -> no resend
assert sent == ['//action:notification Data Left 50/100\r\n'
                '//action:notification Time Left 00h10m00s\r\n'], sent
b._report_progress('paused', 50, 100)
assert b.progress_sent is None
print('ok progress')

# Marlin -> Klipper translations of commands the TFT menus emit
t = tftbridge.TftBridge.__new__(tftbridge.TftBridge)
for line, want in [
        ('M290 Z0.05', 'SET_GCODE_OFFSET Z_ADJUST=0.050 MOVE=1'),
        ('M280 P0 S10', 'BLTOUCH_DEBUG COMMAND=pin_down'),
        ('M280 P0 S90', 'BLTOUCH_DEBUG COMMAND=pin_up'),
        ('M280 P1 S90', ''), ('M401', 'BLTOUCH_DEBUG COMMAND=pin_down'),
        ('M401 H', ''), ('M48', 'PROBE_ACCURACY'),
        ('M303 E-1 S60 C8', 'PID_CALIBRATE HEATER=heater_bed TARGET=60'),
        ('M303 E0 S200', 'PID_CALIBRATE HEATER=extruder TARGET=200'),
        ('M900 K0.05', 'SET_PRESSURE_ADVANCE ADVANCE=0.0500'),
        ('M104 S200', None)]:
    got = t._translate(line.split()[0], line)
    assert got == want, (line, got)
assert 'M203' in tftbridge.QUIET_ACK
print('ok translate')
