"""kendle.toml: where the workspace is and how it is laid out.

The workspace is KENDLE_WORKSPACE, else the nearest folder at or above the current one that holds a
kendle.toml. Every key has a default except repo.path; profiles/example/kendle.toml documents them all.
"""
import os, re, subprocess, sys

FILE = "kendle.toml"

DEFAULTS = {
    "repo": {
        "path": None,              # the primary checkout, relative to the workspace; never a feature
        "remote": "origin",
        "base": "main",            # features branch from <remote>/<base>; the Ask desk follows it
    },
    "workspace": {
        "name": None,              # shown at the top of the sidebar; default: the workspace folder's name
        "docs": "agent_docs",      # per-feature notes: <docs>/<feature>/, reviews in <docs>/reviews/
        "ask": "ask",              # the Ask desk's folder: a detached worktree on the latest base
        "reserved": [],            # more folder names that are never features
        "reserve_gb": 10,          # free disk a new feature needs
        "low_disk_hint": "kendle disk shows what can go",
        # links made in every worktree kendle creates: path in the worktree = path in the workspace.
        # Only where nothing exists yet and git tracks nothing; kept out of git status.
        "links": {"agent_docs": "{docs}", ".claude/agents": "roles"},
    },
    "disk": {
        "budget_gb": 60,           # the workspace's size budget (kendle disk budget <gb> overrides it)
        "caches": [],              # regenerable folders inside each worktree, e.g. "web/dist"
        "bazel": False,            # also count and trim each worktree's bazel output base
        "bazel_root": "",          # default: bazel's own output root for this user
    },
    "team": {
        "mcp_servers": None,       # tool servers kendle's sessions keep (by name); unset: all of yours
        "disabled_plugins": [],    # plugins switched off in kendle's sessions
    },
    "review": {
        "host": None,              # gerrit, github or gitlab; unset: guessed from the remote's URL
        "slots": 3,                # at most this many reviews open side by side
    },
    "task": {
        "fetch": None,             # a command that prints a task, {id} replaced: "jira-cli view {id}"
    },
    "gate": {
        "fetch_base": True,        # fetch the base first, and require the commits to sit on top of it
        "max_commits": 0,          # commits allowed ahead of the base; 0: any number
        "clean_tree": True,        # everything committed
        "stray": [],               # regexes: paths that must never be in the change, e.g. "^\\.idea/"
        "message": None,           # a command checking the commit message; {file} is a file holding it
        "cleanup": [],             # untracked files steps leave behind, removed before and after
        "steps": {},               # [gate.steps.<name>]: run, when, full, lane, needs, timeout, warn_if
    },
    "autopilot": {                 # issues to merged pull requests, unattended - docs/autopilot.md
        "enabled": False,          # kendle autopilot run refuses unless true
        "repo": None,              # owner/name on GitHub; unset: read from the remote's URL
        "label": "autopilot",      # work starts on open issues with this label ...
        "authors": [],             # ... opened by one of these GitHub accounts (required)
        "max_active": 2,           # issues worked at once
        "rounds": 3,               # review rounds before it stops and asks you
        "fixes": 3,                # gate, rebase or CI failures sent back to the team, per issue
        "nudges": 5,               # turns that ended without READY or STUCK, per issue
        "interval": 120,           # seconds between ticks
        "merge": "squash",         # squash, merge or rebase
        "checks": True,            # merge only after the pull request's checks ran and passed
        "allow": [],               # commands the team may run unasked, as Claude Code rules: "Bash(npm test:*)"
    },
    "prompts": {},                 # see PROMPTS below; any of them can be replaced
}

# {name} fields are filled where each prompt is used; an unknown field is left as written.
PROMPTS = {
    "ask": (
        "You are the Ask desk. Answer the user's question about the product or the code, using this "
        "folder: a read-only checkout of the latest {remote}/{base}. Your reply's very first line is the "
        "verdict, nothing before it - Bug, Not a bug, Works as designed, or Needs a change: yes/no (or the "
        "direct answer) - then at most 5 short lines of evidence with file:line, in plain words. "
        "Investigate in the foreground and give the verdict in this same turn - never start background "
        "agents or tasks and never reply 'still checking'. Read code, docs and git history as needed; "
        "never edit, plan, or start a team. If a change is needed, end with a line "
        "'Suggested feature name: <kebab-case>' and 3 short lines on what to change."),
    "review": (
        "You are reviewing someone else's change, checked out in this folder (detached, at their latest "
        "revision). Start with one verdict line - Approve - Comments - Blocked - then the findings, worst "
        "first, each one line: file:line, what is wrong, and what you would do instead. Plain words, no "
        "jargon. Check the change against the conventions in the workspace CLAUDE.md and the pattern the "
        "code already uses in that area. Give the findings in chat; they are saved to "
        "{docs}/reviews/{file}.md when the user closes the review, so write them as the text they would "
        "paste to the author.\n"
        "You are a reader: never edit code, never commit, never push, and never post anything anywhere - "
        "the user decides what to send and sends it themselves."),
    "sub": (
        "You are a read-only sub-agent working for the '{feature}' manager session. "
        "Investigate and report your findings in chat. Do not edit files or change any state."),
    "manager": (
        "You are the manager of the '{feature}' feature team in the kendle console. Your worktree is "
        "{path} on branch {branch}. Run the team as the workspace CLAUDE.md describes; you do not read "
        "or edit code yourself."),
    "fresh": (
        "You are this feature's manager, starting fresh after a restart. Read {docs}/{feature}/team.md "
        "first, then only the docs you need. Continue from the stage it records."),
    "promote": (
        "New feature, promoted from an Ask-desk question. The question, what the desk found and the "
        "whole conversation are in {docs}/{feature}/requirement.md. Start the planner with it."),
}


def fill(template, **fields):
    """str.format for prompts that may hold literal braces: only known {name}s are replaced."""
    return re.sub(r"\{(\w+)\}", lambda m: str(fields[m[1]]) if m[1] in fields else m[0], template)


def find_workspace(start=None):
    if os.environ.get("KENDLE_WORKSPACE"):
        d = os.path.realpath(os.environ["KENDLE_WORKSPACE"])
        if not os.path.isfile(os.path.join(d, FILE)):
            raise SystemExit(f"kendle: KENDLE_WORKSPACE is {d}, which has no {FILE}.")
        return d
    d = os.path.realpath(start or os.getcwd())
    while not os.path.isfile(os.path.join(d, FILE)):
        if os.path.dirname(d) == d:
            raise SystemExit(f"kendle: no workspace here - run it inside one (a folder with {FILE}), "
                             "or set KENDLE_WORKSPACE.")
        d = os.path.dirname(d)
    return d


def check(workspace):
    """None if the workspace's kendle.toml loads as every kendle command loads it, else why not.
    Tried in a fresh Python: kendle reads its config once, as it is imported, and some of it (services,
    review slots) only in the module that uses it."""
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    code = f"import sys; sys.path.insert(0, {root!r}); import kendle.stack, kendle.review, kendle.cmd.disk"
    r = subprocess.run([sys.executable, "-c", code], cwd=workspace, capture_output=True, text=True,
                       env=dict(os.environ, KENDLE_WORKSPACE=workspace), stdin=subprocess.DEVNULL)
    if r.returncode == 0:
        return None
    lines = [line for line in r.stderr.splitlines() if line.strip()] or [f"exit {r.returncode}"]
    last = lines[-1].replace(os.path.join(os.path.realpath(workspace), FILE), FILE)
    return last[len("kendle: "):] if last.startswith("kendle: ") else last


def load(workspace):
    path = os.path.join(workspace, FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = parse(f.read())
    except ValueError as err:
        raise SystemExit(f"kendle: {path}: {err}")
    cfg = {}
    for section, defaults in DEFAULTS.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            raise SystemExit(f"kendle: {path}: [{section}] must be a table")
        unknown = set(given) - set(defaults) - (set(PROMPTS) if section == "prompts" else set())
        if unknown:
            raise SystemExit(f"kendle: {path}: unknown key {section}.{sorted(unknown)[0]}")
        cfg[section] = dict(defaults, **given)
    cfg["prompts"] = dict(PROMPTS, **cfg["prompts"])
    for section in set(raw) - set(DEFAULTS):
        cfg[section] = raw[section]                  # adapters (services, gate, ...) read their own
    if not cfg["repo"]["path"]:
        raise SystemExit(f"kendle: {path}: repo.path is required - the primary checkout's folder")
    return cfg


# ---- a TOML subset, for Pythons before 3.11 -------------------------------------------
# Tables, dotted table names, and values that are strings ("", '', \"\"\" \"\"\"), integers,
# floats, booleans and (multi-line) arrays of those. Enough for kendle.toml; tomllib when present.

def parse(text):
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        return _Parser(text).document()


class _Parser:
    ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r", "b": "\b", "f": "\f"}

    def __init__(self, text):
        self.s, self.i, self.line = text, 0, 1

    def error(self, msg):
        return ValueError(f"line {self.line}: {msg}")

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def take(self, n=1):
        out = self.s[self.i:self.i + n]
        self.line += out.count("\n")
        self.i += n
        return out

    def blank(self, newlines=False):
        """Skip spaces and comments (and newlines, inside arrays)."""
        while self.i < len(self.s):
            c = self.peek()
            if c in " \t" or (newlines and c in "\r\n"):
                self.take()
            elif c == "#":
                while self.i < len(self.s) and self.peek() != "\n":
                    self.take()
            else:
                break

    def end_of_line(self):
        self.blank()
        if self.i < len(self.s) and self.peek() not in "\r\n":
            raise self.error(f"unexpected {self.peek()!r}")

    def document(self):
        root = table = {}
        while self.i < len(self.s):
            self.blank(newlines=True)
            if self.i >= len(self.s):
                break
            if self.peek() == "[":
                self.take()
                if self.peek() == "[":
                    raise self.error("arrays of tables are not supported")
                table = root
                for key in self.keys("]"):
                    table = table.setdefault(key, {})
                    if not isinstance(table, dict):
                        raise self.error(f"{key} is not a table")
                self.take()
            else:
                *path, key = self.keys("=")
                self.take()
                self.blank()
                where = table
                for p in path:
                    where = where.setdefault(p, {})
                if key in where:
                    raise self.error(f"{key} is set twice")
                where[key] = self.value()
            self.end_of_line()
        return root

    def keys(self, stop):
        keys = []
        while True:
            self.blank()
            if self.peek() in "\"'":
                keys.append(self.string())
            else:
                m = re.match(r"[A-Za-z0-9_-]+", self.s[self.i:])
                if not m:
                    raise self.error("expected a key")
                keys.append(self.take(m.end()))
            self.blank()
            if self.peek() == ".":
                self.take()
            elif self.peek() == stop:
                return keys
            else:
                raise self.error(f"expected '{stop}'")

    def value(self):
        c = self.peek()
        if c in "\"'":
            return self.string()
        if c == "[":
            self.take()
            out = []
            while True:
                self.blank(newlines=True)
                if self.peek() == "]":
                    self.take()
                    return out
                out.append(self.value())
                self.blank(newlines=True)
                if self.peek() == ",":
                    self.take()
                elif self.peek() != "]":
                    raise self.error("expected ',' or ']' in an array")
        m = re.match(r"(true|false)\b|[+-]?\d[\d_]*(\.\d+)?([eE][+-]?\d+)?", self.s[self.i:])
        if not m:
            raise self.error("expected a value")
        word = self.take(m.end())
        if word in ("true", "false"):
            return word == "true"
        return float(word.replace("_", "")) if m.group(2) or m.group(3) else int(word.replace("_", ""))

    def string(self):
        q, start = self.peek(), self.line
        if self.peek(3) == q * 3:
            self.take(3)
            if self.peek() == "\n":
                self.take()                          # a newline right after the opening quotes is dropped
            elif self.peek(2) == "\r\n":
                self.take(2)
            end = self.s.find(q * 3, self.i)
            if end < 0:
                raise ValueError(f"line {start}: unterminated string")
            while self.s[end + 3:end + 4] == q:      # up to two quotes may sit right before the closing three
                end += 1
            body = self.take(end - self.i)
            self.take(3)
            return self.unescape(body, multiline=True) if q == '"' else body
        self.take()
        out = []
        while True:
            c = self.take()
            if not c or c == "\n":
                raise ValueError(f"line {start}: unterminated string")
            if c == q:
                return "".join(out)
            if c == "\\" and q == '"':
                c += self.take()
                if c[1] == "u" or c[1] == "U":
                    c += self.take(4 if c[1] == "u" else 8)
            out.append(c)
            if q == '"' and out[-1].startswith("\\"):
                out[-1] = self.unescape(out[-1])

    def unescape(self, body, multiline=False):
        def one(m):
            e = m.group(1)
            if e[0] in "\r\n \t":                    # line-ending backslash: drop the break and the indent
                return ""
            if e[0] in "uU":
                return chr(int(e[1:], 16))
            if e in self.ESCAPES:
                return self.ESCAPES[e]
            raise self.error(f"bad escape \\{e}")
        pattern = r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[ \t]*\r?\n[ \t\r\n]*|.)" if multiline else \
                  r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)"
        return re.sub(pattern, one, body, flags=re.S)
