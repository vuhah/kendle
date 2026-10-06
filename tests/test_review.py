"""Reviews of someone else's change, from Gerrit, GitHub and GitLab refs on a scratch origin."""
import os, subprocess, unittest
from helpers import RUN, KendleTest, read, wait_for


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

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)

    def host(self, name):
        text = read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review]")[0]
        self.w.write_toml(text + f'\n[review]\nhost = "{name}"\n')

    def brief_for(self, number):
        calls = wait_for(lambda: [c for c in self.w.claude_calls() if c["args"] and c["args"][0].startswith("Review ")
                                  and f" {number}" in c["args"][0]])
        return calls[-1]

    def test_gerrit_takes_the_newest_patch_set_even_from_an_older_url(self):
        self.host("gerrit")
        out = self.w.kendle("review", "https://review.example.com/c/app/+/1234/1").stdout
        self.assertIn("review 1234/2 'Login - second try' by Tester", out)
        call = self.brief_for(1234)
        self.assertIn("Review Gerrit change 1234, patch set 2", call["args"][0])
        self.assertIn("1 files. The diff is HEAD against HEAD^", call["args"][0])
        self.assertEqual(read(os.path.join(call["cwd"], "a.txt")), "2")

    def test_github_and_gitlab_review_the_whole_change_against_its_merge_base(self):
        self.host("github")
        self.w.kendle("review", "https://github.com/x/app/pull/7")
        self.assertIn("2 files. The diff is HEAD against", self.brief_for(7)["args"][0])
        self.host("gitlab")
        self.w.kendle("review", "https://gitlab.com/x/app/-/merge_requests/9")
        self.assertIn("Review merge request 9 at", self.brief_for(9)["args"][0])

    def test_the_host_is_guessed_from_the_remote_or_asked_for(self):
        self.w.write_toml(read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review]")[0])
        r = self.w.kendle("review", "7", check=False)
        self.assertIn("which review host?", r.stderr)

    def test_titles_follow_the_host_each_review_was_opened_from(self):
        self.host("gerrit")
        self.w.kendle("review", "1234")
        self.host("github")
        self.w.kendle("review", "7")
        got = self.w.py("""
            from kendle import core, review
            print(json.dumps(sorted(review.title(e).split(" - ")[0] for e in core.reviews())))""")
        self.assertEqual(got, ["Gerrit change 1234 patch set 2", "Pull request 7 at " + got[1].split()[-1]])

    def test_close_by_change_number_saves_findings_and_frees_the_folder(self):
        self.host("gerrit")
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
        self.assertEqual(len(self.w.kendle("reviews").stdout.splitlines()), 3)
        self.host("gerrit")
        self.w.kendle("review", "1234")                           # already open: comes back, no fourth slot
        self.assertEqual(len(self.w.kendle("reviews").stdout.splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
