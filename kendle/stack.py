"""The team's local services for a feature: start, stop, restart, status, logs.

What each service is comes from kendle.toml (kendle.services: a plain command, an IntelliJ run
configuration, or a docker compose service). Here they are run from the feature's worktree, each in
its own process group, with its output in .cache/kendle/stack/<feature>/<service>.log. Only processes
started here are ever stopped: a port held by anything else is refused, never killed.
Two features can run at once: the second stack moves every shiftable port (services.shift_by).
"""
import glob, json, os, re, signal, subprocess, time
from kendle import core, services

STATE = os.path.join(core.STATE, "stack")
SERVICES = services.SERVICES
SLOTS = (0, 1)                 # two stacks at most; slot 1 runs with every shiftable port moved
ERROR_RE = re.compile(r"\b(ERROR|FATAL|Exception|Caused by:|FAILED|BUILD FAILURE|Error:)")


# ---- where and how -----------------------------------------------------------------

def feature_here():
    cwd = os.path.realpath(os.getcwd())
    for f in core.features():
        if cwd == f["path"] or cwd.startswith(f["path"] + os.sep):
            return f
    return None


def resolve(name=None):
    if name:
        return core.feature(name)
    f = feature_here()
    if not f:
        raise LookupError("run it inside a feature worktree, or pass -f <feature>")
    return f


# ---- state ------------------------------------------------------------------------

def _dir(feature):
    d = os.path.join(STATE, feature)
    os.makedirs(d, exist_ok=True)
    return d


def _state(feature, name):
    try:
        with open(os.path.join(STATE, feature, name + ".json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save(feature, name, st):
    path = os.path.join(_dir(feature), name + ".json")
    with open(path + ".tmp", "w") as fh:
        json.dump(st, fh)
    os.replace(path + ".tmp", path)


def log_path(feature, name):
    return os.path.join(_dir(feature), name + ".log")


_procs = {"t": 0.0}


def _snapshot(max_age=1.0):
    """One `ps` and one `lsof` for every service at once, reused for a second: live process groups,
    pid -> process group, and port -> (pid, command) of each listener."""
    if time.time() - _procs["t"] < max_age:
        return _procs
    groups, pgid_of, listen = set(), {}, {}
    for line in subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            pgid_of[int(parts[0])] = int(parts[1])
            if not parts[2].startswith("Z"):
                groups.add(int(parts[1]))
    pid = cmd = None
    for line in subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"], capture_output=True, text=True).stdout.splitlines():
        if line.startswith("p"):
            pid, cmd = int(line[1:]), None
        elif line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                listen.setdefault(int(port), (pid, cmd))
    _procs.update(groups=groups, pgid_of=pgid_of, listen=listen)
    _procs["t"] = time.time()                     # last, so another thread never sees a half-filled snapshot
    return _procs


def _fresh():
    _procs["t"] = 0.0                                 # after starting or stopping something


def _group_alive(pgid):
    """Any live (non-zombie) process left in the group."""
    return pgid in _snapshot()["groups"]


def listener(port):
    """(pid, command) of whatever listens on a port, or None."""
    return _snapshot()["listen"].get(port)


def _pgid(pid):
    return _snapshot()["pgid_of"].get(pid)


def status(feature):
    """Every service ever started for this feature, with its state: up, starting, crashed, stopped."""
    out = []
    d = os.path.join(STATE, feature)
    for fname in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fname.endswith(".json") or fname[:-5] not in SERVICES:
            continue
        st = _state(feature, fname[:-5])
        if not st:
            continue
        alive = _group_alive(st["pgid"]) if not st.get("stopped") else False
        lis = listener(st["port"]) if st.get("port") else None
        # "up": its port answers from its own process group - or from anyone, for a service whose
        # listener runs elsewhere (a docker container: the port belongs to docker's proxy)
        ours = bool(lis) and (_pgid(lis[0]) == st["pgid"] or st.get("up_when") == "listening")
        state = ("up" if alive and ours else "starting" if alive else "stopped" if st.get("stopped") else "crashed")
        out.append(dict(st, state=state))
    return out


def running_stacks():
    if not os.path.isdir(STATE):
        return []
    return sorted({s["feature"] for feat in os.listdir(STATE) for s in status(feat) if s["state"] in ("up", "starting")})


def slot_of(feature):
    try:
        with open(os.path.join(STATE, feature, "stack.json")) as fh:
            return int(json.load(fh).get("slot", 0))
    except (OSError, ValueError):
        return 0


def url_of(feature):
    return services.app_url(slot_of(feature)) or "-"


def _allocate(feature, replace):
    """This feature's slot: the one it already runs in, else the first free; at most two stacks."""
    running = [x for x in running_stacks() if x != feature]
    if feature in running_stacks():
        return slot_of(feature)
    used = {slot_of(x): x for x in running}
    free = [n for n in SLOTS if n not in used]
    if free:
        return free[0]
    if not replace:
        raise RuntimeError(f"two stacks are already running ({', '.join(sorted(running))}) - stop one first "
                           f"(kendle stack stop -f <feature>) or pass --replace to stop the oldest")
    oldest = min(running, key=lambda x: min(s["started"] for s in status(x) if s["state"] in ("up", "starting")))
    stop(oldest)
    return slot_of(oldest)


# ---- actions ----------------------------------------------------------------------

def start(feature_name, names, replace=False):
    f = resolve(feature_name)
    feature = f["name"]
    names = names or (json.load(open(os.path.join(STATE, feature, "stack.json"))).get("services", [])
                      if os.path.exists(os.path.join(STATE, feature, "stack.json")) else [])
    if not names:
        raise ValueError("name the services to start: " + " ".join(n for n in SERVICES if services.startable(n)))
    _fresh()
    slot = _allocate(feature, replace)
    done = []
    for name in names:
        cur = _state(feature, name)
        if cur and not cur.get("stopped") and _group_alive(cur["pgid"]):
            done.append((name, "already running"))
            continue
        cfg = services.spec(name, f, slot)
        lis = listener(cfg["port"]) if cfg.get("port") else None
        if lis:
            raise RuntimeError(f"port {cfg['port']} for {name} is held by pid {lis[0]} ({lis[1]}), which kendle stack "
                               "did not start - stop it yourself (an IDE run?) and retry")
        log = open(log_path(feature, name), "a")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {name} in {feature} "
                  f"({cfg['label']}, stack {slot + 1}, port {cfg.get('port')}) =====\n")
        log.flush()
        p = subprocess.Popen(cfg["argv"], cwd=cfg["cwd"], env=cfg["env"], stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        head = subprocess.run(["git", "-C", f["path"], "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        _save(feature, name, {"service": name, "feature": feature, "pid": p.pid, "pgid": p.pid, "port": cfg.get("port"),
                              "started": time.time(), "head": head, "config": cfg["label"], "log": log.name,
                              "stopped": False, "slot": slot, "up_when": cfg["up_when"], "stop": cfg["stop"],
                              "cwd": cfg["cwd"]})
        done.append((name, f"starting on port {cfg.get('port')}"))
        _fresh()
    stack = os.path.join(_dir(feature), "stack.json")
    prev = json.load(open(stack)).get("services", []) if os.path.exists(stack) else []
    with open(stack, "w") as fh:
        json.dump({"services": sorted(set(prev) | set(names)), "slot": slot}, fh)
    return done


def stop(feature_name, names=None, reason=""):
    # by name, worktree or not: a stack must stay stoppable after its worktree is removed
    feature = feature_name or resolve()["name"]
    done = []
    for st in status(feature):
        if names and st["service"] not in names:
            continue
        if st["state"] in ("up", "starting"):
            try:
                os.killpg(st["pgid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(80):
                _fresh()
                if not _group_alive(st["pgid"]):
                    break
                time.sleep(0.25)
            _fresh()
            if _group_alive(st["pgid"]):
                os.killpg(st["pgid"], signal.SIGKILL)
            if st.get("stop"):                        # e.g. docker compose stop: the container is not in the group
                subprocess.run(st["stop"], cwd=st.get("cwd"), capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            with open(st["log"], "a") as log:
                log.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} stopped by kendle stack"
                          f"{' - ' + reason if reason else ''} =====\n")
            done.append(st["service"])
        st = {k: v for k, v in st.items() if k != "state"}
        st.update(stopped=True, stopped_at=time.time(), stopped_reason=reason or "stopped")
        _save(feature, st["service"], st)
    return done


def restart(feature_name, names):
    feature = resolve(feature_name)["name"]
    stop(feature, names)
    return start(feature, names)


def affected(feature_name=None):
    """Running services whose code changed since they started: {service: [files]}."""
    f = resolve(feature_name)
    out = {}
    for st in status(f["name"]):
        if st["state"] not in ("up", "starting"):
            continue
        git = ["git", "-C", f["path"]]
        changed = set(subprocess.run(git + ["diff", "--name-only", st["head"], "HEAD"], capture_output=True, text=True).stdout.split())
        for line in subprocess.run(git + ["status", "--porcelain"], capture_output=True, text=True).stdout.splitlines():
            path = line[3:].split(" -> ")[-1]
            full = os.path.join(f["path"], path)
            if not os.path.exists(full) or os.path.getmtime(full) > st["started"]:
                changed.add(path)
        hits = sorted(p for p in changed if st["service"] in SERVICES and services.affects(st["service"], p))
        if hits:
            out[st["service"]] = hits
    return out


def restart_affected(feature_name=None):
    hits = affected(feature_name)
    if hits:
        restart(feature_name, list(hits))
    return hits


def _last_event(path):
    """(time of the newest chat event, whether a tool call is still running) for a transcript."""
    try:
        for ev in core.events_backwards(path) if path else []:
            if ev.get("type") in ("user", "assistant"):
                t = core.epoch(ev.get("timestamp")) or 0
                pending = ev.get("type") == "assistant" and (ev.get("message") or {}).get("stop_reason") == "tool_use"
                return t, pending
    except OSError:
        pass
    return 0, False


def last_activity(feature):
    """When the feature's sessions (manager, its roles, spawned subs) last did anything, and whether
    one is in the middle of a tool call right now (a long build or test prints nothing for a while)."""
    newest, busy = 0, False
    for e in core.load():
        if e["feature"] != feature or e["kind"] not in ("manager", "sub"):
            continue
        t = core.transcript(e["id"], e.get("cwd"))
        paths = [t] + (glob.glob(t[:-6] + "/subagents/agent-*.jsonl") if t else [])
        for p in paths:
            when, pending = _last_event(p)
            newest = max(newest, when)
            busy = busy or (pending and time.time() - when < 3 * 3600)   # a tool call "running" for hours is a dead session
    return newest, busy


def idle_minutes():
    return int(core.ui_get("stack_idle_minutes") or services.SETTINGS["idle_minutes"])


def stop_idle(minutes=None):
    """Stop every running stack whose feature's sessions have done nothing for `minutes` (kendle.toml services.idle_minutes)."""
    minutes = minutes or idle_minutes()
    stopped = []
    for feature in running_stacks():
        rows = [s for s in status(feature) if s["state"] in ("up", "starting")]
        if not rows:
            continue
        try:
            newest, busy = last_activity(feature)
            since = max(newest, max(s["started"] for s in rows))
            if not busy and time.time() - since > minutes * 60:
                stop(feature, reason=f"idle for {minutes} min - no session activity")
                stopped.append(feature)
        except Exception:                             # one bad stack never stops the check for the others
            continue
    return stopped


def logs(feature_name, name, n=80, errors=False, all_runs=False):
    feature = resolve(feature_name)["name"]
    path = log_path(feature, name)
    if not os.path.exists(path):
        raise LookupError(f"no log for {name} in {feature}")
    lines = open(path, errors="replace").read().splitlines()
    if not all_runs:
        starts = [i for i, l in enumerate(lines) if l.startswith("===== ") and " start " in l]
        lines = lines[starts[-1]:] if starts else lines
    if errors:
        keep, after = [], 0
        for l in lines:
            if ERROR_RE.search(l):
                keep.append(l); after = 3
            elif after and (l.startswith(("\t", "    at ", "Caused by")) or l.strip().startswith("at ")):
                keep.append(l); after -= 1
            else:
                after = 0
        lines = keep
    return lines[-n:]


def wait(feature_name, names=None, timeout=540):
    """Until every named service is up or one crashed; returns the final status rows and a verdict."""
    feature = resolve(feature_name)["name"]
    end = time.time() + timeout
    while True:
        _fresh()
        rows = [s for s in status(feature) if (not names or s["service"] in names) and not s.get("stopped")]
        if any(s["state"] == "crashed" for s in rows):
            return rows, "crashed"
        if rows and all(s["state"] == "up" for s in rows):
            return rows, "up"
        if time.time() > end:
            return rows, "starting"
        time.sleep(5)
