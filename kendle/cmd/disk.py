"""kendle disk - what the workspace uses, and how to get space back.

  kendle disk                   report: free space, each feature, the desk and reviews, what is safe to reclaim
  kendle disk trim <folder>…    delete a feature's, the desk's or a review folder's regenerable build caches
                                (code and WIP untouched; never while its services run or its session works)
  kendle disk orphans           delete bazel output bases whose worktree no longer exists
  kendle disk idle [days]       trim folders with no session and no commit for N days (default 3)
  kendle disk merged            features whose commit is already on the base branch - the folder can go
  kendle disk budget [gb]       show or set the workspace's size budget (kendle.toml: disk.budget_gb)
  kendle disk enforce [--dry]   trim the quietest folders' caches until the workspace fits its budget

Every size is printed before anything goes, and only regenerable things are ever deleted: the
folders listed in kendle.toml's disk.caches, and bazel output bases when disk.bazel is on. Code,
branches and uncommitted work stay.
"""
import getpass
import os
import shutil
import subprocess
import sys
import time

from kendle import core

DISK = core.CONFIG["disk"]
CACHES = tuple(DISK["caches"])            # regenerable folders inside each worktree
BAZEL = bool(DISK["bazel"])


def bazel_root():
    """Where bazel keeps output bases: kendle.toml's disk.bazel_root, else bazel's own default."""
    if DISK["bazel_root"]:
        return os.path.expanduser(DISK["bazel_root"])
    user = getpass.getuser()
    if sys.platform == "darwin":
        return f"/private/var/tmp/_bazel_{user}"
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "bazel", f"_bazel_{user}")


BASES = bazel_root() if BAZEL else ""


def gb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split()
    return int(out[0]) / 1048576 if out and out[0].isdigit() else 0.0


def git(worktree, *args):
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True).stdout.strip()


def output_base(worktree):
    if not BAZEL:
        return None
    link = os.path.join(worktree, "bazel-out")
    if not os.path.islink(link):
        return None
    target = os.path.realpath(link)
    return target.split("/execroot")[0] if "/_bazel_" in target else None


def workspace_of(base):
    path = os.path.join(base, "server", "cmdline")
    if not os.path.exists(path):
        return ""
    parts = open(path, "rb").read().split(b"\x00")
    return next((p.decode("utf-8", "replace").split("=", 1)[1] for p in parts if b"workspace_directory" in p), "")


def folders():
    """Every folder whose caches kendle may trim: the features, the Ask desk and the review folders."""
    from kendle import stack as _stack
    return _stack.workspaces()


def live_features():
    """Folders doing something right now: a role, a question or a review mid-turn, or their services
    up. A session that merely exists is not activity - it can sit for days and its caches are still
    reclaimable."""
    from kendle import stack as _stack
    snap = core.snapshot()
    busy = set(_stack.running_stacks())
    for f in core.tree():
        for m in f["managers"]:
            if not core.is_live(m, snap):
                continue
            if m["state"] == "working" or any(s["state"] == "working" for s in m["subs"] + m["internal"]):
                busy.add(f["name"])
    if any(q["state"] == "working" for q in core.questions()):
        busy.add(_stack.desk_name())
    busy |= {r["feature"] for r in core.reviews() if r["state"] == "reading"}
    return busy


def last_touch(worktree):
    """When someone last actually worked on it: the newest of its last commit and its sessions'
    last real message. File timestamps are useless here - an idle session keeps touching its own
    transcript, which once made every feature look busy."""
    from kendle import stack as _stack
    when = 0
    stamp = git(worktree, "log", "-1", "--format=%ct")
    if stamp.isdigit():
        when = int(stamp)
    newest, _ = _stack.last_activity(os.path.basename(worktree))
    return max(when, int(newest or 0))


def is_merged(worktree):
    return subprocess.run(["git", "-C", worktree, "merge-base", "--is-ancestor", "HEAD", core.UPSTREAM],
                          capture_output=True).returncode == 0


def ours(workspace):
    """Only this workspace's own caches are ever deleted. The bazel root is shared with every other
    checkout on the machine - another workspace's console is in charge of its own."""
    if not workspace:
        return False
    hub = os.path.realpath(core.HUB) + os.sep
    return (os.path.realpath(workspace) + os.sep).startswith(hub)


def orphan_bases():
    out = []
    for name in sorted(os.listdir(BASES)) if BASES and os.path.isdir(BASES) else []:
        base = os.path.join(BASES, name)
        if not os.path.isdir(base) or name in ("cache", "install"):
            continue
        ws = workspace_of(base)
        if ws and ours(ws) and not os.path.isdir(ws):
            out.append((base, ws))
    return out


def budget(new=None):
    if new is not None:
        core.ui_set("hub_budget_gb", int(new))
    return int(core.ui_get("hub_budget_gb") or DISK["budget_gb"])


def footprint():
    """Everything the workspace occupies: each feature's, the desk's and each review folder's folder
    and bazel base, the repo, the docs, the shared bazel disk cache."""
    total = 0.0
    for f in folders():
        total += gb(f["path"])
        base = output_base(f["path"])
        if base and os.path.isdir(base):
            total += gb(base)
    total += gb(core.PRIMARY) + gb(core.DOCS)
    shared = os.path.join(BASES, "cache") if BASES else ""
    if shared and os.path.isdir(shared):
        total += gb(shared)
    return total


def enforce(dry=False, quiet_hours=8):
    """Over budget: trim the quietest folders' regenerable caches until it fits. A folder working
    right now, or touched in the last few hours, is never trimmed - a rebuild costs the user time."""
    used, cap = footprint(), budget()
    if used <= cap:
        return 0.0, [f"  workspace {used:.0f}G of {cap}G budget - nothing to do"]
    running = live_features()
    rows = []
    for f in folders():
        if f["name"] in running:
            continue
        quiet = (time.time() - last_touch(f["path"])) / 3600
        if quiet < quiet_hours:
            continue
        base = output_base(f["path"])
        size = (gb(base) if base and os.path.isdir(base) else 0.0) + sum(
            gb(os.path.join(f["path"], c)) for c in CACHES if os.path.exists(os.path.join(f["path"], c)))
        if size > 0.5:
            rows.append((quiet, size, f["name"]))
    rows.sort(reverse=True)
    lines = [f"  workspace {used:.0f}G is over its {cap}G budget by {used - cap:.0f}G"]
    freed, picked = 0.0, []
    for quiet, size, name in rows:
        if used - freed <= cap:
            break
        picked.append(name)
        freed += size
        lines.append(f"    {size:5.1f}G  {name} (quiet {quiet / 24:.0f}d)" if quiet >= 24 else
                     f"    {size:5.1f}G  {name} (quiet {quiet:.0f}h)")
    if not picked:
        lines.append("  nothing quiet enough to trim - every feature, the desk and each review folder is in use; delete a finished feature instead")
        return 0.0, lines
    if dry:
        lines.append(f"  would free {freed:.1f}G (dry run)")
        return freed, lines
    freed, trimmed = trim(picked, "over the workspace budget")
    return freed, lines + trimmed


def report():
    used, cap = footprint(), budget()
    print(f"  workspace {used:.0f}G of its {cap}G budget   ·   free on disk {core.free_gb()} GB "
          f"(a new feature needs {core.RESERVE_GB} GB)")
    running = live_features()
    idle_gb = 0.0
    every = folders()
    for title, group in (("features", [f for f in every if f["kind"] == "feature"]),
                         ("desk and reviews", [f for f in every if f["kind"] != "feature"])):
        if not group and title != "features":
            continue
        print(f"\n  {title}")
        for f in group:
            w = f["path"]
            base = output_base(w)
            base_gb = gb(base) if base and os.path.isdir(base) else 0.0
            cache_gb = sum(gb(os.path.join(w, c)) for c in CACHES if os.path.exists(os.path.join(w, c)))
            days = (time.time() - last_touch(w)) / 86400
            state = "busy now" if f["name"] in running else (f"quiet {days*24:.0f}h" if days < 1 else f"quiet {days:.0f}d")
            dirty = [l for l in git(w, "status", "--porcelain").splitlines() if l.strip()]
            note = ("" if f["kind"] != "feature" else       # the desk and reviews are given back by kendle itself
                    " · merged and clean - the folder can go" if is_merged(w) and not dirty else
                    f" · {len(dirty)} uncommitted files" if dirty else "")
            print(f"    {f['name']:<32} {gb(w):5.1f}G folder  " + (f"{base_gb:5.1f}G bazel  " if BAZEL else "") +
                  f"{cache_gb:5.1f}G regenerable  {state}{note}")
            if f["name"] not in running and days >= 1:
                idle_gb += base_gb + cache_gb
    orphans_gb = 0.0
    rows = []
    for base, ws in orphan_bases():
        size = gb(base)
        orphans_gb += size
        rows.append(f"    {size:5.1f}G  from {os.path.basename(ws)} (worktree gone)")
    if rows:
        print("\n  orphan bazel caches - nothing can use these")
        print("\n".join(rows))
    shared = os.path.join(BASES, "cache") if BASES else ""
    if shared and os.path.isdir(shared):
        print(f"\n  shared bazel disk cache {gb(shared):.1f}G - every build reads it; prune only when desperate")
    print(f"\n  reclaimable: {orphans_gb:.1f}G orphans (kendle disk orphans) + "
          f"{idle_gb:.1f}G from folders quiet for a day or more (kendle disk idle 1)")


def trim(names, why=""):
    """Delete the named features' regenerable caches. Prints nothing: returns the GB freed and the
    report lines, so the console can run it without writing into its screen."""
    freed, lines = 0.0, []
    running = live_features()
    every = {f["name"]: f for f in folders()}
    for name in names:
        f = every.get(name)
        if not f:
            lines.append(f"  no feature, Ask desk or review folder '{name}'")
            continue
        if name in running:
            lines.append(f"  skipped {name}: it is working right now or its services are up")
            continue
        w = f["path"]
        dirty = [l for l in git(w, "status", "--porcelain").splitlines() if l.strip()]
        lines.append(f"  {name}{' - ' + why if why else ''}" + (f"  ({len(dirty)} uncommitted files kept)" if dirty else ""))
        base = output_base(w)
        if base and os.path.isdir(base):
            size = gb(base)
            subprocess.run(["bazel", "shutdown"], cwd=w, capture_output=True)
            shutil.rmtree(base, ignore_errors=True)
            freed += size
            lines.append(f"    {size:5.1f}G  bazel output base")
        for c in CACHES:
            p = os.path.join(w, c)
            if os.path.exists(p):
                size = gb(p)
                shutil.rmtree(p, ignore_errors=True)
                freed += size
                lines.append(f"    {size:5.1f}G  {c}")
    lines.append(f"  freed {freed:.1f} GB · free now {core.free_gb()} GB")
    return freed, lines


def orphans():
    freed, lines = 0.0, []
    for base, ws in orphan_bases():
        size = gb(base)
        pid_file = os.path.join(base, "server", "server.pid.txt")
        pid = open(pid_file).read().strip() if os.path.exists(pid_file) else ""
        if pid.isdigit():
            subprocess.run(["kill", pid], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
        freed += size
        lines.append(f"  {size:5.1f}G  orphan of {os.path.basename(ws)}")
    lines.append(f"  freed {freed:.1f} GB · free now {core.free_gb()} GB" if freed else "  no orphan caches")
    return freed, lines


def idle(days=3):
    running = live_features()
    names = [f["name"] for f in folders()
             if f["name"] not in running and (time.time() - last_touch(f["path"])) / 86400 >= days]
    if not names:
        return 0.0, [f"  nothing has been idle for {days} days"]
    return trim(names, f"idle {days}+ days")


def print_lines(result):
    for line in result[1]:
        print(line)


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if not argv:
        report()
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "trim" and rest:
        print_lines(trim(rest))
    elif cmd == "orphans":
        print_lines(orphans())
    elif cmd == "idle":
        print_lines(idle(int(rest[0]) if rest and rest[0].isdigit() else 3))
    elif cmd == "budget":
        print(f"  workspace budget: {budget(rest[0]) if rest and rest[0].isdigit() else budget()} GB "
              f"· using {footprint():.0f} GB")
    elif cmd == "enforce":
        print_lines(enforce(dry="--dry" in rest))
    elif cmd == "merged":
        for f in core.features():
            if is_merged(f["path"]):
                print(f"  {f['name']:<32} {gb(f['path']):5.1f}G  its commit is on {core.UPSTREAM}")
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
