"""kendle services - which local services are up. Checks only; never starts or stops anything.

  kendle services              show every service in kendle.toml that has a port
  kendle services <name> ...   show those; exit 1 if any is down

A service is up when something listens on its port (or on its second-stack port).
"""
import subprocess, sys
from kendle import services

KNOWN = {n: s["port"] for n, s in services.SERVICES.items() if isinstance(s.get("port"), int)}
want = sys.argv[1:]
if any(w in ("-h", "--help") for w in want):
    print(__doc__.strip()); sys.exit(0)
bad = [w for w in want if w not in KNOWN]
if bad:
    print("kendle services: unknown", ", ".join(bad), "- known:", " ".join(KNOWN) or "none (kendle.toml [services])",
          file=sys.stderr); sys.exit(2)
out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
listening = {}
for line in out.splitlines()[1:]:
    parts = line.split()
    if len(parts) > 8:
        port = parts[8].rsplit(":", 1)[-1]
        if port.isdigit():
            listening.setdefault(int(port), parts[0])
down = []
for name in (want or KNOWN):
    port = KNOWN[name]
    ports = [port] + ([port + services.offset(1)] if port in services.SHIFTABLE else [])
    hit = next((p for p in ports if p in listening), None)
    print(f"  {'up  ' if hit else 'DOWN'}  {name:<26} {('port %d (%s)' % (hit, listening[hit])) if hit else 'port ' + '/'.join(map(str, ports))}")
    if not hit: down.append(name)
if not KNOWN:
    print("  no services with a port - define them in kendle.toml [services]")
if want and down:
    print(f"\ndown: {' '.join(down)} - the team starts services with: kendle stack start <service> ..."); sys.exit(1)
