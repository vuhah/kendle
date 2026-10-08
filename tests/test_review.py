"""Reviews of someone else's change, from Gerrit, GitHub, GitLab, Bitbucket and configured hosts' refs
on a scratch origin."""
import json, os, subprocess, textwrap, time, unittest
from helpers import RUN, KendleTest, assistant, read, user, wait_for


class Reviews(KendleTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        w = cls.w
        first = w.push_ref({"a.txt": "1"}, "Login - first try", "refs/changes/34/1234/1")
        w.push_ref({"a.txt": "2"}, "Login - second try", "refs/changes/34/1234/2", parent=first)
        one = w.push_ref({"p1.txt": "1"}, "PR commit one", "refs/pull/7/head")
        w.push_ref({"p2.txt": "2"}, "PR commit two", "refs/pull/7/head", parent=one)
        w.push_ref({"m.txt": "1"}, "MR only commit", "refs/merge-requests/9/head")
        w.push_ref({"b.txt": "1"}, "Bitbucket PR", "refs/pull-requests/5/from")
        w.push_ref({"f.txt": "1"}, "Forge change", "refs/forge/42/head")
        w.push_ref({"d.txt": "1"}, "Bitbucket PR to draft", "refs/pull-requests/6/from")
        w.push_ref({"big.txt": "".join(f"{n}\n" for n in range(500))}, "Big change", "refs/changes/01/601/1")
        w.push_ref({"small.txt": "".join(f"{n}\n" for n in range(20))}, "Small change", "refs/changes/02/602/1")
        with open(os.path.join(w.ws, "draft.sh"), "w") as f:      # a host CLI that would upload a draft
            f.write(f'echo "$@" >> "{w.ws}/draft.log"\n')

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)

    def host(self, name, extra=""):
        """[review] host = name, plus more [review] lines or tables in extra."""
        text = read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review")[0]
        self.w.write_toml(text + f'\n[review]\nhost = "{name}"\n' + extra)

    def opened(self):
        return {e["change"]: e["host"] for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))")}

    def brief_for(self, number):
        """The newest session started on a change, with its brief: the system prompt's first line."""
        def calls():
            out = []
            for c in self.w.claude_calls():
                args = c["args"]
                brief = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
                if brief.startswith("Review ") and f" {number}" in brief:
                    out.append(dict(c, brief=brief))
            return out
        return wait_for(calls)[-1]

    def test_gerrit_takes_the_newest_patch_set_even_from_an_older_url(self):
        self.host("gerrit")
        out = self.w.kendle("review", "https://review.example.com/c/app/+/1234/1").stdout
        self.assertIn("review 1234/2 'Login - second try' by Tester", out)
        call = self.brief_for(1234)
        self.assertIn("Review Gerrit change 1234, patch set 2", call["brief"])
        self.assertIn("1 files. The diff is HEAD against HEAD^", call["brief"])
        self.assertEqual(read(os.path.join(call["cwd"], "a.txt")), "2")

    def test_github_and_gitlab_review_the_whole_change_against_its_merge_base(self):
        self.host("github")
        self.w.kendle("review", "https://github.com/x/app/pull/7")
        self.assertIn("2 files. The diff is HEAD against", self.brief_for(7)["brief"])
        self.host("gitlab")
        self.w.kendle("review", "https://gitlab.com/x/app/-/merge_requests/9")
        self.assertIn("Review merge request 9 at", self.brief_for(9)["brief"])

    def test_the_host_is_guessed_from_the_remote_or_asked_for(self):
        self.w.write_toml(read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review]")[0])
        r = self.w.kendle("review", "7", check=False)
        self.assertIn("which review host?", r.stderr)

    def test_a_url_picks_its_host_and_a_configured_host_needs_no_code(self):
        self.host("github", '[review.hosts.forge]\nfamily = "branch"\nref = "refs/forge/{}/head"\n'
                            'url = "/changes/(\\\\d+)"\nnoun = "forge change"\n')
        out = self.w.kendle("review", "https://forge.example.com/shop/changes/42").stdout
        self.assertIn("review 42/", out)
        self.assertIn("'Forge change'", out)
        self.w.kendle("review", "https://bitbucket.org/team/shop/pull-requests/5")
        self.w.kendle("review", "7")
        self.assertEqual(self.opened(), {"42": "forge", "5": "bitbucket", "7": "github"})
        got = self.w.py("from kendle import core, review; print(json.dumps([review.title(e) for e in core.reviews()]))")
        self.assertTrue(any(t.startswith("Forge change 42 at ") for t in got), got)

    def test_opening_does_not_review_and_sync_brings_the_newest_patch_set(self):
        self.host("gerrit")
        first = self.w.push_ref({"r.txt": "1"}, "Retry - first", "refs/changes/21/4321/1")
        out = self.w.kendle("review", "4321").stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        call = self.brief_for(4321)
        self.assertTrue(call["args"][0].startswith("--"), call["args"][:2])     # no first prompt: it waits for you
        flags = call["args"][call["args"].index("--allowedTools"):][:3]
        self.assertEqual(flags, ["--allowedTools", "Bash(kendle review-sync:*)", "Bash(kendle review-draft:*)"])
        self.assertNotIn("Give me the verdict", call["brief"])
        prompt = " ".join(call["args"][call["args"].index("--append-system-prompt") + 1:call["args"].index("--session-id")])
        for words in ("Do nothing until the user asks for a review", "Run `kendle review-sync 4321` first",
                      "Say which one you read", "agent_docs/reviews/4321-*.md", "agent_docs/<feature>/spec.md",
                      "kendle-reviewer, kendle-review-correctness and kendle-review-security", "Round 2",
                      "drop the false ones and merge the duplicates", "the context first",
                      "`kendle review-draft 4321`", "never post, approve or request changes"):
            self.assertIn(words, prompt)
        state = lambda: next(line.split()[0] for line in self.w.kendle("reviews").stdout.splitlines() if " 4321/" in line)
        self.assertEqual(state(), "open")
        drawn = self.w.py("""
            from kendle import core
            from kendle.cmd import sidebar
            s = sidebar.Sidebar.__new__(sidebar.Sidebar)
            s.slot, s.viewer, calls = None, None, []
            s.put = lambda y, x, text, color="text", selected=False, attr=0: calls.append([text, color])
            s.row(0, {"kind": "review", "e": next(e for e in core.reviews() if e["change"] == "4321")}, False, 60)
            print(json.dumps(calls))""")
        self.assertIn(["open", "yellow"], drawn)

        self.w.push_ref({"r.txt": "2"}, "Retry - second", "refs/changes/21/4321/2", parent=first)
        out = self.w.kendle("review-sync", "4321").stdout
        self.assertIn("Review Gerrit change 4321, patch set 2: \"Retry - second\"", out)
        self.assertIn("New since the last sync: yes", out)
        self.assertEqual(read(os.path.join(folder, "r.txt")), "2")
        self.assertIn("New since the last sync: no", self.w.kendle("review-sync", "4321").stdout)
        self.assertIn("4321/2 ", self.w.kendle("reviews").stdout)

        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "4321")["id"]
        self.w.write_transcript(self.w.transcript_path(sid, folder), [user("review it"), assistant("Comments\n- r.txt:1 ...")])
        self.assertEqual(state(), "reviewed")

    def test_titles_follow_the_host_each_review_was_opened_from(self):
        self.host("gerrit")
        self.w.kendle("review", "1234")
        self.host("github")
        self.w.kendle("review", "7")
        got = self.w.py("""
            from kendle import core, review
            print(json.dumps(sorted(review.title(e).split(" - ")[0] for e in core.reviews())))""")
        self.assertEqual(got, ["Gerrit change 1234 patch set 2", "Pull request 7 at " + got[1].split()[-1]])

    def review_with_findings(self, change):
        """Open a review and give it a reply, as if the user had asked and it had answered; its folder."""
        out = self.w.kendle("review", change).stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        number = out.split()[1].split("/")[0]
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == number)["id"]
        self.w.write_transcript(self.w.transcript_path(sid, folder),
                                [user("review it"), assistant(f"Comments\n- a.txt:1 - {number} is hard-coded.")])
        return folder

    def test_close_by_change_number_saves_findings_and_frees_the_folder(self):
        self.host("gerrit", "large = 1\n")
        out = self.w.kendle("review", "1234").stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "1234")["id"]
        self.w.write_transcript(self.w.transcript_path(sid, folder),
                                [{"type": "user", "message": {"content": "review it"}},
                                 {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
                                     {"type": "text", "text": "Comments\n- a.txt:1 - the value is hard-coded."}]}}])
        out = self.w.kendle("review-close", "1234").stdout
        self.assertIn("closed review 1234", out)
        saved = read(os.path.join(self.w.ws, "agent_docs", "reviews", "1234-2.md"))
        self.assertIn("# Gerrit change 1234 patch set 2 - Login - second try", saved)
        self.assertIn("a.txt:1 - the value is hard-coded.", saved)
        self.assertFalse(os.path.exists(folder))

    def test_only_a_large_change_leaves_its_findings_on_close(self):
        self.host("gerrit")                                       # large: 300 changed lines, by default
        self.review_with_findings("601")
        self.review_with_findings("602")
        self.assertIn("findings: " + os.path.join(self.w.ws, "agent_docs", "reviews", "601-1.md"),
                      self.w.kendle("review-close", "601").stdout)
        self.assertIn("findings: kept in chat (small change)", self.w.kendle("review-close", "602").stdout)
        self.assertIn("601 is hard-coded", read(os.path.join(self.w.ws, "agent_docs", "reviews", "601-1.md")))
        self.assertFalse(os.path.exists(os.path.join(self.w.ws, "agent_docs", "reviews", "602-1.md")))

    def test_a_draft_is_always_written_and_sent_only_where_it_stays_unpublished(self):
        log = os.path.join(self.w.ws, "draft.log")
        drafts = os.path.join(self.w.ws, "agent_docs", "reviews")
        sent = lambda: read(log).splitlines() if os.path.exists(log) else []
        draft = f'draft = "sh {self.w.ws}/draft.sh {{file}} {{change}} {{patchset}}"\n'
        self.host("gerrit", draft)
        self.review_with_findings("1234")
        out = self.w.kendle("review-draft", "1234").stdout
        self.assertIn("sent by [review] draft", out)
        path = os.path.join(drafts, "1234-2.draft.md")
        self.assertIn("1234 is hard-coded", read(path))
        self.assertEqual(sent(), [f"{path} 1234 2"])              # once, with {file} filled in
        self.host("gerrit")                                       # unset: written, nothing run
        self.assertIn("kept here: [review] draft is not set", self.w.kendle("review-draft", "1234").stdout)
        self.assertEqual(len(sent()), 1)
        self.host("bitbucket", draft)                             # no unpublished drafts there: never sent
        self.review_with_findings("6")
        self.assertIn("kept here: bitbucket has no unpublished drafts", self.w.kendle("review-draft", "6").stdout)
        self.assertIn("6 is hard-coded", read(os.path.join(drafts, f"6-{self.opened_patch('6')}.draft.md")))
        self.assertEqual(len(sent()), 1)

    def test_a_draft_with_no_reply_yet_is_refused_before_anything_is_written_or_sent(self):
        log = os.path.join(self.w.ws, "draft.log")
        before = read(log) if os.path.exists(log) else ""
        self.host("gerrit", f'draft = "sh {self.w.ws}/draft.sh {{file}} {{change}} {{patchset}}"\n')
        self.w.push_ref({"n.txt": "1"}, "Nothing said yet", "refs/changes/03/603/1")
        self.w.kendle("review", "603")                             # opened, never asked: no reply
        r = self.w.kendle("review-draft", "603", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing to draft yet", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.w.ws, "agent_docs", "reviews", "603-1.draft.md")))
        self.assertEqual(read(log) if os.path.exists(log) else "", before)

    def opened_patch(self, change):
        return next(e["patchset"] for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))")
                    if e["change"] == change)

    def test_starting_reviews_at_once_never_doubles_one_or_shares_a_folder(self):
        self.host("gerrit")
        self.w.push_ref({"b.txt": "1"}, "Logout", "refs/changes/78/5678/1")
        runs = [subprocess.Popen(RUN + ["review", c], cwd=self.w.ws, env=self.w.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for c in ("1234", "1234", "5678")]
        outs = [p.communicate(timeout=120) for p in runs]
        self.assertEqual([p.returncode for p in runs], [0, 0, 0], outs)
        open_ = self.w.py("from kendle import core; print(json.dumps(core.reviews()))")
        self.assertEqual(sorted(e["change"] for e in open_), ["1234", "5678"])         # one review of 1234, not two
        self.assertEqual(len({e["cwd"] for e in open_}), 2)                          # each in a folder of its own

    def test_at_most_three_at_once(self):
        self.host("gerrit")
        self.w.kendle("review", "1234")
        self.host("github")
        self.w.kendle("review", "7")
        self.host("gitlab")
        self.w.kendle("review", "9")
        self.assertEqual(self.open_rows(), 3)
        self.host("gerrit")
        self.w.kendle("review", "1234")                           # already open: comes back, no fourth slot
        self.assertEqual(self.open_rows(), 3)

    def open_rows(self):
        return len([r for r in self.w.kendle("reviews").stdout.splitlines() if r.split()[0] != "detected"])


class Inbox(KendleTest):
    """Changes waiting for review, listed by each host's inbox command: detected, never opened on their own."""
    TOML = """
        [workspace]
        reserve_gb = 0
        [review]
        host = "github"
        [review.hosts.github]
        inbox = "sh {workspace}/inbox.sh github"
        [review.hosts.forge]
        family = "branch"
        ref = "refs/forge/{}/head"
        url = "/forge/(\\\\d+)"
        inbox = "sh {workspace}/inbox.sh forge"
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        w = cls.w
        w.push_ref({"l.txt": "1"}, "Login fix", "refs/pull/11/head")
        cls.pr12 = w.push_ref({"s.txt": "1\n2\n"}, "Search speed-up", "refs/pull/12/head")
        w.push_ref({"f.txt": "1"}, "Forge work", "refs/forge/77/head")
        w.kendle("new", "login-fix")
        with open(os.path.join(w.ws, "inbox.sh"), "w") as f:     # what a host's CLI would print, from files
            f.write(f'[ -e "{w.ws}/inbox.fail" ] && {{ echo "inbox down" >&2; exit 1; }}\n'
                    f'cat "{w.ws}/inbox-$1.txt" 2>/dev/null\nexit 0\n')

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            if row.split()[0] != "detected":
                self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)
        for path in (os.path.join(self.w.ws, n) for n in ("inbox-github.txt", "inbox-forge.txt", "inbox.fail")):
            if os.path.exists(path):
                os.remove(path)
        if os.path.exists(os.path.join(self.w.state, "inbox.json")):
            os.remove(os.path.join(self.w.state, "inbox.json"))

    def inbox(self, host, *lines):
        with open(os.path.join(self.w.ws, f"inbox-{host}.txt"), "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def poll(self):
        return self.w.py("from kendle import core; print(json.dumps(core.poll_inbox()))")

    def two(self):
        self.inbox("github", "11\tlogin-fix\tLogin fix, take two", "", "no number here",
                   "https://github.com/x/shop/pull/12")

    def test_detected_changes_are_stored_once_with_their_feature_and_take_no_slot(self):
        self.two()
        calls = len(self.w.claude_calls())
        rows, errors = self.poll()
        self.assertEqual(errors, [])
        got = sorted((r["host"], r["change"], r["title"][:15], r["own"]) for r in rows)
        self.assertEqual(got, [("github", "11", "Login fix, take", "login-fix"),     # the line's title
                               ("github", "12", "Search speed-up", None)])           # the commit's subject
        rows, _ = self.poll()                                     # again: the same two, never four
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(json.loads(read(os.path.join(self.w.state, "inbox.json")))), 2)
        state = self.w.py("""
            from kendle import core
            print(json.dumps([core.reviews(), [e for _, _, e in core.review_slots()]]))""")
        self.assertEqual(state, [[], [None] * 3])                 # nothing started, every slot free
        self.assertEqual(len(self.w.claude_calls()), calls)

    def test_a_failed_run_keeps_its_rows_and_a_good_one_prunes_them(self):
        self.two()
        self.poll()
        open(os.path.join(self.w.ws, "inbox.fail"), "w").close()
        self.inbox("github", "12")
        rows, errors = self.poll()
        self.assertIn("github inbox failed: inbox down", errors)
        self.assertEqual(sorted(r["change"] for r in rows), ["11", "12"])
        os.remove(os.path.join(self.w.ws, "inbox.fail"))
        rows, errors = self.poll()
        self.assertEqual((errors, [r["change"] for r in rows]), ([], ["12"]))

    def test_a_change_that_cannot_be_looked_up_this_time_keeps_its_row(self):
        self.w.push_ref({"g.txt": "1"}, "Forge gone for now", "refs/forge/88/head")
        self.inbox("forge", "77", "88")
        self.poll()
        self.w.git(self.w.origin, "update-ref", "-d", "refs/forge/88/head")   # the lookup fails this once
        rows, errors = self.poll()
        self.assertTrue(any(e.startswith("forge inbox:") and "88" in e for e in errors), errors)
        self.assertEqual(sorted(r["change"] for r in rows if r["host"] == "forge"), ["77", "88"])
        self.inbox("forge", "77")                                 # no longer listed: now it goes
        self.assertEqual([r["change"] for r in self.poll()[0] if r["host"] == "forge"], ["77"])

    def test_a_host_whose_inbox_is_removed_no_longer_shows_its_rows(self):
        self.two()
        self.inbox("forge", "77")
        self.poll()
        toml = read(os.path.join(self.w.ws, "kendle.toml"))
        try:
            self.w.write_toml(toml.replace('inbox = "sh {workspace}/inbox.sh github"\n', ""))
            listed = self.w.kendle("reviews").stdout.splitlines()
        finally:
            self.w.write_toml(toml)
        self.assertNotEqual(toml, toml.replace('inbox = "sh {workspace}/inbox.sh github"\n', ""))
        self.assertEqual([line.split()[1].split("/")[0] for line in listed if line.split()[0] == "detected"], ["77"])

    def test_dismissing_a_detected_row_leaves_the_same_number_on_another_host(self):
        self.w.push_ref({"t.txt": "1"}, "Forge twelve", "refs/forge/12/head")
        self.inbox("forge", "12")
        self.inbox("github", "12")
        self.poll()
        waiting = self.sidebar("""
            s.sel = "d:github:12"
            s.stop()                                            # K
            print(json.dumps([[r["host"], r["change"]] for r in core.detected()]))""")
        self.assertEqual(waiting, [["forge", "12"]])

    def test_opening_a_change_leaves_the_same_number_on_another_host_waiting(self):
        self.w.push_ref({"t.txt": "1"}, "Forge eleven", "refs/forge/11/head")
        self.inbox("forge", "11")
        self.inbox("github", "11")
        self.poll()
        self.w.py("from kendle import core; core.start_review('11', host='github')")
        waiting = self.w.py("from kendle import core\n"
                            "print(json.dumps([[r['host'], r['change']] for r in core.detected()]))")
        self.assertEqual(waiting, [["forge", "11"]])

    def test_an_open_review_from_before_hosts_were_kept_still_hides_its_row(self):
        self.inbox("github", "11")
        self.poll()
        self.w.py("from kendle import core; core.start_review('11')")
        self.w.py("from kendle import core; core.update(lambda rows: [r.pop('host', None) for r in rows])")
        self.assertEqual(self.w.py("from kendle import core; print(json.dumps(core.detected()))"), [])

    def test_a_known_title_is_fetched_again_only_for_a_new_patch_set(self):
        self.inbox("github", "12")
        self.poll()
        path = os.path.join(self.w.state, "inbox.json")
        rows = json.loads(read(path))
        rows[0]["title"] = "Kept from the last poll"
        with open(path, "w") as f:
            json.dump(rows, f)
        self.assertEqual([r["title"] for r in self.poll()[0]], ["Kept from the last poll"])    # no fetch
        type(self).pr12 = self.w.push_ref({"s.txt": "1\n2\n4\n"}, "Search speed-up, third", "refs/pull/12/head",
                                          parent=self.pr12)
        self.assertEqual([r["title"] for r in self.poll()[0]], ["Search speed-up, third"])

    def test_a_failed_fetch_stores_no_title_from_an_earlier_one(self):
        self.inbox("github", "11")
        self.poll()                                               # FETCH_HEAD now holds 11's commit
        fetch_head = os.path.join(self.w.ws, "app", ".git", "FETCH_HEAD")
        self.inbox("github", "11", "12")
        os.chmod(fetch_head, 0o444)                               # the next fetch fails; FETCH_HEAD stays as it was
        try:
            rows, errors = self.poll()
        finally:
            os.chmod(fetch_head, 0o644)
        self.assertEqual([(r["change"], r["title"]) for r in rows], [("11", "Login fix")])
        self.assertTrue(any(e.startswith("github inbox: git fetch refs/pull/12/head failed") for e in errors), errors)

    def test_the_console_runs_the_inbox_at_most_once_a_minute(self):
        toml = read(os.path.join(self.w.ws, "kendle.toml"))
        every = "from kendle.cmd import sidebar; print(json.dumps(sidebar.inbox_every()))"
        try:
            self.w.write_toml(toml.replace('host = "github"\n', 'host = "github"\ninbox_every = 0\n', 1))
            self.assertEqual(self.w.py(every), 60)
            self.w.write_toml(toml.replace('host = "github"\n', 'host = "github"\ninbox_every = 2\n', 1))
            self.assertEqual(self.w.py(every), 120)
        finally:
            self.w.write_toml(toml)

    def test_a_dismissed_change_comes_back_with_a_newer_patch_set(self):
        self.inbox("github", "12")
        self.poll()
        self.w.py("from kendle import core; core.dismiss('12')")
        self.assertEqual(self.poll()[0], [])
        type(self).pr12 = self.w.push_ref({"s.txt": "1\n2\n3\n"}, "Search speed-up, again", "refs/pull/12/head",
                                          parent=self.pr12)
        self.assertEqual([r["change"] for r in self.poll()[0]], ["12"])

    def test_kendle_review_with_no_change_lists_the_inbox(self):
        self.two()
        r = self.w.kendle("review")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(any(line.split()[1].startswith("11/") and line.endswith("own: login-fix") for line in lines), lines)
        self.assertTrue(any(line.split()[1].startswith("12/") and "Search speed-up" in line for line in lines), lines)
        listed = self.w.kendle("reviews").stdout.splitlines()
        self.assertEqual([line.split()[0] for line in listed], ["detected", "detected"])
        open(os.path.join(self.w.ws, "inbox.fail"), "w").close()
        r = self.w.kendle("review", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("github inbox failed: inbox down", r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 2)           # what it knew stays

    def test_kendle_review_with_no_inbox_says_how_to_set_one_up(self):
        toml = read(os.path.join(self.w.ws, "kendle.toml"))
        try:
            self.w.write_toml(toml.split("\n[review]")[0])
            r = self.w.kendle("review", check=False)
        finally:
            self.w.write_toml(toml)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no inbox set up", r.stderr)
        self.assertIn("[review.hosts.github]", r.stderr)

    SIDEBAR = """
        from kendle import core
        from kendle.cmd import sidebar
        s = sidebar.Sidebar.__new__(sidebar.Sidebar)
        s.tree, s.questions, s.reviews, s.rows = [], [], core.reviews(), []
        s.desk = s.slot = s.sel = s.viewer = None
        s.folded, s.expanded, s.collapsed = set(), set(), set()
        s.detected = core.detected()
        s.autopilot = []
        said, drawn = [], []
        s.say = lambda text, error=False: said.append(text)
        s.put = lambda y, x, text, color="text", selected=False, attr=0: drawn.append([text, color])
        s.background = lambda busy, job, done: job()          # the job, on this thread
        s.refresh_now = lambda: None
        s.load()
    """

    def sidebar(self, code):
        return self.w.py(textwrap.dedent(self.SIDEBAR) + textwrap.dedent(code))

    def test_the_sidebar_lists_detected_changes_after_the_reviews(self):
        self.two()
        self.poll()
        got = self.sidebar("""
            headers = [r["text"] for r in s.rows if r["kind"] == "header"]
            mine = next(r for r in s.rows if r["kind"] == "detected" and r["e"]["change"] == "11")
            s.row(0, mine, False, 60)
            print(json.dumps([headers, drawn, [e for _, _, e in core.review_slots()]]))""")
        headers, drawn, slots = got
        self.assertEqual(headers[0], "REVIEW  0 of 3 · 2 waiting")
        self.assertIn(["detected", "faint"], drawn)
        self.assertIn(["own: login-fix", "cyan"], drawn)
        self.assertEqual(slots, [None] * 3)

    def test_enter_on_a_detected_row_opens_it_on_its_host_and_k_dismisses(self):
        self.inbox("forge", "77")
        self.inbox("github", "12")
        self.poll()
        got = self.sidebar("""
            s.sel = "d:github:12"
            s.stop()                                            # K
            s.sel = "d:forge:77"
            s.enter()                                           # Enter
            print(json.dumps([[[e["host"], e["change"]] for e in core.reviews()], core.detected(), said]))""")
        opened, waiting, said = got
        self.assertEqual(opened, [["forge", "77"]])               # [review] host is github; 77 is forge's
        self.assertEqual(waiting, [])
        self.assertEqual(said[0], "12 dismissed until a newer revision of it shows up")

    def test_the_console_runs_the_inbox_and_says_a_failure_once(self):
        self.two()
        fail = os.path.join(self.w.ws, "inbox.fail")
        got = self.w.py(f"""
            import os, threading
            from kendle import core
            from kendle.cmd.sidebar import Sidebar
            said = []
            class StandIn:
                inbox_errors, wake = set(), threading.Event()
                say = staticmethod(lambda text, error=False: said.append([text, error]))
            os.environ.pop("KENDLE_STATE")                      # outside the test console's guard
            Sidebar.watch_inbox(StandIn)
            found = len(core.detected())
            open({fail!r}, "w").close()
            Sidebar.watch_inbox(StandIn)
            Sidebar.watch_inbox(StandIn)                        # the same failure: said once
            os.remove({fail!r})
            Sidebar.watch_inbox(StandIn)
            open({fail!r}, "w").close()
            Sidebar.watch_inbox(StandIn)                        # broken again after a good run: said again
            os.remove({fail!r})
            print(json.dumps([found, said, StandIn.wake.is_set()]))""")
        found, said, woke = got
        self.assertEqual(found, 2)
        self.assertEqual([s for s in said if s[0].startswith("github")], [["github inbox failed: inbox down", True]] * 2)
        self.assertTrue(woke)

    def test_a_bare_number_from_a_host_inbox_opens_on_that_host(self):
        self.inbox("forge", "77")
        rows, _ = self.poll()
        self.assertEqual([(r["host"], r["change"], r["title"]) for r in rows], [("forge", "77", "Forge work")])
        out = self.w.kendle("review", "77").stdout                # [review] host is github; 77 is forge's
        self.assertIn("review 77/", out)
        got = self.w.py("from kendle import core; print(json.dumps([[e['host'], e['change']] for e in core.reviews()]))")
        self.assertEqual(got, [["forge", "77"]])
        self.assertEqual(self.poll()[0], [])                      # open now: a review, no longer waiting


class FromTheSession(KendleTest):
    """What a review session itself runs: kendle review-sync and review-draft from inside its own folder,
    with no KENDLE_WORKSPACE - kendle finds the workspace by walking up, as in a real review pane."""
    TOML = """
        [workspace]
        reserve_gb = 0
        [review]
        host = "github"
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = cls.w.push_ref({"g.txt": "1\n"}, "Cache - first", "refs/pull/31/head")
        cls.w.push_ref({"h.txt": "1\n"}, "Header tweak", "refs/pull/32/head")

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)

    def inside(self, folder, *args, check=True):
        """kendle run in a review folder (or below it) the way the session's Bash tool runs it."""
        return self.w.kendle(*args, cwd=folder, check=check, KENDLE_WORKSPACE="")

    def opened(self, change):
        out = self.w.kendle("review", change).stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        e = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))")
                 if e["change"] == change)
        return folder, e

    def calls_for(self, change):
        return [c for c in self.w.claude_calls() if f"review-{change}" in c["args"] or
                any(a.startswith("Review ") and f" {change}" in a for a in c["args"])]

    def test_sync_and_draft_work_from_inside_the_review_folder(self):
        folder, e = self.opened("31")
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(self.inside(folder, "list", check=False).returncode, 0)
        out = self.inside(folder, "review-sync", "31").stdout
        self.assertIn("Review pull request 31 at", out)
        self.assertIn("New since the last sync: no", out)
        self.w.push_ref({"g.txt": "1\n2\n"}, "Cache - second", "refs/pull/31/head", parent=self.first)
        sub = os.path.join(folder, "deep")
        os.makedirs(sub, exist_ok=True)
        out = self.inside(sub, "review-sync", "https://github.com/x/shop/pull/31").stdout
        self.assertIn("\"Cache - second\"", out)
        self.assertIn("New since the last sync: yes", out)
        self.assertEqual(read(os.path.join(folder, "g.txt")), "1\n2\n")
        head = self.w.git(folder, "rev-parse", "HEAD")
        row = next(r for r in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if r["change"] == "31")
        self.assertEqual((row["head"], row["subject"], row["patchset"]), (head, "Cache - second", head[:len(row["patchset"])]))
        self.w.write_transcript(self.w.transcript_path(e["id"], folder),
                                [user("review it"), assistant("Comments\n- g.txt:2 - the cache never expires.")])
        out = self.inside(folder, "review-draft", "31").stdout
        self.assertIn("kept here: [review] draft is not set", out)
        path = os.path.join(self.w.ws, "agent_docs", "reviews", f"31-{row['patchset']}.draft.md")
        self.assertIn(f"draft {path}", out)
        self.assertIn("the cache never expires", read(path))

    def test_sync_and_draft_of_a_change_not_open_say_so(self):
        for cmd in ("review-sync", "review-draft"):
            r = self.w.kendle(cmd, "999", check=False)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("no open review of 999", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_a_failing_draft_command_is_reported_and_the_draft_kept(self):
        toml = read(os.path.join(self.w.ws, "kendle.toml"))
        try:
            self.w.write_toml(toml + 'draft = "echo host said no >&2; exit 3"\n')
            folder, e = self.opened("32")
            self.w.write_transcript(self.w.transcript_path(e["id"], folder),
                                    [user("review it"), assistant("Approve\n- nothing to add.")])
            r = self.inside(folder, "review-draft", "32", check=False)
        finally:
            self.w.write_toml(toml)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[review] draft failed: host said no", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        path = os.path.join(self.w.ws, "agent_docs", "reviews", f"32-{e['patchset']}.draft.md")
        self.assertIn("nothing to add", read(path))

    def test_a_draft_command_that_hangs_is_stopped_and_reported(self):
        toml = read(os.path.join(self.w.ws, "kendle.toml"))
        try:
            self.w.write_toml(toml + 'draft = "exec sleep 5"\n')
            folder, e = self.opened("32")
            self.w.write_transcript(self.w.transcript_path(e["id"], folder),
                                    [user("review it"), assistant("Approve\n- nothing to add.")])
            said = self.w.py("""
                from kendle import core
                core.DRAFT_TIMEOUT = 1
                try:
                    core.review_draft("32")
                except RuntimeError as err:                     # what kendle prints as `kendle: ...`
                    print(json.dumps(str(err)))""")
        finally:
            self.w.write_toml(toml)
        self.assertIsInstance(said, str)
        self.assertIn("[review] draft timed out after 1s - the draft is in ", said)

    def test_the_claude_command_line_has_no_prompt_and_exactly_the_two_kendle_tools(self):
        folder, e = self.opened("32")
        mine = lambda: [c for c in self.calls_for("32") if e["id"] in c["args"]]     # files sort by pid, not time
        call = wait_for(mine)[-1]
        args = call["args"]
        self.assertEqual(call["cwd"], os.path.realpath(folder))
        self.assertTrue(all(a.startswith("-") for a in (args[0],)), args[:3])
        i = args.index("--allowedTools")
        self.assertEqual(args[i:i + 4][:3], ["--allowedTools", "Bash(kendle review-sync:*)", "Bash(kendle review-draft:*)"])
        self.assertTrue(args[i + 3].startswith("--"), args[i:i + 4])          # nothing else rides on the variadic flag
        self.assertEqual(args.count("--allowedTools"), 1)
        for flag in ("--permission-mode", "--disallowedTools", "--append-system-prompt", "--session-id"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        d = args.index("--disallowedTools")
        self.assertEqual(args[d + 1:d + 5], ["Edit", "Write", "NotebookEdit", "ExitPlanMode"])
        self.assertNotIn("--dangerously-skip-permissions", args)
        a, z = args.index("--append-system-prompt"), args.index("--session-id")
        rest = args[:a] + args[z:]                                # the fake writes one line per line of an arg
        values = {"plan", "Edit", "Write", "NotebookEdit", "ExitPlanMode", "Bash(kendle review-sync:*)",
                  "Bash(kendle review-draft:*)", "review-32", e["id"], os.path.join(self.w.ws, "agent_docs")}
        positional = [x for x in rest if not x.startswith("-") and x not in values]
        self.assertEqual(positional, [], args)                                    # no first prompt anywhere
        system = "\n".join(args[a + 1:z])
        self.assertTrue(system.startswith("Review pull request 32 at "), system[:80])
        self.assertIn("Do nothing until the user asks for a review", system)

    def test_reopening_an_earlier_review_resumes_and_says_to_sync_first(self):
        folder, e = self.opened("31")
        self.w.write_transcript(self.w.transcript_path(e["id"], folder),
                                [user("review it"), assistant("Comments\n- g.txt:1 ...")])
        self.w.tmux("kill-pane", "-t", e["pane"])
        before = len(self.w.claude_calls())
        self.w.kendle("review", "31")
        calls = wait_for(lambda: self.w.claude_calls()[before:] or None)
        args = calls[-1]["args"]
        self.assertEqual(args[args.index("--resume") + 1], e["id"])
        back = ("This folder holds pull request 31 again. Run `kendle review-sync 31` now, say in one line what it "
                "holds, then do nothing more until the user asks for a review.")
        self.assertEqual(args[0], back)                         # its system prompt is not re-sent on --resume
        self.assertFalse(any("I am back" in a for a in args), args)
        i = args.index("--allowedTools")
        self.assertEqual(args[i:i + 3], ["--allowedTools", "Bash(kendle review-sync:*)", "Bash(kendle review-draft:*)"])
        self.assertIn("--disallowedTools", args)

        time.sleep(1)                                           # the resumed turn comes after it started
        path = self.w.transcript_path(e["id"], folder)
        history = [user("review it", ago=60), assistant("Comments\n- g.txt:1 ...", ago=60)]
        self.w.write_transcript(path, history + [user(back), assistant("It holds pull request 31 at abc.")])
        self.assertEqual(self.row("31")["state"], "open")      # it only synced: nobody asked for a review
        self.w.write_transcript(path, history + [user(back), assistant("It holds pull request 31 at abc."),
                                                 user("review it again"), assistant("No new comments.")])
        self.assertEqual(self.row("31")["state"], "reviewed")

    def screen(self, e):
        return self.w.tmux("capture-pane", "-p", "-J", "-t", e["pane"])

    def row(self, change):
        return next(r for r in self.w.py("from kendle import core; print(json.dumps(core.reviews()))")
                    if r["change"] == change)

    def test_reopening_an_open_review_touches_nothing_and_a_request_checks_out_first(self):
        first = self.w.push_ref({"q.txt": "1\n"}, "Queue - first", "refs/pull/33/head")
        folder, e = self.opened("33")
        before = len(self.w.claude_calls())
        self.w.push_ref({"q.txt": "2\n"}, "Queue - second", "refs/pull/33/head", parent=first)
        newest = self.w.git(self.w.origin, "rev-parse", "refs/pull/33/head")
        screen = self.screen(e)

        self.w.kendle("review", "33")                                     # by hand, no request
        self.w.py("from kendle import core; core.start_review('33')")
        self.assertEqual(self.w.git(folder, "rev-parse", "HEAD"), first)
        self.assertEqual(self.row("33")["head"], first)
        self.assertEqual(len(self.w.claude_calls()), before)
        time.sleep(1)
        self.assertEqual(self.screen(e), screen)                         # nothing typed

        self.w.py("from kendle import core; core.start_review('33', prompt='Please review 33 now')")
        self.assertEqual(self.w.git(folder, "rev-parse", "HEAD"), newest)
        row = self.row("33")
        self.assertEqual((row["head"], row["patchset"], row["subject"]), (newest, newest[:8], "Queue - second"))
        self.assertTrue(row["name"].startswith(f"33/{newest[:8]} Queue - second"), row["name"])
        self.assertTrue(wait_for(lambda: "Please review 33 now" in self.screen(e)), self.screen(e))
        self.assertEqual(len(self.w.claude_calls()), before)

        self.w.py(f"from kendle import core; core.start_review('33', prompt='Please review {first[:8]}', "
                  f"head={first!r})")
        self.assertEqual(self.w.git(folder, "rev-parse", "HEAD"), first)
        row = self.row("33")
        self.assertEqual((row["head"], row["patchset"], row["subject"]), (first, first[:8], "Queue - first"))
        self.assertTrue(wait_for(lambda: f"Please review {first[:8]}" in self.screen(e)), self.screen(e))

    def test_head_opens_that_commit_instead_of_the_hosts_ref(self):
        older = self.w.push_ref({"s.txt": "1\n"}, "Sort - first", "refs/pull/34/head")
        self.w.git(os.path.join(self.w.ws, "app"), "fetch", "-q", "origin", "refs/pull/34/head")   # local, as pushed
        self.w.push_ref({"s.txt": "2\n"}, "Sort - second", "refs/pull/34/head", parent=older)
        e = self.w.py(f"from kendle import core; print(json.dumps(core.start_review('34', head={older!r})))")
        self.assertEqual(self.w.git(e["cwd"], "rev-parse", "HEAD"), older)
        self.assertEqual((e["head"], e["subject"], e["patchset"]), (older, "Sort - first", older[:8]))
        self.assertEqual(self.row("34")["patchset"], older[:8])   # not the host's ref, which is ahead of it
        args = wait_for(lambda: [c for c in self.calls_for("34") if e["id"] in c["args"]])[-1]["args"]
        self.assertTrue(args[0].startswith("-"), args[:3])                    # head= alone asks for nothing


if __name__ == "__main__":
    unittest.main()
