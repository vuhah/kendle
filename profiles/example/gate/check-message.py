#!/usr/bin/env python3
"""An example [gate] message check: "<Area> - <what changed>", a short title, a body for big changes.

  [gate]
  message = "python3 {workspace}/gate/check-message.py {file} {source}"

{source} is "commit" for the commit being shipped and "file" for `kendle gate --message <file>`.
Exit 0 passes; whatever it prints is shown when it fails.
"""
import re, subprocess, sys

path, source = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "file"
lines = open(path).read().rstrip("\n").splitlines()
title = lines[0] if lines else ""
problems = []
if not re.match(r"^\S.{1,30}? - \S", title):
    problems.append('title must read "<Area> - <what changed>"')
if len(title) > 72:
    problems.append(f"title is {len(title)} characters (max 72)")
if source == "commit":
    changed = subprocess.run(["git", "diff", "--shortstat", "HEAD^", "HEAD"], capture_output=True, text=True).stdout
    insertions = int((re.search(r"(\d+) insertion", changed) or [0, 0])[1])
    if insertions > 200 and len([l for l in lines[1:] if l.strip()]) < 2:
        print(f"::warn:: message body: {insertions} lines added - say why in the body")
print("\n".join(problems))
sys.exit(1 if problems else 0)
