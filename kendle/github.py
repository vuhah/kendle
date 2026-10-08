"""GitHub for kendle autopilot: issues, labels, comments, pull requests, checks and merges, through
`gh api` (GitHub's REST API, with the token `gh auth login` stored). The only module that writes to
GitHub; nothing here runs unless autopilot is on.
"""
import json, re, subprocess
from kendle import core


class GitHubError(RuntimeError):
    pass


def repo():
    """owner/name: [autopilot] repo, else from the remote's URL (git@github.com:o/r.git, https://...)."""
    if core.CONFIG["autopilot"]["repo"]:
        return core.CONFIG["autopilot"]["repo"]
    url = core.git("remote", "get-url", core.REMOTE)
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise GitHubError(f"{core.REMOTE} is not a GitHub repository: {url or '(no URL)'}")
    return f"{m.group(1)}/{m.group(2)}"


def api(method, path, body=None, ok=()):
    """One REST call; the parsed reply, or None for an empty one. A status in ok is not an error."""
    args = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True,
                       text=True, timeout=120)
    if r.returncode:
        status = re.search(r"\(HTTP (\d{3})\)", r.stderr)
        if status and int(status.group(1)) in ok:
            return None
        raise GitHubError(f"gh api {method} {path}: {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def _r(path):
    return f"repos/{repo()}/{path}"


# ---- issues ----

def issues(label, authors):
    """Open issues with the label, opened by one of the authors, oldest first."""
    out = []
    for author in authors:
        for i in api("GET", _r(f"issues?state=open&labels={label}&creator={author}&per_page=100")) or []:
            if "pull_request" not in i and i["user"]["login"] in authors:
                out.append(i)
    return sorted({i["number"]: i for i in out}.values(), key=lambda i: i["number"])


def issue(number):
    return api("GET", _r(f"issues/{number}"))


def comment(number, body):
    """A comment on an issue or a pull request (they share numbers)."""
    return api("POST", _r(f"issues/{number}/comments"), {"body": body})


def ensure_label(name, color="ededed"):
    api("POST", _r("labels"), {"name": name, "color": color}, ok=(422,))      # 422: it exists


def add_label(number, name):
    ensure_label(name)
    api("POST", _r(f"issues/{number}/labels"), {"labels": [name]})


def remove_label(number, name):
    api("DELETE", _r(f"issues/{number}/labels/{name}"), ok=(404,))


# ---- pull requests ----

def pull_for(branch):
    """The newest pull request from this repository's branch, open or not; None if there is none."""
    owner = repo().split("/")[0]
    pulls = api("GET", _r(f"pulls?state=all&head={owner}:{branch}&per_page=10")) or []
    return max(pulls, key=lambda p: p["number"]) if pulls else None


def pull(number):
    return api("GET", _r(f"pulls/{number}"))


def open_pull(branch, base, title, body):
    return api("POST", _r("pulls"), {"title": title, "head": branch, "base": base, "body": body})


def checks(sha):
    """('passed' | 'failed' | 'pending' | 'none', names of the failed ones) for a commit's check runs."""
    runs = (api("GET", _r(f"commits/{sha}/check-runs?per_page=100")) or {}).get("check_runs", [])
    if not runs:
        return "none", []
    failed = [r["name"] for r in runs if r["status"] == "completed"
              and r["conclusion"] not in ("success", "neutral", "skipped")]
    if failed:
        return "failed", failed
    return ("passed" if all(r["status"] == "completed" for r in runs) else "pending"), []


def merge(number, method, sha):
    """Merge if the head is still sha; returns the merge commit's sha."""
    return api("PUT", _r(f"pulls/{number}/merge"), {"merge_method": method, "sha": sha})["sha"]


def delete_branch(branch):
    api("DELETE", _r(f"git/refs/heads/{branch}"), ok=(404, 422))
