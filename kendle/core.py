"""Core of the kendle console: feature worktrees, session registry, tmux panes, transcripts.

Shared by every kendle subcommand (CLI, curses sidebar, viewer). Python 3.9 stdlib only.
Every tmux call goes to a socket of its own per workspace (tmux -L kendle-<hash>), so the
console never touches any other tmux use on this machine, nor another workspace's console.
"""
import calendar, contextlib, datetime, fcntl, glob, hashlib, json, os, re, shutil, subprocess, time, uuid
from kendle import config

HUB      = config.find_workspace()
CONFIG   = config.load(HUB)
ROOT     = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
KENDLE     = os.path.join(ROOT, "bin", "kendle")          # the one entry point; panes run its subcommands
PRIMARY  = os.path.join(HUB, CONFIG["repo"]["path"])  # the git repo: never a feature
REMOTE   = CONFIG["repo"]["remote"]
BASE     = CONFIG["repo"]["base"]
UPSTREAM = f"{REMOTE}/{BASE}"                         # what features start from and the Ask desk follows
DOCS     = os.path.join(HUB, CONFIG["workspace"]["docs"])
NAME     = CONFIG["workspace"]["name"] or os.path.basename(HUB)
STATE    = os.environ.get("KENDLE_STATE") or os.path.join(HUB, ".cache", "kendle")
REGISTRY = os.path.join(STATE, "agents.json")
PROJECTS = os.environ.get("KENDLE_PROJECTS") or os.path.expanduser("~/.claude/projects")   # tests use their own
SOCKET   = os.environ.get("KENDLE_SOCKET") or \
           "kendle-" + hashlib.sha1(HUB.encode()).hexdigest()[:8]   # one per workspace; tests use their own
SESSION  = "kendle"                                   # also in tmux.conf
SIDEBAR_WIDTH = 34
NAME_RE  = r"[a-z0-9][a-z0-9-]{0,39}"
RESERVE_GB = CONFIG["workspace"]["reserve_gb"]      # free disk a new feature needs

# Read-only sessions never edit. The deny list closes the edit tools and stops the model proposing
# to leave plan mode; plan mode itself does not block Bash (tested 2026-10), so what
# a session runs is held back by its prompt. --disallowedTools is variadic, so the prompt must come
# BEFORE it. The Ask desk and reviews may also run their own folder's services (kendle stack).
READ_ONLY = ["--permission-mode", "plan",
             "--disallowedTools", "Edit", "Write", "NotebookEdit", "ExitPlanMode"]
STACK_READ_ONLY = READ_ONLY + ["--allowedTools", "Bash(kendle stack *)"]
ASK = "ask"                                         # the Ask desk's registry name
ASK_PATH = os.path.join(HUB, CONFIG["workspace"]["ask"])   # one worktree on the latest base


TASK_HINT = "If the user mentions a task or ticket, read it with `kendle task <id or url>` - it works from any folder."


def note(name, **fields):
    """A session prompt from kendle.toml (or the default), with the workspace's facts filled in. The
    Ask desk and reviews are told they may run their folder's services; with a task tracker set up,
    the sessions that talk to the user are told how to read a task."""
    text = config.fill(CONFIG["prompts"][name], remote=REMOTE, base=BASE,
                       docs=CONFIG["workspace"]["docs"], **fields)
    if name in ("ask", "review") and "kendle stack" not in text:
        text += " " + note("stack")
    if CONFIG["task"]["fetch"] and name in ("ask", "review", "manager") and "kendle task" not in text:
        text += " " + TASK_HINT
    return text


def git(*args, cwd=None):
    """git in a worktree (the primary repo by default), stdout stripped."""
    return subprocess.run(["git", "-C", cwd or PRIMARY, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


REVIEW_SLOTS = int(CONFIG["review"]["slots"])      # at most this many of someone else's changes at once
RESERVED = {".cache", CONFIG["workspace"]["docs"], CONFIG["workspace"]["ask"],
            os.path.basename(PRIMARY), *CONFIG["workspace"]["reserved"]} | \
           {f"review-{n}" for n in range(1, REVIEW_SLOTS + 1)}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def label(e):
    return f"{e['feature']}/{e['name']}"


# ---- tmux ---------------------------------------------------------------------

def tmux(*args, check=True):
    r = subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"tmux {args[0]}: {r.stderr.strip()}")
    return r.stdout.strip()


def clean_env():
    """Environment for the tmux server, minus variables a parent Claude session
    leaks - sessions started from inside Claude then behave like ones from a shell."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def snapshot():
    """(server id, {pane id: {dead, window, index}}); (None, {}) when not running.
    Pane ids restart with the tmux server, so a recorded pane only counts together
    with the server id it was recorded under."""
    r = subprocess.run(["tmux", "-L", SOCKET, "list-panes", "-s", "-t", SESSION, "-F",
                        "#{pid}:#{start_time}\t#{pane_id}\t#{pane_dead}\t#{window_name}\t#{pane_index}"],
                       capture_output=True, text=True)
    server, panes = None, {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            server, pane, dead, window, index = line.split("\t")
            panes[pane] = {"dead": dead == "1", "window": window, "index": int(index)}
    return server, panes


def is_live(entry, snap):
    server, panes = snap
    p = panes.get(entry.get("pane") or "")
    return bool(p) and entry.get("server") == server and not p["dead"]


TRUST_RE = re.compile(r"trust (this|the files in this) folder", re.I)
PERMISSION_RE = re.compile(r"Do you want to (proceed|make this edit|create|allow|run)", re.I)


def screen(e):
    """What a live session's screen waits for that its transcript cannot show: 'trust' (Claude Code's
    question about trusting the folder, asked before any chat), 'permission' (a tool it may not run
    unasked), or None."""
    r = subprocess.run(["tmux", "-L", SOCKET, "capture-pane", "-p", "-t", e["pane"]],
                       capture_output=True, text=True)
    return "trust" if TRUST_RE.search(r.stdout) else "permission" if PERMISSION_RE.search(r.stdout) else None


def held_at_trust(e):
    return screen(e) == "trust"


def type_into(e, text):
    """Send a live session its next message, however many lines: pasted as one (bracketed paste),
    then Enter - the session goes on running, with any teammates it has in process."""
    buf = f"kendle-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "-L", SOCKET, "load-buffer", "-b", buf, "-"], input=text, text=True, check=True)
    tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", e["pane"])
    time.sleep(0.5)                                  # let the paste land before it is submitted
    tmux("send-keys", "-t", e["pane"], "Enter")


def reply_since(e, since):
    """The text of the session's turn that answered a message sent at `since` (epoch seconds), or ""."""
    t = transcript(e["id"], e.get("cwd"))
    texts = []
    for ev in events_backwards(t) if t else []:
        stamp = epoch(ev.get("timestamp"))
        if stamp is not None and stamp < int(since):
            break
        msg = ev.get("message") or {}
        content = msg.get("content")
        if ev.get("type") == "assistant":
            texts += [c.get("text", "") for c in reversed(content or []) if isinstance(c, dict) and c.get("type") == "text"]
        elif ev.get("type") == "user" and not ev.get("isMeta") and not (
                isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)):
            break                                    # the message this turn answers
    return "\n\n".join(t for t in reversed(texts) if t.strip())


def display_pane(snap=None):
    """The pane currently in the console's right-hand slot."""
    for pane, p in (snap or snapshot())[1].items():
        if p["window"] == "console" and p["index"] == 1:
            return pane
    return None


def ensure_session():
    """Start the console on its socket if needed, and put back a missing right pane."""
    if subprocess.run(["tmux", "-L", SOCKET, "has-session", "-t", SESSION],
                      capture_output=True).returncode:
        left = [KENDLE, "sidebar"] if os.environ.get("KENDLE_NO_SIDEBAR") != "1" else [KENDLE, "view", "idle"]
        subprocess.run(["tmux", "-L", SOCKET, "-f", os.path.join(ROOT, "kendle", "tmux.conf"),
                        "new-session", "-d", "-s", SESSION, "-n", "console", "-c", HUB,
                        "-x", "200", "-y", "50", *left],
                       env=dict(clean_env(), KENDLE_WORKSPACE=HUB), check=True, capture_output=True)
    snap = snapshot()
    if display_pane(snap) is None:
        idle = tmux("show", "-gqv", "@kendle_idle", check=False)
        if idle in snap[1]:
            tmux("join-pane", "-h", "-d", "-s", idle, "-t", f"{SESSION}:console.0")
        else:
            idle = tmux("split-window", "-h", "-d", "-P", "-F", "#{pane_id}", "-t", f"{SESSION}:console.0",
                        "-c", HUB, KENDLE, "view", "idle")
            tmux("set", "-g", "@kendle_idle", idle)
        tmux("resize-pane", "-t", f"{SESSION}:console.0", "-x", str(sidebar_width()))
    # the logs column is put up by the sidebar when the selected feature's services run (logs_mode)


def sidebar_width():
    """The width the user chose (keys < > or dragging the border), else the default."""
    try:
        with open(os.path.join(STATE, "ui.json")) as f:
            return max(24, min(90, int(json.load(f).get("sidebar_width", SIDEBAR_WIDTH))))
    except (OSError, ValueError, TypeError):
        return SIDEBAR_WIDTH


def set_sidebar_width(width):
    width = max(24, min(90, int(width)))
    path = os.path.join(STATE, "ui.json")
    os.makedirs(STATE, exist_ok=True)
    try:
        with open(path) as f:
            ui = json.load(f)
    except (OSError, ValueError):
        ui = {}
    ui["sidebar_width"] = width
    with open(path + ".tmp", "w") as f:
        json.dump(ui, f)
    os.replace(path + ".tmp", path)
    return width


def ui_get(key, default=None):
    try:
        with open(os.path.join(STATE, "ui.json")) as f:
            return json.load(f).get(key, default)
    except (OSError, ValueError):
        return default


def ui_set(key, value):
    path = os.path.join(STATE, "ui.json")
    os.makedirs(STATE, exist_ok=True)
    try:
        with open(path) as f:
            ui = json.load(f)
    except (OSError, ValueError):
        ui = {}
    ui[key] = value
    with open(path + ".tmp", "w") as f:
        json.dump(ui, f)
    os.replace(path + ".tmp", path)


def logs_pane():
    p = tmux("show", "-gqv", "@kendle_logs", check=False)
    return p if p and p in snapshot()[1] else None


def toggle_logs(on=None):
    """Show or hide the logs column to the right of the display slot; remembered."""
    p = logs_pane()
    if p and on is not True:
        tmux("kill-pane", "-t", p, check=False)
        ui_set("logs_on", False)
        return False
    if not p and on is not False:
        slot = display_pane()
        if slot is None:
            raise RuntimeError("the console is not running")
        width = ui_get("logs_width")
        p = tmux("split-window", "-h", "-d", "-P", "-F", "#{pane_id}", "-t", slot,
                 "-l", str(width) if width else "50%", "-c", HUB, KENDLE, "logs")
        tmux("set", "-g", "@kendle_logs", p)
        ui_set("logs_on", True)
        return True
    return bool(p)


def new_pane(cwd, argv, name):
    """Run argv in a new parked window; returns its pane id."""
    return tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"{SESSION}:",
                "-n", name, "-c", cwd, *argv)


def show_pane(pane):
    """Swap a pane into the right-hand slot; whatever was there is parked in its place."""
    slot = display_pane()
    if slot is None:
        raise RuntimeError("the console is not running")
    if pane != slot:
        tmux("swap-pane", "-d", "-s", pane, "-t", slot)


def release(pane):
    """Kill a pane. If it is on screen, the idle placeholder takes its slot first."""
    _, panes = snapshot()
    p = panes.get(pane)
    if not p:
        return
    if p["window"] == "console":
        if p["index"] != 1:
            return                                   # never the sidebar
        idle = tmux("show", "-gqv", "@kendle_idle", check=False)
        if idle in panes and idle != pane:
            tmux("swap-pane", "-d", "-s", idle, "-t", pane)
    tmux("kill-pane", "-t", pane, check=False)


# ---- registry -------------------------------------------------------------------

def load():
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def update(fn):
    """Apply fn(rows) under an exclusive lock, then save atomically; returns fn's result.
    If fn raises, nothing is written."""
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "registry.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = load()
        result = fn(rows)
        tmp = REGISTRY + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rows, f, indent=2)
            f.write("\n")
        os.replace(tmp, REGISTRY)
        return result


@contextlib.contextmanager
def starting(what):
    """One start of `what` at a time, across threads and processes: the look for a running session
    and the registration of a new one must not interleave with another start (a double key press,
    two shells), or both start one - or both take the same free review folder."""
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, f"start-{what}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def find(rows, key):
    """A session by id, unique id prefix, feature (its manager) or feature/sub-name."""
    hits = [e for e in rows if e["id"] == key] or [
        e for e in rows
        if e["id"].startswith(key) or label(e) == key
        or (e["kind"] == "manager" and e["feature"] in (key, key.rsplit("-manager", 1)[0] if key.endswith("-manager") else None))]
    if len(hits) != 1:
        raise LookupError(f"no session matches '{key}'" if not hits
                          else f"'{key}' matches {len(hits)} sessions - use more of the id")
    return hits[0]


# ---- worktrees ----------------------------------------------------------------

def features():
    """Every worktree except the primary repo, named by its hub folder."""
    out = subprocess.run(["git", "-C", PRIMARY, "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout
    hub = os.path.realpath(HUB)
    links = {}                                       # worktrees outside the hub show up as hub symlinks
    for n in os.listdir(HUB):
        p = os.path.join(HUB, n)
        if os.path.islink(p) and os.path.isdir(p):
            links[os.path.realpath(p)] = n
    feats = []
    for block in out.strip().split("\n\n"):
        kv = dict(line.partition(" ")[::2] for line in block.splitlines())
        if "worktree" not in kv:
            continue
        real = os.path.realpath(kv["worktree"])
        if os.path.basename(real) in RESERVED or real in (os.path.realpath(PRIMARY), os.path.realpath(ASK_PATH)):
            continue
        inside = os.path.dirname(real) == hub
        feats.append({"name": os.path.basename(real) if inside else links.get(real, os.path.basename(real)),
                      "path": real, "inside_hub": inside,
                      "branch": kv.get("branch", "").replace("refs/heads/", "") or "(detached)",
                      "missing": not os.path.isdir(real)})
    return sorted(feats, key=lambda f: f["name"])


def feature(name):
    for f in features():
        if f["name"] == name:
            return f
    raise LookupError(f"no feature worktree named '{name}'")


# ---- transcripts ----------------------------------------------------------------

def project_dir(cwd):
    return os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def transcript(sid, cwd=None):
    """A session's transcript: in its own folder's project first, else wherever it is."""
    if cwd:
        p = os.path.join(project_dir(cwd), sid + ".jsonl")
        if os.path.exists(p):
            return p
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def events_backwards(path, chunk=65536):
    """Transcript events, newest first, read from the end in chunks. Claude writes
    large attachment events after a reply, so a fixed-size tail can miss the chat."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, rest = f.tell(), b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines.pop(0)                      # may be cut: completed by the next chunk
            for line in reversed(lines):
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
        try:
            yield json.loads(rest)
        except ValueError:
            pass


def epoch(stamp, utc=True):
    """Seconds since the epoch for a transcript timestamp (UTC) or a registry one (local)."""
    try:
        t = time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return calendar.timegm(t) if utc else time.mktime(t)


_READS = {}                                         # (what, file) -> ((size, mtime), value)


def cached(what, path, compute, extra=None):
    """Re-read a transcript only when it changed. A finished role's file never does, and the
    sidebar tracks a hundred of them - re-parsing them all every two seconds was the lag."""
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    if st is None:
        return compute()
    key = (what, path, extra)
    stamp = (st.st_size, st.st_mtime)
    hit = _READS.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    value = compute()
    if len(_READS) > 4000:
        _READS.clear()
    _READS[key] = (stamp, value)
    return value


def turn_state(path, since=None):
    """'waiting' (your turn), 'working' (the model's turn) or 'new', from the newest chat event.
    Events from before `since` (when the session's process started) are history: a
    resumed session whose last turn was cut off is not working - it waits for you."""
    return cached("turn", path, lambda: _turn_state(path, since), since)


def _turn_state(path, since=None):
    try:
        events = events_backwards(path) if path else []
    except OSError:
        events = []
    for e in events:
        kind, msg = e.get("type"), e.get("message") or {}
        if kind == "system" and e.get("subtype") in ("turn_duration", "local_command"):
            state = "waiting"                        # written when a turn ends
        elif kind == "assistant":
            state = "working" if msg.get("stop_reason") in (None, "tool_use") else "waiting"
        elif kind == "user" and not e.get("isMeta"):
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                                 for c in content):
                state = "working"
            else:
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict))
                # a local slash command's output, or an interrupt, hands the turn back to you
                state = "waiting" if text.lstrip().startswith(("<local-command", "[Request interrupted")) else "working"
        else:
            continue
        stamp = epoch(e.get("timestamp"))
        return "new" if since and stamp and stamp < since else state
    return "new"


def _default_model():
    try:
        with open(os.path.expanduser("~/.claude/settings.json")) as f:
            return str(json.load(f).get("model", ""))
    except (OSError, ValueError):
        return ""


DEFAULT_MODEL = _default_model()


def context_of(path, hint=""):
    return cached("ctx", path, lambda: _context_of(path, hint), hint)


def _context_of(path, hint=""):
    """How full a session's context window is, from its latest model call."""
    try:
        events = events_backwards(path) if path else []
        for ev in events:
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            u = msg.get("usage") or {}
            tokens = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if tokens:
                limit = 1_000_000 if ("1m" in (hint or "").lower() or tokens > 200_000) else 200_000
                return {"tokens": tokens, "limit": limit, "model": msg.get("model") or ""}
    except OSError:
        pass
    return None


def rss_by_pane():
    """RAM in MB of each pane's whole process tree."""
    kids, rss, out = {}, {}, {}
    for line in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            pid, ppid, r = map(int, parts)
            kids.setdefault(ppid, []).append(pid)
            rss[pid] = r
    for line in tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}", check=False).splitlines():
        pane, _, pid = line.partition(" ")
        if not pid.isdigit():
            continue
        total, stack = 0, [int(pid)]
        while stack:
            p = stack.pop()
            total += rss.get(p, 0)
            stack += kids.get(p, [])
        out[pane] = total // 1024
    return out


def stopped_subs(path, limit=600):
    """Names the manager stopped: a stopped sub-agent's own transcript just ends mid-action, so its
    last event still looks like work in progress. Only the recent events matter, and only when the
    manager's transcript has changed - scanning every manager's (tens of MB) on each refresh was
    most of the sidebar's lag."""
    return cached("stops", path, lambda: _stopped_subs(path, limit), limit)


def _stopped_subs(path, limit):
    names = set()
    try:
        for n, e in enumerate(events_backwards(path)):
            if n > limit:
                break
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TaskStop":
                    task = (b.get("input") or {}).get("task_id")
                    if task:
                        names.add(str(task))
    except OSError:
        pass
    return names


def internal_subs(manager, alive=True):
    """Sub-agents a manager ran in-process (Task, teammates), read from its transcript folder."""
    t = transcript(manager["id"], manager.get("cwd"))
    if not t:
        return []
    stopped = stopped_subs(t)
    subs = []
    for p in sorted(glob.glob(os.path.join(t[:-len(".jsonl")], "subagents", "agent-*.jsonl")),
                    key=os.path.getmtime):
        base = p[:-len(".jsonl")]
        try:
            with open(base + ".meta.json") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        state = turn_state(p)
        name = meta.get("name") or meta.get("agentType") or "agent"
        # a role blocked in one long tool call (the ship gate waits 9 minutes at a time) writes nothing
        # meanwhile - still working. But not if the manager stopped it, and not for ever.
        recent = alive and name not in stopped and time.time() - os.path.getmtime(p) < 1800
        subs.append({"id": os.path.basename(base)[len("agent-"):], "kind": "internal",
                     "feature": manager["feature"], "parent": manager["id"], "transcript": p,
                     "name": name,
                     "ctx": context_of(p, meta.get("model", "")), "mtime": os.path.getmtime(p),
                     "description": meta.get("description", ""),
                     "state": "idle" if state == "waiting" else ("working" if recent else "stopped")})
    # a role restarted after a manager restart gets a new record: show only its latest
    latest = {}
    for sub in subs:
        latest[sub["name"]] = sub
    return sorted(latest.values(), key=lambda sub: os.path.getmtime(sub["transcript"]))


# ---- sessions -------------------------------------------------------------------

def claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def manager_note(f):
    """Who a fresh manager is. Only for new sessions: --append-system-prompt is ignored on
    --resume (tested 2026-09-10), so resumed managers get current facts from the workspace CLAUDE.md."""
    return note("manager", feature=f["name"], path=f["path"], branch=f["branch"])


def lean():
    """Flags for every session kendle starts. Tool servers and plugins cost tokens on every turn, so
    kendle.toml can keep only the servers the team uses ([team] mcp_servers) and switch plugins off
    ([team] disabled_plugins). Unset, sessions get everything. The user's own sessions are untouched.
    Every session may also use the docs folder (--add-dir): the roles hand work over through it."""
    team, flags = CONFIG["team"], []
    if os.path.isdir(DOCS):
        # the team's handoffs live in <docs>/, linked into each worktree; Claude Code follows the link
        # to a folder outside the session's own and would refuse (or ask about) every read and write
        flags += ["--add-dir", DOCS]
    if team["mcp_servers"] is not None:
        path = os.path.join(STATE, "mcp-team.json")
        try:
            with open(os.path.expanduser("~/.claude.json")) as fh:
                servers = json.load(fh).get("mcpServers", {})
        except (OSError, ValueError):
            servers = {}
        os.makedirs(STATE, exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump({"mcpServers": {k: v for k, v in servers.items() if k in team["mcp_servers"]}}, fh)
        os.replace(path + ".tmp", path)
        flags += ["--strict-mcp-config", "--mcp-config", path]
    if team["disabled_plugins"]:
        flags += ["--settings", json.dumps({"enabledPlugins": {p: False for p in team["disabled_plugins"]}})]
    return flags


def start_manager(name, extra=(), prompt=None, fresh=False):
    """Start the feature's one manager, resuming its previous conversation if it has one - unless
    fresh, when a new conversation starts from the feature's docs. Returns (entry, started)."""
    with starting(f"manager-{name}"):
        return _start_manager(name, extra, prompt, fresh)


def _start_manager(name, extra, prompt, fresh):
    f = feature(name)
    ensure_session()
    reap()
    mine = [e for e in load() if e["kind"] == "manager" and e["feature"] == name]
    snap = snapshot()
    for e in mine:
        if is_live(e, snap):
            return e, False
    prev = None if fresh else next((e for e in reversed(mine) if transcript(e["id"])), None)
    if prev:
        sid = prev["id"]
        here = os.path.join(project_dir(f["path"]), sid + ".jsonl")
        if not os.path.exists(here):                 # worktree moved: copy (never move) the history
            src = transcript(sid)
            os.makedirs(os.path.dirname(here), exist_ok=True)
            shutil.copy2(src, here)
            if os.path.isdir(src[:-6]) and not os.path.exists(here[:-6]):
                shutil.copytree(src[:-6], here[:-6])
        argv = [claude_bin(), *([prompt] if prompt else []), *lean(), "--resume", sid, *extra]
    else:
        sid = str(uuid.uuid4())
        argv = [claude_bin(), *([prompt] if prompt else []), *lean(), "--session-id", sid, "-n", f"{name}-manager",
                "--append-system-prompt", manager_note(f), *extra]
    pane = new_pane(f["path"], argv, f"{name}-manager")
    entry = {"id": sid, "feature": name, "kind": "manager", "parent": None, "name": "manager",
             "pane": pane, "server": snapshot()[0], "cwd": f["path"], "started": now(), "stopped": None}

    def put(rows):
        # one manager per feature: replace its old row, drop managers that never got a transcript
        rows[:] = [e for e in rows if not (e["kind"] == "manager" and e["feature"] == name
                                           and (e["id"] == sid or not transcript(e["id"])))]
        rows.append(entry)
    update(put)
    return entry, True


def spawn_sub(manager_key, name, prompt, extra=()):
    """Spawn a read-only sub-agent in the manager's worktree, listed under the manager."""
    rows = load()
    m = find([e for e in rows if e["kind"] == "manager"], manager_key)
    if not re.fullmatch(NAME_RE, name):
        raise ValueError(f"sub-agent name '{name}': use lowercase letters, digits and dashes")
    if any(e["kind"] == "sub" and e["parent"] == m["id"] and e["name"] == name for e in rows):
        raise ValueError(f"'{m['feature']}' already has a sub-agent named '{name}'")
    cwd = feature(m["feature"])["path"]
    ensure_session()
    sid = str(uuid.uuid4())
    argv = [claude_bin(), (" " + prompt) if prompt.startswith("-") else prompt, *lean(), *READ_ONLY,
            "--append-system-prompt", note("sub", feature=m["feature"]),
            "--session-id", sid, "-n", f"{m['feature']}-{name}", *extra]
    pane = new_pane(cwd, argv, f"{m['feature']}-{name}")
    entry = {"id": sid, "feature": m["feature"], "kind": "sub", "parent": m["id"], "name": name,
             "pane": pane, "server": snapshot()[0], "cwd": cwd, "started": now(), "stopped": None}
    update(lambda rows: rows.append(entry))
    return entry


def show(key):
    e = find(load(), key)
    if not is_live(e, snapshot()):
        raise RuntimeError(f"{label(e)} is not running")
    show_pane(e["pane"])
    return e


def stop(key):
    """Stop a session. Its transcript and worktree are never touched."""
    e = find(load(), key)
    if e.get("pane") and e.get("server") == snapshot()[0]:
        release(e["pane"])

    def mark(rows):
        for r in rows:
            if r["id"] == e["id"]:
                r["pane"], r["stopped"] = None, now()
    update(mark)
    if e["kind"] == "question":
        _desk_closed()
    return e


def _desk_name():
    """Mirrors stack.desk_name() - stack imports core, so core keeps its own copy; change both together."""
    return os.path.basename(ASK_PATH)


def _stack_running(name):
    """Whether a folder's stack may still be running: a service file not marked stopped. Only a file
    check (no ps) - reap() runs on every refresh. It mirrors stack.py's state files (stack imports
    core); a change to their layout changes this too."""
    for path in glob.glob(os.path.join(STATE, "stack", name, "*.json")):
        if os.path.basename(path) == "stack.json":
            continue
        try:
            with open(path) as fh:
                if json.load(fh).get("stopped") is False:
                    return True
        except (OSError, ValueError):
            continue
    return False


def _stop_stack_now(name, why):
    """Stop a folder's stack and wait for it - before its code moves or its folder goes."""
    if _stack_running(name):
        from kendle import stack                     # stack imports core
        stack.stop(name, reason=why)


def _stop_stack_detached(name, why):
    """Stop a folder's stack in the background: it can take minutes (a compose stop), and nothing that
    closes a session - tree(), the sidebar's refresh, a start - may wait for it."""
    if _stack_running(name):
        subprocess.Popen([KENDLE, "stack", "stop", "-f", name, "--why", why], cwd=HUB, start_new_session=True,
                         env=dict(os.environ, KENDLE_WORKSPACE=HUB), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _desk_closed():
    """The last question on the desk has closed: its stack stops too."""
    snap = snapshot()
    if not any(e["kind"] == "question" and is_live(e, snap) for e in load()):
        _stop_stack_detached(_desk_name(), "question closed")


def adopt(name, sid):
    """Register an existing Claude session as a feature's manager; `manager` resumes it."""
    f = feature(name)
    if not transcript(sid):
        raise LookupError(f"no transcript for session {sid}")
    entry = {"id": sid, "feature": name, "kind": "manager", "parent": None, "name": "manager",
             "pane": None, "server": None, "cwd": f["path"], "started": None, "stopped": now()}

    def put(rows):
        if any(e["id"] == sid for e in rows):
            raise ValueError(f"session {sid} is already registered")
        if any(e["kind"] == "manager" and e["feature"] == name for e in rows):
            raise ValueError(f"'{name}' already has a manager")
        rows.append(entry)
    update(put)
    return entry


def reap():
    """Rows whose pane has gone are marked stopped. Finished panes are closed, except
    the one on screen, so its last output stays readable until you move on."""
    rows = load()                                    # rows first: a row is written only after its pane exists
    snap = snapshot()
    server, panes = snap
    slot = display_pane(snap)
    gone = {e["id"]: e for e in rows if e.get("pane") and not is_live(e, snap)
            and not (e.get("server") == server and e["pane"] == slot)}
    if not gone:
        return
    for e in gone.values():
        if e.get("server") == server and e["pane"] in panes:
            tmux("kill-pane", "-t", e["pane"], check=False)

    def mark(rows):
        for r in rows:
            if r["id"] in gone and r.get("pane") == gone[r["id"]]["pane"]:
                r["pane"], r["stopped"] = None, now()
    update(mark)
    if any(e["kind"] == "question" for e in gone.values()):
        _desk_closed()


def tree():
    """features > managers > sub-agents (spawned and internal), each with a state."""
    reap()
    rows = load()
    snap = snapshot()

    def state(e):
        if not is_live(e, snap):
            return "stopped"
        s = turn_state(transcript(e["id"], e.get("cwd")), since=epoch(e.get("started"), utc=False))
        return ("trust?" if held_at_trust(e) else "idle") if s == "new" else s
    out = []
    for f in features():
        managers = []
        for m in (e for e in rows if e["kind"] == "manager" and e["feature"] == f["name"]):
            subs = [dict(e, state=state(e), ctx=context_of(transcript(e["id"], e.get("cwd")), DEFAULT_MODEL))
                    for e in rows if e["kind"] == "sub" and e["parent"] == m["id"]]
            m_state = state(m)
            managers.append(dict(m, state=m_state, subs=subs, internal=internal_subs(m, m_state != "stopped"),
                                 ctx=context_of(transcript(m["id"], m.get("cwd")), DEFAULT_MODEL)))
        out.append(dict(f, managers=managers))
    return out


# ---- new feature -----------------------------------------------------------------

def free_gb():
    st = os.statvfs(HUB)
    return st.f_bavail * st.f_frsize // 2**30


def new_feature(name, base=None):
    """Create a feature worktree as a real folder under the hub, branched from a freshly
    fetched base branch. A post-checkout hook in the repo can link shared state into it."""
    base = base or UPSTREAM
    if not re.fullmatch(NAME_RE, name) or name in RESERVED:
        raise ValueError(f"feature name '{name}': lowercase letters, digits and dashes, not a hub name")
    path = os.path.join(HUB, name)
    if os.path.lexists(path):
        raise ValueError(f"{path} already exists")
    git = ["git", "-C", PRIMARY]
    if subprocess.run(git + ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).returncode == 0:
        raise ValueError(f"branch '{name}' already exists")
    if free_gb() < RESERVE_GB:
        raise RuntimeError(f"only {free_gb()} GB free - {CONFIG['workspace']['low_disk_hint']}")
    for step in (["fetch", REMOTE, BASE],
                 ["worktree", "add", "--no-track", "-b", name, path, base]):   # --no-track: never upstream the base
        try:
            r = subprocess.run(git + step, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))     # fail, never hang on a prompt
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git {step[0]} timed out")
        if r.returncode:
            raise RuntimeError(f"git {step[0]} failed: {r.stderr.strip()[-300:]}")
    docs = os.path.join(DOCS, name)
    if not os.path.exists(docs):
        os.makedirs(docs)
        with open(os.path.join(docs, "session.json"), "w") as f:
            json.dump({"slug": name, "worktree": name, "branch": name, "base": base, "tool": "kendle",
                       "created": datetime.date.today().isoformat()}, f, indent=2)
            f.write("\n")
    link_shared(path)
    return feature(name)


def link_shared(worktree):
    """Link the workspace's shared folders into a worktree (kendle.toml workspace.links): the team's
    docs and role files must be reachable from every session. Never replaces anything, never a
    path git tracks; each link is added to the repo's exclude list so it never shows as a change."""
    common = git("rev-parse", "--git-common-dir", cwd=worktree)
    exclude = os.path.join(worktree, common, "info", "exclude") if common else None
    made = []
    for rel, target in (CONFIG["workspace"]["links"] or {}).items():
        target = os.path.join(HUB, config.fill(target, docs=CONFIG["workspace"]["docs"]))
        here = os.path.join(worktree, rel)
        if os.path.lexists(here) or not os.path.exists(target) or git("ls-files", rel, cwd=worktree):
            continue
        os.makedirs(os.path.dirname(here), exist_ok=True)
        os.symlink(target, here)
        made.append("/" + rel)
    if made and exclude:
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        have = open(exclude).read().split("\n") if os.path.exists(exclude) else []
        new = [m for m in made if m not in have]
        if new:
            with open(exclude, "a") as f:
                f.write(("" if not have or have[-1] == "" else "\n") + "\n".join(new) + "\n")
    return made


# ---- the Ask desk ------------------------------------------------------------------

def _git_desk(*args, timeout=120):
    return subprocess.run(["git", "-C", ASK_PATH, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def desk():
    """The desk's commit, its date, and how far it is behind the base; None if it doesn't exist."""
    if not os.path.isdir(ASK_PATH):
        return None
    behind = _git_desk("rev-list", "--count", f"HEAD..{UPSTREAM}").stdout.strip()
    return {"path": ASK_PATH, "sha": _git_desk("rev-parse", "--short", "HEAD").stdout.strip(),
            "date": _git_desk("log", "-1", "--format=%cd", "--date=short").stdout.strip(),
            "behind": int(behind or 0)}


def refresh_desk():
    """Move the desk to the latest base, but never while a question is running there
    (code must not shift under a live answer) and never over local changes. True if it moved.
    A desk stack still running is stopped first: code never moves under running services."""
    if not os.path.isdir(ASK_PATH):
        raise RuntimeError(f"no Ask desk - create it: git -C {os.path.relpath(PRIMARY, HUB)} worktree add --detach "
                           f"{os.path.relpath(ASK_PATH, PRIMARY)} {UPSTREAM}")
    snap = snapshot()
    if any(e["kind"] == "question" and is_live(e, snap) for e in load()):
        return False
    if _git_desk("status", "--porcelain").stdout.strip():
        return False
    if _git_desk("fetch", "-q", REMOTE, BASE, timeout=300).returncode:
        return False
    if _git_desk("rev-parse", "HEAD").stdout.strip() != _git_desk("rev-parse", UPSTREAM).stdout.strip():
        _stop_stack_now(_desk_name(), "the desk moved to the latest base")
    return _git_desk("checkout", "--quiet", "--detach", UPSTREAM).returncode == 0


def _short(text, n=42):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n - 1].rsplit(" ", 1)[0] + "…"


def review_slots():
    """The three review folders and what each holds: (slot, path, entry or None)."""
    rows = [e for e in load() if e["kind"] == "review" and not e.get("released")]
    by_slot = {e["slot"]: e for e in rows}
    return [(n, os.path.join(HUB, f"review-{n}"), by_slot.get(n)) for n in range(1, REVIEW_SLOTS + 1)]


def start_review(change, extra=()):
    """Check out someone else's change in a review folder and read it. An earlier review of the same
    change comes back with its conversation - a console restart or a reboot doesn't lose it."""
    with starting("review"):                         # one at a time: they share the review folders
        return _start_review(change, extra)


def _start_review(change, extra):
    from kendle import review
    host = review.host()
    ensure_session()
    reap()
    ref, number, patch = host.locate(change)
    snap = snapshot()
    open_ = [e for e in load() if e["kind"] == "review" and not e.get("released") and e["change"] == number]
    live = next((e for e in open_ if is_live(e, snap)), None)
    if live:
        return live                                  # already open - even before it has written anything
    prior = next((e for e in open_ if transcript(e["id"], e.get("cwd"))), None)
    if prior:
        slot, path = prior["slot"], prior["cwd"]
    else:
        free = [(n, pth) for n, pth, e in review_slots() if e is None or not is_live(e, snap)]
        if not free:
            raise RuntimeError(f"all {REVIEW_SLOTS} review slots are busy - close one first (K on its row)")
        slot, path = free[0]
        for e in [e for e in load() if e["kind"] == "review" and e.get("slot") == slot and not e.get("released")]:
            release_review(e["id"], keep_folder=True)
    git("fetch", "-q", REMOTE, ref, cwd=PRIMARY)
    head = git("rev-parse", "FETCH_HEAD", cwd=PRIMARY)
    if prior and os.path.isdir(path) and git("rev-parse", "HEAD", cwd=path) != head:
        _stop_stack_now(f"review-{slot}", "the review folder moved to a new revision")   # a new folder's was stopped on release
    if os.path.isdir(path):
        git("checkout", "--quiet", "--detach", head, cwd=path)
    else:
        git("worktree", "add", "--quiet", "--detach", path, head, cwd=PRIMARY)
        link_shared(path)
    subject = git("log", "-1", "--format=%s", head, cwd=PRIMARY)
    author = git("log", "-1", "--format=%an", head, cwd=PRIMARY)
    base = host.base(head)
    files = git("diff", "--name-only", base, head, cwd=PRIMARY).split()
    system = note("review", file=f"{number}-{patch}")
    if prior:
        sid = prior["id"]
        back = (f"I am back - this folder now holds {host.revision} {patch} of {host.noun} {number}. "
                "Nothing else changed; carry on from where we were. " + note("stack"))
        # --append-system-prompt is ignored on --resume: the stack line comes with the message
        argv = [claude_bin(), back, *lean(), *STACK_READ_ONLY, "--resume", sid, *extra]
    else:
        sid = str(uuid.uuid4())
        brief = host.brief(number, patch, subject, author, len(files), base)
        argv = [claude_bin(), brief, *lean(), *STACK_READ_ONLY, "--append-system-prompt", system,
                "--session-id", sid, "-n", f"review-{number}", *extra]
    pane = new_pane(path, argv, f"review-{number}")
    entry = {"id": sid, "feature": f"review-{slot}", "kind": "review", "parent": None, "host": host.name,
             "name": f"{number}/{patch} {subject}"[:60], "change": number, "patchset": patch, "slot": slot,
             "subject": subject, "author": author, "files": len(files), "pane": pane,
             "server": snapshot()[0], "cwd": path, "started": now(), "stopped": None, "released": None}
    if prior:
        update(lambda rows: [r.update(pane=pane, server=snapshot()[0], patchset=patch, subject=subject, host=host.name,
                                      author=author, files=len(files), started=now(), stopped=None)
                             for r in rows if r["id"] == sid])   # a reboot gives tmux a new server id
    else:
        update(lambda rows: rows.append(entry))
    return entry


def reviews(all_=False):
    """Open reviews for the sidebar, newest first."""
    reap()
    snap = snapshot()
    out = []
    for e in load():
        if e["kind"] != "review" or (e.get("released") and not all_):
            continue
        live = is_live(e, snap)
        if e.get("released"):
            state = "closed"
        elif not live:
            state = "stopped"
        else:
            s = turn_state(transcript(e["id"], e.get("cwd")), since=epoch(e.get("started"), utc=False))
            state = {"waiting": "reviewed", "working": "reading"}.get(s) or \
                ("trust?" if s == "new" and held_at_trust(e) else "starting")
        out.append(dict(e, state=state, ctx=context_of(transcript(e["id"], e.get("cwd")), DEFAULT_MODEL)))
    return sorted(out, key=lambda e: e.get("slot", 0))


def save_review(e):
    """Keep the review's findings: its last substantive reply, in <docs>/reviews/."""
    from kendle import review
    text = _answer(_texts(e))
    if not text.strip():
        return None
    out = os.path.join(DOCS, "reviews")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"{e['change']}-{e['patchset']}.md")
    with open(path, "w") as fh:
        fh.write(f"# {review.title(e)}\n"
                 f"by {e['author']} · {e['files']} files · reviewed {time.strftime('%Y-%m-%d')}\n\n{text}\n")
    return path


def release_review(key, keep_folder=False):
    """Close a review: keep its findings, stop its session and its folder's services, give the folder back."""
    rows = [x for x in load() if x["kind"] == "review"]
    by_change = [x for x in rows if not x.get("released") and str(x.get("change")) == str(key)]
    e = by_change[0] if len(by_change) == 1 else find(rows, key)    # the change number, or a session id
    saved = save_review(e)
    if e.get("pane"):
        stop(e["id"])
    _stop_stack_now(e["feature"], "review closed")
    path = e["cwd"]
    if not keep_folder and os.path.isdir(path):
        git("worktree", "remove", "--force", path, cwd=PRIMARY)
    update(lambda rows: [r.update(released=now(), saved=saved) for r in rows if r["id"] == e["id"]])
    return dict(e, released=now(), saved=saved)      # update() mutates its own copy of the rows


def start_question(text, extra=()):
    """Start one read-only question session on the desk; returns its registry entry."""
    if not text.strip():
        raise ValueError("the question is empty")
    ensure_session()
    refresh_desk()                                   # first: a desk that moves stops its stack here, and
    reap()                                           # reap() then finds nothing left to stop
    sid = str(uuid.uuid4())
    argv = [claude_bin(), (" " + text) if text.startswith("-") else text, *lean(), *STACK_READ_ONLY,
            "--append-system-prompt", note("ask"), "--session-id", sid,
            "-n", "ask-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30], *extra]
    pane = new_pane(ASK_PATH, argv, "ask")
    entry = {"id": sid, "feature": ASK, "kind": "question", "parent": None, "name": _short(text),
             "question": text, "pane": pane, "server": snapshot()[0], "cwd": ASK_PATH,
             "desk": (desk() or {}).get("sha"), "started": now(), "stopped": None, "promoted": None}
    update(lambda rows: rows.append(entry))
    return entry


def reopen_question(key, extra=()):
    """Bring a question's session back with its conversation - a console restart or a reboot closes
    them all, and the answer is usually still worth continuing."""
    with starting("question"):
        return _reopen_question(key, extra)


def _reopen_question(key, extra):
    e = find([x for x in load() if x["kind"] == "question"], key)
    ensure_session()
    reap()
    if is_live(e, snapshot()):
        return e
    if not transcript(e["id"], e.get("cwd")):
        raise LookupError("that question's history is gone - press a to ask it again")
    refresh_desk()
    # --append-system-prompt is ignored on --resume: the stack line comes with the message
    argv = [claude_bin(), "I am back - carry on from where we were. " + note("stack"), *lean(), *STACK_READ_ONLY,
            "--resume", e["id"], *extra]
    pane = new_pane(ASK_PATH, argv, "ask")
    update(lambda rows: [r.update(pane=pane, server=snapshot()[0], started=now(), stopped=None)
                         for r in rows if r["id"] == e["id"]])
    return dict(e, pane=pane)


def _texts(e):
    """The conversation of a session, oldest first: [(\"You\"|\"Desk\", text)]."""
    t = transcript(e["id"], e.get("cwd"))
    out = []
    for ev in reversed(list(events_backwards(t))) if t else []:
        kind, msg = ev.get("type"), ev.get("message") or {}
        content = msg.get("content")
        if kind == "user" and not ev.get("isMeta"):
            if isinstance(content, list):
                if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    continue
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            text = (content or "").strip()
            if text and not text.startswith("<"):
                out.append(("You", text))
        elif kind == "assistant":
            text = "".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text").strip()
            if text:
                out.append(("Desk", text))
    return out


SUGGESTED = re.compile(r"Suggested feature name:\**\s*`?([a-z0-9][a-z0-9-]{1,39})`?")
VERDICT = re.compile(r"(?i)^\W*(verdict\W*)?(bug|not a bug|works as designed|needs a change|yes|no)\b")


def _answer(convo):
    """The desk's reply to the user's latest message: the part carrying the verdict or the suggested
    name, not a later aside (e.g. a helper agent's "done" notice waking the session)."""
    last_you = max((i for i, (who, _) in enumerate(convo) if who == "You"), default=-1)
    replies = [t for who, t in convo[last_you + 1:] if who == "Desk"]
    for pick in (lambda t: SUGGESTED.search(t), lambda t: VERDICT.match(t.strip())):
        hits = [t for t in replies if pick(t)]
        if hits:
            return hits[-1]
    return max(replies, key=len) if replies else ""


def answer_of(e):
    """The desk's answer to the latest question turn and the feature name it suggested (or None)."""
    ans = _answer(_texts(e))
    m = SUGGESTED.search(ans)
    return ans, (m.group(1) if m else None)


def questions(all_=False):
    """Questions for the sidebar: running ones, and ones from the last day (all with all_)."""
    reap()
    snap = snapshot()
    out = []
    for e in load():
        if e["kind"] != "question":
            continue
        live = is_live(e, snap)
        when = epoch(e.get("stopped") or e.get("started"), utc=False) or 0
        if not (all_ or live or time.time() - when < 86400):
            continue
        if e.get("promoted"):
            state = "→ " + e["promoted"]
        elif not live:
            state = "closed"
        else:
            s = turn_state(transcript(e["id"], e.get("cwd")), since=epoch(e.get("started"), utc=False))
            state = {"waiting": "answered", "working": "working"}.get(s) or \
                ("trust?" if s == "new" and held_at_trust(e) else "starting")
        out.append(dict(e, state=state, ctx=context_of(transcript(e["id"], e.get("cwd")), DEFAULT_MODEL)))
    return out


def promote(key, slug):
    """Turn a question into a feature: a worktree from the latest base, the conversation as
    <docs>/<slug>/requirement.md, and the feature's manager started on it."""
    q = find([e for e in load() if e["kind"] == "question"], key)
    f = new_feature(slug)
    convo = _texts(q)
    last = _answer(convo) or "(no answer yet)"
    lines = [f"# {q['name']}", "",
             f"Promoted {datetime.date.today().isoformat()} from an Ask-desk question, answered on {BASE} @ "
             f"{q.get('desk') or '?'}.", "", "## Question", "", q.get("question", ""), "",
             "## What the desk found", "", last, "", "## Whole conversation", ""]
    for who, text in convo:
        lines += [f"**{who}:** {text}", ""]
    docs = os.path.join(DOCS, slug)
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "requirement.md"), "w") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    manager, _ = start_manager(slug, prompt=note("promote", feature=slug))

    def mark(rows):
        for r in rows:
            if r["id"] == q["id"]:
                r["promoted"] = slug
    update(mark)
    return f, manager
