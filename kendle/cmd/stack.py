"""kendle stack - the feature's local services, run by the team.

  kendle stack services                  known services, their run configuration and port
  kendle stack status                    this feature's services: up / starting / crashed / stopped
  kendle stack start <service> ...       start them (no names: the feature's saved set)
  kendle stack stop [service ...]        stop them (no names: all)
  kendle stack restart <service> ...
  kendle stack restart-affected          restart only services whose code changed since they started
  kendle stack logs <service> [-n N] [--errors] [--all]
  kendle stack wait [service ...]        wait until up or crashed (max 9 min; run again if still starting);
                                         at once 'not started' for one never started or stopped
  kendle stack stop-idle [--minutes N]   stop stacks whose feature did nothing for N min (default 30; the
                                         console does this every minute on its own)

Options: -f <feature> (default: the worktree you are in) · --replace (start: when two stacks already run,
         stop the oldest). Up to two features run at once: stack 1 on the usual ports, stack 2 with
         every shiftable port moved (kendle.toml services.shift_by) on its own host, so logins don't collide.
Services are defined in kendle.toml [services]; `kendle stack services` lists them.
Only processes kendle stack started are ever stopped; a port held by anything else is refused.
"""
import os, sys, time
from kendle import core, services
from kendle import stack as _stack


def ago(t):
    d = int(time.time() - t)
    return f"{d // 3600}h{(d % 3600) // 60:02d}m" if d >= 3600 else f"{d // 60}m{d % 60:02d}s"


def show(rows):
    if not rows:
        print("  no services started for this feature")
    for s in rows:
        up = ago(s["started"]) if s["state"] in ("up", "starting") else ""
        print(f"  {s['state']:<9} {s['service']:<26} port {str(s['port']):<5} {up:<8} {s['log']}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip()); return 0
    feature, replace, n, errors, all_runs = None, False, 80, False, False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-f": feature = argv[i + 1]; i += 2; continue
        if a in ("-n", "--minutes"): n = int(argv[i + 1]); i += 2; continue
        if a == "--replace": replace = True
        elif a == "--errors": errors = True
        elif a == "--all": all_runs = True
        else: rest.append(a)
        i += 1
    cmd, names = rest[0], rest[1:]
    try:
        if cmd == "services":
            f = _stack.feature_here() or {"path": core.PRIMARY}
            if not _stack.SERVICES:
                print("  no services - define them in kendle.toml [services]")
            for name in _stack.SERVICES:
                port, source = services.describe(name, f["path"])
                print(f"  {name:<26} port {str(port):<5} {source}")
        elif cmd == "status":
            f = _stack.resolve(feature)
            print(f"{f['name']}  (stack {_stack.slot_of(f['name']) + 1} · app {_stack.url_of(f['name'])}):")
            show(_stack.status(f["name"]))
            newest, busy = _stack.last_activity(f["name"])
            if any(s["state"] in ("up", "starting") for s in _stack.status(f["name"])):
                print("  sessions: busy right now" if busy else
                      f"  last session activity: {ago(newest) + ' ago' if newest else 'none'} "
                      f"(services stop after {_stack.idle_minutes()} min idle)")
            others = [x for x in _stack.running_stacks() if x != f["name"]]
            if others: print(f"  (also running: {', '.join(others)})")
        elif cmd == "start":
            for name, what in _stack.start(feature, names, replace): print(f"  {name}: {what}")
            f = _stack.resolve(feature)
            print(f"  stack {_stack.slot_of(f['name']) + 1} · app at {_stack.url_of(f['name'])}")
            print("  follow with: kendle stack wait" + (f" -f {feature}" if feature else ""))
        elif cmd == "stop":
            done = _stack.stop(feature, names or None); print("  stopped:", ", ".join(done) or "nothing was running")
        elif cmd == "restart" and names:
            for name, what in _stack.restart(feature, names): print(f"  {name}: {what}")
        elif cmd == "restart-affected":
            hits = _stack.restart_affected(feature)
            if not hits: print("  nothing to restart - no running service's code changed")
            for name, files in hits.items(): print(f"  restarting {name}: {', '.join(files[:4])}{' …' if len(files) > 4 else ''}")
        elif cmd == "logs" and len(names) == 1:
            print("\n".join(_stack.logs(feature, names[0], n, errors, all_runs)))
        elif cmd == "stop-idle":
            done = _stack.stop_idle(n if "-n" in argv or "--minutes" in argv else None)
            print("  stopped idle stacks:", ", ".join(done) or "none")
        elif cmd == "wait":
            rows, verdict = _stack.wait(feature, names or None)
            show(rows); print(f"  -> {verdict}")
            return {"up": 0, "crashed": 1, "not started": 1}.get(verdict, 75)
        else:
            print(__doc__.strip(), file=sys.stderr); return 2
    except (LookupError, ValueError, RuntimeError) as err:
        print("kendle stack:", err, file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
