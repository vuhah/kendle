"""kendle task - print a task from the team's tracker, with the command in kendle.toml.

  kendle task <id or url> [more words]   runs [task] fetch with {id} replaced, e.g.
                                         fetch = "jira issue view {id} --plain"

Sessions are told about it, so an agent can read the task a user mentions.
"""
import os, shlex, subprocess, sys
from kendle import config, core

args = sys.argv[1:]
if not args or args[0] in ("-h", "--help"):
    print(__doc__.strip()); sys.exit(0 if args else 2)
fetch = core.CONFIG["task"]["fetch"]
if not fetch:
    sys.exit("kendle task: no tracker set up - add to kendle.toml:\n  [task]\n  fetch = \"<command> {id}\"")
command = config.fill(fetch, id=" ".join(shlex.quote(a) for a in args), workspace=core.HUB)
sys.exit(subprocess.run(["/bin/sh", "-c", command], cwd=os.getcwd()).returncode)
