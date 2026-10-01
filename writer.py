#!/usr/bin/env python3
"""
Drafts copy for a slot, using Claude.

The desk cannot invent facts, so this hands the model exactly what a person
would be given: the campaign's source pack, the voice rules from SKILL.md, the
profile's own voice block, the angles already spent, the constraints of the
channel, and your description of the asset. Anything not in there is not
available to it, and it is told to say so rather than fill the space.

  python3 writer.py 29          draft one slot and print the result
"""

import json, os, re, sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
MODEL = "claude-opus-5"
# Guavy's own skill, and only that. There is deliberately no fallback to the
# user-level social-schedule skill: that one is the band's, and its voice
# section would sit beside Guavy's claims rules and contradict them.
SKILL = ROOT / ".claude" / "skills" / "guavy-post" / "SKILL.md"


def _read(p, default=""):
    try:
        return Path(p).read_text()
    except OSError:
        return default


def voice_rules():
    """The Voice section of Guavy's SKILL.md."""
    text = _read(SKILL)
    m = re.search(r"^## Voice\b.*?(?=^## )", text, re.S | re.M)
    return m.group(0).strip() if m else text


# A model call with no ceiling can hang, and an autopost that hangs holds the
# one-at-a-time lock until the desk is restarted. That is what wedged a run of
# eight posts after three.
CALL_TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT", "300"))


def client():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Put it in .env.")
    headers = {}
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if ws:
        # Org-scoped keys must name a workspace on every request.
        headers["anthropic-workspace-id"] = ws
    return anthropic.Anthropic(default_headers=headers or None,
                               timeout=CALL_TIMEOUT, max_retries=2)


SYSTEM = """You write social copy for Guavy's release desk. Guavy is a market
sentiment intelligence product.

Every factual claim you write must trace to something you were given. You have
two possible sources and both count:

- The source pack, which is the standing set of confirmed facts.
- The page this post is about, when one is included. Its words are published
  and are as good as the pack. If the brief is a page, the job is usually just
  to say what that page says, in this profile's voice, at the length the
  channel allows. That is a summary, not an investigation, and it does not
  need the source pack to corroborate it.

If neither gives you something, you do not know it, and you do not write it.
That includes numbers, dates, credits and names.

You are also given voice rules, and a claims block that is binding. Read it
before anything else and stay inside it.

Only set "thin": true when you genuinely have nothing: no page, and a source
pack that does not carry a fact worth a post. Do not set it merely because
the source pack is quiet while a page is present. A page with a heading and a
paragraph is enough to write from.

Reply with JSON only, no prose around it, in this shape:

{
  "copy": "the post text",
  "first_comment": "the link, or empty string",
  "hashtags": ["marketsentiment"],
  "tags": ["@handle"],
  "collaborators": ["@handle"],
  "thin": false,
  "why": "one line on the angle you took, or what is missing if thin"
}

hashtags carry no leading #. tags and collaborators carry the leading @, and
may only be handles listed in the brief. Never use em dashes."""


def brief(post, media, profile, channels, campaign_dir, page=None):
    """Everything the model is allowed to know about this one slot."""
    ch = (channels.get("channels") or {}).get(post["channel"]) or {}
    plat = ch.get("platform") or post["channel"].split("_")[0]
    pspec = (channels.get("platforms") or {}).get(plat) or {}
    v = profile.get("voice") or {}
    handles = (profile.get("handles") or {}).get(plat) or []

    link_policy = ("the link goes in first_comment, never in the body"
                   if ch.get("links_in_first_comment")
                   else "a link may go in the body")
    hashtag_policy = ("yes, close the post with them"
                      if plat in ("instagram", "facebook")
                      else "no hashtags on this platform")

    lines = [
        "## The slot", "",
        f"Channel: {post['channel_label']} ({plat})",
        f"Date and time: {post['date']} at {post['time']}",
        f"Phase: {post['phase']}",
        f"Character limit: {post['max_chars'] or 'none, but LinkedIn cuts at 210'}",
        f"Link policy: {link_policy}",
        f"Hashtags: {hashtag_policy}",
        f"Collaborators supported here: {'yes' if pspec.get('collab') else 'no'}",
    ]
    if ch.get("strict_duplicates"):
        lines.append("This account rejects copy close to anything already posted "
                     "on it. The angle must be its own.")
    if ch.get("angle"):
        lines.append(f"Angle for this channel: {ch['angle']}")

    lines += ["", "## The asset", ""]
    if media:
        lines += [f"File: {media['original']} ({media['kind']})",
                  f"What it is, as described on the desk: {media.get('note') or '(no description given)'}"]
    else:
        lines.append(f"No file attached. The slot expects: {post.get('asset_label') or 'unknown'}")

    if handles:
        lines += ["", "## Handles you may use on this platform", ""]
        lines += [f"- {h['handle']} — {h.get('name','')}, {h.get('role','')}"
                  f"{', collaborator-eligible' if h.get('collab') else ''}"
                  for h in handles]

    if v:
        lines += ["", "## This profile's voice", "", yaml.safe_dump(v, sort_keys=False).strip()]

    if page and (page.get("text") or "").strip():
        lines += ["", "## The page this post is about, in its own words", "",
                  f"Title: {page.get('title') or '(none)'}",
                  f"Link: {page.get('url') or '(none)'}", "",
                  page["text"].strip(), "",
                  "Those are published words. Summarising them is the job."]

    lines += ["", "## Voice rules", "", voice_rules(),
              "", "## Source pack, the standing facts", "",
              _read(campaign_dir / "source-pack.md", "(missing)"),
              "", "## Angles already spent", "",
              _read(ROOT / "posted.md", "(none yet)")]
    return "\n".join(lines)


SHORTEN_SYSTEM = """You cut a social post down to fit X.

The copy you are given has already been approved. Your job is to compress it,
not to rewrite it and not to add to it. Every fact in your version must appear
in the copy you were given. You may drop facts. You may not introduce one, and
you may not sharpen a claim into something the original did not say.

Keep the voice you are given. Keep the link exactly as it appears, character
for character. X counts any link as 23 characters however long it really is,
so the budget you are given is for everything else.

Return JSON and nothing else:
{"copy": "the short version, link included", "why": "one line on what you cut"}
"""


def x_budget(text, limit=280):
    """Characters left for words, once the links have taken their 23 each."""
    links = re.findall(r"https?://\S+", text or "")
    return limit - 23 * len(links) - (1 if links else 0), links


def shorten(copy, profile, limit=280):
    """Compress approved copy to fit X, keeping the link.

    A 280 character post is a different post, not a trimmed one, so this is
    asked for explicitly rather than run over everything on the way out.
    """
    copy = (copy or "").strip()
    if not copy:
        raise RuntimeError("There is no copy to shorten.")
    budget, links = x_budget(copy, limit)
    if budget < 20:
        raise RuntimeError("The links alone do not leave room for a post.")
    voice = (profile or {}).get("voice") or {}
    ask = [
        f"Voice rules for this profile:\n{json.dumps(voice, indent=1)}",
        voice_rules(),
        f"The approved copy:\n\n{copy}",
        f"\nWrite a version for X. It must include this link verbatim: "
        f"{links[0]}" if links else "\nWrite a version for X. There is no link.",
        f"You have about {budget} characters for the words, not counting the "
        f"link. Going under is fine. Going over is not.",
    ]
    c = client()
    kw = dict(model=MODEL, max_tokens=4000, system=SHORTEN_SYSTEM,
              messages=[{"role": "user", "content": "\n\n".join(ask)}])
    try:
        r = c.messages.create(thinking={"type": "adaptive"},
                              output_config={"effort": "medium"}, **kw)
    except TypeError:
        r = c.messages.create(**kw)
    if getattr(r, "stop_reason", None) == "refusal":
        raise RuntimeError("The model declined this one.")
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"No JSON came back: {text[:200]}")
    out = json.loads(m.group(0))
    out["model"] = MODEL
    return out


CARDS_SYSTEM = """You turn an approved social post into captions for the video.

The captions are read on a muted phone while photos go past, so they are short.
Two lines at most each, a handful of words a line. No sentences that need a
second reading, no punctuation doing clever work.

Every fact must already be in the copy you were given. You may leave things
out. You may not add a fact, a name, a number or a claim that is not there.

Keep the voice you are given. Write them as a sequence: the first one lands the
news, the ones after it add to it, the last one closes it.

Return JSON and nothing else:
{"cards": [{"text": "line one\nline two"}, ...], "why": "one line on the shape"}
"""


def caption_cards(copy, profile, n=3, seconds=None):
    """Short on-picture captions, drawn from the approved copy.

    The copy is written to be read at leisure under a post. A caption is read
    in three seconds over a photo, so this is a rewrite for a different job,
    not an extract.
    """
    copy = (copy or "").strip()
    if not copy:
        raise RuntimeError("There is no copy to work from.")
    n = max(1, min(int(n or 3), 6))
    voice = (profile or {}).get("voice") or {}
    ask = [
        f"Voice rules for this profile:\n{json.dumps(voice, indent=1)}",
        voice_rules(),
        f"The approved copy:\n\n{copy}",
        f"\nWrite {n} captions for the video.",
    ]
    if seconds:
        ask.append(f"The post runs about {round(float(seconds))} seconds, so "
                   f"each caption is on screen for roughly "
                   f"{round(float(seconds) / n)} of them.")
    ask.append("Leave the link out: a caption cannot be tapped.")
    c = client()
    kw = dict(model=MODEL, max_tokens=4000, system=CARDS_SYSTEM,
              messages=[{"role": "user", "content": "\n\n".join(ask)}])
    try:
        r = c.messages.create(thinking={"type": "adaptive"},
                              output_config={"effort": "medium"}, **kw)
    except TypeError:
        r = c.messages.create(**kw)
    if getattr(r, "stop_reason", None) == "refusal":
        raise RuntimeError("The model declined this one.")
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"No JSON came back: {text[:200]}")
    out = json.loads(m.group(0))
    out["cards"] = [{"text": (c.get("text") or "").strip()}
                    for c in (out.get("cards") or [])
                    if (c.get("text") or "").strip()]
    return out


def draft(post, media, profile, channels, campaign_dir, page=None):
    c = client()
    kw = dict(model=MODEL, max_tokens=16000, system=SYSTEM,
              messages=[{"role": "user", "content":
                         brief(post, media, profile, channels, campaign_dir,
                               page)}])
    try:
        r = c.messages.create(thinking={"type": "adaptive"},
                              output_config={"effort": "high"}, **kw)
    except TypeError:
        # Older SDK build without those parameters.
        r = c.messages.create(**kw)

    if getattr(r, "stop_reason", None) == "refusal":
        raise RuntimeError("The model declined this one.")
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"No JSON came back: {text[:200]}")
    out = json.loads(m.group(0))
    out["model"] = MODEL
    return out


if __name__ == "__main__":
    import sqlite3
    from push_zernio import load_env
    load_env()
    pid = int(sys.argv[1])
    con = sqlite3.connect(ROOT / "desk.db"); con.row_factory = sqlite3.Row
    p = dict(con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
    m = con.execute("SELECT * FROM media WHERE id=?", (p["media_id"],)).fetchone()
    profs = yaml.safe_load((ROOT / "profiles.yaml").read_text())["profiles"]
    chans = yaml.safe_load((ROOT / "channels.yaml").read_text())
    print(json.dumps(draft(p, dict(m) if m else None, profs[p["profile"]], chans,
                           ROOT / "campaigns" / p["campaign"]), indent=2))


SUGGEST_SYSTEM = """You suggest things Guavy could post. Not copy: ideas.

Guavy is a market sentiment intelligence product for people who build things:
developers, quant and fund teams, bot builders. You are given its source pack,
the angles already spent, and its voice block, whose claims rules are binding.
An idea that only works by breaking them is not an idea: no predictions, no
returns, no accuracy rates, nothing that reads as a recommendation.

Every idea must be something Guavy could actually post with what it has, or a
small ask you name plainly ("needs a screenshot of an MCP query"). Hang ideas
on real occasions where one fits: a day of the week that suits the content, a
date in the source pack. Do not invent customers, partners, press, figures or
anniversaries you cannot derive from what you were given.

Vary them. Some should be quick and cheap, some a proper piece. Some about what
the product does and how, some about reading the market as reported news, some
about a published study. Avoid repeating an angle already listed as spent.

Reply with JSON only: an array of exactly 10 objects, each:

{
  "title": "five to eight words, what the post is",
  "day": "when it would run, and why that day",
  "idea": "two or three sentences on what it actually is",
  "opening": "a possible first line, in their voice",
  "needs": "what they would have to shoot or find, or empty if they have it",
  "platforms": ["instagram"]
}"""


def suggest(profile_slug, profile, campaigns, channels, keeping=None):
    c = client()
    packs = []
    for name in campaigns:
        d = ROOT / "campaigns" / name
        packs.append(f"### {name}\n\n" + _read(d / "source-pack.md", "(none)"))

    import yaml as _y
    brief = "\n\n".join([
        "## Today", "", datetime.now().strftime("%A %d %B %Y"),
        "## The profile", "", _y.safe_dump(profile.get("voice") or {}, sort_keys=False),
        "## Voice rules", "", voice_rules(),
        "## Source packs", "", "\n\n".join(packs),
        "## Angles already spent", "", _read(ROOT / "posted.md", "(none yet)"),
    ] + ([
        "## Already on the board, kept for later", "",
        "These are frozen and are staying. Do not suggest them again, and do "
        "not suggest a near-duplicate of one.", "",
        "\n".join(f"- {k}" for k in keeping),
    ] if keeping else []))
    kw = dict(model=MODEL, max_tokens=16000, system=SUGGEST_SYSTEM,
              messages=[{"role": "user", "content": brief}])
    try:
        r = c.messages.create(thinking={"type": "adaptive"},
                              output_config={"effort": "high"}, **kw)
    except TypeError:
        r = c.messages.create(**kw)
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError(f"No JSON came back: {text[:200]}")
    return json.loads(m.group(0))


PICK_SYSTEM = """You choose which market story a sentiment-intelligence product
should post about today.

The shortlist was ranked by how strong its sentiment is and how far it
travelled. Those measure tone and reach, not importance, so the top of it is
usually a mix of real market news and things that merely mention a company. Your
job is the judgement that ranking cannot make.

Pick the one story that is most material to the instrument as an investment:
something that changes what the company or asset is worth, what it earns, what
it is allowed to do, or what it costs to make. Earnings, contracts, regulatory
decisions, supply shocks, guidance, approvals, defaults, macro data.

Reject, however high it scored:
- a story where the company is incidental: a sponsor, a venue, a payment
  option, a presale partner, a brand that happens to appear
- entertainment, sport, celebrity or consumer-interest news
- a story about a company that is not the instrument it was filed under
- anything whose only substance is that a price moved
- a near-duplicate of another story on the list. Several outlets cover one
  event; pick the fullest telling and say the others were the same story.
- anything covering the same event, company or announcement as a headline
  under "Already posted". A different outlet, a different angle or a later
  update of something already posted is still the same story. Reject it
  even if it is the strongest thing on the list.

Prefer specific and consequential over loud. A modest story with a real number
in it beats a dramatic one without.

Reply with JSON only:

{
  "article_id": "the id you chose",
  "why": "one sentence on what makes it material",
  "duplicates": ["ids covering the same event, if any"],
  "rejected": [{"article_id": "...", "why": "one short phrase"}],
  "none": false,
  "best_available": "the id you would pick if you had to, or empty"
}

Set "none": true and leave article_id empty if nothing on the list is a real
market story. An empty slot costs nothing; a post about a concert presale
costs the account's credibility.

Even when you set "none", name the least bad candidate in "best_available":
the one closest to a real market story, never one that is plainly
entertainment, sport or a sponsor mention, and never one covering something
already posted. A person asking by hand may want
one anyway. Leave it empty only if every candidate is that kind of story."""


def pick(briefs, market, profile=None, posted=None):
    """Choose the one story worth posting, out of a shortlist.

    The shortlist is already the strongest by score. What is left is whether
    each one actually matters, which is a reading job rather than a sorting
    one.
    """
    if not briefs:
        return {}
    c = client()
    lines = []
    for b in briefs:
        imp = b.get("impacted") or []
        lines.append(
            f"- id: {b.get('article_id')}\n"
            f"  symbol: {b.get('symbol')}   sentiment: {b.get('sentiment')}   "
            f"clout: {b.get('clout')}   impacted: {imp}\n"
            f"  title: {b.get('title')}\n"
            f"  body: {(b.get('body') or '')[:400]}")

    v = (profile or {}).get("voice") or {}
    brief = "\n\n".join([
        f"## The market\n\n{market}",
        "## What the voice rules will not let us say", "",
        (v.get("claims") or "(none written)")[:1500],
        "## Already posted in the last 48 hours", "",
        "\n".join(f"- {t}" for t in (posted or [])) or "(nothing yet)",
        "## The shortlist", "", "\n\n".join(lines),
    ])
    kw = dict(model=MODEL, max_tokens=4000, system=PICK_SYSTEM,
              messages=[{"role": "user", "content": brief}])
    try:
        r = c.messages.create(thinking={"type": "adaptive"},
                              output_config={"effort": "high"}, **kw)
    except TypeError:
        r = c.messages.create(**kw)
    text = "".join(x.text for x in r.content if x.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}
