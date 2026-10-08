# kendle autopilot - issues to merged pull requests, unattended

Status: design, 2026-10-08. Built as `kendle/autopilot.py`, `kendle/github.py`, `kendle/cmd/autopilot.py`.

## What it is for

You file an issue and label it; a feature team builds it, a reviewer reviews the pull request, the
team answers the review, and kendle merges once the review approves and CI is green - nobody at the
keyboard. You watch it in the console and step in only when it asks (a round limit, a permission the
team was not given, a stuck team).

## Who may start work

Only an open issue opened by one of `[autopilot] authors` **and** carrying `[autopilot] label`.
Labels can be added only by collaborators, so nobody else can set a team to work on a public repo.
Comments by anyone outside `authors` are never read.

## Who does what

kendle does every step with an effect outside a worktree: GitHub (labels, comments, the pull
request, the merge), `git push`, the gate, rebasing, the round count. The agents only think:

- the **team** (the feature's manager and its roles) edits, tests and commits inside its worktree.
  It runs with edits accepted and the shell commands in `[autopilot] allow`; `git push` and `gh`
  are always denied. It never needs a human: in autopilot mode the manager stands in for the user
  at the spec, the plan and the push (kendle pushes).
- the **reviewer** is a normal read-only kendle review of the pull request. Opening it does not
  review: kendle asks it each round. Its reply - verdict line first: Approve, Comments or Blocked -
  is posted on the pull request by kendle.

kendle never starts a session with permission checks switched off.

## The loop

One `tick()` moves every issue at most one step; `kendle autopilot run` repeats it every
`interval` seconds. Each issue is a record in `<state>/autopilot.json` with a phase:

| Phase | Waiting for | Then |
|---|---|---|
| building | the manager's turn to end with `AUTOPILOT: READY` or `AUTOPILOT: STUCK <why>` | READY → checking; STUCK → stuck. A turn that ends with neither gets a nudge ("no human is here - decide with your recommendation"), at most `nudges` times |
| checking | - (done within the tick) | commits ahead of the base and a clean tree; rebase onto the base if behind (a conflict goes back to the team); `kendle gate`; push; open the pull request (`Closes #<issue>`) or update it → reviewing. A failure goes back to the team as a fix, at most `fixes` times |
| reviewing | the review session's reply to its latest message | posted as "kendle review - round k of `rounds`". Approve → merging; Comments/Blocked → the findings go to the manager → building, until round `rounds`, then stuck |
| merging | the pull request's checks | all passed → merge (`merge` method, branch deleted), tell both sessions it is merged, stop them, close the review, remove the worktree → done. A failed check goes back to the team as a fix |
| stuck | you | labelled `needs-human` with a comment saying why, and you are notified. `kendle autopilot resume <issue>` puts it back to building |

Messages reach a live session by pasting into its pane (one message, however many lines - tested
2026-10-08), so a manager's in-process teammates keep running. A stopped session is resumed with
the message. A manager is never messaged while one of its teammates is still working.

A new issue becomes the feature `issue-<n>`, its title and body saved as
`<docs>/issue-<n>/requirement.md`; at most `max_active` issues are worked at once, oldest first.
`kendle autopilot adopt <issue> <feature>` hands an existing feature (and its manager) to the loop.

## Restarts

The record file and GitHub are the truth. After a restart, a record whose session died gets it
resumed; one whose pull request was merged or closed by hand is finished; an issue labelled
`<label>:working` with no record is left alone (another kendle, or a person, has it).

## Asking you

You are notified (macOS notification or notify-send, the console's message line, and
`<state>/autopilot.log`) when an issue is stuck, merged, or waits at Claude Code's folder-trust
question or a permission prompt. Claude Code asks the trust question once per folder; trusting the
workspace folder covers every feature and review folder inside it (tested 2026-10-08).

## Settings

```toml
[autopilot]
enabled = false        # kendle autopilot run refuses unless true
label = "autopilot"    # work starts on open issues with this label ...
authors = []           # ... opened by one of these GitHub accounts (required)
max_active = 2         # issues worked at once
rounds = 3             # review rounds before it stops and asks you
fixes = 3              # gate, rebase or CI failures sent back to the team, per issue
nudges = 5             # turns that ended without READY or STUCK, per issue
interval = 120         # seconds between ticks
merge = "squash"       # squash, merge or rebase
allow = []             # shell commands the team may run unasked, as Claude Code rules: "Bash(npm test:*)"
```

## Out of scope

Gerrit and GitLab (GitHub only, through `gh`); approving as a second GitHub account; several
workspaces sharing one repository's issues.
