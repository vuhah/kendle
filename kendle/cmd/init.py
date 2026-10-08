"""kendle init - make this folder a kendle workspace around a git checkout.

  kendle init                       use the one git checkout in this folder
  kendle init <folder>              use that checkout (a folder in this workspace)
  kendle init --clone <url> [dir]   clone it here first
  kendle init --roles               in a workspace: add the role files it is missing (a newer kendle's)
  options: --base <branch>  the branch features start from (default: the remote's default branch)
           --dir <path>     the workspace folder (default: the current one)

It writes kendle.toml, a starter CLAUDE.md (the team model; your conventions go in its section 4),
the role files in roles/, agent_docs/, and the Ask desk (ask/: a detached checkout of the
latest base). Nothing that already exists is overwritten, and the checkout itself is not changed
beyond fetching it and adding the Ask desk worktree. A workspace made by an older kendle gets the
roles added since with `kendle init --roles`; the role files it has are kept as they are.
"""
import os, subprocess, sys
from kendle import config

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "templates")


def git(*args, cwd=None, check=False):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if check and r.returncode:
        raise SystemExit(f"kendle init: git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def is_checkout(path):
    return os.path.isdir(path) and git("rev-parse", "--show-toplevel", cwd=path) == os.path.realpath(path)


def pick_repo(ws, given, clone, name):
    if clone:
        if not name:
            name = os.path.basename(clone.rstrip("/").split(":")[-1])
            name = name[:-4] if name.endswith(".git") else name
        path = os.path.join(ws, name)
        if os.path.exists(path):
            raise SystemExit(f"kendle init: {path} already exists")
        print(f"  cloning {clone} into {name}/ ...", flush=True)
        git("clone", "--quiet", clone, path, check=True)
        return name
    if given:
        path = os.path.realpath(os.path.join(ws, given))
        if not is_checkout(path):
            raise SystemExit(f"kendle init: {given} is not a git checkout")
        return os.path.relpath(path, ws)
    found = sorted(n for n in os.listdir(ws) if not n.startswith(".") and is_checkout(os.path.join(ws, n)))
    if len(found) != 1:
        raise SystemExit("kendle init: " + (f"several checkouts here ({', '.join(found)}) - name one: kendle init <folder>"
                                          if found else "no git checkout in this folder - kendle init <folder>, "
                                          "or kendle init --clone <url>"))
    return found[0]


def base_of(repo, remote):
    head = git("symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", cwd=repo)       # origin/main
    if head.startswith(remote + "/"):
        return head[len(remote) + 1:]
    for name in ("main", "master", "trunk", "develop"):
        if git("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{name}", cwd=repo):
            return name
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)


def write(path, text, made):
    if os.path.exists(path):
        print(f"  kept     {os.path.relpath(path)} (already there)")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    made.append(path)
    print(f"  wrote    {os.path.relpath(path)}")


def add_roles(ws):
    """Write the role files a workspace is missing - a kendle update can bring new ones - never touching
    one it has: the team may have edited them."""
    if not os.path.exists(os.path.join(ws, config.FILE)):
        raise SystemExit(f"kendle init --roles: {ws} is not a workspace (no {config.FILE}) - run kendle init first")
    made = []
    for role in sorted(os.listdir(os.path.join(TEMPLATES, "roles"))):
        path = os.path.join(ws, "roles", role)
        if not os.path.exists(path):
            write(path, open(os.path.join(TEMPLATES, "roles", role)).read(), made)
    print(f"added {len(made)} role file{'' if len(made) == 1 else 's'}" if made else "every role file is already there")
    return 0


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if "--roles" in argv:
        rest = [a for a in argv if a != "--roles"]
        if rest[:1] == ["--dir"] and len(rest) == 2:
            return add_roles(os.path.realpath(rest[1]))
        if rest:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        return add_roles(os.path.realpath(os.getcwd()))
    opts, rest = {}, []
    i = 0
    while i < len(argv):
        if argv[i] in ("--base", "--dir", "--clone") and i + 1 < len(argv):
            opts[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    if len(rest) > 1 or (opts.get("clone") and len(rest) > 1):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    ws = os.path.realpath(opts.get("dir") or os.getcwd())
    os.makedirs(ws, exist_ok=True)
    if os.path.exists(os.path.join(ws, config.FILE)):
        raise SystemExit(f"kendle init: {ws} is already a workspace ({config.FILE} exists)")
    os.chdir(ws)
    repo = pick_repo(ws, None if opts.get("clone") else (rest[0] if rest else None), opts.get("clone"),
                     rest[0] if opts.get("clone") and rest else None)
    repo_path = os.path.join(ws, repo)
    remotes = git("remote", cwd=repo_path).split()
    if not remotes:
        raise SystemExit(f"kendle init: {repo} has no remote - features start from <remote>/<base>")
    remote = "origin" if "origin" in remotes else remotes[0]
    print(f"  fetching {remote} ...", flush=True)
    git("fetch", "--quiet", remote, cwd=repo_path, check=True)
    base = opts.get("base") or base_of(repo_path, remote)
    if not git("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{base}", cwd=repo_path):
        raise SystemExit(f"kendle init: {remote}/{base} does not exist - pass --base <branch>")
    name = os.path.basename(ws)
    made = []
    toml = (f'# kendle.toml - written by kendle init. Every setting: kendle\'s profiles/example/kendle.toml\n\n'
            f'[repo]\npath = "{repo}"\nremote = "{remote}"\nbase = "{base}"\n\n'
            f'[workspace]\nname = "{name}"\n\n'
            f'# [services.web]          # what `kendle stack start web` runs in a feature worktree\n'
            f'# run = "npm run dev -- --port {{port}}"\n# port = 3000\n\n'
            f'# [gate.steps.test]       # what `kendle gate` runs before a push\n# run = "npm test"\n')
    write(os.path.join(ws, config.FILE), toml, made)
    fields = {"name": name, "repo": repo, "upstream": f"{remote}/{base}"}
    write(os.path.join(ws, "CLAUDE.md"), config.fill(open(os.path.join(TEMPLATES, "CLAUDE.md")).read(), **fields), made)
    for role in sorted(os.listdir(os.path.join(TEMPLATES, "roles"))):
        write(os.path.join(ws, "roles", role), open(os.path.join(TEMPLATES, "roles", role)).read(), made)
    os.makedirs(os.path.join(ws, "agent_docs"), exist_ok=True)
    ask = os.path.join(ws, "ask")
    if os.path.exists(ask):
        print("  kept     ask/ (already there)")
    else:
        git("worktree", "add", "--quiet", "--detach", ask, f"{remote}/{base}", cwd=repo_path, check=True)
        print(f"  made     ask/ - the Ask desk, on {remote}/{base}")
    print(f"\n{name} is a kendle workspace: {repo}/ on {remote}/{base}.\n"
          f"  next: write your conventions in CLAUDE.md section 4, your services and gate steps in kendle.toml,\n"
          f"        then `kendle new <feature>` and `kendle manager <feature>`, or just `kendle` for the console.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
