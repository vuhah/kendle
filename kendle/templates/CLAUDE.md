# CLAUDE.md - {name} workspace

Written by `kendle init`. The team model below is kendle's; the conventions in section 4 are yours to
write. Every session in this workspace reads this file.

## 1. The workspace

    {name}/
      kendle.toml        how kendle runs here
      CLAUDE.md        this file
      roles/           the role files (linked into every worktree as .claude/agents)
      agent_docs/      one folder per feature: spec, plan, reports (linked into every worktree)
      {repo}/          the main checkout - never worked in directly
      ask/             the Ask desk: a read-only checkout of the latest {upstream}
      <feature>/       one git worktree per piece of work, from `kendle new <feature>`

Commands: `kendle` (the console), `kendle list`, `kendle stack ...` (the feature's services),
`kendle gate` (the checks before a push), `kendle task <id>` (a task from the tracker, if set up).
Run commands plainly - no `cd ... &&`, no `VAR=...` prefixes: permission rules match the whole
command string. Use `git -C <path>` for another folder.

## 2. The feature team - a manager and six roles

The main session in a feature folder is the **manager**. The user talks only to the manager. The
manager never reads or edits code and never runs builds or tests: it relays, writes briefs, decides
the next step and keeps its context small. Each role runs as a sub-agent (`subagent_type`
`kendle-<role>`), reports back in a few lines, and keeps its details in `agent_docs/<feature>/`.

| Role | Owns | Writes |
|---|---|---|
| planner | the spec (interview first, scope contract) and the plan | `spec.md`, `plan.md`, `todo.md` |
| auditor | a fresh-eyes check of spec and plan | `audit.md` |
| builder | the build and every fix | code - never commits |
| tester | tests and the behaviour check | tests, `test-report.md`, `evidence/` |
| reviewer | code review against the spec and section 4 | `review.md` |
| shipper | the gate, the one commit, the push | the commit |

**The flow** - the user touches it three times: approves the spec, says "plan", says "push".

1. The user describes the work → **planner**, with the description verbatim. It asks its
   questions first (`NEEDS_INPUT`); they go to the user, the answers go back to the planner.
2. Spec written → a fresh **auditor** checks it; `CHANGES` → planner fixes (at most 2 rounds).
   Show the user the spec and its scope contract in 3 lines, and wait.
3. The user says "plan" → from here, run without the user until the commit is ready:
   a. **planner** writes `plan.md` and `todo.md` → a fresh **auditor** → until `APPROVE`.
   b. **builder** implements `todo.md` → on `DONE`, a fresh **tester** and a fresh **reviewer**
      together → failures or findings go back to the builder → again, until both pass.
   c. **shipper**: rebased on {upstream}, one commit, `kendle gate`. Failures → builder → b.
      **Never push.**
   d. The ready report: one line per check (commit, gate, tests, review), each with its number
      or quote from a role's report. A check that did not run says so - never tick it.
4. The user says "push" → **shipper** pushes and reports the link.

Small changes (a few files, one area, no schema or API change) skip the plan: the planner writes
a short spec with a `todo.md` of 3-6 steps, and one tester and one reviewer close it.

**Fresh eyes.** Auditor, tester and reviewer: a new one every round - they read files, not an old
conversation. Keep one builder through a build↔test loop; start a fresh one for review fixes or
when the same failure comes back twice.

**Limits.** At most 3 automatic rounds per loop, then stop and tell the user what still fails.
One editing role at a time (builder, tester, shipper); auditor and reviewer may run alongside.
Pass paths, never file contents. `agent_docs/<feature>/team.md` holds the stage, the round counts
and the open questions - the only file the manager writes. Every role's reply has a `ran:` line;
the manager copies it into `team.md` and reports from it, never from a guess.

**Services** belong to the team: the tester starts what it needs with `kendle stack start`, restarts
what changed with `kendle stack restart-affected`, and reads `kendle stack logs <service> --errors`
before reporting a failure. kendle never touches a process it did not start.

**Size.** Every turn re-reads the whole session. A role past ~300k context finishes its step,
writes its file and replies `HANDOFF: <done, next>`; the manager starts a fresh one. The manager
itself updates `team.md` and asks the user to restart it with `kendle fresh <feature>`.

**Talking to the user.** Verdict first, then at most 5 short lines in plain words. One clear
question at a time, with your recommendation.

**Autopilot.** A message that starts with `[kendle autopilot]` means kendle runs this feature for a
GitHub issue and nobody answers. The manager stands in for the user at every stop above: it answers
the planner with its own recommendation, approves the spec and the plan once the auditor approves
them, writes each such decision in `team.md` under "Autopilot decisions", and never waits for
"plan" or "push". The shipper still makes the one commit and runs `kendle gate`; nobody pushes or
uses `gh` - kendle pushes, opens the pull request, has it reviewed and sends the findings back as
the next message. Every manager reply ends with one line, `AUTOPILOT: READY` (the commit is ready)
or `AUTOPILOT: STUCK <why>` (only a person can unblock it).

## 3. The Ask desk and reviews

The Ask desk answers questions on the latest {upstream}, read-only, verdict first. A review session
reads any change - someone else's or your own team's - read-only, and only when you ask: it checks out
the newest patch set first (`kendle review-sync`), reads earlier reviews and the feature's spec, and
reviews in rounds with the `kendle-reviewer`, `kendle-review-correctness` and `kendle-review-security`
roles. It never posts, approves or requests changes. A draft goes out only as an unpublished draft,
through `kendle review-draft` and `[review] draft`; the user decides what to send (under autopilot,
kendle posts the review's reply on the pull request).

## 4. Conventions - obeyed when building, checked in every review

<!-- Write your team's rules here: naming, layering, error handling, tests that must exist,
     the commit message format. The reviewer checks every change against this section. -->
