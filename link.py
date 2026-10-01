#!/usr/bin/env python3
"""
Reads a web page well enough to post about it.

Pulls the title, the feature image and the opening of the article, which is
what a social post about a blog post actually needs. Nothing here is clever:
it prefers the Open Graph tags the page already publishes for exactly this
purpose, and falls back to the document itself.

  python3 link.py https://example.com/post
"""

import html, json, re, sys
import urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; ReleaseDesk/1.0; +local)"
MAX_EXCERPT = 600


class Reader(HTMLParser):
    """Collects the meta tags and the body paragraphs, skipping furniture."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.paras = []
        self._depth = 0
        self._p = None
        self._title = []
        self._in_title = False
        self._h1 = []
        self._in_h1 = False
        self.h1 = ""
        # Sections, in document order: an h2 and the paragraphs under it, up
        # to the next h2. What a page is actually made of, and the unit a post
        # about one part of it wants.
        self.sections = []
        self._h2 = None
        self._in_h2 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._depth += 1
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "link" and (a.get("rel") or "").lower() in ("image_src",):
            self.meta.setdefault("link:image", a.get("href", ""))
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self.h1:
            # Deliberately not gated on _depth. An article headline is very
            # often inside <header>, which is on the skip list because site
            # furniture lives there too, and skipping the headline to avoid
            # the navigation throws away the one line worth having.
            self._in_h1 = True
            self._h1 = []
        elif tag in ("h2", "h3") and not self._depth:
            self._in_h2 = True
            self._h2 = []
        elif tag == "p" and not self._depth:
            self._p = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._depth:
            self._depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.h1 = " ".join("".join(self._h1).split())
        elif tag in ("h2", "h3") and self._in_h2:
            self._in_h2 = False
            head = " ".join("".join(self._h2 or []).split())
            if head:
                self.sections.append(dict(heading=head, paras=[]))
            self._h2 = None
        elif tag == "p" and self._p is not None:
            text = " ".join("".join(self._p).split())
            if len(text) > 40:
                self.paras.append(text)
                if self.sections:
                    self.sections[-1]["paras"].append(text)
            self._p = None

    def handle_data(self, data):
        if self._in_h1:
            self._h1.append(data)
        if self._in_h2:
            self._h2.append(data)
        if self._in_title:
            self._title.append(data)
        elif self._p is not None and not self._depth:
            self._p.append(data)

    @property
    def title(self):
        return " ".join("".join(self._title).split())


def trim(text, limit=MAX_EXCERPT):
    """Cut on a word, and say so."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind(" ")] + "\u2026"


def fetch(url, timeout=20):
    """Returns title, image, excerpt and the canonical url."""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        final = r.geturl()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            raise RuntimeError(f"that is not a web page ({ctype or 'unknown type'})")
        raw = r.read(2_000_000)
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        enc = m.group(1)
    text = raw.decode(enc, errors="replace")

    p = Reader()
    try:
        p.feed(text)
    except Exception:
        pass
    g = p.meta

    image = (g.get("og:image") or g.get("twitter:image")
             or g.get("twitter:image:src") or g.get("link:image") or "")
    if image:
        image = urllib.parse.urljoin(final, html.unescape(image))

    # The page's own summary first, since that is what it publishes for this.
    excerpt = (g.get("og:description") or g.get("description")
               or g.get("twitter:description") or "").strip()
    if len(excerpt) < 120 and p.paras:
        excerpt = ""
        for para in p.paras:
            if len(excerpt) + len(para) > MAX_EXCERPT and excerpt:
                break
            excerpt = f"{excerpt}\n\n{para}".strip()
    if len(excerpt) > MAX_EXCERPT:
        cut = excerpt[:MAX_EXCERPT]
        excerpt = cut[:cut.rfind(" ")] + "…"

    # The h1 leads. og:title is written for search results and social cards
    # and is often a different, longer, keyword-shaped line; <title> usually
    # carries the site name bolted on the end. The h1 is the headline a reader
    # actually saw on the page, which is what a post about it should quote.
    return dict(url=g.get("og:url") or final,
                title=(p.h1 or g.get("og:title") or p.title or "").strip(),
                h1=p.h1, og_title=(g.get("og:title") or "").strip(),
                sections=[dict(heading=s["heading"],
                               text=trim(" ".join(s["paras"])))
                          for s in p.sections if s["paras"]],
                site=(g.get("og:site_name") or "").strip(),
                image=image, excerpt=excerpt)


if __name__ == "__main__":
    try:
        print(json.dumps(fetch(sys.argv[1]), indent=2))
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
