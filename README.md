# kendle

[![tests](https://github.com/vuhah/kendle/actions/workflows/tests.yml/badge.svg)](https://github.com/vuhah/kendle/actions/workflows/tests.yml)

*A kendle is a litter of kittens - here, a small team of agents that work together.*

One terminal screen for running many Claude Code sessions over one git repository: a sidebar of
panels on the left, the session you picked on the right, and the services' logs beside it.

    ┌ panels ──────────┬ the session you picked ─┬ service logs ─┐
    │ ASK    2         │                         │ ● api         │
    │ REVIEW 1 of 3    │  …                      │ ● web         │
    │ ACTIVE 3         │                         │ ✗ worker      │
    │ IDLE   5         │                         │               │
    └──────────────────┴─────────────────────────┴───────────────┘

- **Active / Idle** — one git worktree per piece of work, each run by a manager you talk to and a
  small team of sub-agents: planner, auditor, builder, tester, reviewer, shipper.
- **Ask** — read-only questions on the latest base branch, for things that aren't work yet; a good
  answer can be promoted to a feature with the whole conversation as its requirement.
- **Review** — up to three of someone else's changes checked out side by side, read-only
  (Gerrit, GitHub or GitLab).
- **Logs** — the selected feature's services, live, with crash detection.
- **Housekeeping** — context and memory per session, idle services stopped, a disk budget that
  trims build caches but never code or uncommitted work, and everything resumable after a reboot.

Status: young. It ran for a month inside one team's monorepo and has just been lifted out of it.

## Install

You need macOS (Linux should work, but is untested), Python 3.9+, tmux 3.x, git, and
[Claude Code](https://claude.com/claude-code) (`claude` on your `PATH`). Nothing else - kendle is
Python's standard library and a tmux config.

    git clone https://github.com/vuhah/kendle.git ~/kendle
    export PATH="$HOME/kendle/bin:$PATH"          # in ~/.zshrc or ~/.bashrc

## A worked example

Say you work on `shop`, a web app on GitHub, with a frontend and an API.

**1. Make a workspace.** A workspace is a folder that holds your checkout, one folder per piece of
work, and the team's notes. `kendle init` sets one up:

    mkdir ~/work/shop-ws && cd ~/work/shop-ws
    kendle init --clone git@github.com:you/shop.git

      wrote    kendle.toml
      wrote    CLAUDE.md
      wrote    roles/kendle-auditor.md  … roles/kendle-tester.md
      made     ask/ - the Ask desk, on origin/main

It found the default branch itself. Already have a checkout? Move it in and run `kendle init shop`.

**2. Tell kendle how your app runs and what must pass before a push** - in `kendle.toml`:

    [services.web]
    run = "npm run dev -- --port {port}"
    cwd = "web"
    port = 3000
    watch = ["web/"]

    [services.api]
    run = "go run ./cmd/api --port {port}"
    port = 8080
    watch = ["api/", "go.mod"]

    [gate]
    max_commits = 1

    [gate.steps.test]
    run = "npm --prefix web test && go test ./..."

    [gate.steps.lint]
    run = "npm --prefix web run lint"
    when = ["web/"]
    lane = "web"

And write your team's conventions in section 4 of `CLAUDE.md` - the reviewer checks every change
against it.

**3. Open the console** with `kendle`. Then, without leaving it:

- `a` — *"why does checkout double-charge on retry?"* An Ask session answers on the latest `main`,
  verdict first, and suggests a feature name if it needs a change.
- `p` — promote that answer to the feature `retry-idempotency`: a worktree on a fresh branch, the
  conversation saved as `agent_docs/retry-idempotency/requirement.md`, and its manager started.
- Talk to the manager. It runs the team: the planner asks its questions and writes the spec; you
  approve it and say **plan**; builder, tester and reviewer go round until they pass; the shipper
  makes one commit and runs `kendle gate`. You get a ready report with evidence for every line, and
  nothing is pushed until you say **push**.
- `g` — meanwhile, review a colleague's pull request: paste `https://github.com/you/shop/pull/42`.
  Its findings are saved to `agent_docs/reviews/` when you close it.

Everything the console does is a command too, so a script or another agent can drive it:

    kendle ask "why does checkout double-charge on retry?"
    kendle new retry-idempotency && kendle manager retry-idempotency
    kendle stack start web api -f retry-idempotency    # the feature's own copy of the app
    kendle gate --start && kendle gate --wait            # inside the feature's folder
    kendle review https://github.com/you/shop/pull/42
    kendle list                                        # everything, with state

**4. Two features at once.** Start the stack of a second feature and it runs on shifted ports
(3100, 8180) on `127.0.0.1`, so both apps and their logins work side by side.

## Configuration

`kendle.toml` sits at the workspace root; only `[repo] path` is required.
[`profiles/example/kendle.toml`](profiles/example/kendle.toml) documents every setting:

| Section | What it sets |
|---|---|
| `[repo]` | the checkout, its remote, the base branch |
| `[workspace]` | the docs folder, the Ask desk, folders that are never features, links into each worktree |
| `[disk]` | the size budget and which folders are regenerable caches (and bazel output bases) |
| `[team]` | which tool servers and plugins kendle's sessions keep - each costs tokens every turn |
| `[services]` | each service as a plain command, an IntelliJ run configuration, or a docker compose service |
| `[gate]` | git checks (rebased, commits ahead, clean tree, stray files, message) and named steps |
| `[review]` | `gerrit`, `github` or `gitlab` - guessed from the remote when unset |
| `[task]` | a command that prints a task from your tracker: `kendle task PROJ-12` |
| `[prompts]` | the note each kind of session starts with, if you want your own |

## The console

    j k  or click     select             Enter       show it on the right
    Tab  or click     type into it       Ctrl-q      sidebar ⇄ session
    a  ask            p  promote          g  review someone else's change
    n  new feature    m  manager          s  read-only sub-agent
    K  stop session   r  refresh          q  close the console (sessions keep running)
    < >  sidebar width (or drag the border)

It runs on a tmux socket of its own per workspace (`kendle-<hash of the path>`), so it never touches
your other tmux sessions or another workspace's console.

## What it promises

Each of these came from something that went wrong once:

- **Keys never wait on data.** The sidebar gathers in a background thread and draws from a
  snapshot; transcripts are re-read only when they change. Keystrokes stay around 10 ms with over
  a hundred sessions.
- **Quiet is not dead.** A role blocked in one long tool call still counts as working; one its
  manager stopped does not.
- **Idle means nobody worked on it** - measured from the last real message, never from file times,
  because idle sessions keep touching their own transcripts.
- **Nothing is deleted on its own** except build caches whose worktree is gone. Sizes are shown
  first; code, branches and uncommitted work are never touched.
- **kendle only stops what it started.** A port held by anything else is reported, never killed.
- **Commands run plainly** - no `cd … &&`, no `VAR=…` prefixes - because Claude Code's permission
  rules match the whole command string.

## Tests

    python3 -m unittest discover -s tests

About 50 tests, half a minute. Each runs kendle the way a user does, against a throwaway git repo
with a scratch origin, on a tmux socket, state folder and transcript folder of its own, with a
stand-in for `claude` that records what it was asked to do - nothing touches your real sessions.

## License

MIT - see [LICENSE](LICENSE).
