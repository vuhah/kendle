"""Review hosts: how someone else's change is found and fetched, by plain git - no API, no token.

  gerrit   refs/changes/NN/<change>/<patch set>    the newest patch set; the diff is HEAD^..HEAD
  github   refs/pull/<n>/head                      the PR's head; the diff is against its merge base
  gitlab   refs/merge-requests/<n>/head            the MR's head; the diff is against its merge base

kendle.toml [review] host picks one; unset, it is guessed from the remote's URL (github / gitlab).
"""
import re
from kendle import core


class Host:
    name, noun, revision = "", "change", "revision"

    def number(self, change):
        text = str(change).strip().rstrip("/")
        m = re.search(self.URL, text) if "/" in text else None
        number = m.group(1) if m else re.sub(r"\D", "", text.split("/")[-1])
        if not number:
            raise ValueError(f"'{change}' has no {self.noun} number in it")
        return number

    def title(self, e):
        return f"{self.noun.capitalize()} {e['change']} {self.revision} {e['patchset']} - {e['subject']}"


class Gerrit(Host):
    name, noun, revision, URL = "gerrit", "Gerrit change", "patch set", r"/\+/(\d+)"

    def locate(self, change):
        """The newest patch set: (ref, number, patch set)."""
        number = self.number(change)
        out = core.git("ls-remote", core.REMOTE, f"refs/changes/*/{number}/*")
        refs = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
        sets = [(int(r.rsplit("/", 1)[1]), r) for r in refs if r.rsplit("/", 1)[1].isdigit()]
        if not sets:
            raise LookupError(f"Gerrit has no change {number}")
        patch, ref = max(sets)
        return ref, number, patch

    def base(self, head):
        return head + "^"

    def brief(self, number, patch, subject, author, files, base):
        return (f"Review Gerrit change {number}, patch set {patch}: \"{subject}\" by {author}, "
                f"{files} files. The diff is HEAD against HEAD^ in this folder. Give me the verdict "
                "and the findings.")


class _Branchy(Host):
    """Hosts whose change is a branch head: the revision is its commit, the diff its merge base."""
    REF = ""

    def locate(self, change):
        number = self.number(change)
        ref = self.REF.format(number)
        out = core.git("ls-remote", core.REMOTE, ref)
        if "\t" not in out:
            raise LookupError(f"{core.REMOTE} has no {self.noun} {number}")
        return ref, number, out.split("\t")[0][:8]

    def base(self, head):
        core.git("fetch", "-q", core.REMOTE, core.BASE)
        return core.git("merge-base", head, core.UPSTREAM) or head + "^"

    def brief(self, number, patch, subject, author, files, base):
        return (f"Review {self.noun} {number} at {patch}: \"{subject}\" (newest commit) by {author}, "
                f"{files} files. The diff is HEAD against {base[:10]}, its merge base with {core.UPSTREAM}, "
                "in this folder. Give me the verdict and the findings.")


class GitHub(_Branchy):
    name, noun, revision, URL, REF = "github", "pull request", "at", r"/pull/(\d+)", "refs/pull/{}/head"


class GitLab(_Branchy):
    name, noun, revision, URL, REF = "gitlab", "merge request", "at", r"/merge_requests/(\d+)", \
        "refs/merge-requests/{}/head"


HOSTS = {h.name: h for h in (Gerrit(), GitHub(), GitLab())}


def host():
    name = core.CONFIG["review"]["host"]
    if not name:
        url = core.git("remote", "get-url", core.REMOTE)
        name = "github" if "github" in url else "gitlab" if "gitlab" in url else None
    if not name:
        raise LookupError("which review host? set kendle.toml [review] host = \"gerrit\", \"github\" or \"gitlab\"")
    if name not in HOSTS:
        raise LookupError(f"review host '{name}' - kendle knows {', '.join(HOSTS)}")
    return HOSTS[name]


def title(e):
    """How a review reads in the sidebar and its saved findings - by the host it was opened from."""
    if e.get("host") in HOSTS:
        return HOSTS[e["host"]].title(e)
    try:
        return host().title(e)
    except LookupError:
        return Host().title(e)
