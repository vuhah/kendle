"""Throwaway workspaces for kendle's tests: a bare origin, a clone, a kendle.toml, and a tmux socket,
state folder and transcript folder of their own - nothing a test does reaches the real ones.

Every test drives kendle the way a user does, through bin/kendle, or runs a snippet in a fresh Python
with the workspace's environment (kendle reads its config when it is imported).
"""
import json, os, shutil, subprocess, sys, tempfile, textwrap, time, unittest, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
KENDLE = os.path.join(ROOT, "bin", "kendle")
PY = sys.executable
RUN = [PY, KENDLE]           # kendle on the Python running the tests, not whatever python3 is on PATH
FAKEBIN = os.path.join(ROOT, "tests", "fakebin")


def sh(*args, cwd=None, check=True, env=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise AssertionError(f"{' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def read(path):
    with open(path, errors="replace") as f:
        return f.read()


def wait_for(predicate, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class Workspace:
    """A workspace in a temp folder: <root>/origin.git, <root>/ws/{app,ask,agent_docs,kendle.toml}."""

    def __init__(self, toml="", base="main", ask=True):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="kendle-test-"))
        self.ws = os.path.join(self.root, "ws")
        self.origin = os.path.join(self.root, "origin.git")
        self.app = os.path.join(self.ws, "app")
        self.state = os.path.join(self.root, "state")
        self.projects = os.path.join(self.root, "projects")
        self.socket = f"kendletest-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        os.makedirs(self.state)
        os.makedirs(self.projects)
        seed = os.path.join(self.root, "seed")
        sh("git", "init", "-q", "-b", base, seed)
        self.git(seed, "commit", "-q", "--allow-empty", "-m", "first")
        sh("git", "clone", "-q", "--bare", seed, self.origin)
        os.makedirs(os.path.join(self.ws, "agent_docs"))
        sh("git", "clone", "-q", self.origin, self.app)
        if ask:
            sh("git", "-C", self.app, "worktree", "add", "-q", "--detach", os.path.join(self.ws, "ask"), f"origin/{base}")
        self.write_toml(f'[repo]\npath = "app"\nbase = "{base}"\n' + textwrap.dedent(toml))

    # -- setup --

    def write_toml(self, text):
        with open(os.path.join(self.ws, "kendle.toml"), "w") as f:
            f.write(text)

    def add_toml(self, text):
        with open(os.path.join(self.ws, "kendle.toml"), "a") as f:
            f.write("\n" + textwrap.dedent(text))

    @staticmethod
    def git(path, *args):
        return sh("git", "-C", path, "-c", "user.email=t@example.com", "-c", "user.name=Tester", *args).stdout.strip()

    def commit(self, path, files, message):
        for name, text in files.items():
            full = os.path.join(path, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", message)
        return self.git(path, "rev-parse", "HEAD")

    def push_ref(self, files, message, ref, parent="origin/main"):
        """A commit on top of parent, pushed to origin as ref (a review host's change ref)."""
        work = os.path.join(self.root, "pusher")
        if not os.path.isdir(work):
            sh("git", "clone", "-q", self.origin, work)
        self.git(work, "fetch", "-q", "origin")
        self.git(work, "checkout", "-q", "--detach", parent)
        sha = self.commit(work, files, message)
        self.git(work, "push", "-q", "origin", f"HEAD:{ref}")
        return sha

    # -- running kendle --

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "KENDLE_", "TMUX"))}
        env.update(KENDLE_WORKSPACE=self.ws, KENDLE_STATE=self.state, KENDLE_SOCKET=self.socket,
                   KENDLE_PROJECTS=self.projects, KENDLE_NO_SIDEBAR="1", PATH=FAKEBIN + os.pathsep + env["PATH"])
        env.update(extra)
        return env

    def kendle(self, *args, cwd=None, check=True, **env):
        return sh(*RUN, *args, cwd=cwd or self.ws, check=check, env=self.env(**env))

    def py(self, code, cwd=None, check=True, **env):
        """Run a snippet with `from kendle import ...` available; returns its stdout, or its JSON."""
        prog = f"import sys, json\nsys.path.insert(0, {ROOT!r})\n" + textwrap.dedent(code)
        out = sh(PY, "-c", prog, cwd=cwd or self.ws, check=check, env=self.env(**env)).stdout.strip()
        try:
            return json.loads(out.splitlines()[-1]) if out else None
        except ValueError:
            return out

    def tmux(self, *args, check=False):
        return sh("tmux", "-L", self.socket, *args, check=check).stdout.strip()

    # -- what the fake claude saw, and transcripts it would have written --

    def claude_calls(self):
        calls = []
        for name in sorted(os.listdir(self.state)):
            if name.startswith("claude-") and name.endswith(".args"):
                lines = read(os.path.join(self.state, name)).splitlines()
                calls.append({"cwd": lines[0], "args": lines[1:]})
        return calls

    def transcript_path(self, sid, cwd):
        import re
        d = os.path.join(self.projects, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, sid + ".jsonl")

    def write_transcript(self, path, events):
        with open(path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def close(self):
        sh("tmux", "-L", self.socket, "kill-server", check=False)
        sock = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", self.socket)
        for d in (sock, os.path.join("/private/tmp", f"tmux-{os.getuid()}", self.socket)):
            if os.path.exists(d):
                os.remove(d)
        sh("pkill", "-f", self.root, check=False)               # anything a test started in here
        shutil.rmtree(self.root, ignore_errors=True)


def stamp(seconds_ago=0):
    import datetime
    t = datetime.datetime.utcnow() - datetime.timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(text, ago=0):
    return {"type": "user", "timestamp": stamp(ago), "message": {"role": "user", "content": text}}


def assistant(text="", stop="end_turn", ago=0, tool=None):
    content = [{"type": "text", "text": text}] if text else []
    if tool:
        content.append({"type": "tool_use", "id": "t1", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "timestamp": stamp(ago),
            "message": {"role": "assistant", "stop_reason": stop, "content": content,
                        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


class KendleTest(unittest.TestCase):
    """A fresh workspace per test class; subclasses set TOML."""
    TOML = ""
    ASK = True

    @classmethod
    def setUpClass(cls):
        cls.w = Workspace(cls.TOML, ask=cls.ASK)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
