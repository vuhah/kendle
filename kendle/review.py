"""Review hosts: how someone else's change is found and fetched, by plain git - no API, no token.

  gerrit      refs/changes/NN/<change>/<patch set>    the newest patch set; the diff is HEAD^..HEAD
  github      refs/pull/<n>/head                      the PR's head; the diff is against its merge base
  gitlab      refs/merge-requests/<n>/head            the MR's head; the diff is against its merge base
  bitbucket   refs/pull-requests/<n>/from             the PR's head; the diff is against its merge base

kendle.toml [review] host picks one; unset, it is guessed from the remote's URL (github / gitlab /
bitbucket). A [review.hosts.<name>] table adds a host of either family - "parent" (Gerrit's: the
newest patch set by number, diffed against HEAD^) or "branch" (a branch head, diffed against its merge
base) - with its `ref` ({} for the number), a `url` regex whose group 1 is the number, and optionally
`noun`, `revision`, `inbox` (a command listing the changes waiting for you) and `draft_kind`
("remote", or "local" where the host has no unpublished drafts). A table named after a built-in only
changes the keys it gives. A URL picks its host by these regexes; a bare number uses [review] host.
"""
import re
from kendle import core


class Host:
    name, noun, revision, family = "", "change", "revision", ""
    URL, REF = r"$^", ""
    inbox, draft_kind = None, "remote"

    def number(self, change):
        text = str(change).strip().rstrip("/")
        m = re.search(self.URL, text) if "/" in text else None
        number = m.group(1) if m else re.sub(r"\D", "", text.split("/")[-1])
        if not number:
            raise ValueError(f"'{change}' has no {self.noun} number in it")
        return number

    def title(self, e):
        return f"{self.noun[:1].upper() + self.noun[1:]} {e['change']} {self.revision} {e['patchset']} - {e['subject']}"


class Gerrit(Host):
    """A change whose patch sets are refs numbered under it; the diff is the newest one's own commit."""
    name, noun, revision, family = "gerrit", "Gerrit change", "patch set", "parent"
    URL, REF = r"/\+/(\d+)", "refs/changes/*/{}/*"

    def locate(self, change):
        """The newest patch set: (ref, number, patch set)."""
        number = self.number(change)
        out = core.git("ls-remote", core.REMOTE, self.REF.format(number))
        refs = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
        sets = [(int(r.rsplit("/", 1)[1]), r) for r in refs if r.rsplit("/", 1)[1].isdigit()]
        if not sets:
            raise LookupError(f"{core.REMOTE} has no {self.noun} {number}")
        patch, ref = max(sets)
        return ref, number, patch

    def base(self, head):
        return head + "^"

    def brief(self, number, patch, subject, author, files, base):
        return (f"Review {self.noun} {number}, {self.revision} {patch}: \"{subject}\" by {author}, "
                f"{files} files. The diff is HEAD against HEAD^ in this folder.")


class _Branchy(Host):
    """Hosts whose change is a branch head: the revision is its commit, the diff its merge base."""
    family = "branch"

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
                "in this folder.")


class GitHub(_Branchy):
    name, noun, revision, URL, REF = "github", "pull request", "at", r"/pull/(\d+)", "refs/pull/{}/head"


class GitLab(_Branchy):
    name, noun, revision, URL, REF = "gitlab", "merge request", "at", r"/merge_requests/(\d+)", \
        "refs/merge-requests/{}/head"


class Bitbucket(_Branchy):
    name, noun, revision, URL, REF = "bitbucket", "pull request", "at", r"/pull-requests/(\d+)", \
        "refs/pull-requests/{}/from"
    draft_kind = "local"                             # Bitbucket Cloud has no unpublished drafts


class ParentHost(Gerrit):
    """A [review.hosts.<name>] table of the "parent" family; its ref is a glob, e.g. refs/changes/*/{}/*."""
    noun, revision = "change", "patch set"


class BranchHost(_Branchy):
    """A [review.hosts.<name>] table of the "branch" family."""
    noun, revision = "change", "at"


FAMILIES = {"parent": ParentHost, "branch": BranchHost}
KEYS = {"family": "family", "ref": "REF", "url": "URL", "noun": "noun", "revision": "revision",
        "inbox": "inbox", "draft_kind": "draft_kind"}


def _merge(tables):
    """The built-in hosts plus kendle.toml's [review.hosts.<name>] tables, in that order."""
    hosts = {h.name: h for h in (Gerrit(), GitHub(), GitLab(), Bitbucket())}
    for name, table in (tables or {}).items():
        def broken(why):
            return SystemExit(f"kendle: kendle.toml: review.hosts.{name}: {why}")
        if not isinstance(table, dict):
            raise broken("must be a table")
        unknown = set(table) - set(KEYS)
        if unknown:
            raise broken(f"unknown key {sorted(unknown)[0]}")
        if not all(isinstance(v, str) for v in table.values()):
            raise broken("every value is a string")
        if name in hosts:
            h = hosts[name]
            if table.get("family", h.family) != h.family:
                raise broken(f"{name} is built in, of the \"{h.family}\" family")
        else:
            if table.get("family") not in FAMILIES:
                raise broken("family must be \"branch\" or \"parent\"")
            missing = [k for k in ("ref", "url") if not table.get(k)]
            if missing:
                raise broken(f"needs a {' and a '.join(missing)}")
            h = hosts[name] = FAMILIES[table["family"]]()
            h.name = name
        for key, value in table.items():
            setattr(h, KEYS[key], value)
        try:
            groups = re.compile(h.URL).groups
        except re.error as err:
            raise broken(f"url is not a regex: {err}")
        if not groups:
            raise broken("url needs a group, e.g. /changes/(\\d+), for the number")
        if "{}" not in h.REF:
            raise broken("ref needs {} where the number goes")
        if h.draft_kind not in ("remote", "local"):
            raise broken("draft_kind must be \"remote\" or \"local\"")
    return hosts


HOSTS = _merge(core.CONFIG["review"]["hosts"])


def host():
    """The host for a bare number: [review] host, else guessed from the remote's URL."""
    name = core.CONFIG["review"]["host"]
    if not name:
        url = core.git("remote", "get-url", core.REMOTE)
        name = next((n for n in ("github", "gitlab", "bitbucket") if n in url), None)
    if not name:
        raise LookupError(f"which review host? set kendle.toml [review] host = one of {', '.join(HOSTS)}")
    if name not in HOSTS:
        raise LookupError(f"review host '{name}' - kendle knows {', '.join(HOSTS)}")
    return HOSTS[name]


def host_for(change):
    """The host a change belongs to: a URL by the first host whose `url` matches it, else host()."""
    text = str(change).strip()
    if "/" in text:
        for h in HOSTS.values():
            if re.search(h.URL, text):
                return h
    return host()


def title(e):
    """How a review reads in the sidebar and its saved findings - by the host it was opened from."""
    if e.get("host") in HOSTS:
        return HOSTS[e["host"]].title(e)
    try:
        return host().title(e)
    except LookupError:
        return Host().title(e)
