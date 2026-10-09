"""The launch site in site/: it has to open from a double-clicked file, load nothing from anywhere
else, and carry the metadata a shared link needs. These read the files the repository ships - no
workspace, no tmux, no network - the way test_config reads the example profile.
"""
import os, re, unittest
from html.parser import HTMLParser
from xml.etree import ElementTree
from helpers import ROOT, read

SITE = os.path.join(ROOT, "site")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "pages.yml")
SITE_FILES = ("index.html", "404.html", "style.css", "og.svg", "og.png", "CNAME", "robots.txt",
              "sitemap.xml")
DOMAIN = "https://kendle.fit"

# html.parser reports a void element through handle_starttag like any other, so the closing check
# below needs the HTML5 void set by name or it fails on correct markup.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}

# What actually fetches something when the page loads. An <a> may point anywhere; these may not.
FETCHING = {"link", "script", "img", "iframe", "source", "embed", "object", "video", "audio",
            "track"}

# Inline SVG, start tag to end tag. Hand-written SVG writes <path d="…"/>, so the one place the
# page is allowed an XHTML-style slash is inside one of these.
SVG = re.compile(r"<svg\b.*?</svg>", re.S | re.I)

# The shapes invented traction takes. No test can read a page for honesty; this one refuses the
# forms a made-up number arrives in, and the reviewer does the rest.
FABRICATED = ("TAM", "SAM", "SOM", "ARR", "MRR", "CAGR", "ROI", "pre-seed", "seed round",
              "Series A", "Series B", "term sheet", "valuation", "cap table", "per seat",
              "per user", "per month", "/mo", "/month", "billion", "million", "trillion",
              "testimonial")

# The four facts only the user has. Each ships as a marker comment and nothing else.
SLOTS = ("stage", "raising", "who", "usage")


class Page(HTMLParser):
    """One pass over a page: every tag with its attributes, its ids, and how it closes."""

    def __init__(self, name):
        super().__init__(convert_charrefs=True)
        self.name = name
        self.path = os.path.join(SITE, name)
        self.text = read(self.path)
        self.tags = []           # (tag, {attribute: value}) in document order
        self.ids = set()
        self.comments = []       # the text inside every <!-- --> on the page
        self.mismatched = []     # end tags that closed something that was not open
        self._stack = []
        self.feed(self.text)
        self.close()
        self.unclosed = list(self._stack)

    def handle_starttag(self, tag, attrs):
        values = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, values))
        if "id" in values:
            self.ids.add(values["id"])
        if tag not in VOID:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        else:
            self.mismatched.append(tag)

    def handle_comment(self, data):
        self.comments.append(data)

    def named(self, tag):
        return [a for t, a in self.tags if t == tag]

    def metas(self):
        """name= and property= metas as one dict, which is how the head reads to a crawler."""
        found = {}
        for a in self.named("meta"):
            key = a.get("name") or a.get("property")
            if key:
                found[key] = a.get("content", "")
        return found

    def references(self):
        """(tag, attribute, value) for every href and src on the page, in order."""
        out = []
        for tag, a in self.tags:
            for attr in ("href", "src"):
                if attr in a:
                    out.append((tag, attr, a[attr]))
        return out


def resolve(value, page_name):
    """Where a reference lands on disk, with site/ as the root - how Pages serves it."""
    target = value.split("#")[0].split("?")[0]
    if not target:
        return os.path.join(SITE, page_name)
    if target.startswith("/"):
        path = os.path.join(SITE, target.lstrip("/"))
    else:
        path = os.path.join(os.path.dirname(os.path.join(SITE, page_name)), target)
    if target.endswith("/") or os.path.isdir(path):
        path = os.path.join(path, "index.html")
    return path


def is_external(value):
    return value.startswith(("http://", "https://", "mailto:", "//"))


def media_block(css, condition):
    """The whole of the first @media block whose condition mentions `condition`, braces matched."""
    at = css.find("@media")
    while at != -1:
        opening = css.find("{", at)
        depth, i = 0, opening
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if condition in css[at:opening]:
            return css[at:i + 1]
        at = css.find("@media", i)
    return ""


def without_roots(css):
    """The CSS with every :root block cut out - everything that is not a token definition."""
    return re.sub(r":root\s*\{[^}]*\}", "", css, flags=re.S)


def outside_media(css):
    """The CSS that is not inside an @media block, counted by braces - enough for one hand-written
    stylesheet, and all the width rule below needs."""
    kept, depth, media_depth = [], 0, None
    i = 0
    while i < len(css):
        if media_depth is None and css.startswith("@media", i):
            media_depth = depth
            i += len("@media")
            continue
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if media_depth is not None and depth == media_depth:
                media_depth = None
                i += 1
                continue
        if media_depth is None:
            kept.append(c)
        i += 1
    return "".join(kept)


class Site(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = Page("index.html")
        cls.notfound = Page("404.html")
        cls.css = read(os.path.join(SITE, "style.css"))

    def test_the_files_and_the_workflow_are_there(self):
        for name in SITE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(SITE, name)),
                            f"site/{name} is missing")
        self.assertTrue(os.path.isfile(WORKFLOW), ".github/workflows/pages.yml is missing")
        workflow = read(WORKFLOW)
        self.assertIn("environment: github-pages", workflow,
                      "pages.yml must set environment: github-pages on one line - deploy-pages needs it")
        self.assertIn("path: site", workflow, "pages.yml must upload site/ as the artifact")

    def test_every_element_is_closed_explicitly(self):
        for page in (self.index, self.notfound):
            self.assertEqual(page.unclosed, [],
                             f"site/{page.name} leaves these open: {page.unclosed}")
            self.assertEqual(page.mismatched, [],
                             f"site/{page.name} closes tags that were not open: {page.mismatched}")

    def test_the_pages_are_plain_html5_not_xhtml(self):
        # Inside an <svg>, <path d="…"/> is how hand-written SVG reads, so those spans come out of
        # the text first. Nothing is asserted about what is in them: the closing check above still
        # covers them, because html.parser reports <path/> through handle_startendtag, which pushes
        # and pops in one step, so a <g> left open inside an SVG still fails there.
        for page in (self.index, self.notfound):
            markup = SVG.sub("", page.text)
            self.assertNotIn("/>", markup,
                             f"site/{page.name} writes a void element XHTML-style outside an <svg>; "
                             "HTML5 leaves it unclosed")

    def test_the_sitemap_is_valid_xml(self):
        ElementTree.parse(os.path.join(SITE, "sitemap.xml"))

    def test_the_head_carries_what_a_shared_link_needs(self):
        html = self.index.named("html")
        self.assertTrue(html and html[0].get("lang"), "site/index.html needs <html lang=...>")
        self.assertRegex(self.index.text, r"<title>\s*\S[^<]*</title>",
                         "site/index.html needs a non-empty <title>")
        metas = self.index.metas()
        for key in ("description", "viewport", "og:title", "og:description", "og:url", "og:type",
                    "og:site_name", "og:image", "twitter:card", "twitter:image"):
            self.assertTrue(metas.get(key), f"site/index.html is missing the {key} meta")
        self.assertEqual(metas["og:url"], DOMAIN + "/")
        self.assertEqual(metas["og:type"], "website")
        self.assertEqual(metas["twitter:card"], "summary_large_image")
        canonical = [a for a in self.index.named("link") if a.get("rel") == "canonical"]
        self.assertEqual([a["href"] for a in canonical], [DOMAIN + "/"],
                         "site/index.html needs exactly one canonical link, to the site root")
        self.assertEqual(len([t for t, _ in self.index.tags if t == "h1"]), 1,
                         "site/index.html must have exactly one <h1>")

    def test_every_reference_resolves(self):
        for page in (self.index, self.notfound):
            for tag, attr, value in page.references():
                if is_external(value):
                    continue
                if value.startswith("#"):
                    self.assertIn(value[1:], page.ids,
                                  f"site/{page.name}: <{tag} {attr}=\"{value}\"> names no id on the page")
                    continue
                path = resolve(value, page.name)
                self.assertTrue(os.path.isfile(path),
                                f"site/{page.name}: <{tag} {attr}=\"{value}\"> resolves to {path}, which does not exist")

    def test_index_is_relative_and_404_is_root_absolute(self):
        stylesheets = [a["href"] for a in self.index.named("link")
                       if a.get("rel") == "stylesheet"]
        self.assertIn("style.css", stylesheets, "site/index.html must load style.css")
        for value in [v for _, _, v in self.index.references()] + re.findall(r"url\(\s*['\"]?([^'\")]+)", self.css):
            self.assertFalse(value.startswith("/"),
                             f"site/index.html and style.css must stay relative; found {value!r}")
        for tag, attr, value in self.notfound.references():
            if is_external(value) or value.startswith("#"):
                continue
            self.assertTrue(value.startswith("/"),
                            f"site/404.html serves any unknown path, so {value!r} must start with /")
        self.assertEqual([a for a in self.notfound.named("link") if a.get("rel") == "stylesheet"], [],
                         "site/404.html carries its CSS inline - it must not link a stylesheet")

    def test_nothing_is_fetched_from_another_host(self):
        for page in (self.index, self.notfound):
            self.assertEqual([t for t, _ in page.tags if t == "script"], [],
                             f"site/{page.name} must have no <script>")
            for tag, attrs in page.tags:
                if tag not in FETCHING:
                    continue
                for attr in ("href", "src", "data"):
                    value = attrs.get(attr, "")
                    if tag == "link" and attrs.get("rel") in ("canonical", "alternate"):
                        continue
                    self.assertFalse(value.startswith(("http://", "https://", "//")),
                                     f"site/{page.name}: <{tag} {attr}> fetches {value!r} from another host")
        for value in re.findall(r"url\(\s*['\"]?([^'\")]+)", self.css):
            path = resolve(value, "style.css")
            self.assertTrue(os.path.isfile(path),
                            f"site/style.css: url({value!r}) must be a file under site/")
        for tag in ("img", "picture", "video"):
            self.assertEqual([t for t, _ in self.index.tags if t == tag], [],
                             f"site/index.html has a <{tag}>; the page carries no images, so nothing shifts")
        # Inline SVG fetches nothing, and these are the three ways it could start.
        for page in (self.index, self.notfound):
            self.assertNotIn("xlink:href", page.text,
                             f"site/{page.name} uses xlink:href; plain href is the one that resolves")
            for span in SVG.findall(page.text):
                for banned in ("<image", "<foreignObject"):
                    self.assertNotIn(banned.lower(), span.lower(),
                                     f"site/{page.name}: the inline SVG has a {banned}>, which "
                                     "loads something from outside the page")

    def test_the_domain_is_written_the_same_everywhere(self):
        cname = read(os.path.join(SITE, "CNAME"))
        self.assertEqual(cname.rstrip("\n"), "kendle.fit",
                         "site/CNAME holds the bare domain and nothing else")
        self.assertLessEqual(cname.count("\n"), 1, "site/CNAME may end in at most one newline")
        self.assertNotRegex(cname, r"[/ :#]", "site/CNAME takes no scheme, slash, space or comment")
        self.assertIn(DOMAIN + "/sitemap.xml", read(os.path.join(SITE, "robots.txt")),
                      "site/robots.txt must point at the sitemap")
        tree = ElementTree.parse(os.path.join(SITE, "sitemap.xml"))
        locs = [e.text for e in tree.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertEqual(locs, [DOMAIN + "/"], "site/sitemap.xml lists the one page")
        metas = self.index.metas()
        self.assertEqual(metas["og:image"], DOMAIN + "/og.png")
        self.assertEqual(metas["twitter:image"], DOMAIN + "/og.png")

    def test_the_page_and_the_card_stay_small(self):
        page = (os.path.getsize(os.path.join(SITE, "index.html"))
                + os.path.getsize(os.path.join(SITE, "style.css")))
        self.assertLess(page, 72 * 1024, f"index.html plus style.css is {page} bytes, over the 72 KB budget")
        card = os.path.join(SITE, "og.png")
        self.assertLess(os.path.getsize(card), 100 * 1024,
                        "site/og.png is over the 100 KB ceiling")
        with open(card, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n", "site/og.png is not a PNG")

    def test_it_reads_on_a_phone_as_far_as_the_source_shows(self):
        self.assertIn("width=device-width", self.index.metas().get("viewport", ""),
                      "site/index.html needs the width=device-width viewport meta")
        fixed = outside_media(self.css)
        for name, size in re.findall(r"\b((?:min-|max-)?width)\s*:\s*([0-9.]+)px", fixed):
            self.assertLessEqual(float(size), 320,
                                 f"site/style.css sets {name}: {size}px outside a media query; "
                                 "cap the measure in ch or rem instead")
        body = re.search(r"(?:^|\})\s*body\s*\{([^}]*)\}", self.css, re.S)
        self.assertIsNotNone(body, "site/style.css needs a body rule")
        font = re.search(r"font-size\s*:\s*([^;]+);", body.group(1))
        self.assertIsNotNone(font, "site/style.css must set body font-size")
        value = font.group(1).strip()
        if value.endswith("px"):
            self.assertGreaterEqual(float(value[:-2]), 16, "body text must be at least 16px")
        else:
            self.assertIn(value, ("1rem", "100%", "1em"),
                          f"body font-size {value!r} is not clearly at least 16px")
        rules = [chunk for chunk in self.css.split("}") if "overflow-x" in chunk]
        self.assertEqual(len(rules), 1,
                         "style.css declares overflow-x in more than one rule; only the diagram scrolls")
        selector = rules[0].split("{")[0].strip().split("\n")[-1].strip()
        self.assertTrue(selector.startswith("pre"),
                        f"the one overflow-x rule is {selector!r}; only the <pre> holding the diagram scrolls")
        if "." in selector:
            wanted = selector.split(".", 1)[1]
            classes = [a.get("class", "") for t, a in self.index.tags if t == "pre"]
            self.assertTrue(any(wanted in c.split() for c in classes),
                            f"no <pre> in site/index.html carries the class {wanted!r} that rule styles")


    def test_the_page_claims_no_traction_it_cannot_show(self):
        """Every figure on the page traces to README.md or the repository. These are the shapes an
        invented one takes; the fix for a match is always to delete the claim, never to reword it."""
        text = self.index.text
        money = re.search(r"[$€£¥] ?\d", text)
        self.assertIsNone(money, "site/index.html quotes a currency amount "
                          f"({money.group(0) if money else ''!r}); delete the claim, do not reword it")
        percent = re.search(r"\d\s*%", text)
        self.assertIsNone(percent, "site/index.html quotes a percentage "
                          f"({percent.group(0) if percent else ''!r}); delete the claim, do not reword it")
        for term in FABRICATED:
            found = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I)
            self.assertIsNone(found, f"site/index.html says {term!r}, which is a claim this page "
                              "cannot show; delete it, do not reword it")

    def test_there_is_one_way_to_reach_us_and_it_is_not_a_form(self):
        mailto = [v for _, _, v in self.index.references() if v.startswith("mailto:")]
        self.assertEqual(mailto, ["mailto:admin@kendle.fit"],
                         f"site/index.html must carry exactly one mailto:, the address; found {mailto}")
        for page in (self.index, self.notfound):
            for tag in ("form", "input", "button", "textarea"):
                self.assertEqual([t for t, _ in page.tags if t == tag], [],
                                 f"site/{page.name} has a <{tag}>; the page collects nothing")

    def test_no_comment_carries_a_half_written_figure(self):
        """The optional slots are marker comments and ship empty. A placeholder that looks like a
        real number is the fabrication the rest of the page is careful to avoid."""
        for comment in self.index.comments:
            self.assertNotRegex(comment, r"[\d$€£¥]",
                                f"site/index.html has a comment with a figure in it: {comment.strip()!r}")
            body = comment.strip()
            if body.startswith("slot:"):
                marker = re.match(r"slot:\s+([a-z]+)\s+-\s+\S", body)
                self.assertIsNotNone(marker, "a slot marker reads "
                                     f"'<!-- slot: <name> - <what belongs here> -->'; found {body!r}")
                self.assertIn(marker.group(1), SLOTS,
                              f"site/index.html names an unknown slot {marker.group(1)!r}")

    def test_the_design_is_a_system_not_one_off_rules(self):
        roots = re.findall(r":root\s*\{[^}]*\}", self.css, re.S)
        self.assertTrue(roots, "site/style.css must define its tokens on :root")
        tokens = "\n".join(roots)
        steps = re.findall(r"--[\w-]+\s*:\s*[0-9.]+rem\s*;", tokens)
        self.assertGreaterEqual(len(steps), 4,
                                "site/style.css must define a type scale on :root, not one-off sizes")
        self.assertIn("calc(var(--", tokens,
                      "site/style.css must build its spacing out of one step on :root")

        rest = without_roots(self.css)
        for value in re.findall(r"font-size\s*:\s*([^;}]+)", rest):
            value = value.strip()
            self.assertRegex(value, r"^(var\(--[\w-]+\)|[0-9.]+(em|%))$",
                             f"site/style.css sets font-size: {value}; every size names a scale "
                             "step, or is relative in em or %")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}", rest), [],
                         "site/style.css writes a colour outside :root; both schemes change "
                         "together only while every colour is a token")

        if re.search(r"\b(animation|transition)\s*:|@keyframes", self.css):
            quiet = media_block(self.css, "prefers-reduced-motion: no-preference")
            self.assertTrue(quiet, "site/style.css animates something without a "
                            "prefers-reduced-motion: no-preference block to put it in")
            still = self.css.replace(quiet, "")
            self.assertIsNone(re.search(r"\b(animation|transition)\s*:|@keyframes", still),
                              "site/style.css moves something outside the "
                              "prefers-reduced-motion: no-preference block")

    def test_the_inline_svg_themes_and_scales(self):
        spans = SVG.findall(self.index.text)
        self.assertTrue(spans, "site/index.html should carry the team diagram as an inline <svg>")
        for span in spans:
            head = span[:span.index(">") + 1]
            self.assertIn("viewBox", head, f"an inline <svg> has no viewBox, so it cannot scale: {head}")
            fixed = re.search(r"\s(width|height)\s*=", head)
            self.assertIsNone(fixed, "an inline <svg> sets "
                              f"{fixed.group(1) if fixed else ''}= as an attribute; it is sized in "
                              "CSS so it stays fluid at 320 pixels")
            if 'aria-hidden="true"' in head:
                continue
            self.assertIn('role="img"', head,
                          f"an inline <svg> is neither aria-hidden nor role=\"img\": {head}")
            self.assertRegex(span, r"^<svg\b[^>]*>\s*<title>\s*\S",
                             "an inline <svg> with role=\"img\" needs a <title> as its first child")


if __name__ == "__main__":
    unittest.main()
