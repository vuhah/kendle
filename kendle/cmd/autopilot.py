"""kendle autopilot - GitHub issues to merged pull requests, unattended (docs/autopilot.md).

  kendle autopilot start                  run the loop in a console window of its own (`run` in a pane)
  kendle autopilot run                    run the loop here: a tick every [autopilot] interval seconds
  kendle autopilot once                   one tick
  kendle autopilot status                 every issue it has: phase, round, pull request
  kendle autopilot adopt <issue> <feature>   hand an existing feature and its manager to the loop
  kendle autopilot resume <issue>         a stuck issue back to its team (after you looked at it)
  kendle autopilot release <issue>        stop running an issue; its sessions stay as they are

It works on open issues labelled [autopilot] label and opened by one of [autopilot] authors. kendle
does everything outside the worktrees - GitHub, push, gate, rebase - and the team only edits and
commits; the reviewer only reads. Off unless kendle.toml says [autopilot] enabled = true.
"""
import sys, time
from kendle import core


def status():
    from kendle import autopilot
    recs = autopilot.load()
    if not recs:
        print("  autopilot has no issues yet")
    for r in recs:
        pr = f"PR #{r['pr']}" if r.get("pr") else "no PR yet"
        rnd = f"round {r.get('round', 0)}/{autopilot.AP['rounds']}" if r.get("round") else ""
        why = f"  - {r['why']}" if r.get("phase") == "stuck" and r.get("why") else ""
        print(f"  #{r['issue']:<5} {r['phase']:<10} {r['feature']:<26} {pr:<10} {rnd}{why}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    from kendle import autopilot, github
    try:
        if cmd in ("start", "run", "once"):
            why = autopilot.ready()
            if why:
                print("kendle autopilot:", why, file=sys.stderr)
                return 1
        if cmd == "start":
            core.ensure_session()
            if "autopilot" in core.tmux("list-windows", "-t", core.SESSION, "-F", "#{window_name}").split():
                print("autopilot is already running in the console's autopilot window")
                return 0
            core.new_pane(core.HUB, [sys.executable, core.KENDLE, "autopilot", "run"], "autopilot")
            print("autopilot started - its log: kendle autopilot status, or", autopilot.LOG)
        elif cmd == "run":
            autopilot.log(None, f"running: issues labelled {autopilot.AP['label']} by "
                                f"{', '.join(autopilot.AP['authors'])}, a tick every {autopilot.AP['interval']}s")
            while True:
                try:
                    autopilot.tick()
                except Exception as err:                 # one bad tick must never end the loop
                    autopilot.log(None, f"tick failed: {type(err).__name__}: {err}")
                time.sleep(autopilot.AP["interval"])
        elif cmd == "once":
            autopilot.tick()
            status()
        elif cmd == "status":
            status()
        elif cmd == "adopt" and len(args) == 2:
            r = autopilot.adopt(int(args[0].lstrip("#")), args[1])
            print(f"adopted {r['feature']} for issue #{r['issue']}")
        elif cmd == "resume" and len(args) == 1:
            r = autopilot.resume(int(args[0].lstrip("#")))
            print(f"issue #{r['issue']} is back with {r['feature']}")
        elif cmd == "release" and len(args) == 1:
            r = autopilot.release(int(args[0].lstrip("#")))
            print(f"released issue #{r['issue']}")
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (LookupError, ValueError, RuntimeError, github.GitHubError) as err:
        print("kendle autopilot:", err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
