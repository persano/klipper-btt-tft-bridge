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
