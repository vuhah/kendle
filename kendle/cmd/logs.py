"""kendle logs - the logs column of the kendle console: the selected feature's services, live.

  ← → or 1-9    switch service         v    split: two services stacked (Tab switches the active half)
  j k / PgUp PgDn  scroll              f    follow the newest lines again
  r             restart the service    K    stop the service (asks first)
"""
import curses, locale, os, re, signal, subprocess, sys, textwrap, threading, time, traceback
os.environ.setdefault("ESCDELAY", "25")
if not any(os.environ.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
    os.environ["LC_CTYPE"] = "en_US.UTF-8"
from kendle import core
from kendle import stack as _stack

ERR = re.compile(r"\b(ERROR|FATAL|SEVERE|Exception|Caused by:|FAILED|BUILD FAILURE)\b|^\s+at ")
WARN = re.compile(r"\b(WARN|WARNING)\b")
GLYPH = {"up": ("●", "green"), "starting": ("○", "yellow"), "crashed": ("✗", "red"), "stopped": ("·", "faint")}


def fit(text, room):
    return text if len(text) <= room else text[:max(0, room - 1)] + "…"


class Logs:
    def __init__(self, scr):
        self.scr = scr
        self.feature, self.rows = None, []
        self.tab, self.second, self.split, self.half = 0, 1, False, 0
        self.scroll = [0, 0]                           # lines up from the bottom, per half; 0 = follow
        self.msg, self.busy, self.window_width = "", None, None
        self.pending, self.wake = None, threading.Event()   # data from the background thread
        self.cache = {}                                     # log path -> (inode, offset, lines)
        self.stamp = None                                   # what was last drawn

    def colors(self):
        curses.start_color(); curses.use_default_colors()
        big = curses.COLORS >= 256
        c = lambda n, basic: n if big else basic
        self.pair = {}
        for i, (name, fg) in enumerate([("text", c(252, -1)), ("bright", c(255, curses.COLOR_WHITE)),
                                        ("dim", c(244, curses.COLOR_WHITE)), ("faint", c(239, curses.COLOR_WHITE)),
                                        ("green", c(114, curses.COLOR_GREEN)), ("yellow", c(179, curses.COLOR_YELLOW)),
                                        ("red", c(167, curses.COLOR_RED)), ("cyan", c(110, curses.COLOR_CYAN))]):
            curses.init_pair(1 + i, fg, -1)
            self.pair[name] = curses.color_pair(1 + i)

    def put(self, y, x, text, color="text", attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addstr(y, x, text[:max(0, w - 1 - x)], self.pair[color] | attr)
            except curses.error:
                pass

    # ---- data ----
    def fetch(self):
        """Which feature and its services' states - the slow part, off the key-handling thread."""
        focus = core.ui_get("focus_feature")
        has = lambda f: f and os.path.isdir(os.path.join(_stack.STATE, f)) and _stack.status(f)
        running = _stack.running_stacks()
        feature = focus if has(focus) else (running[0] if running else focus)
        return feature, (_stack.status(feature) if feature and has(feature) else [])

    def fetcher(self):
        while True:
            try:
                self.pending = self.fetch()
            except Exception as err:
                self.msg = f"refresh failed: {err}"
            self.wake.wait(1.5)
            self.wake.clear()

    def apply(self):
        if self.pending is None:
            return False
        (self.feature, self.rows), self.pending = self.pending, None
        n = len(self.rows)
        self.tab = min(self.tab, max(0, n - 1))
        self.second = min(self.second, max(0, n - 1))
        return True

    def raw(self, path):
        """The log's last lines, read incrementally: only what was appended since last time."""
        try:
            st = os.stat(path)
        except OSError:
            return []
        ino, off, lines = self.cache.get(path, (None, 0, []))
        if ino != st.st_ino or st.st_size < off:      # new or truncated file: read the tail again
            off, lines = max(0, st.st_size - 200_000), []
        if st.st_size > off:
            with open(path, "rb") as f:
                f.seek(off)
                chunk = f.read().decode("utf-8", "replace")
            parts = chunk.split("\n")
            if lines and not chunk.startswith("\n") and off:
                lines[-1] += parts.pop(0)
            lines = (lines + parts)[-3000:]
            off = st.st_size
        self.cache[path] = (st.st_ino, off, lines)
        return lines

    def visible(self, row, width, room, scroll):
        """Only the wrapped lines that fit on screen, found from the bottom up."""
        out, skip = [], scroll
        for line in reversed(self.raw(row["log"])):
            line = line.replace("\t", "    ")
            parts = textwrap.wrap(line, max(10, width), drop_whitespace=False) or [""]
            for part in reversed(parts):
                if skip:
                    skip -= 1
                    continue
                out.append((part, line))
            if len(out) >= room:
                break
        return list(reversed(out[:room]))

    # ---- drawing ----
    def draw_pane(self, top, height, row, half, w):
        state = row["state"]
        glyph, color = GLYPH.get(state, ("·", "faint"))
        active = (half == self.half) and self.split
        self.put(top, 1, glyph, color)
        self.put(top, 3, row["service"], "bright", curses.A_BOLD)
        info = f"{state} · port {row['port']}" + ("  ▲ scrolled - f follows" if self.scroll[half] else "")
        self.put(top, 5 + len(row["service"]), info, "cyan" if active else "dim")
        room = height - 1
        shown = self.visible(row, w - 2, room, self.scroll[half])
        if self.scroll[half] and len(shown) < room:   # scrolled past the top: pin to it
            self.scroll[half] = max(0, self.scroll[half] - (room - len(shown)))
            shown = self.visible(row, w - 2, room, self.scroll[half])
        for i, (text, full) in enumerate(shown):
            col = ("cyan" if full.startswith("=====") else "red" if ERR.search(full)
                   else "yellow" if WARN.search(full) else "text")
            self.put(top + 1 + i, 1, text, col)

    def draw(self, prompt=None):
        scr = self.scr; scr.erase()
        h, w = scr.getmaxyx()
        self.put(0, 1, "logs", "cyan", curses.A_BOLD)
        if self.feature:
            self.put(0, 7, fit(self.feature, w - 9), "dim")
        if not self.rows:
            self.put(2, 1, "No services for this feature yet.", "dim")
            self.put(3, 1, "The tester starts them with kendle stack; you can too:", "faint")
            self.put(4, 3, "kendle stack start <service> ...", "faint")
        else:
            x = 1
            for i, r in enumerate(self.rows):          # tabs
                glyph, color = GLYPH.get(r["state"], ("·", "faint"))
                label = f" {glyph} {r['service']} "
                if x + len(label) >= w - 1:
                    self.put(1, x, "…", "faint"); break
                sel = i == self.tab or (self.split and i == self.second)
                self.put(1, x, label, color if not sel else "bright", curses.A_REVERSE if sel else 0)
                x += len(label) + 1
            self.put(2, 1, "─" * (w - 3), "faint")
            body_top, body_h = 3, h - 5
            if self.split and len(self.rows) > 1:
                top_h = body_h // 2
                self.draw_pane(body_top, top_h, self.rows[self.tab], 0, w)
                self.put(body_top + top_h, 1, "─" * (w - 3), "faint")
                self.draw_pane(body_top + top_h + 1, body_h - top_h - 1, self.rows[self.second], 1, w)
            else:
                self.draw_pane(body_top, body_h, self.rows[self.tab], 0, w)
        foot = prompt or self.busy or self.msg or "← → service  v split  r restart  K stop  f follow"
        self.put(h - 1, 1, fit(foot, w - 3), "cyan" if prompt else "yellow" if self.busy else "faint")
        scr.refresh()

    # ---- actions ----
    def current(self):
        if not self.rows:
            return None
        return self.rows[self.second if (self.split and self.half == 1) else self.tab]

    def switch(self, d=None, to=None):
        if not self.rows:
            return
        n = len(self.rows)
        if self.split and self.half == 1:
            self.second = to if to is not None else (self.second + d) % n
        else:
            self.tab = to if to is not None else (self.tab + d) % n
        self.scroll[self.half if self.split else 0] = 0

    def background(self, busy, job):
        def work():
            try:
                job(); self.msg = busy.replace("ing", "ed", 1).split(" …")[0]
            except Exception as err:
                self.msg = str(err)
            self.busy = None
            self.wake.set()                               # refresh the states now
        self.busy = busy
        threading.Thread(target=work, daemon=True).start()

    def cli(self, *args):
        """Through the kendle stack command, so the service is never this pane's child (no zombies,
        and closing the column never takes a service with it)."""
        r = subprocess.run([core.KENDLE, "stack", args[0], "-f", self.feature, *args[1:]],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1])

    def restart(self):
        r = self.current()
        if r:
            self.background(f"restarting {r['service']} …", lambda: self.cli("restart", r["service"]))

    def stop(self):
        r = self.current()
        if not r:
            return
        self.draw(f"stop {r['service']}? y/N")
        self.scr.timeout(-1)
        ch = self.scr.get_wch()
        self.scr.timeout(200)
        if ch in ("y", "Y"):
            self.background(f"stopping {r['service']} …", lambda: self.cli("stop", r["service"]))
        else:
            self.msg = "cancelled"

    @staticmethod
    def size_of(path):
        try:
            return os.stat(path).st_size
        except OSError:
            return -1

    def scroll_by(self, n):
        i = self.half if self.split else 0
        self.scroll[i] = max(0, self.scroll[i] + n)

    def remember_width(self, start=False):
        """Fit the screen to the real size (whole at start-up), and remember a border the user
        dragged (the window kept its width)."""
        core.fit_screen(self.scr, start)
        win = core.tmux("display", "-p", "-t", f"{core.SESSION}:console", "#{window_width}", check=False)
        mine = core.tmux("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{pane_width}", check=False)
        if win == self.window_width and mine.isdigit():
            core.ui_set("logs_width", int(mine))          # the user dragged the border
        self.window_width = win

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        curses.curs_set(0); self.scr.keypad(True); self.scr.timeout(200)
        curses.mousemask(curses.ALL_MOUSE_EVENTS); curses.mouseinterval(0)
        self.colors(); self.remember_width(start=True)
        keys = {curses.KEY_RIGHT: lambda: self.switch(1), "l": lambda: self.switch(1),
                curses.KEY_LEFT: lambda: self.switch(-1), "h": lambda: self.switch(-1),
                "v": lambda: setattr(self, "split", not self.split), "\t": lambda: setattr(self, "half", 1 - self.half),
                "j": lambda: self.scroll_by(-1), curses.KEY_DOWN: lambda: self.scroll_by(-1),
                "k": lambda: self.scroll_by(1), curses.KEY_UP: lambda: self.scroll_by(1),
                curses.KEY_NPAGE: lambda: self.scroll_by(-20), curses.KEY_PPAGE: lambda: self.scroll_by(20),
                "f": lambda: self.scroll.__setitem__(self.half if self.split else 0, 0),
                curses.KEY_END: lambda: self.scroll.__setitem__(self.half if self.split else 0, 0),
                "r": self.restart, "K": self.stop, curses.KEY_RESIZE: self.remember_width}
        self.pending = self.fetch()
        threading.Thread(target=self.fetcher, daemon=True).start()
        dirty = True
        while True:
            dirty = self.apply() or dirty
            sizes = tuple(self.size_of(r["log"]) for r in self.rows)
            stamp = (sizes, self.tab, self.second, self.split, self.half, tuple(self.scroll), self.msg, self.busy,
                     self.feature, tuple(r["state"] for r in self.rows), self.scr.getmaxyx())
            if dirty or stamp != self.stamp:
                self.draw()
                self.stamp, dirty = stamp, False
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            dirty = True
            if isinstance(ch, str) and ch.isdigit() and ch != "0":
                self.switch(to=min(int(ch), len(self.rows)) - 1) if self.rows else None
                continue
            if ch == curses.KEY_MOUSE:
                try:
                    _, x, y, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & getattr(curses, "BUTTON4_PRESSED", 0): self.scroll_by(3)
                elif b & getattr(curses, "BUTTON5_PRESSED", 0): self.scroll_by(-3)
                continue
            action = keys.get(ch)
            if action:
                self.msg = "" if not self.busy else self.msg
                action()


def main():
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda scr: Logs(scr).run())
    except Exception:
        os.makedirs(core.STATE, exist_ok=True)
        with open(os.path.join(core.STATE, "logs.log"), "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        traceback.print_exc()
        print("\nlogs column crashed - L in the sidebar twice restarts it")
        time.sleep(3600)


if __name__ == "__main__":
    main()
