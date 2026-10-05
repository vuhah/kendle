"""kendle gate - the checks a change must pass before it is pushed, run locally.

  kendle gate                        run the whole gate here, in the foreground
  kendle gate --start                run it in the background (it can take a long time)
  kendle gate --wait                 wait up to 9 min; exit 0 PASS, 1 FAIL, 75 still running
  kendle gate --message <file|rev>   check only a commit message
  --full                             also run the steps marked full (what CI runs after the push)

gate.message is a command run on a file holding the message ({file}; {source} is commit or file).

Run inside a feature worktree. First the git checks kendle.toml [gate] asks for (on top of the base,
how many commits, a clean tree, no stray files, the commit message), then the steps in
[gate.steps.<name>]: each a shell command that passes with exit code 0.

  [gate.steps.lint]
  run = "npm run lint"          {worktree} {workspace} {base_sha} {changed} {full} {gate_dir} filled in
  when = ["web/", "*.ts"]       only when a changed file is under one of these (prefix or glob)
  lane = "web"                  steps in one lane run in order; lanes run side by side
  full = true                   only with --full
  needs = ["build"]             skipped unless these steps passed
  timeout = 1800                seconds (default 3600)
  warn_if = "Exec format error" output matching this is a WARN (the tool could not run here), not a FAIL

A step's environment has KENDLE_BASE_SHA, KENDLE_UPSTREAM, KENDLE_CHANGED (a file listing the changed
paths), KENDLE_FULL (1/0) and KENDLE_GATE_DIR. A line it prints as `::warn:: <name>: <detail>` or
`::fail:: <name>: <detail>` becomes a row of its own. A gate that passed is not run again for the
same commit, base and settings.
"""
import fnmatch, hashlib, json, os, re, subprocess, sys, threading, time

FULL = False


def sh(args, timeout=120, **kw):
    try:
        return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, e.stdout or "", f"timed out after {timeout}s")


def git(*a):
    return sh(["git", *a]).stdout.strip()


def settings():
    from kendle import core
    return core, core.CONFIG["gate"]


def kendle_bin():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "bin", "kendle")


# ---- commit message ----------------------------------------------------------------

def message_check(msg, source="commit"):
    """(ok, detail) from kendle.toml's gate.message command, run on a file holding the message;
    {source} tells it whether the message is a commit's (trailers present) or a draft file's."""
    core, gate = settings()
    if not gate["message"]:
        return True, ""
    from kendle import config
    path = os.path.join(git("rev-parse", "--absolute-git-dir") or core.STATE, "kendle-gate-message.txt")
    with open(path, "w") as f:
        f.write(msg)
    r = sh(["/bin/sh", "-c", config.fill(gate["message"], file=path, source=source, workspace=core.HUB)], timeout=120)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def message_only(arg):
    source = "commit"
    if os.path.isfile(arg):
        msg, source = open(arg).read(), "file"
    else:
        r = sh(["git", "log", "-1", "--format=%B", arg])
        if r.returncode:
            sys.exit(f"kendle gate: '{arg}' is neither a file nor a commit")
        msg = r.stdout
    ok, detail = message_check(msg, source)
    print("  PASS  message format" if ok else "  FAIL  message format")
    if detail and not ok:
        print("          " + detail.replace("\n", "\n          "))
    return 0 if ok else 1


# ---- the gate ------------------------------------------------------------------------

class Gate:
    def __init__(self):
        self.top = git("rev-parse", "--show-toplevel")
        if not self.top:
            sys.exit("kendle gate: run it inside a feature worktree")
        os.chdir(self.top)
        self.core, self.cfg = settings()
        self.state_dir = os.path.join(git("rev-parse", "--absolute-git-dir"), "kendle-gate")
        os.makedirs(self.state_dir, exist_ok=True)
        self.rows, self.lock, self.results = [], threading.Lock(), {}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        nvm = os.path.expanduser("~/.nvm")
        if not self.env.get("NVM_DIR") and os.path.exists(f"{nvm}/nvm.sh"):
            self.env["NVM_DIR"] = nvm                 # agent shells don't inherit it

    def status(self, **kw):
        with open(os.path.join(self.state_dir, "status.json"), "w") as f:
            json.dump(dict(kw, rows=self.rows, updated=time.time()), f)

    def check(self, name, result, detail="", log=None):
        with self.lock:
            self.rows.append({"name": name, "result": result})
            line = f"  {result:<4}  {name}"
            if detail and result != "PASS":
                line += "\n          " + detail.replace("\n", "\n          ")
            if log and result == "FAIL":
                line += f"\n          full log: {log}"
            print(line, flush=True)
            self.status(running=True, current=None)

    def run(self, name, step, fields):
        """A step, with its output written to its log while it runs and a heartbeat in status.json -
        a long step must never look dead from outside."""
        from kendle import config
        log = os.path.join(self.state_dir, re.sub(r"\W+", "-", name.lower()).strip("-") + ".log")
        timeout = int(step.get("timeout", 3600))
        print(f"  ....  {name}", flush=True)
        t0 = time.time()
        with open(log, "w") as f:
            proc = subprocess.Popen(["/bin/bash", "-c", config.fill(step["run"], **fields)], stdout=f,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self.step_env, text=True)
            code = None
            while code is None:
                try:
                    code = proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.status(running=True, current=name, since=t0)
                    if time.time() - t0 > timeout:
                        proc.kill()
                        code = 124
        text = open(log, errors="replace").read()
        extra_failed = False
        for kind, row in re.findall(r"^::(warn|fail)::\s*(.+)$", text, re.M):
            title, _, detail = row.partition(": ")
            self.check(title.strip(), kind.upper(), detail.strip(), log if kind == "fail" else None)
            extra_failed = extra_failed or kind == "fail"
        plain = "\n".join(l for l in text.strip().splitlines() if not l.startswith("::"))
        tail = "\n".join(plain.splitlines()[-15:]) + ("\ntimed out after %ds" % timeout if code == 124 else "")
        broken = step.get("warn_if") and re.search(step["warn_if"], text)
        if broken and code:
            tail += "\nthe tool itself could not run here - this is not a verdict on the code"
        result = "PASS" if code == 0 and not extra_failed else ("WARN" if broken else "FAIL")
        self.check(f"{name} ({int(time.time() - t0)}s)", result, tail, log)
        self.results[name] = result
        return result != "FAIL"

    # -- the git checks --

    def git_checks(self):
        cfg, up = self.cfg, self.core.UPSTREAM
        if cfg["fetch_base"]:
            f = sh(["git", "fetch", "-q", self.core.REMOTE, self.core.BASE], timeout=180)
            self.check(f"fetched {up}", "PASS" if f.returncode == 0 else "FAIL", f.stderr.strip()[-200:])
            on_top = sh(["git", "merge-base", "--is-ancestor", up, "HEAD"]).returncode == 0
            self.check(f"on top of {up} (rebased)", "PASS" if on_top else "FAIL",
                       f"{git('rev-list', '--count', 'HEAD..' + up)} commits behind - rebase onto {up}")
        ahead = int(git("rev-list", "--count", f"{up}..HEAD") or 0)
        most = int(cfg["max_commits"] or 0)
        ok = 1 <= ahead <= most if most else ahead >= 1
        self.check("exactly one commit ahead" if most == 1 else f"commits ahead: {ahead}" + (f" (at most {most})" if most else ""),
                   "PASS" if ok else "FAIL", f"{ahead} commits ahead" if most else "nothing to ship - no commit ahead")
        self.cleanup()                                # a step's own leftovers, never the user's
        if cfg["clean_tree"]:
            dirty = [l for l in sh(["git", "status", "--porcelain"]).stdout.splitlines() if l.strip()]
            self.check("clean tree - everything is in the commit", "PASS" if not dirty else "FAIL", "; ".join(dirty[:6]))
        self.base_sha = git("merge-base", up, "HEAD") or git("rev-parse", "HEAD^")
        self.files = git("diff", "--name-only", f"{self.base_sha}...HEAD").split()
        if cfg["stray"]:
            stray = [p for p in self.files if any(re.search(rx, p) for rx in cfg["stray"])]
            self.check("no stray files in the change", "PASS" if not stray else "FAIL", ", ".join(stray[:6]))
        if cfg["message"]:
            ok, detail = message_check(git("log", "-1", "--format=%B"))
            self.check("commit message follows the template", "PASS" if ok else "FAIL", detail)
        return all(r["result"] == "PASS" for r in self.rows)

    def cleanup(self):
        for f in self.cfg.get("cleanup") or []:
            if os.path.exists(f) and not git("ls-files", f):
                os.remove(f)

    # -- the steps --

    def wanted(self, step):
        if step.get("full") and not FULL:
            return False
        when = step.get("when")
        if not when:
            return True
        return any(f.startswith(w) or fnmatch.fnmatch(f, w) for f in self.files for w in when)

    def lane(self, steps, fields):
        for name, step in steps:
            missing = [n for n in step.get("needs", []) if self.results.get(n) not in ("PASS", "WARN")]
            if missing:
                self.check(name, "SKIP", f"needs {', '.join(missing)} to pass")
                continue
            self.run(name, step, fields)

    def key(self):
        """What a pass is valid for: this commit, this base, these settings, --full or not."""
        conf = json.dumps(self.cfg, sort_keys=True, default=str)
        return f"{git('rev-parse', 'HEAD')} {git('rev-parse', self.core.UPSTREAM)} {int(FULL)} " \
               f"{hashlib.sha1(conf.encode()).hexdigest()[:12]}"

    def main(self):
        t0 = time.time()
        head = git("rev-parse", "HEAD")
        self.status(running=True, head=head)
        print(f"kendle gate on {os.path.basename(self.top)} @ {head[:10]}" + ("  (--full)" if FULL else "") + "\n", flush=True)
        if not self.git_checks():
            return self.finish(t0, "fix the lines above first - the steps did not run")
        steps = [(n, s) for n, s in (self.cfg["steps"] or {}).items() if self.wanted(s)]
        if not steps:
            return self.finish(t0)
        passed, key = os.path.join(self.state_dir, "passed"), self.key()
        if os.path.exists(passed) and open(passed).read().strip() == key:
            print("\n  PASS  steps - nothing changed since they last passed on this commit and base", flush=True)
            return self.finish(t0)
        changed = os.path.join(self.state_dir, "changed.txt")
        with open(changed, "w") as f:
            f.write("\n".join(self.files) + ("\n" if self.files else ""))
        self.step_env = dict(self.env, KENDLE_BASE_SHA=self.base_sha, KENDLE_UPSTREAM=self.core.UPSTREAM,
                             KENDLE_CHANGED=changed, KENDLE_FULL="1" if FULL else "0", KENDLE_GATE_DIR=self.state_dir)
        fields = {"worktree": self.top, "workspace": self.core.HUB, "base_sha": self.base_sha, "changed": changed,
                  "full": "1" if FULL else "0", "gate_dir": self.state_dir}
        lanes = {}
        for name, step in steps:
            lanes.setdefault(step.get("lane", "main"), []).append((name, step))
        print("\n  Plan: " + " · ".join(n for n, _ in steps) + "\n", flush=True)
        try:
            threads = [threading.Thread(target=self.lane, args=(ls, fields)) for ls in lanes.values()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            self.cleanup()
            if git("rev-parse", "--abbrev-ref", "HEAD") == "HEAD":
                print("  WARN  HEAD is detached - check out the feature branch again", flush=True)
        if all(r["result"] in ("PASS", "WARN") for r in self.rows):
            open(passed, "w").write(key + "\n")
        return self.finish(t0)

    def finish(self, t0, note=""):
        ok = all(r["result"] != "FAIL" for r in self.rows)
        mins = (time.time() - t0) / 60
        print(f"\n{'GATE PASSED' if ok else 'GATE FAILED - do not push'}  ({mins:.0f} min)" + (f"\n{note}" if note else ""), flush=True)
        self.status(running=False, passed=ok, minutes=round(mins, 1))
        return 0 if ok else 1


def start():
    state = os.path.join(git("rev-parse", "--absolute-git-dir"), "kendle-gate")
    os.makedirs(state, exist_ok=True)
    log = os.path.join(state, "gate.log")
    json.dump({"running": True, "rows": [], "updated": time.time()}, open(os.path.join(state, "status.json"), "w"))
    subprocess.Popen([kendle_bin(), "gate"] + (["--full"] if FULL else []),
                     stdout=open(log, "w"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=git("rev-parse", "--show-toplevel"))
    print(f"gate started in the background\n  progress: kendle gate --wait\n  log: {log}")
    return 0


def wait(limit=540):
    state = os.path.join(git("rev-parse", "--absolute-git-dir"), "kendle-gate")
    log, st = os.path.join(state, "gate.log"), os.path.join(state, "status.json")
    end, shown = time.time() + limit, 0
    while True:
        text = open(log).read() if os.path.exists(log) else ""
        print(text[shown:], end="", flush=True); shown = len(text)
        s = json.load(open(st)) if os.path.exists(st) else {}
        if not s.get("running"):
            return 0 if s.get("passed") else 1
        if time.time() > end:
            print("\n(still running - run kendle gate --wait again)")
            return 75
        time.sleep(10)


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--full" in a:
        FULL = True
        a = [x for x in a if x != "--full"]
    if a and a[0] in ("-h", "--help"):
        print(__doc__.strip()); sys.exit(0)
    if len(a) == 2 and a[0] == "--message":
        sys.exit(message_only(a[1]))
    if a == ["--start"]:   # --full was taken out above and is forwarded by start()
        sys.exit(start())
    if a == ["--wait"]:
        sys.exit(wait())
    if a:
        print(__doc__.strip(), file=sys.stderr); sys.exit(2)
    sys.exit(Gate().main())
