"""kendle sidebar - left pane of the kendle console: ASK questions, ACTIVE features, IDLE features.

  j k / click   select            Enter  show it on the right      Tab   type into it
  h l           fold / unfold     a      ask on the latest base    p     promote a question
  m             start manager     n      new feature               s     read-only sub-agent
  K             stop session      < >    narrower / wider          r     refresh
  q             close the console (every session keeps running)
"""
import curses, locale, os, signal, sys, textwrap, threading, time, traceback
os.environ.setdefault("ESCDELAY", "25")
if not any(os.environ.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
    os.environ["LC_CTYPE"] = "en_US.UTF-8"
from kendle import core
from kendle import stack as _stack

REFRESH = 2.0
TOP = 2                                              # first row of the list
MSG_LINES = 3                                        # messages and prompts wrap over up to 3 lines
DETAIL = 3                                           # separator + 2 lines about the selected row
SELECTABLE = {"desk", "question", "review", "feature", "manager", "sub", "internal", "idlehdr", "idle"}
STATE_COLOR = {"waiting": "yellow", "answered": "yellow", "working": "green", "starting": "green"}


def fit(text, room):
    return text if len(text) <= room else text[:max(0, room - 1)] + "…"


def wrap(text, width):
    return textwrap.wrap(text, width, break_on_hyphens=False)


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n // 1000}k"


def pct(ctx):
    return int(100 * ctx["tokens"] / ctx["limit"]) if ctx else None


def ctx_color(p):
    return "dim" if p is None or p < 60 else ("yellow" if p < 85 else "red")


def ago(ts):
    if not ts:
        return ""
    d = int(time.time() - ts)
    for lim, unit, div in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if d < lim:
            return f"{d // div}{unit} ago"
    return f"{d // 86400}d ago"


def last_reply(path):
    """When the session last answered (its newest model reply) - idle sessions keep touching the file."""
    return core.cached("reply", path, lambda: _last_reply(path))


def _last_reply(path):
    try:
        for ev in core.events_backwards(path) if path else []:
            if ev.get("type") == "assistant":
                return core.epoch(ev.get("timestamp"))
    except OSError:
        pass
    return None


class Sidebar:
    def __init__(self, scr):
        self.scr = scr
        self.tree, self.rows, self.row_at = [], [], {}
        self.sel, self.top = None, 0
        self.folded = {"__idle__"}                    # IDLE starts folded
        self.expanded = set()                         # features are one line unless opened (or on screen)
        self.collapsed = set()                        # ... unless you closed one yourself: that wins
        self.msg, self.err = "", False
        self.viewer = None                            # {"id", "pane"}: open internal sub-agent transcript
        self.busy, self.job_done = None, False
        self.slot, self.free, self.loaded = None, None, 0
        self.window_width = None
        self.desk, self.questions, self.pending_show = None, [], None
        self.reviews = []
        self.rss, self.need_you = {}, 0
        self.pending_data, self.wake = None, threading.Event()
        self.stacks, self.logs_shown = set(), False   # features whose services run; is the column up?
        self.idle_checked = self.disk_checked = 0

    # ---- data ----
    def fetch(self):
        """Everything slow (git, tmux, ps, transcripts). Runs on a background thread every 2 s, and
        right after an action - never while a key waits."""
        tree = core.tree()
        snap = core.snapshot()
        for f in tree:
            live = [m for m in f["managers"] if m["state"] != "stopped"]
            working = any(s["state"] == "working" for m in f["managers"] for s in m["subs"] + m["internal"])
            busy = working or any(m["state"] == "working" for m in live)
            fresh = False                             # a reply in the last 2 hours still needs you
            for m in live:
                if m["state"] == "waiting":
                    replied = last_reply(core.transcript(m["id"], m.get("cwd")))
                    fresh = fresh or bool(replied and time.time() - replied < 7200)
            f["status"] = ("needs you" if fresh else "working" if busy else "ready" if live else "")
            f["_active"] = bool(live or working)
        return {"tree": tree, "desk": core.desk(), "questions": core.questions(), "reviews": core.reviews(),
                "slot": core.display_pane(snap),
                "panes": set(snap[1]), "free": core.free_gb(), "rss": core.rss_by_pane(),
                "stacks": set(_stack.running_stacks())}

    def fetcher(self):
        while True:
            try:
                self.pending_data = self.fetch()
            except Exception as err:
                self.say(f"refresh failed: {err}", error=True)
            self.wake.wait(REFRESH)
            self.wake.clear()

    def apply(self, data):
        self.tree, self.desk, self.questions = data["tree"], data["desk"], data["questions"]
        self.reviews = data["reviews"]
        self.slot, self.free, self.rss = data["slot"], data["free"], data["rss"]
        self.stacks = data["stacks"]
        self.logs_shown = bool(core.logs_pane())
        if self.viewer and self.viewer["pane"] not in data["panes"]:
            self.viewer = None
        self.load()

    def refresh_now(self):
        """After an action: fresh data at once, so the result shows immediately."""
        try:
            self.apply(self.fetch())
        except Exception as err:
            self.say(f"refresh failed: {err}", error=True)

    def load(self):
        """Rows from the data already fetched - cheap, safe on every key."""
        active = [f for f in self.tree if f.get("_active")]
        idle = [f for f in self.tree if not f.get("_active")]
        self.need_you = sum(1 for f in active if f["status"] == "needs you") + \
            sum(1 for q in self.questions if q["state"] == "answered")
        rows = []
        if self.desk:
            rows.append({"kind": "desk", "key": "desk"})
            if "desk" not in self.folded:
                rows += [{"kind": "question", "key": q["id"], "e": q} for q in self.questions]
            rows.append({"kind": "gap"})
        if self.reviews:
            rows.append({"kind": "header", "text": f"REVIEW  {len(self.reviews)} of {core.REVIEW_SLOTS}"})
            rows.append({"kind": "gap"})
            rows += [{"kind": "review", "key": r["id"], "e": r} for r in self.reviews]
            rows.append({"kind": "gap"})
        rows.append({"kind": "header", "text": f"ACTIVE  {len(active)}" if active else "ACTIVE  nothing running"})
        rows.append({"kind": "gap"})
        for f in active:
            on_screen = any(x.get("pane") and x["pane"] == self.slot
                            for m in f["managers"] for x in [m] + m["subs"])
            is_open = f["name"] in self.expanded or (on_screen and f["name"] not in self.collapsed)
            if rows and rows[-1]["kind"] not in ("header", "gap"):
                rows.append({"kind": "gap"})
            rows.append({"kind": "feature", "key": "f:" + f["name"], "f": f, "open": is_open})
            if not is_open:
                continue
            for m in f["managers"]:
                if m["state"] == "stopped" and not m.get("pane"):
                    continue
                since = core.epoch(m.get("started"), utc=False) or 0
                rows.append({"kind": "manager", "key": m["id"], "f": f, "m": m, "e": m})
                rows += [{"kind": "sub", "key": s["id"], "f": f, "m": m, "e": s}
                         for s in m["subs"] if s["state"] != "stopped"]
                rows += [{"kind": "internal", "key": "i:" + s["id"], "f": f, "m": m, "e": s}
                         for s in m["internal"] if s.get("mtime", 0) >= since - 60]
        if idle:
            rows.append({"kind": "gap"})
            rows.append({"kind": "idlehdr", "key": "idle", "n": len(idle)})
            if "__idle__" not in self.folded:
                rows += [{"kind": "idle", "key": "f:" + f["name"], "f": f} for f in idle]
        old = self.current()
        self.rows = rows
        if not self.current():
            fallback = "f:" + old["f"]["name"] if old and old.get("f") else None
            keys = [r["key"] for r in rows if r["kind"] in SELECTABLE]
            self.sel = fallback if fallback in keys else (keys[0] if keys else None)
        self.loaded = time.time()

    def current(self):
        return next((r for r in self.rows if r.get("key") == self.sel and r["kind"] in SELECTABLE), None)

    def say(self, text, error=False):
        self.msg, self.err = text, error

    # ---- drawing ----
    def colors(self):
        curses.start_color()
        curses.use_default_colors()
        big = curses.COLORS >= 256
        c = lambda n, basic: n if big else basic
        bg = c(236, curses.COLOR_BLUE)
        palette = [("text", c(252, -1)), ("bright", c(255, curses.COLOR_WHITE)), ("dim", c(244, curses.COLOR_WHITE)),
                   ("faint", c(239, curses.COLOR_WHITE)), ("green", c(114, curses.COLOR_GREEN)),
                   ("yellow", c(179, curses.COLOR_YELLOW)), ("red", c(167, curses.COLOR_RED)),
                   ("cyan", c(110, curses.COLOR_CYAN)),
                   ("i1", c(75, curses.COLOR_BLUE)), ("i2", c(141, curses.COLOR_MAGENTA)), ("i3", c(175, curses.COLOR_MAGENTA)),
                   ("i4", c(73, curses.COLOR_CYAN)), ("i5", c(173, curses.COLOR_RED)), ("i6", c(180, curses.COLOR_WHITE))]
        self.pair = {}
        for i, (name, fg) in enumerate(palette):
            curses.init_pair(1 + i, fg, -1)
            curses.init_pair(20 + i, fg, bg)
            self.pair[name] = (curses.color_pair(1 + i), curses.color_pair(20 + i))

    def put(self, y, x, text, color="text", selected=False, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addstr(y, x, text[:max(0, w - 1 - x)], self.pair[color][selected] | attr)
            except curses.error:
                pass

    def draw(self, prompt=None):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.put(0, 2, core.NAME[:max(1, w - 12)], "cyan", attr=curses.A_BOLD)
        right = (f"{self.need_you} need you · " if self.need_you else "") + (f"{self.free}G" if self.free is not None else "")
        self.put(0, w - len(right) - 2, right, "yellow" if self.need_you else "dim")
        if self.free is not None and self.free < core.RESERVE_GB:
            self.put(0, w - len(f"{self.free}G") - 2, f"{self.free}G", "red")
        width = max(10, w - 3)
        if prompt is not None:
            msg, color = wrap(prompt, width)[-MSG_LINES:], "cyan"
        elif self.busy:
            msg, color = wrap(self.busy, width)[:MSG_LINES], "yellow"
        else:
            msg, color = wrap(self.msg, width)[:MSG_LINES], "red" if self.err else "dim"
        bottom = max(1, len(msg))                    # footer, or the message / prompt in its place
        detail_y = h - bottom - DETAIL
        body = max(1, detail_y - TOP)
        sel_i = next((i for i, r in enumerate(self.rows) if r.get("key") == self.sel and r["kind"] in SELECTABLE), 0)
        self.top = min(max(self.top, sel_i - body + 1), sel_i)
        self.row_at = {}
        for n, r in enumerate(self.rows[self.top:self.top + body]):
            self.row_at[TOP + n] = r
            self.row(TOP + n, r, r.get("key") == self.sel and r["kind"] in SELECTABLE, w)
        self.detail(detail_y, w)
        r = self.current()                            # tell the logs column which feature is selected
        f = r["f"]["name"] if r and r.get("f") else None
        if f and f != getattr(self, "focus_sent", None):
            self.focus_sent = f
            core.ui_set("focus_feature", f)
        for n, line in enumerate(msg):
            self.put(h - len(msg) + n, 1, line, color)
        self.put(h - 1, 2, "a ask  g review  p promote  m manager  ⇥ type", "faint") if not msg else None
        scr.refresh()

    def right(self, y, w, text, color, sel):
        self.put(y, w - len(text) - 2, text, color, sel)
        return len(text)

    def row(self, y, r, sel, w):
        kind = r["kind"]
        if kind == "gap":
            return
        if kind == "header":
            self.put(y, 2, r["text"], "dim")
            return
        if sel:
            self.put(y, 0, " " * (w - 1), selected=True)
        e = r.get("e")
        on_screen = bool(e) and bool(e.get("pane")) and e["pane"] == self.slot
        if kind == "internal" and self.viewer and self.viewer["id"] == e["id"] and self.viewer["pane"] == self.slot:
            on_screen = True
        if on_screen:
            self.put(y, 0, "▎", "cyan", sel)
        if kind == "desk":
            n = len(self.questions)
            self.put(y, 2, f"ASK  {n}" if n else "ASK", "dim", sel)
            d = self.desk
            self.right(y, w, f"{core.BASE} {d['sha'][:7]}" + (f" ↓{d['behind']}" if d["behind"] else ""), "faint", sel)
            return
        if kind == "idlehdr":
            self.put(y, 2, f"IDLE  {r['n']}", "dim", sel)
            self.right(y, w, "▸" if "__idle__" in self.folded else "▾", "faint", sel)
            return
        if kind in ("feature", "idle"):
            f = r["f"]
            status = f.get("status", "")
            color = {"needs you": "yellow", "working": "green"}.get(status, "faint")
            used = 0
            if status:
                used = self.right(y, w, status, color if status != "ready" else "faint", sel)
                self.put(y, w - used - 4, "●", color, sel)
                used += 2
            if kind == "feature":
                self.put(y, 2, "▾" if r.get("open") else "▸", "faint", sel)
                self.put(y, 4, fit(f["name"], w - 7 - used), "bright", sel, curses.A_BOLD)
            else:
                self.put(y, 2, "·", "faint", sel)
                self.put(y, 4, fit(f["name"], w - 7), "dim", sel)
            return
        if kind == "review":
            state = e["state"]
            p = pct(e.get("ctx"))
            used = self.right(y, w, f"{p:>3}%" if p is not None else "    ", "faint" if ctx_color(p) == "dim" else ctx_color(p), sel)
            shown = fit(state, 10)
            self.put(y, w - used - len(shown) - 3, shown,
                     {"reviewed": "yellow", "reading": "green", "starting": "green"}.get(state, "faint"), sel)
            self.put(y, 2, "›", "faint", sel)
            label = f"{e['change']}/{e['patchset']} {e['subject']}"
            self.put(y, 4, fit(label, w - used - len(shown) - 9), "text" if state != "closed" else "dim", sel,
                     curses.A_BOLD if on_screen else 0)
            return
        # session rows: manager, sub, internal, question
        indent = {"manager": 6, "question": 4}.get(kind, 8)
        state = e["state"]
        p = pct(e.get("ctx"))
        ctx_txt = f"{p:>3}%" if p is not None else "    "
        used = self.right(y, w, ctx_txt, "faint" if ctx_color(p) == "dim" else ctx_color(p), sel)
        shown = fit(state, 12)
        self.put(y, w - used - len(shown) - 3, shown, STATE_COLOR.get(state, "cyan" if state.startswith("→") else "faint"), sel)
        name = "manager" if kind == "manager" else e["name"]
        room = w - indent - used - len(shown) - 5
        dimmed = kind == "internal" or state in ("closed", "stopped", "done")
        self.put(y, indent, fit(name, room), "dim" if dimmed else "text", sel, curses.A_BOLD if on_screen else 0)
        if kind == "question":
            self.put(y, 2, "›", "faint", sel)

    def detail(self, y, w):
        self.put(y, 1, "─" * (w - 3), "faint")
        r = self.current()
        if not r:
            return
        kind, e = r["kind"], r.get("e")
        if kind == "desk":
            d = self.desk
            line1, line2 = f"Ask desk - questions on the latest {core.BASE}", f"{core.BASE} @ {d['sha']} · {d['date']}" + (f" · {d['behind']} behind" if d["behind"] else "")
        elif kind == "idlehdr":
            line1, line2 = f"{r['n']} features with nothing running", "l or Enter lists them · m starts a manager"
        elif kind in ("feature", "idle"):
            f = r["f"]
            line1, line2 = f["name"], f"branch {f['branch']}"
        else:
            ctx = e.get("ctx")
            model = (ctx or {}).get("model", "").replace("claude-", "")
            parts = [model] if model else []
            if ctx:
                parts.append(f"context {k(ctx['tokens'])}/{k(ctx['limit'])}")
            ram = self.rss.get(e.get("pane") or "")
            if ram:
                parts.append(f"RAM {ram}MB")
            ts = e.get("mtime") or (os.path.getmtime(e["transcript"]) if e.get("transcript") and os.path.exists(e["transcript"]) else None)
            if ts is None:
                t = core.transcript(e["id"], e.get("cwd")) if e.get("id") else None
                ts = os.path.getmtime(t) if t else None
            parts.append(ago(ts))
            if kind == "question":
                title = e.get("question") or ""
            elif kind == "review":
                from kendle import review
                title = review.title(e)
                parts = [f"by {e['author']}", f"{e['files']} files"] + parts
            else:
                title = f"{r['f']['name']} / {'manager' if kind == 'manager' else e['name']}"
            line1, line2 = title or "", " · ".join(x for x in parts if x)
        self.put(y + 1, 2, fit(line1, w - 4), "bright")
        self.put(y + 2, 2, fit(line2, w - 4), "dim")

    # ---- input helpers ----
    def ask(self, prompt, default=""):
        buf = default
        curses.curs_set(1)
        try:
            while True:
                self.draw(prompt + buf)
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    continue
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return buf.strip()
                if ch == "\x1b":
                    return ""
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    buf = buf[:-1]
                elif isinstance(ch, str) and ch.isprintable():
                    buf += ch
        finally:
            curses.curs_set(0)

    def confirm(self, question):
        self.draw(question + " y/N")
        while True:
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            return ch in ("y", "Y")

    def background(self, busy, job, done):
        """Run a slow job (git) off the UI thread; only attributes are touched from it. One at a time:
        a second key press while one runs would start the same thing twice."""
        if self.busy:
            self.say(f"still {self.busy[0].lower() + self.busy[1:]} - one moment")
            return

        def work():
            try:
                job()
                self.say(done)
            except Exception as err:
                self.say(str(err), error=True)
            self.busy, self.job_done = None, True
        self.busy = busy
        threading.Thread(target=work, daemon=True).start()

    # ---- actions ----
    def focus_display(self):
        core.tmux("select-pane", "-t", f"{core.SESSION}:console.1")

    def close_viewer(self):
        if self.viewer and self.viewer["pane"] != core.display_pane():
            core.release(self.viewer["pane"])
            self.viewer = None

    def show(self, e, focus):
        core.show_pane(e["pane"])
        self.close_viewer()
        self.slot = core.display_pane()
        if focus:
            self.focus_display()

    def enter(self, focus=False):
        r = self.current()
        if not r:
            return
        kind, e = r["kind"], r.get("e")
        if kind in ("feature", "idle"):
            live = [m for m in r["f"]["managers"] if m["state"] != "stopped"]
            if live:
                self.show(live[0], focus)
            elif kind == "idle":
                self.say("nothing running - m starts its manager")
            else:
                name = r["f"]["name"]
                if r.get("open"):
                    self.expanded.discard(name); self.collapsed.add(name)
                else:
                    self.collapsed.discard(name); self.expanded.add(name)
                self.load()
        elif kind == "idlehdr":
            self.folded ^= {"__idle__"}
            self.load()
        elif kind == "desk":
            self.say(f"a asks a question on the latest {core.BASE}")
        elif kind == "review":
            if e.get("pane") and e["state"] not in ("closed", "stopped"):
                self.show(e, focus)
            elif e["state"] == "closed":
                self.say("this review is closed - g starts another")
            else:                                     # stopped (console restart, reboot): bring it back
                def job(change=e["change"]):
                    self.pending_show = core.start_review(change)
                self.background(f"reopening review {e['change']}…", job, f"review {e['change']} is back")
        elif kind == "question":
            if e.get("pane") and (e["state"] != "closed" or e["pane"] == self.slot):
                self.show(e, focus)
            else:                                     # closed by a console restart or a reboot
                def job(key=e["id"]):
                    self.pending_show = core.reopen_question(key)
                self.background("reopening the question…", job, "the question is back")
        elif kind in ("manager", "sub"):
            if e["state"] == "stopped" and e.get("pane") != self.slot:
                self.say(f"{core.label(e)} is stopped" + (" - m resumes it" if kind == "manager" else ""))
            elif e.get("pane"):
                self.show(e, focus)
        elif kind == "internal":
            if not (self.viewer and self.viewer["id"] == e["id"]):
                pane = core.new_pane(core.HUB, [core.KENDLE, "view", "transcript", e["transcript"],
                                              f"{e['feature']}/{e['name']}", e.get("description", "")], "viewer")
                old, self.viewer = self.viewer, {"id": e["id"], "pane": pane}
                core.show_pane(pane)
                if old:
                    core.release(old["pane"])
            else:
                core.show_pane(self.viewer["pane"])
            self.slot = core.display_pane()
            if focus:
                self.focus_display()

    def manager(self):
        r = self.current()
        if not r or not r.get("f"):
            self.say("select a feature, then m")
            return
        e, started = core.start_manager(r["f"]["name"])
        self.refresh_now()
        self.sel = e["id"]
        self.show(e, focus=True)
        self.say(("started" if started else "already running") + f" {r['f']['name']} manager")

    def sub(self):
        r = self.current()
        if not r or not r.get("f"):
            self.say("select a feature, then s")
            return
        m = r.get("m") or next(iter(r["f"]["managers"]), None)
        if not m:
            self.say("start the manager first - m", error=True)
            return
        name = self.ask("sub-agent name: ")
        prompt = name and self.ask(f"{name} prompt: ")
        if not prompt:
            self.say("cancelled")
            return
        e = core.spawn_sub(m["id"], name, prompt)
        self.refresh_now()
        self.sel = e["id"]
        self.show(e, focus=False)
        self.say(f"started read-only {core.label(e)}")

    def new_review(self):
        free = [n for n, _, e in core.review_slots() if e is None]
        if not free and len(self.reviews) >= core.REVIEW_SLOTS:
            self.say(f"all {core.REVIEW_SLOTS} review slots are open - close one with K first")
            return
        change = self.ask("review which change (number or URL): ")
        if not change:
            self.say("cancelled")
            return

        def job():
            e = core.start_review(change)
            self.pending_show = e
        self.background(f"fetching change {change}…", job, f"change {change} checked out - reading it now")

    def new_question(self):
        text = self.ask(f"ask (on latest {core.BASE}): ")
        if not text:
            self.say("cancelled")
            return
        e = core.start_question(text)
        self.folded.discard("desk")
        self.refresh_now()
        self.sel = e["id"]
        self.show(e, focus=False)
        self.say("asking - Tab or Ctrl-q to follow up")

    def promote(self):
        r = self.current()
        if not r or r["kind"] != "question":
            self.say("select a question under ASK, then p")
            return
        e = r["e"]
        if e.get("promoted"):
            self.say(f"already promoted to {e['promoted']}")
            return
        _, suggested = core.answer_of(e)
        name = self.ask("feature name: ", suggested or "")
        if not name:
            self.say("cancelled")
            return

        def job():
            _, manager = core.promote(e["id"], name)
            self.pending_show = manager
        self.background(f"creating {name} and its manager...", job, f"{name} ready - its manager has the question")

    def new(self):
        name = self.ask("new feature name: ")
        if not name:
            return
        if not self.confirm(f"create {name} from {core.UPSTREAM}?"):
            self.say("cancelled")
            return
        self.sel = "f:" + name
        self.background(f"creating {name} (fetch + checkout)...", lambda: core.new_feature(name),
                        f"{name} ready - m starts its manager")

    def stop(self):
        r = self.current()
        e = r and r.get("e")
        if r and r["kind"] == "review":
            if self.confirm(f"close review {e['change']}/{e['patchset']} and free its folder?"):
                self.background(f"closing review {e['change']}…", lambda: core.release_review(e["id"]),
                                f"review {e['change']} closed - slot free")
            else:
                self.say("cancelled")
            return
        if not r or r["kind"] not in ("manager", "sub", "question") or not e.get("pane"):
            self.say("nothing running here to stop")
            return
        if self.confirm(f"stop {core.label(e)}?"):
            core.stop(e["id"])
            self.refresh_now()
            self.say(f"stopped {core.label(e)}")
        else:
            self.say("cancelled")

    def keep_width(self):
        """Keep the width the user chose: re-apply it when the window resizes, and remember a
        border the user drags."""
        win = core.tmux("display", "-p", "-t", f"{core.SESSION}:console", "#{window_width}", check=False)
        pane = core.tmux("display", "-p", "-t", f"{core.SESSION}:console.0", "#{pane_width}", check=False)
        if win and win != self.window_width:
            self.window_width = win
            core.tmux("resize-pane", "-t", f"{core.SESSION}:console.0", "-x", str(core.sidebar_width()), check=False)
        elif pane.isdigit() and int(pane) != core.sidebar_width():
            core.set_sidebar_width(int(pane))

    def watch_disk(self):
        """Tell the user before the disk bites, and clear caches whose worktree is gone (the user
        allowed that much on its own, 2026-09-28). Everything else stays report-only."""
        if os.environ.get("KENDLE_STATE"):                # a test console: never touch the real disk
            return
        from kendle.cmd import disk as disk
        free = core.free_gb()
        orphans = disk.orphan_bases()
        if orphans and (free is None or free < 25):   # nothing can use these - the user allowed this
            freed = sum(disk.gb(b) for b, _ in orphans)
            disk.orphans()
            self.say(f"disk {free}G - deleted {freed:.0f}G of caches whose worktree is gone")
        used, cap = disk.footprint(), disk.budget()
        if used > cap:                                # over the hub's budget: trim what is quiet
            freed = disk.enforce()
            if freed:
                self.say(f"hub was {used:.0f}G of its {cap}G budget - trimmed {freed:.0f}G of caches "
                         "from features nobody has touched (code and uncommitted work kept)")
            else:
                self.say(f"hub {used:.0f}G is over its {cap}G budget and everything is in use - "
                         "finish or delete a feature", error=True)
        elif free is not None and free < 20:
            self.say(f"disk {free}G free - kendle disk shows what can go", error=True)

    def stop_idle(self):
        if os.environ.get("KENDLE_STATE"):                # a test console: never touch the real stacks
            return
        try:
            done = _stack.stop_idle()
            if done:
                self.say(f"stopped idle services of {', '.join(done)} ({_stack.idle_minutes()} min without activity)")
        except Exception as err:
            self.say(f"idle check failed: {err}", error=True)

    def sync_logs(self):
        """The logs column follows the selection: up while the selected feature's services run, gone
        otherwise. Nothing happens when the user switched it off with L."""
        if core.ui_get("logs_mode", "auto") != "auto":
            return
        r = self.current()
        name = r["f"]["name"] if r and r.get("f") else None
        want = bool(name and name in self.stacks)
        if want != self.logs_shown:
            try:
                self.logs_shown = core.toggle_logs(want)
            except RuntimeError:
                pass

    def toggle_logs(self):
        if core.ui_get("logs_mode", "auto") == "auto":
            core.ui_set("logs_mode", "off")
            if self.logs_shown:
                core.toggle_logs(False)
            self.logs_shown = False
            self.say("logs column off - L makes it automatic again")
        else:
            core.ui_set("logs_mode", "auto")
            self.say("logs column automatic - it appears when the selected feature's services run")
            self.sync_logs()

    def resize_by(self, delta):
        width = core.set_sidebar_width(core.sidebar_width() + delta)
        core.tmux("resize-pane", "-t", f"{core.SESSION}:console.0", "-x", str(width), check=False)
        self.say(f"sidebar width {width} - remembered")

    def move(self, d):
        keys = [r["key"] for r in self.rows if r["kind"] in SELECTABLE]
        if keys:
            i = keys.index(self.sel) if self.sel in keys else 0
            self.sel = keys[max(0, min(len(keys) - 1, i + d))]

    def fold(self, folded):
        r = self.current()
        if not r:
            return
        section = {"desk": "desk", "idlehdr": "__idle__"}.get(r["kind"])
        if section:
            (self.folded.add if folded else self.folded.discard)(section)
        elif r.get("f"):
            name = r["f"]["name"]
            if folded:
                self.expanded.discard(name); self.collapsed.add(name)
            else:
                self.collapsed.discard(name); self.expanded.add(name)
            if folded and r["kind"] in ("manager", "sub", "internal"):
                self.sel = "f:" + name
        self.load()

    def mouse(self):
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        if b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            r = self.row_at.get(y)
            if r and r["kind"] in SELECTABLE:
                self.sel = r["key"]
                self.enter()
        elif b & curses.BUTTON4_PRESSED:
            self.move(-1)
        elif b & getattr(curses, "BUTTON5_PRESSED", 0):
            self.move(1)

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        curses.curs_set(0)
        self.scr.keypad(True)
        self.scr.timeout(250)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        self.colors()
        self.keep_width()
        self.refresh_now()
        threading.Thread(target=self.fetcher, daemon=True).start()
        actions = {"<": lambda: self.resize_by(-4), ">": lambda: self.resize_by(4), "g": self.new_review,
                   "L": self.toggle_logs,
                   "a": self.new_question, "p": self.promote, "m": self.manager, "s": self.sub, "n": self.new,
                   "K": self.stop, "r": self.refresh_now,
                   "\t": lambda: self.enter(focus=True), "\n": self.enter, "\r": self.enter,
                   curses.KEY_ENTER: self.enter, curses.KEY_MOUSE: self.mouse,
                   "j": lambda: self.move(1), curses.KEY_DOWN: lambda: self.move(1),
                   "k": lambda: self.move(-1), curses.KEY_UP: lambda: self.move(-1),
                   "h": lambda: self.fold(True), curses.KEY_LEFT: lambda: self.fold(True),
                   "l": lambda: self.fold(False), curses.KEY_RIGHT: lambda: self.fold(False),
                   "q": lambda: core.tmux("detach-client", check=False), curses.KEY_RESIZE: self.keep_width}
        while True:
            if time.time() - self.disk_checked > 900:    # disk watch: warn early, clear orphans when low
                self.disk_checked = time.time()
                threading.Thread(target=self.watch_disk, daemon=True).start()
            if time.time() - self.idle_checked > 60:      # stop stacks nobody has used for 30 min
                self.idle_checked = time.time()
                threading.Thread(target=self.stop_idle, daemon=True).start()
            if self.job_done:
                self.job_done = False
                self.refresh_now()
            elif self.pending_data is not None:       # new data from the background thread
                data, self.pending_data = self.pending_data, None
                self.apply(data)
            self.sync_logs()
            if self.pending_show:                     # a promoted question's new manager
                e, self.pending_show = self.pending_show, None
                self.sel = e["id"]
                try:
                    self.show(e, focus=True)
                except RuntimeError as err:
                    self.say(str(err), error=True)
            self.draw()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            action = actions.get(ch)
            if action:
                if ch not in (curses.KEY_MOUSE, curses.KEY_RESIZE):
                    self.msg = "" if not self.busy else self.msg
                try:
                    action()
                except (LookupError, ValueError, RuntimeError) as err:
                    self.say(str(err), error=True)


def main():
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda scr: Sidebar(scr).run())
    except Exception:
        os.makedirs(core.STATE, exist_ok=True)
        log = os.path.join(core.STATE, "sidebar.log")
        with open(log, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        traceback.print_exc()
        print(f"\nsidebar crashed - logged to {log}\nCtrl-b r restarts it")


if __name__ == "__main__":
    main()
