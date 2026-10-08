"""kendle - one terminal screen for many Claude Code sessions over a git repository.

  kendle init [<checkout>]     make this folder a workspace (kendle.toml, CLAUDE.md, roles, Ask desk)
  kendle                       open the console (starts it if needed; closing it leaves sessions running)
  kendle <session command>     list, new, manager, sub, show, stop, adopt, fresh, ask, promote, review,
                               reviews, review-sync, review-draft, review-close - see `kendle help agent`
  kendle stack ...             the feature's local services (start, stop, logs, ...)
  kendle services [name ...]   which local services are up (checks only)
  kendle disk ...              what the workspace uses, and how to get space back
  kendle gate ...              the pre-push gate: the checks in kendle.toml, run locally
  kendle task <id>             print a task from your tracker (kendle.toml [task] fetch)
  kendle autopilot ...         GitHub issues to merged pull requests, unattended ([autopilot])

`kendle help <command>` shows a command's own help. The workspace is KENDLE_WORKSPACE, else the
nearest folder at or above the current one that has a kendle.toml.
"""
import os, runpy, sys

# kendle <name> runs kendle/cmd/<module>.py as a script; the console's own panes use the hidden ones
TOOLS = {"stack": "stack", "services": "services", "disk": "disk", "gate": "gate", "task": "task", "init": "init",
         "autopilot": "autopilot"}
PANES = {"sidebar": "sidebar", "logs": "logs", "view": "view"}
SESSION_COMMANDS = {"list", "new", "manager", "sub", "show", "stop", "adopt", "fresh", "ask", "promote",
                    "review", "reviews", "review-sync", "review-draft", "review-close"}


def run(module, name, args):
    sys.argv = [f"kendle {name}", *args]
    runpy.run_module(f"kendle.cmd.{module}", run_name="__main__", alter_sys=True)
    return 0


def open_console():
    from kendle import core
    core.ensure_session()
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}    # attach from inside another tmux too
    os.execvpe("tmux", ["tmux", "-L", core.SOCKET, "attach", "-t", f"{core.SESSION}:console"], env)


def main(argv):
    if not argv or argv[0] == "open":
        return open_console()
    cmd, args = argv[0], argv[1:]
    if cmd in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if cmd == "help":
        if not args:
            print(__doc__.strip())
            return 0
        cmd, args = ("agent" if args[0] in SESSION_COMMANDS else args[0]), ["--help"]
    if cmd == "agent" or cmd in SESSION_COMMANDS:
        from kendle.cmd import agent
        return agent.main(args if cmd == "agent" else argv)
    module = TOOLS.get(cmd) or PANES.get(cmd)
    if module:
        return run(module, cmd, args)
    print(f"kendle: unknown command '{cmd}'\n\n{__doc__.strip()}", file=sys.stderr)
    return 2
