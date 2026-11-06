import time, traceback, subprocess, os
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
from locum import Desktop
d = Desktop()
p = subprocess.Popen(["mousepad"])
time.sleep(4)
f = d.focused()
print("focused", f and (f.role, f.bounds, f.node.pid))
acc = f.node._acc
print("acc type", type(acc), acc.get_role_name())
t = acc.get_text_iface() if hasattr(acc, "get_text_iface") else None
print("text iface", t, type(t))
try:
    print("count", Atspi.Text.get_character_count(acc))
except Exception: traceback.print_exc()
print("set ok", f.node.set_value("hello probe"))
time.sleep(1)
for label, fn in [("iface.get_text(0,-1)", lambda: t.get_text(0, -1)),
                  ("Atspi.Text.get_text(acc,0,-1)", lambda: Atspi.Text.get_text(acc, 0, -1)),
                  ("count", lambda: Atspi.Text.get_character_count(acc)),
                  ("get_text(0,count)", lambda: Atspi.Text.get_text(acc, 0, Atspi.Text.get_character_count(acc))),
                  ("node.value", lambda: f.node.value)]:
    try:
        print(label, repr(fn()))
    except Exception:
        print(label, "EXC"); traceback.print_exc()
d.type("typed!", verify=False)
time.sleep(1)
print("after type value", repr(f.node.value), "count", Atspi.Text.get_character_count(acc))
f2 = d.focused()
print("refocused same?", f2 and f2.node._acc == acc, f2 and repr(f2.node.value))
p.terminate()
