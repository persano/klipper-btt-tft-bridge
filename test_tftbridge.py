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
