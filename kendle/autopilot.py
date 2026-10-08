"""kendle autopilot: GitHub issues to merged pull requests, with nobody at the keyboard (docs/autopilot.md).

One tick() moves every issue at most one step. Each issue is a record in <state>/autopilot.json:

  building   the team works; its manager ends a turn with AUTOPILOT: READY or AUTOPILOT: STUCK <why>
  reviewing  the pushed pull request is with the review session; its verdict decides the next step
  merging    approved: waiting for the pull request's checks, then merged
  closing    merged: both sessions are told, then stopped, and the worktree is removed
  done / stuck / released

kendle does everything with an effect outside a worktree (GitHub, push, gate, rebase, round counts);
the agents only edit and commit (the team) or read and judge (the reviewer).
"""
import json, os, platform, re, shutil, subprocess, sys, time
from kendle import core, github

AP = core.CONFIG["autopilot"]
RECORDS = os.path.join(core.STATE, "autopilot.json")
LOG = os.path.join(core.STATE, "autopilot.log")
WORKING = AP["label"] + ":working"
NEEDS_HUMAN = "needs-human"
ACTIVE = ("building", "reviewing", "merging", "closing")
TAG = "[kendle autopilot]"
MARKER = re.compile(r"^\W*AUTOPILOT:\s*(READY|STUCK)\b[\s:-]*(.*)$", re.I | re.M)
VERDICT = re.compile(r"^\W*(?:verdict\W*)?(Approve|Comments|Blocked)\b", re.I)
CLOSE_AFTER = 180                                    # seconds the sessions get to answer "we're done"
NO_CHECKS_AFTER = 1800                               # a pull request with no checks after this is stuck

# the team edits and commits in its worktree unasked; these and [autopilot] allow are the shell
# commands it may run without a human. Pushing and GitHub are kendle's, never the team's.
BASE_ALLOW = ["Bash(kendle:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
              "Bash(git add:*)", "Bash(git commit:*)", "Bash(git rebase:*)", "Bash(git restore:*)",
              "Bash(git stash:*)", "Bash(git reset:*)", "Bash(git fetch:*)", "Bash(git rev-parse:*)",
              "Bash(git branch:*)", "Bash(git checkout:*)", "Bash(git merge-base:*)", "Bash(ls:*)",
              "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)", "Bash(rg:*)",
              "Bash(find:*)", "Bash(mkdir:*)", "Bash(cd:*)", "Bash(pwd)"]
DENY = ["Bash(git push:*)", "Bash(gh:*)"]


def team_flags():
    return ["--permission-mode", "acceptEdits", "--allowedTools", *BASE_ALLOW, *AP["allow"],
            "--disallowedTools", *DENY]


# ---- records, log, notifications ----

def load():
    try:
        with open(RECORDS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(recs):
    os.makedirs(core.STATE, exist_ok=True)
    with open(RECORDS + ".tmp", "w") as f:
        json.dump(recs, f, indent=2)
        f.write("\n")
    os.replace(RECORDS + ".tmp", RECORDS)


def log(rec, text):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} #{rec['issue'] if rec else '-'} {text}"
    os.makedirs(core.STATE, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    if rec is not None:
        rec.setdefault("history", []).append(line[11:])
    print(line, flush=True)


def notify(rec, text):
    """Tell the person: the log, the console's message line, and the desktop."""
    log(rec, "NOTIFY " + text)
    with open(os.path.join(core.STATE, "autopilot.msg"), "w") as f:
        f.write(f"#{rec['issue']}: {text}" if rec else text)
    if os.environ.get("KENDLE_NO_NOTIFY"):
        return
    title = f"kendle autopilot - #{rec['issue']}" if rec else "kendle autopilot"
    if platform.system() == "Darwin" and shutil.which("osascript"):
        script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, text], capture_output=True)


def notify_once(rec, key, text):
    if key not in rec.setdefault("told", []):
        rec["told"].append(key)
        notify(rec, text)


# ---- sessions ----

def manager_of(rec):
    rows = [e for e in core.load() if e["kind"] == "manager" and e["feature"] == rec["feature"]]
    return rows[-1] if rows else None


def review_of(rec):
    return next((e for e in core.load() if e["id"] == rec.get("review")), None)


def busy_team(m):
    return any(s["state"] == "working" for s in core.internal_subs(m, True))


def tell_manager(rec, text):
    """The manager's next message: typed into its pane if it runs, else it is resumed with it (and
    the team's permissions)."""
    m = manager_of(rec)
    if m and core.is_live(m, core.snapshot()):
        core.type_into(m, text)
    else:
        core.start_manager(rec["feature"], team_flags(), prompt=text)
    rec["sent"] = time.time()


def tell_review(rec, text):
    e = review_of(rec)
    if e and core.is_live(e, core.snapshot()):
        core.type_into(e, text)
        rec["review_sent"] = time.time()
        return True
    return False


def held(rec, e, key):
    """Tell the person, once, when a session waits at a question only a person can answer: Claude
    Code's folder trust (before any chat) or a permission prompt (in the middle of a turn)."""
    t = core.transcript(e["id"], e.get("cwd"))
    turn = core.turn_state(t) if t else "new"
    seen = core.screen(e)
    if (seen == "trust" and turn == "new") or (seen == "permission" and turn == "working"):
        notify_once(rec, f"{key}-{seen}-{rec.get('review_sent' if key == 'review' else 'sent')}",
                    f"the {key} waits at a {seen} question in its pane ({rec['feature']}) - only you can answer it")


def answered(e, since):
    """The session's reply to the message sent at `since`, once its turn is over; else None."""
    t = core.transcript(e["id"], e.get("cwd")) if e else None
    if not t or not core.is_live(e, core.snapshot()) or core.turn_state(t, since=int(since)) != "waiting":
        return None
    return core.reply_since(e, since)


# ---- messages ----

def kickoff(rec, issue, new):
    body = (issue.get("body") or "").strip() or "(no description)"
    where = (f"The issue is saved in {core.CONFIG['workspace']['docs']}/{rec['feature']}/requirement.md - "
             "start the planner with it.\n\n" if new else "")
    return (f"{TAG} From now on kendle autopilot runs this feature for GitHub issue #{rec['issue']}, and "
            "nobody answers you. Stand in for the user: answer the planner's questions with your "
            "recommendation, approve the spec and the plan once the auditor approves them, and write each "
            "decision you took for the user in team.md under 'Autopilot decisions'. Run the flow to the ready "
            f"commit: the shipper rebases on {core.UPSTREAM}, makes the one commit and runs kendle gate. Never "
            "push and never use gh - kendle pushes, opens the pull request and has it reviewed. Whenever you "
            "stop, for any reason, end your reply with one line: `AUTOPILOT: READY` when the commit is "
            "ready, or `AUTOPILOT: STUCK <why>` if only a human can unblock you.\n\n" + where +
            f"Issue #{rec['issue']}: {issue['title']}\n\n{body}")


NUDGE = (f"{TAG} Nobody is here to answer. Decide with your own recommendation, write the decision in "
         "team.md, and carry on. End your reply with `AUTOPILOT: READY` when the commit is ready, or "
         "`AUTOPILOT: STUCK <why>` if only a human can unblock you.")


def fix(rec, what):
    """Send a failure back to the team, or stop when it has had its fixes."""
    rec["fixes"] = rec.get("fixes", 0) + 1
    if rec["fixes"] > AP["fixes"]:
        return stuck(rec, f"still failing after {AP['fixes']} fixes: {what.splitlines()[0][:200]}")
    log(rec, f"fix {rec['fixes']}/{AP['fixes']}: {what.splitlines()[0][:120]}")
    tell_manager(rec, f"{TAG} {what}\n\nFix it, keep it to the one commit (as the shipper does), run kendle "
                      "gate, and end your reply with `AUTOPILOT: READY`.")
    rec["phase"] = "building"


def stuck(rec, why):
    rec["phase"], rec["why"] = "stuck", why
    target = rec.get("pr") or rec["issue"]
    try:
        github.add_label(target, NEEDS_HUMAN)
        github.comment(target, f"kendle autopilot stopped and needs a person: {why}\n\n"
                               f"The team ({rec['feature']}) and the review stay open in the kendle console; "
                               f"`kendle autopilot resume {rec['issue']}` hands it back.")
    except github.GitHubError as err:
        log(rec, f"could not mark it on GitHub: {err}")
    notify(rec, f"stuck - {why}")


# ---- picking up issues ----

def start(issue):
    n = issue["number"]
    rec = {"issue": n, "title": issue["title"], "feature": f"issue-{n}", "phase": "building", "round": 0,
           "fixes": 0, "nudges": 0, "pr": None, "review": None, "started": time.time()}
    try:
        f = core.feature(rec["feature"])           # a start that failed half-way: carry on with it
    except LookupError:
        f = core.new_feature(rec["feature"])
    rec["branch"] = f["branch"]
    github.add_label(n, WORKING)
    github.remove_label(n, AP["label"])
    req = os.path.join(core.DOCS, rec["feature"], "requirement.md")
    with open(req, "w") as fh:
        fh.write(f"# Issue #{n}: {issue['title']}\n\n{(issue.get('body') or '').strip()}\n")
    core.start_manager(rec["feature"], team_flags(), prompt=kickoff(rec, issue, new=True))
    rec["sent"] = time.time()
    github.comment(n, f"kendle autopilot is on it: feature `{rec['feature']}`. Its pull request will "
                      "close this issue.")
    log(rec, f"started {rec['feature']}: {issue['title']}")
    return rec


def adopt(number, feature):
    """Hand an existing feature (and its manager) to autopilot for an issue."""
    recs = load()
    if any(r["issue"] == number and r["phase"] in ACTIVE + ("stuck",) for r in recs):
        raise ValueError(f"autopilot already has issue #{number}")
    issue = github.issue(number)
    if issue["user"]["login"] not in AP["authors"]:
        raise ValueError(f"issue #{number} is by {issue['user']['login']}, not one of [autopilot] authors")
    f = core.feature(feature)
    rec = {"issue": number, "title": issue["title"], "feature": feature, "branch": f["branch"],
           "phase": "building", "round": 0, "fixes": 0, "nudges": 0, "pr": None, "review": None,
           "started": time.time()}
    m = manager_of(rec)
    if m and core.is_live(m, core.snapshot()):
        t = core.transcript(m["id"], m.get("cwd"))
        if busy_team(m) or (t and core.turn_state(t) == "working"):
            raise RuntimeError(f"{feature}'s team is working - adopt it when its manager waits")
        core.stop(m["id"])                          # resumed below with the team's permissions
    pull = github.pull_for(f["branch"])
    if pull and pull["state"] == "open":
        rec["pr"] = pull["number"]
    github.add_label(number, WORKING)
    github.remove_label(number, AP["label"])
    core.start_manager(feature, team_flags(), prompt=kickoff(rec, issue, new=False))
    rec["sent"] = time.time()
    log(rec, f"adopted {feature}")
    save(recs + [rec])
    return rec


# ---- the steps ----

def step_building(rec):
    m = manager_of(rec)
    if m is None or not core.is_live(m, core.snapshot()):
        log(rec, "the manager was not running - resumed")
        return tell_manager(rec, f"{TAG} You were stopped (a restart). Carry on from team.md; end with "
                                 "`AUTOPILOT: READY` or `AUTOPILOT: STUCK <why>`.")
    if busy_team(m):
        return
    text = answered(m, rec["sent"])
    if text is None:
        return held(rec, m, "manager")
    marks = MARKER.findall(text)
    if not marks:
        rec["nudges"] = rec.get("nudges", 0) + 1
        if rec["nudges"] > AP["nudges"]:
            return stuck(rec, f"the team ended {AP['nudges']} turns without READY or STUCK")
        log(rec, f"nudge {rec['nudges']}/{AP['nudges']}")
        return tell_manager(rec, NUDGE)
    kind, why = marks[-1]
    if kind.upper() == "STUCK":
        return stuck(rec, f"the team: {why.strip() or 'no reason given'}")
    rec["ready"] = text
    check(rec)


def _git(rec, *args, timeout=300):
    return subprocess.run(["git", "-C", core.feature(rec["feature"])["path"], *args], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=timeout,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


def check(rec):
    """READY: the commit, rebased if the base moved, through the gate, pushed, and its pull request."""
    path = core.feature(rec["feature"])["path"]
    _git(rec, "fetch", "-q", core.REMOTE, core.BASE)
    if _git(rec, "status", "--porcelain").stdout.strip():
        return fix(rec, "The worktree has uncommitted changes: everything must be in the commit.")
    if int(_git(rec, "rev-list", "--count", f"{core.UPSTREAM}..HEAD").stdout.strip() or 0) == 0:
        return fix(rec, f"There is no commit ahead of {core.UPSTREAM}.")
    if _git(rec, "merge-base", "--is-ancestor", core.UPSTREAM, "HEAD").returncode:
        if _git(rec, "rebase", "-q", core.UPSTREAM).returncode:
            _git(rec, "rebase", "--abort")
            return fix(rec, f"{core.UPSTREAM} moved on and the commit does not rebase cleanly onto it. Rebase "
                            "it yourself and resolve the conflicts.")
        log(rec, f"rebased onto {core.UPSTREAM}")
    gate = subprocess.run([sys.executable, core.KENDLE, "gate"], cwd=path, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=3600)
    if gate.returncode:
        tail = "\n".join((gate.stdout + gate.stderr).strip().splitlines()[-40:])
        return fix(rec, f"kendle gate failed:\n\n{tail}")
    head = _git(rec, "rev-parse", "HEAD").stdout.strip()
    push = _git(rec, "push", "--force-with-lease", core.REMOTE, f"HEAD:refs/heads/{rec['branch']}")
    if push.returncode:
        return stuck(rec, f"git push failed: {push.stderr.strip()[-200:]}")
    rec["sha"] = head
    if not rec.get("pr"):
        report = MARKER.sub("", rec.get("ready", "")).strip()
        pull = github.open_pull(rec["branch"], core.BASE, rec["title"],
                                f"Closes #{rec['issue']}\n\n{report}\n\n---\nBuilt by a kendle feature team "
                                "and reviewed by a kendle review session (kendle autopilot).")
        rec["pr"] = pull["number"]
        log(rec, f"opened pull request #{rec['pr']} at {head[:8]}")
    else:
        log(rec, f"pushed {head[:8]} to pull request #{rec['pr']}")
    rec["phase"], rec["round"] = "reviewing", rec.get("round", 0) + 1
    ask_review(rec)


def review_request(rec):
    """What autopilot asks the review session each round: opening or resuming it does not review, and
    autopilot reads the verdict from the reply's first line. The folder already holds what kendle
    pushed; `kendle review-sync` would check out GitHub's ref, which can lag behind it."""
    head, rounds = rec["sha"][:8], AP["rounds"]
    if rec["round"] == 1:
        what = (f"review pull request #{rec['pr']} at {head}, its newest patch set, which this folder already "
                "holds. Review the whole change")
    else:
        what = (f"the author pushed {head} to answer your findings, and this folder already holds it. Review "
                "the whole change again")
    return (f"{TAG} Round {rec['round']} of {rounds}: {what} against its merge base with {core.UPSTREAM}. Do not "
            f"run `kendle review-sync` this time: it would check out GitHub's ref, which can lag behind {head}. "
            "Give the verdict line first - Approve, Comments or Blocked - then the context, then the findings.")


def ask_review(rec):
    """Round 1 opens the review; later rounds check the new head out in its folder. Every round then
    asks for the review itself - the session reviews only when asked."""
    head = rec["sha"]                                 # what kendle pushed; GitHub's ref can lag behind it
    e = review_of(rec)
    if e is None or e.get("released"):
        e = core.start_review(str(rec["pr"]), prompt=review_request(rec), head=head)
        rec["review"], rec["review_sent"] = e["id"], time.time()
        return log(rec, f"review round {rec['round']} started in {os.path.basename(e['cwd'])}")
    if not core.is_live(e, core.snapshot()):
        core.start_review(str(rec["pr"]), prompt=review_request(rec), head=head)   # resumes it on the new head
        rec["review_sent"] = time.time()
    else:
        core.refresh_review(e, head)                  # the folder and its row move to the new head together
        tell_review(rec, review_request(rec))
    log(rec, f"review round {rec['round']} asked at {head[:8]}")


def step_reviewing(rec):
    e = review_of(rec)
    if e is None or e.get("released") or not core.is_live(e, core.snapshot()):
        return ask_review(rec)
    text = answered(e, rec["review_sent"])
    if text is None or not text.strip():
        return held(rec, e, "review")
    first = next((line for line in text.splitlines() if line.strip()), "")
    m = VERDICT.match(first)
    verdict = m.group(1).capitalize() if m else "Comments"
    github.comment(rec["pr"], f"**kendle review - round {rec['round']} of {AP['rounds']}: {verdict}**\n\n{text}")
    log(rec, f"review round {rec['round']}: {verdict}")
    if verdict == "Approve":
        rec["phase"], rec["approved"] = "merging", time.time()
        return step_merging(rec)
    if rec["round"] >= AP["rounds"]:
        return stuck(rec, f"review round {rec['round']} of {AP['rounds']} was still {verdict}")
    tell_manager(rec, f"{TAG} Review round {rec['round']} of {AP['rounds']}: {verdict}. Address every finding "
                      "below - a fresh builder for the fixes, then tester and reviewer as usual - keep it to the "
                      "one commit, run kendle gate, and end your reply with `AUTOPILOT: READY`. If a finding is "
                      "wrong, say why in team.md and in your reply instead of changing the code.\n\n" + text)
    rec["phase"] = "building"


def step_merging(rec):
    p = github.pull(rec["pr"])
    if p.get("merged"):
        return closing(rec, p.get("merge_commit_sha") or "", by_hand=True)
    if p["state"] != "open":
        return stuck(rec, f"pull request #{rec['pr']} was closed without a merge")
    if AP["checks"]:
        state, failed = github.checks(rec["sha"])
        if state == "failed":
            return fix(rec, f"CI failed on the pull request: {', '.join(failed)}. Reproduce it locally.")
        if state == "none" and time.time() - rec.get("approved", time.time()) > NO_CHECKS_AFTER:
            return stuck(rec, f"no checks reported on {rec['sha'][:8]} after {NO_CHECKS_AFTER // 60} minutes")
        if state != "passed":
            return
    try:
        sha = github.merge(rec["pr"], AP["merge"], rec["sha"])
    except github.GitHubError as err:               # e.g. the base moved and it conflicts: rebase, review again
        log(rec, f"merge refused ({err}) - rebasing and asking for another review")
        return check(rec)
    github.delete_branch(rec["branch"])
    closing(rec, sha)


def closing(rec, sha, by_hand=False):
    rec["phase"], rec["merged"], rec["closing_since"] = "closing", sha, time.time()
    text = (f"{TAG} Pull request #{rec['pr']} is merged{' (by hand)' if by_hand else ''} as {sha[:8]}, and "
            f"issue #{rec['issue']} is closed with it. Thank you - we're done; nothing more to do.")
    m, e = manager_of(rec), review_of(rec)
    if m and core.is_live(m, core.snapshot()):
        core.type_into(m, text)
    if e and core.is_live(e, core.snapshot()):
        core.type_into(e, text)
    log(rec, f"merged pull request #{rec['pr']} as {sha[:8]}")


def step_closing(rec):
    """Give both sessions a moment to answer "we're done", then stop them and free the folders."""
    m, e = manager_of(rec), review_of(rec)
    quiet = all(x is None or not core.is_live(x, core.snapshot())
                or answered(x, rec["closing_since"]) is not None for x in (m, e))
    if not quiet and time.time() - rec["closing_since"] < CLOSE_AFTER:
        return
    if e and not e.get("released"):
        core.release_review(e["id"])
    if m and m.get("pane"):
        core.stop(m["id"])
    try:
        github.remove_label(rec["issue"], WORKING)
    except github.GitHubError as err:
        log(rec, f"could not remove the {WORKING} label: {err}")
    try:
        path = core.feature(rec["feature"])["path"]
    except LookupError:
        path = None                                  # already removed
    if path and subprocess.run(["git", "-C", core.PRIMARY, "worktree", "remove", path],
                               capture_output=True).returncode == 0:
        subprocess.run(["git", "-C", core.PRIMARY, "branch", "-D", rec["branch"]], capture_output=True)
    rec["phase"] = "done"
    notify(rec, f"merged - pull request #{rec['pr']} is in {core.BASE} as {rec['merged'][:8]}")


STEPS = {"building": step_building, "reviewing": step_reviewing, "merging": step_merging, "closing": step_closing}


def tick():
    """One step for every issue, and new issues picked up while there is room."""
    with core.starting("autopilot"):
        recs = load()
        for rec in recs:
            if rec["phase"] in STEPS:
                try:
                    STEPS[rec["phase"]](rec)
                except (github.GitHubError, LookupError, RuntimeError, ValueError, OSError,
                        subprocess.SubprocessError) as err:
                    log(rec, f"{rec['phase']}: {type(err).__name__}: {err}")
                save(recs)
        mine = {r["issue"] for r in recs}
        room = AP["max_active"] - sum(r["phase"] in ACTIVE for r in recs)
        for issue in github.issues(AP["label"], AP["authors"]) if room > 0 else []:
            if room <= 0:
                break
            if issue["number"] in mine or WORKING in [l["name"] for l in issue["labels"]]:
                continue
            try:
                recs.append(start(issue))
            except (github.GitHubError, LookupError, RuntimeError, ValueError, OSError) as err:
                log({"issue": issue["number"]}, f"could not start: {err}")
                continue
            save(recs)
            room -= 1
        save(recs)
        return recs


def resume(number):
    recs = load()
    rec = next((r for r in recs if r["issue"] == number and r["phase"] in ("stuck", "released")), None)
    if rec is None:
        raise LookupError(f"issue #{number} is not stuck")
    rec.update(phase="building", fixes=0, nudges=0, why=None, told=[])
    if rec.get("round", 0) >= AP["rounds"]:
        rec["round"] = AP["rounds"] - 1              # one more review round
    try:
        github.remove_label(rec.get("pr") or number, NEEDS_HUMAN)
    except github.GitHubError:
        pass
    tell_manager(rec, f"{TAG} A person looked at this and hands it back to you. Read the latest comments in "
                      "team.md, carry on, and end with `AUTOPILOT: READY` or `AUTOPILOT: STUCK <why>`.")
    log(rec, "resumed by hand")
    save(recs)
    return rec


def release(number):
    recs = load()
    rec = next((r for r in recs if r["issue"] == number and r["phase"] not in ("done", "released")), None)
    if rec is None:
        raise LookupError(f"autopilot does not have issue #{number}")
    rec["phase"] = "released"
    log(rec, "released: autopilot no longer runs it; the sessions stay as they are")
    save(recs)
    return rec


def ready():
    """Why autopilot may not run here, or None."""
    if not AP["enabled"]:
        return "set [autopilot] enabled = true in kendle.toml first"
    if not AP["authors"]:
        return "set [autopilot] authors in kendle.toml: the GitHub accounts whose issues it may work on"
    if AP["merge"] not in ("squash", "merge", "rebase"):
        return f"[autopilot] merge must be squash, merge or rebase, not {AP['merge']!r}"
    if not shutil.which("gh"):
        return "autopilot needs GitHub's gh, signed in: gh auth login"
    return None
