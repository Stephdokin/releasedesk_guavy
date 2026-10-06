#!/usr/bin/env python3
"""
Guavynator: the local approval desk for Guavy.

  pip install flask pyyaml
  python3 app.py            ->  http://127.0.0.1:5000

Source of truth is desk.db. The allocator proposes slots; nothing leaves this
machine until a post is approved here, the desk is armed with the Live button,
and it is explicitly pushed.
"""

import hashlib, json, mimetypes, os, re, shutil, smtplib, socket, sqlite3, \
       random, subprocess, sys, tempfile, threading, time, urllib.parse, \
       urllib.request
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from pathlib import Path

try:                                  # 3.9 has it; keep the desk running if not
    from zoneinfo import ZoneInfo
except ImportError:                   # pragma: no cover
    ZoneInfo = None

import yaml
from flask import Flask, abort, g, jsonify, request, Response, send_file
from werkzeug.utils import secure_filename

from push_zernio import load_env

ROOT = Path(__file__).parent
DB = ROOT / "desk.db"
MEDIA = ROOT / "media"           # the library. Durable, local, never public.
# Where a published video is dropped so it can be uploaded by hand. Personal
# Facebook profiles are not reachable from any API, so that upload is manual
# and the file has to be somewhere findable.
DOWNLOADS = Path(os.environ.get("DOWNLOAD_DIR") or (Path.home() / "Downloads"))
PROFILES = ROOT / "profiles.yaml"

# Big enough for video. Werkzeug spools to disk, so this is not memory.
MAX_UPLOAD = 2 * 1024 * 1024 * 1024

# Campaign inks. Order is stable so a campaign keeps its colour.
INKS = ["#E0A33E", "#C4634A", "#7FA07A", "#6E8CA8", "#B08BBA", "#D08C5E"]

LOCKED = ("drafted", "approved", "rejected", "scheduled", "published", "failed")

load_env()                       # .env -> os.environ, before anything reads it

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
app.json.sort_keys = False        # profiles keep profiles.yaml order


# ---------------------------------------------------------------- profiles

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def profiles():
    """Read profiles.yaml on every call so edits land without a restart."""
    if not PROFILES.exists():
        return {}
    try:
        return (yaml.safe_load(PROFILES.read_text()) or {}).get("profiles") or {}
    except yaml.YAMLError:
        return {}


def account_owner():
    """Zernio account id -> the profile that owns it."""
    out = {}
    for slug, p in profiles().items():
        z = ((p or {}).get("zernio") or {})
        for chan, acct in (z.get("accounts") or {}).items():
            if acct in (None, "", "TODO"):
                continue
            out[acct] = dict(slug=slug, label=(p or {}).get("label") or slug)
    return out


def channel_account(profile, channel):
    """The Zernio account a channel points at, as that profile names it."""
    z = ((profiles().get(profile) or {}).get("zernio") or {})
    acct = (z.get("accounts") or {}).get(channel)
    return None if acct in (None, "", "TODO") else acct


def account_ids(profile):
    """Every Zernio account this profile owns."""
    z = ((profiles().get(profile) or {}).get("zernio") or {})
    return {a for c, a in (z.get("accounts") or {}).items()
            if a not in (None, "", "TODO")}


def channel_spec(cfg, profile, channel):
    """A channel's settings as this profile sees them.

    channels.yaml describes the channel; a profile may lean on it differently.
    Anything in that profile's channel_overrides wins.
    """
    base = dict((cfg.get("channels") or {}).get(channel) or {})
    over = (((profiles().get(profile) or {}).get("channel_overrides") or {})
            .get(channel) or {})
    base.update(over)
    return base


def platform_alias(cfg=None):
    """Their name for a platform -> ours. Zernio says twitter, we say x."""
    cfg = cfg or channels_cfg()
    out = {}
    for key, spec in (cfg.get("platforms") or {}).items():
        for alt in ((spec or {}).get("aliases") or []):
            out[alt] = key
    return out


def channels_cfg():
    try:
        return yaml.safe_load((ROOT / "channels.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def campaign_profiles():
    """campaign slug -> profile slug, read from each campaign.yaml."""
    out = {}
    for f in sorted((ROOT / "campaigns").glob("*/campaign.yaml")):
        try:
            c = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        out[c.get("name") or f.parent.name] = (c.get("profile") or c.get("entity")
                                               or slug(c.get("artist")))
    return out


def default_collaborators(cfg, profile, channel):
    """Who a post on this channel co-authors with by default.

    Only on platforms that actually have a co-author feature: a collaborator on
    a platform without one is silently dropped, which looks like it worked.
    """
    ch = (cfg.get("channels") or {}).get(channel) or {}
    plat = ch.get("platform") or channel.split("_")[0]
    if not ((cfg.get("platforms") or {}).get(plat) or {}).get("collab"):
        return []
    return ((profiles().get(profile) or {}).get("collaborate_with") or {}).get(plat) or []


def stamp_profiles(con):
    """Fill posts.profile for any row that predates the column."""
    for camp, prof in campaign_profiles().items():
        con.execute("UPDATE posts SET profile=? WHERE campaign=? "
                    "AND (profile IS NULL OR profile='')", (prof, camp))


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id            INTEGER PRIMARY KEY,
  slot_key      TEXT UNIQUE NOT NULL,   -- date|channel|campaign|asset
  date          TEXT NOT NULL,
  time          TEXT NOT NULL,
  channel       TEXT NOT NULL,
  channel_label TEXT NOT NULL,
  campaign      TEXT NOT NULL,
  profile       TEXT,
  phase         TEXT NOT NULL,
  asset         TEXT,
  asset_label   TEXT,
  asset_type    TEXT,
  media_url     TEXT,
  media_id      INTEGER,
  media_ids     TEXT,          -- ordered, for a carousel
  copy          TEXT,
  first_comment TEXT,
  tags          TEXT,
  collaborators TEXT,
  hashtags      TEXT,
  group_id      TEXT,          -- one asset posted across several accounts
  why           TEXT,          -- the angle the draft took, or why it is thin
  state         TEXT NOT NULL DEFAULT 'allocated',
  needs_signoff INTEGER DEFAULT 0,   -- unused, kept so older desk.db files load
  signed_off    INTEGER DEFAULT 0,   -- unused, as above
  handed_off    TEXT,
  max_chars     INTEGER,
  zernio_id     TEXT,
  post_url      TEXT,
  stats         TEXT,          -- engagement, copied back from Zernio
  note          TEXT,
  updated       TEXT
);
CREATE INDEX IF NOT EXISTS idx_date ON posts(date);
CREATE INDEX IF NOT EXISTS idx_state ON posts(state);

-- Somewhere to put things. Per profile, like the media itself, and only one
-- level deep: this is a shelf, not a filing system.
-- One row per ad. The picture is a media row on the ads shelf; the words are
-- here, because they are what gets posted, and they come from the ad's own
-- manifest rather than from anyone writing copy.
CREATE TABLE IF NOT EXISTS ads (
  id             INTEGER PRIMARY KEY,
  profile        TEXT NOT NULL,
  media_id       INTEGER,
  property       TEXT,
  property_name  TEXT,
  variation      INTEGER,
  audience       TEXT,
  headline       TEXT,
  short_headline TEXT,
  sub            TEXT,
  cta            TEXT,
  url            TEXT,
  file           TEXT,
  active         INTEGER NOT NULL DEFAULT 1,
  added          TEXT,
  UNIQUE (profile, property, variation)
);

-- Every size an ad was made in. The zip can carry several per ad, and each
-- channel posts the size chosen for it in the ads pacing table.
CREATE TABLE IF NOT EXISTS ad_sizes (
  id       INTEGER PRIMARY KEY,
  ad_id    INTEGER NOT NULL,
  size     TEXT NOT NULL,
  width    INTEGER,
  height   INTEGER,
  media_id INTEGER NOT NULL,
  UNIQUE (ad_id, size)
);

CREATE TABLE IF NOT EXISTS folders (
  id      INTEGER PRIMARY KEY,
  profile TEXT NOT NULL,
  name    TEXT NOT NULL,
  created TEXT,
  UNIQUE (profile, name)
);

CREATE TABLE IF NOT EXISTS media (
  id       INTEGER PRIMARY KEY,
  profile  TEXT NOT NULL,
  campaign TEXT,
  path     TEXT NOT NULL,          -- relative to media/
  original TEXT NOT NULL,
  kind     TEXT NOT NULL,          -- image | video
  mime     TEXT,
  bytes    INTEGER,
  sha256   TEXT NOT NULL,          -- same file twice on one profile is one row
  width    INTEGER,
  height   INTEGER,
  seconds  REAL,
  note     TEXT,
  url      TEXT,                   -- the page this came from, if it is a link
  title    TEXT,
  caption  TEXT,                   -- words drawn on the picture, if any
  folder_id INTEGER,               -- null is loose, and loose is fine
  made_from TEXT,                  -- the files a render was built out of
  source   TEXT,                   -- drop | paste | phone | link
  added    TEXT,
  -- Per profile, not globally. The same song belongs to both profiles, and
  -- each keeps its own copy.
  UNIQUE (profile, sha256)
);
CREATE INDEX IF NOT EXISTS idx_media_profile ON media(profile);

CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);

-- People added from the compose box. profiles.yaml stays the curated list;
-- these are the ones picked up in passing, merged in on read.
CREATE TABLE IF NOT EXISTS handles (
  profile  TEXT NOT NULL,
  platform TEXT NOT NULL,
  handle   TEXT NOT NULL,
  name     TEXT,
  role     TEXT,
  collab   INTEGER DEFAULT 1,
  tag      INTEGER DEFAULT 1,
  added    TEXT,
  PRIMARY KEY (profile, platform, handle)
);

-- Zernio reports a follower count but keeps no history, so the desk keeps its
-- own. One row per account per day; growth only becomes visible from the first
-- day this ran.
CREATE TABLE IF NOT EXISTS followers (
  day        TEXT NOT NULL,
  account_id TEXT NOT NULL,
  platform   TEXT,
  name       TEXT,
  profile    TEXT,
  n          INTEGER,
  PRIMARY KEY (day, account_id)
);

-- The blog index, read from guavy.com/blog/feed.json. A blog behaves like an
-- upload in the UI, but it has no file, no hash and is neither image nor
-- video, so it does not belong in `media`. Keyed by slug, because that is the
-- one part of a blog's identity the site will not change.
--
-- `postable` is not about whether the blog is good. It is about whether its
-- own words can go in a post without breaking voice.claims. The site was
-- written before those rules were, and two of the blogs say things the desk is
-- no longer allowed to repeat.
-- A local mirror of the Wire. The API has no count endpoint and no "what is
-- new since" parameter, so answering "how many articles today" by asking means
-- reading the whole feed every time, which on a metered API is paying again
-- and again for something that only changes at the edges.
--
-- So the desk keeps its own copy and tops it up. A sync reads each symbol
-- until it meets an article it already holds: one page on a quiet symbol, a
-- few on a busy one. After that every count is a SQL query.
--
-- article_id is the key, so one story reaching four symbols is one row. The
-- symbols it touched live in brief_symbols, which is how a per-ticker count
-- can still count it once for each.
CREATE TABLE IF NOT EXISTS briefs (
  article_id  TEXT PRIMARY KEY,
  market      TEXT NOT NULL,
  title       TEXT,
  body        TEXT,
  day         TEXT,
  ts          INTEGER,          -- epoch milliseconds, as the Wire gives it
  clout       REAL,
  sentiment   REAL,
  speculation REAL,
  fud_fomo    REAL,
  bias        TEXT,
  tone        TEXT,
  impacted    TEXT,             -- json, as it arrived
  seen        TEXT              -- when this desk first saw it
);
CREATE INDEX IF NOT EXISTS idx_briefs_ts ON briefs (market, ts);

CREATE TABLE IF NOT EXISTS brief_symbols (
  article_id TEXT NOT NULL,
  market     TEXT NOT NULL,
  symbol     TEXT NOT NULL,
  PRIMARY KEY (article_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_bs_symbol ON brief_symbols (market, symbol);

-- How far each symbol has been read. Needed because "this article is already
-- held" does not mean "this symbol is caught up": one story reaches dozens of
-- coins, so BTC's feed is full of articles first seen under someone else's.
-- Stopping at the first familiar article left BTC with two rows out of a
-- hundred. The watermark is per symbol, so each feed is read until it reaches
-- ground it has actually covered.
CREATE TABLE IF NOT EXISTS symbol_sync (
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  last_ts INTEGER NOT NULL DEFAULT 0,
  updated TEXT,
  PRIMARY KEY (market, symbol)
);

-- Suggestions used to live only in the browser, so asking for ten more threw
-- away the ones you wanted to keep. A frozen row survives a regeneration and
-- is named to the writer so it does not simply suggest it again.
CREATE TABLE IF NOT EXISTS suggestions (
  id        INTEGER PRIMARY KEY,
  profile   TEXT NOT NULL,
  title     TEXT,
  idea      TEXT,
  opening   TEXT,
  needs     TEXT,
  day       TEXT,
  platforms TEXT,          -- json list
  frozen    INTEGER NOT NULL DEFAULT 0,
  added     TEXT
);

CREATE TABLE IF NOT EXISTS blogs (
  id        INTEGER PRIMARY KEY,
  slug      TEXT UNIQUE NOT NULL,
  url       TEXT NOT NULL,
  title     TEXT NOT NULL,
  summary   TEXT,
  image     TEXT,
  published TEXT,
  postable  INTEGER NOT NULL DEFAULT 1,
  why_not   TEXT,
  added     TEXT
);
"""

# "entity" was renamed to "profile" once it was clear the word matched Zernio's
# own team -> profile -> account model. Applied once, to databases predating it.
RENAMED_COLUMNS = [
    ("posts", "entity", "profile"),
    ("media", "entity", "profile"),
]

# Columns added after the first release. Applied in order, once each.
ADDED_COLUMNS = [
    # Folders belong to a shelf. The ads tab keeps its own, one per group of
    # ads, and the media tab never sees them, nor they its.
    ("folders", "bucket", "ALTER TABLE folders ADD COLUMN bucket TEXT "
                          "NOT NULL DEFAULT 'library'"),
    # Ads live in the same library as everything else, on their own shelf.
    # A separate table would have meant a second copy of uploads, folders,
    # selection, the views, the compose path and the push path, all to hold
    # the same kind of file.
    ("media", "bucket", "ALTER TABLE media ADD COLUMN bucket TEXT "
                        "NOT NULL DEFAULT 'library'"),
    ("posts", "profile", "ALTER TABLE posts ADD COLUMN profile TEXT"),
    ("posts", "media_id", "ALTER TABLE posts ADD COLUMN media_id INTEGER"),
    ("posts", "tags", "ALTER TABLE posts ADD COLUMN tags TEXT"),
    ("posts", "collaborators", "ALTER TABLE posts ADD COLUMN collaborators TEXT"),
    ("posts", "handed_off", "ALTER TABLE posts ADD COLUMN handed_off TEXT"),
    ("posts", "hashtags", "ALTER TABLE posts ADD COLUMN hashtags TEXT"),
    ("posts", "group_id", "ALTER TABLE posts ADD COLUMN group_id TEXT"),
    ("posts", "why", "ALTER TABLE posts ADD COLUMN why TEXT"),
    ("media", "width", "ALTER TABLE media ADD COLUMN width INTEGER"),
    ("media", "height", "ALTER TABLE media ADD COLUMN height INTEGER"),
    ("media", "seconds", "ALTER TABLE media ADD COLUMN seconds REAL"),
    ("media", "url", "ALTER TABLE media ADD COLUMN url TEXT"),
    ("media", "title", "ALTER TABLE media ADD COLUMN title TEXT"),
    ("posts", "post_url", "ALTER TABLE posts ADD COLUMN post_url TEXT"),
    ("posts", "stats", "ALTER TABLE posts ADD COLUMN stats TEXT"),
    ("posts", "media_ids", "ALTER TABLE posts ADD COLUMN media_ids TEXT"),
    # The words drawn on the picture. Kept on the file rather than the post,
    # so the same photo carries its caption into every post it appears in.
    ("media", "caption", "ALTER TABLE media ADD COLUMN caption TEXT"),
    ("media", "folder_id", "ALTER TABLE media ADD COLUMN folder_id INTEGER"),
    # YouTube will not take a video without one, so every post carries a
    # headline whether or not it is going anywhere that needs it.
    ("posts", "title", "ALTER TABLE posts ADD COLUMN title TEXT"),
    # What a rendered video was made out of. Kept on the render, not on the
    # post: media_ids is what goes out, and push_zernio uploads every id in
    # it, so the source photos cannot live there.
    ("media", "made_from", "ALTER TABLE media ADD COLUMN made_from TEXT"),
]


def rebuild_media_unique(con):
    """Move the media table from UNIQUE(sha256) to UNIQUE(profile, sha256).

    SQLite cannot alter a constraint, so the table is rebuilt. Uploading the
    same file to a second profile was silently refused before this.
    """
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
    ).fetchone()
    if not row or "UNIQUE (profile, sha256)" in row[0]:
        return
    cols = [r[1] for r in con.execute("PRAGMA table_info(media)")]
    con.execute("ALTER TABLE media RENAME TO media_old")
    con.executescript(SCHEMA)
    keep = [c for c in cols
            if c in {r[1] for r in con.execute("PRAGMA table_info(media)")}]
    names = ", ".join(keep)
    con.execute(f"INSERT INTO media ({names}) SELECT {names} FROM media_old")
    con.execute("DROP TABLE media_old")
    con.commit()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        # Renames run before the schema script, because the script creates an
        # index on the new column name and would fail against an old database.
        for table, was, now in RENAMED_COLUMNS:
            cols = {r[1] for r in g.db.execute(f"PRAGMA table_info({table})")}
            if was in cols and now not in cols:
                g.db.execute(f"ALTER TABLE {table} RENAME COLUMN {was} TO {now}")
        g.db.execute("DROP INDEX IF EXISTS idx_media_entity")
        rebuild_media_unique(g.db)
        g.db.executescript(SCHEMA)
        for table, col, ddl in ADDED_COLUMNS:
            if col not in {r[1] for r in g.db.execute(f"PRAGMA table_info({table})")}:
                g.db.execute(ddl)
        stamp_profiles(g.db)
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d:
        d.close()


def touch(row_id):
    db().execute("UPDATE posts SET updated=? WHERE id=?",
                 (datetime.now().isoformat(timespec="seconds"), row_id))


# ---------------------------------------------------------------- reads

@app.get("/api/posts")
def posts():
    q = "SELECT * FROM posts WHERE 1=1"
    p = []
    for field, arg in (("date >= ?", "from"), ("date <= ?", "to"),
                       ("state = ?", "state"), ("campaign = ?", "campaign"),
                       ("channel = ?", "channel")):
        v = request.args.get(arg)
        if v:
            q += f" AND {field}"
            p.append(v)
    q += " ORDER BY date, time, channel"
    rows = [dict(r) for r in db().execute(q, p)]

    only = request.args.get("profile")
    if only:
        rows = [r for r in rows if r["profile"] == only]
    return jsonify(rows)


@app.get("/api/summary")
def summary():
    """Everything the overview needs, scoped to one profile if asked."""
    con = db()
    prof = request.args.get("profile")
    w, p = ("WHERE profile = ?", [prof]) if prof else ("", [])

    camps = [r[0] for r in con.execute(
        f"SELECT DISTINCT campaign FROM posts {w} ORDER BY campaign", p)]
    ink = {c: INKS[i % len(INKS)] for i, c in enumerate(camps)}

    weeks = {}
    for r in con.execute(f"SELECT date, campaign FROM posts {w}", p):
        d = date.fromisoformat(r["date"])
        wk = date.fromordinal(d.toordinal() - d.weekday()).isoformat()
        weeks.setdefault(wk, {}).setdefault(r["campaign"], 0)
        weeks[wk][r["campaign"]] += 1

    states = dict(con.execute(
        f"SELECT state, COUNT(*) FROM posts {w} GROUP BY state", p).fetchall())
    by_chan = [dict(channel=r[0], label=r[1], n=r[2]) for r in con.execute(
        f"""SELECT channel, channel_label, COUNT(*) FROM posts {w}
            GROUP BY channel ORDER BY COUNT(*) DESC""", p)]
    by_camp = [dict(campaign=r[0], n=r[1], ink=ink[r[0]]) for r in con.execute(
        f"SELECT campaign, COUNT(*) FROM posts {w} GROUP BY campaign", p)]

    def one(extra):
        j = "AND" if w else "WHERE"
        return con.execute(f"SELECT COUNT(*) FROM posts {w} {j} {extra}", p).fetchone()[0]

    return jsonify(ink=ink, weeks=weeks, states=states, by_channel=by_chan,
                   by_campaign=by_camp,
                   waiting=one("state='drafted'"),
                   ready=one("state='allocated' AND media_id IS NOT NULL"),
                   unwritten=one("state='allocated' AND media_id IS NULL"),
                   media=con.execute(
                       "SELECT COUNT(*) FROM media" + (" WHERE profile=?" if prof else ""),
                       [prof] if prof else []).fetchone()[0])


# ---------------------------------------------------------------- writes

@app.patch("/api/posts/<int:pid>")
def edit(pid):
    body = request.get_json(force=True)
    fields = {k: v for k, v in body.items()
              if k in ("copy", "first_comment", "media_url", "media_id",
                       "note", "date", "time", "title")}
    # Handle lists arrive as arrays and are stored as JSON.
    for k in ("tags", "collaborators", "hashtags"):
        if k in body:
            fields[k] = json.dumps(body[k] or [])
    if not fields:
        return jsonify(error="nothing to change"), 400
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), pid))
    # writing copy moves an untouched slot into review
    db().execute("""UPDATE posts SET state='drafted'
                    WHERE id=? AND state='allocated' AND copy IS NOT NULL
                    AND TRIM(copy) != ''""", (pid,))
    touch(pid)
    db().commit()
    return jsonify(dict(db().execute(
        "SELECT * FROM posts WHERE id=?", (pid,)).fetchone()))


@app.post("/api/posts/<int:pid>/draft")
def draft_post(pid):
    """Write the copy for one slot, with Claude, from the source pack."""
    import writer
    con = db()
    row = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="no such post"), 404
    if row["state"] in ("scheduled", "published"):
        return jsonify(error="Already sent. Nothing to rewrite."), 400

    m = None
    if row["media_id"]:
        r = con.execute("SELECT * FROM media WHERE id=?", (row["media_id"],)).fetchone()
        m = dict(r) if r else None
    prof = profiles().get(row["profile"]) or {}
    try:
        out = writer.draft(dict(row), m, prof, channels_cfg(),
                           ROOT / "campaigns" / row["campaign"])
    except Exception as e:                      # surfaced to the browser as-is
        return jsonify(error=f"{type(e).__name__}: {e}"[:600]), 502

    con.execute("""UPDATE posts SET copy=?, first_comment=?, hashtags=?, tags=?,
                   collaborators=?, why=?, updated=? WHERE id=?""",
                (out.get("copy", ""), out.get("first_comment") or "",
                 json.dumps(out.get("hashtags") or []),
                 json.dumps(out.get("tags") or []),
                 json.dumps(out.get("collaborators") or []),
                 out.get("why") or "",
                 datetime.now().isoformat(timespec="seconds"), pid))
    con.execute("""UPDATE posts SET state='drafted' WHERE id=? AND state='allocated'
                   AND TRIM(COALESCE(copy,'')) != ''""", (pid,))
    con.commit()
    return jsonify(thin=bool(out.get("thin")), why=out.get("why") or "",
                   post=dict(con.execute("SELECT * FROM posts WHERE id=?",
                                         (pid,)).fetchone()))


@app.post("/api/posts/<int:pid>/<action>")
def decide(pid, action):
    if action not in ("approve", "reject", "reset"):
        return jsonify(error="unknown action"), 400
    row = db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="no such post"), 404
    if action == "approve":
        if not (row["copy"] or "").strip():
            return jsonify(error="Write the copy before approving."), 400
        if row["state"] in ("scheduled", "published"):
            return jsonify(error="Already sent to Zernio."), 400
        new = "approved"
    elif action == "reject":
        new = "rejected"
    else:
        new = "drafted" if (row["copy"] or "").strip() else "allocated"
    db().execute("UPDATE posts SET state=? WHERE id=?", (new, pid))
    touch(pid)
    db().commit()
    return jsonify(state=new)


# ---------------------------------------------------------------- live gate

def setting(key, default=None):
    r = db().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(key, value):
    db().execute("INSERT INTO settings (k,v) VALUES (?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
    db().commit()


@app.get("/api/defaults")
def defaults_get():
    """Preferences that outlive one post."""
    return jsonify(transition=setting("transition", "fade"),
                   fit_mode=setting("fit_mode", "blur"))


@app.post("/api/defaults")
def defaults_set():
    import slideshow as sl
    b = request.get_json(force=True)
    if "transition" in b:
        v = (b["transition"] or "fade").lower()
        set_setting("transition", v if v in sl.TRANSITIONS else "fade")
    if "fit_mode" in b:
        v = (b["fit_mode"] or "blur").lower()
        set_setting("fit_mode", v if v in ("blur", "fill", "auto") else "blur")
    return defaults_get()


# Zernio's list endpoints are account-wide, not per profile: the desk fetches
# everything and filters afterwards. So the Published tab, the Queued tab and a
# sync all pull the same two lists, and switching profile pulls them again to
# filter them differently. Held briefly, that repetition costs nothing.
ZERNIO_TTL = 90          # seconds
_ZCACHE = {}             # path -> (fetched at, rows)


def zernio_all(path, force=False, ttl=ZERNIO_TTL):
    """Every page of a Zernio list, reusing the last answer if it is recent.

    `force` is for when you have asked for the truth on purpose: pressing Sync,
    or Try again. Never for a tab that merely got opened.
    """
    import push_zernio as pz
    now = time.monotonic()
    hit = _ZCACHE.get(path)
    if hit and not force and (now - hit[0]) < ttl:
        return hit[1]
    rows = pz.api_all(path)
    _ZCACHE[path] = (now, rows)
    return rows


def zernio_age(path):
    """How old the held copy is, in seconds, or None if there is not one."""
    hit = _ZCACHE.get(path)
    return round(time.monotonic() - hit[0]) if hit else None


def zernio_forget():
    _ZCACHE.clear()


def is_live():
    r = db().execute("SELECT v FROM settings WHERE k='live'").fetchone()
    return bool(r and r["v"] == "1")


@app.get("/api/live")
def live_get():
    return jsonify(live=is_live(), key=bool(os.environ.get("ZERNIO_API_KEY")))


@app.post("/api/live")
def live_set():
    want = bool((request.get_json(silent=True) or {}).get("live"))
    if want and not os.environ.get("ZERNIO_API_KEY"):
        return jsonify(error="No ZERNIO_API_KEY, so there is nothing to go live "
                             "against."), 400
    db().execute("INSERT INTO settings (k,v) VALUES ('live',?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("1" if want else "0",))
    db().commit()
    return jsonify(live=want)


@app.post("/api/push")
def push():
    """Dry run by default. With confirm, runs push_zernio.py and returns its output."""
    body = request.get_json(silent=True) or {}
    q, p = "SELECT * FROM posts WHERE state='approved'", []
    if body.get("id"):
        q += " AND id=?"
        p.append(int(body["id"]))
    rows = db().execute(q + " ORDER BY date, time", p).fetchall()

    if not body.get("confirm"):
        return jsonify(dry_run=True, count=len(rows),
                       posts=[dict(id=r["id"], date=r["date"], channel=r["channel"],
                                   campaign=r["campaign"]) for r in rows])
    if not rows:
        return jsonify(error="Nothing approved to push."), 400
    if not os.environ.get("ZERNIO_API_KEY"):
        return jsonify(error="ZERNIO_API_KEY is not set. Put it in .env and "
                             "restart the desk."), 400

    cmd = [sys.executable, "push_zernio.py", "push"]
    if body.get("id"):
        cmd += ["--id", str(int(body["id"]))]
    if body.get("soon"):
        cmd += ["--soon"]
    # Not live means never send. The command still runs, so you see the exact
    # payload that would have gone, but --dry-run makes it impossible to post.
    live = is_live()
    if not live:
        cmd += ["--dry-run"]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return jsonify(error="Push timed out. Check Zernio before retrying."), 504
    return jsonify(ok=r.returncode == 0, live=live,
                   output=((r.stdout or "") + (r.stderr or ""))[-4000:])


# ---------------------------------------------------------------- config API

@app.get("/api/profiles")
def api_profiles():
    """profiles.yaml, with anything added from the compose box folded in."""
    out = profiles()
    for r in db().execute("SELECT * FROM handles ORDER BY name, handle"):
        p = out.get(r["profile"])
        if not p:
            continue
        book = p.setdefault("handles", {}) or {}
        p["handles"] = book
        lst = book.setdefault(r["platform"], []) or []
        book[r["platform"]] = lst
        if any((h or {}).get("handle") == r["handle"] for h in lst):
            continue
        lst.append(dict(handle=r["handle"], name=r["name"] or r["handle"],
                        role=r["role"] or "", collab=bool(r["collab"]),
                        tag=bool(r["tag"]), added_here=True))
    return jsonify(out)


@app.post("/api/handles")
def add_handle():
    b = request.get_json(force=True)
    handle = (b.get("handle") or "").strip()
    platform = (b.get("platform") or "").strip()
    profile = (b.get("profile") or "").strip()
    if not handle or not platform or profile not in profiles():
        return jsonify(error="Need a handle, a platform and a profile."), 400
    if platform in ("instagram", "tiktok", "x") and not handle.startswith("@"):
        handle = "@" + handle
    db().execute(
        """INSERT INTO handles (profile,platform,handle,name,role,collab,tag,added)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(profile,platform,handle) DO UPDATE SET name=excluded.name""",
        (profile, platform, handle, (b.get("name") or handle).strip(),
         (b.get("role") or "").strip(), 1, 1,
         datetime.now().isoformat(timespec="seconds")))
    db().commit()
    return jsonify(added=handle, platform=platform)


@app.delete("/api/handles")
def del_handle():
    b = request.get_json(force=True)
    db().execute("DELETE FROM handles WHERE profile=? AND platform=? AND handle=?",
                 (b.get("profile"), b.get("platform"), b.get("handle")))
    db().commit()
    return jsonify(deleted=b.get("handle"))


@app.get("/api/accounts")
def api_accounts():
    """Every connected account, with whether it is ticked by default and
    whether there is actually a free slot to put something in."""
    cfg = channels_cfg()
    chans = cfg.get("channels") or {}
    plats = cfg.get("platforms") or {}
    today = date.today().isoformat()
    only = request.args.get("profile")
    out = []
    for prof, pdata in profiles().items():
        if only and prof != only:
            continue
        z = (pdata or {}).get("zernio") or {}
        defaults = set((pdata or {}).get("post_by_default") or [])
        for chan, acct in (z.get("accounts") or {}).items():
            if acct in (None, "", "TODO"):
                continue
            ch = chans.get(chan) or {}
            plat = ch.get("platform") or chan.split("_")[0]
            free = db().execute(
                """SELECT COUNT(*) FROM posts WHERE profile=? AND channel=?
                   AND state='allocated' AND media_id IS NULL AND date >= ?""",
                (prof, chan, today)).fetchone()[0]
            out.append(dict(
                profile=prof,
                profile_label=(pdata or {}).get("label") or prof,
                channel=chan, platform=plat, account_id=acct,
                # The channel's own label first: two X accounts both called
                # "X" in the post sheet is no way to choose between them.
                label=ch.get("label") or (plats.get(plat) or {}).get("label") or plat,
                ink=(plats.get(plat) or {}).get("ink", "#8A8F86"),
                delivery=ch.get("delivery") or "zernio",
                free_slots=free, on=chan in defaults))
    return jsonify(out)


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def next_open_time(cfg, channel, taken, after=None, profile=None):
    """The next date and time this channel actually posts on.

    Used when an account has no allocated slot free, so choosing it can still
    work. It obeys the channel's posting days, its time, and the blackouts,
    which is the part of channels.yaml that matters here. It does not do the
    allocator's job: no pacing, no contention, no asset accounting.
    """
    ch = channel_spec(cfg, profile, channel)
    days = {DAYS.index(d) for d in (ch.get("days") or DAYS) if d in DAYS}
    black = {str(b) for b in (cfg.get("blackouts") or [])}
    t = ch.get("time") or "09:00"
    d = (after or date.today()) + timedelta(days=1)
    for _ in range(400):
        iso = d.isoformat()
        if d.weekday() in days and iso not in black and (iso, channel) not in taken:
            return iso, t
        d += timedelta(days=1)
    return None, None


def phase_for(cfg, campaign_dir, when):
    """Which phase a date falls in, relative to that campaign's release week."""
    try:
        c = yaml.safe_load((campaign_dir / "campaign.yaml").read_text()) or {}
        rel = c["release"]
    except (OSError, yaml.YAMLError, KeyError):
        return "taper"
    d = date.fromisoformat(when)
    offset = ((d - timedelta(days=d.weekday()))
              - (rel - timedelta(days=rel.weekday()))).days // 7
    for name, p in (cfg.get("phases") or {}).items():
        if p["from"] <= offset <= p["to"]:
            return name
    return "taper"


@app.post("/api/media/<int:mid>/create-draft")
def create_draft(mid):
    """Take one asset to a set of accounts. Finds the next free slot on each,
    attaches the file, and hands back the post ids for copy to be written into.
    """
    body = request.get_json(silent=True) or {}
    # Each entry names the account: a channel and the profile that owns it,
    # because the same channel key exists on more than one profile.
    want = body.get("accounts") or [{"channel": c, "profile": None}
                                    for c in (body.get("channels") or [])]
    con = db()
    m = con.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if not m:
        return jsonify(error="no such file"), 404
    if not want:
        return jsonify(error="No accounts chosen."), 400

    gid = hashlib.sha256(
        f"{mid}|{datetime.now().isoformat()}".encode()).hexdigest()[:16]
    today = date.today().isoformat()
    made, skipped = [], []
    for entry in want:
        chan = entry["channel"] if isinstance(entry, dict) else entry
        prof = (entry.get("profile") if isinstance(entry, dict) else None) or m["profile"]
        row = con.execute(
            """SELECT * FROM posts WHERE profile=? AND channel=? AND state='allocated'
               AND media_id IS NULL AND date >= ? ORDER BY date, time LIMIT 1""",
            (prof, chan, today)).fetchone()
        if not row:
            # Nothing allocated free here, so make one on the channel's next
            # posting day rather than refusing.
            cfg = channels_cfg()
            taken = {(r[0], r[1]) for r in con.execute(
                "SELECT date, channel FROM posts WHERE profile=?", (prof,))}
            when, at = next_open_time(cfg, chan, taken, profile=prof)
            ch = (cfg.get("channels") or {}).get(chan) or {}
            if not when:
                skipped.append(dict(channel=chan, profile=prof,
                                 why="no posting day found"))
                continue
            camp = m["campaign"] or "one-off"
            key = f"{when}|{chan}|{camp}|{prof}|adhoc-{mid}"
            con.execute(
                """INSERT OR IGNORE INTO posts (slot_key,date,time,channel,
                   channel_label,campaign,profile,phase,asset_label,asset_type,
                   max_chars,updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, when, at, chan, ch.get("label") or chan, camp, prof,
                 phase_for(cfg, ROOT / "campaigns" / camp, when),
                 m["original"], "video_9x16" if m["kind"] == "video" else "image",
                 ch.get("max_chars"),
                 datetime.now().isoformat(timespec="seconds")))
            row = con.execute("SELECT * FROM posts WHERE slot_key=?", (key,)).fetchone()
            if not row:
                skipped.append(dict(channel=chan, why="could not make a slot"))
                continue
        col = default_collaborators(channels_cfg(), prof, chan)
        con.execute("""UPDATE posts SET media_id=?, group_id=?, collaborators=?
                       WHERE id=?""",
                    (mid, gid, json.dumps(col) if col else None, row["id"]))
        made.append(dict(id=row["id"], channel=chan, profile=prof,
                         date=row["date"], time=row["time"]))
    con.commit()
    return jsonify(group=gid, created=made, skipped=skipped)


# What the compose box can ask for. The box said "man" and the desk tested for
# "date", so a chosen date fell through to the next free slot and said nothing
# about it. Both spellings are listed here rather than left to agree by luck,
# and anything not on the list is refused rather than quietly treated as auto.
WHEN_MODES = {"auto": "auto", "soon": "soon",
              "man": "date", "date": "date", "manual": "date"}


def slot_for(con, cfg, prof, chan, media, mode, when, at):
    """Find or make the slot this post should occupy.

    "soon" pins every account to the same moment, two minutes out. Without
    this the desk borrowed each channel's next free slot and only Zernio knew
    the real time, so the calendar said next Thursday for something going out
    now.
    """
    # Auto means auto even if a date came along for the ride. A mode nobody
    # recognises is refused where the request arrives, not quietly demoted
    # here, which is how a chosen date went missing in the first place.
    mode = WHEN_MODES.get((mode or "auto").lower(), "auto")

    if mode == "soon":
        from push_zernio import SOON_MINUTES
        try:
            from zoneinfo import ZoneInfo
            z = ZoneInfo(cfg.get("timezone") or "UTC")
        except Exception:
            z = None
        n = datetime.now(z) + timedelta(minutes=SOON_MINUTES)
        when, at, mode = n.strftime("%Y-%m-%d"), n.strftime("%H:%M"), "date"

    if mode == "date" and when:
        ch = channel_spec(cfg, prof, chan)
        camp = media["campaign"] or "one-off"
        key = f"{when}|{at}|{chan}|{camp}|{prof}|picked-{media['id']}"
        con.execute(
            """INSERT OR IGNORE INTO posts (slot_key,date,time,channel,channel_label,
               campaign,profile,phase,asset_label,asset_type,
               max_chars,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, when, at or ch.get("time") or "09:00", chan,
             ch.get("label") or chan, camp, prof,
             phase_for(cfg, ROOT / "campaigns" / camp, when),
             media["original"],
             "video_9x16" if media["kind"] == "video" else "image",
             ch.get("max_chars"),
             datetime.now().isoformat(timespec="seconds")))
        return con.execute("SELECT * FROM posts WHERE slot_key=?", (key,)).fetchone()

    row = con.execute(
        """SELECT * FROM posts WHERE profile=? AND channel=? AND state='allocated'
           AND media_id IS NULL AND date >= ? ORDER BY date, time LIMIT 1""",
        (prof, chan, date.today().isoformat())).fetchone()
    if row:
        return row

    taken = {(r[0], r[1]) for r in con.execute(
        "SELECT date, channel FROM posts WHERE profile=?", (prof,))}
    d, t = next_open_time(cfg, chan, taken, profile=prof)
    if not d:
        return None
    ch = channel_spec(cfg, prof, chan)
    camp = media["campaign"] or "one-off"
    key = f"{d}|{chan}|{camp}|{prof}|adhoc-{media['id']}"
    con.execute(
        """INSERT OR IGNORE INTO posts (slot_key,date,time,channel,channel_label,
           campaign,profile,phase,asset_label,asset_type,
           max_chars,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, d, t, chan, ch.get("label") or chan, camp, prof,
         phase_for(cfg, ROOT / "campaigns" / camp, d), media["original"],
         "video_9x16" if media["kind"] == "video" else "image",
         ch.get("max_chars"),
         datetime.now().isoformat(timespec="seconds")))
    return con.execute("SELECT * FROM posts WHERE slot_key=?", (key,)).fetchone()


def lead_profile(media):
    return media[0]["profile"] if media else None


@app.post("/api/compose/draft")
def compose_draft():
    """Draft copy for a post that does not exist yet, from your description."""
    import writer
    b = request.get_json(force=True)
    ids = b.get("media") or []
    con = db()
    m = None
    if ids:
        r = con.execute("SELECT * FROM media WHERE id=?", (int(ids[0]),)).fetchone()
        m = dict(r) if r else None
    if m and (b.get("brief") or "").strip():
        m = dict(m, note=b["brief"].strip())

    prof_slug = b.get("profile") or (m or {}).get("profile")
    prof = profiles().get(prof_slug) or {}
    cfg = channels_cfg()
    chan = b.get("channel") or "instagram"
    ch = (cfg.get("channels") or {}).get(chan) or {}
    camp = (m or {}).get("campaign") or "one-off"

    # A stand-in slot, so the writer sees the same constraints it would for a
    # real one without anything being created yet.
    stub = dict(channel=chan, channel_label=ch.get("label") or chan,
                campaign=camp, profile=prof_slug, phase="launch",
                date=date.today().isoformat(), time=ch.get("time") or "09:00",
                max_chars=ch.get("max_chars"),
                asset_label=(m or {}).get("original"))
    # A post about a page is a summary of that page, so the page travels with
    # the brief. Without it the writer has only the source pack, which on this
    # desk is mostly still TODO, and it correctly answers that it has nothing
    # to say.
    page = None
    url = (b.get("page") or (m or {}).get("url") or "").strip()
    if url:
        try:
            import link as linkmod
            got = linkmod.fetch(url)
            body = "\n\n".join(
                [got.get("excerpt") or ""]
                + [f"{s['heading']}. {s['text']}" for s in (got.get("sections") or [])])
            page = dict(title=got.get("title") or "", url=got.get("url") or url,
                        text=body.strip()[:6000])
        except Exception:
            page = None          # unreachable page is not a reason to refuse

    try:
        out = writer.draft(stub, m, prof, cfg, ROOT / "campaigns" / camp, page)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"[:400]), 502
    return jsonify(copy=out.get("copy", ""), why=out.get("why", ""),
                   thin=bool(out.get("thin")))


@app.post("/api/caption/frame")
def caption_frame():
    """One frame showing where the caption lands, with the platform's own
    furniture shaded in.

    Rendering a whole video to find out the words sit under the Reels buttons
    is a slow way to learn it. The guides are drawn for looking at only; the
    publish path never calls this.
    """
    import base64, slideshow as sl
    b = request.get_json(force=True)
    ids = b.get("media") or []
    if not ids:
        return jsonify(error="Pick a photo first."), 400
    con = db()
    row = con.execute("SELECT * FROM media WHERE id=?", (int(ids[0]),)).fetchone()
    if not row or row["kind"] != "image":
        return jsonify(error="That is not a photo."), 400
    opts = caption_of(b)
    size = sl.SHAPES.get(b.get("shape") or "portrait", sl.SIZE)
    tmp = Path(tempfile.mkdtemp(prefix="frame-")) / "where.jpg"
    try:
        info = sl.still(MEDIA / row["path"], tmp, size=size,
                        caption=opts.get("caption"), font=opts.get("font"),
                        cap_size=opts.get("cap_size"),
                        cap_color=opts.get("cap_color", "white"),
                        cap_shadow=opts.get("cap_shadow", 4),
                        cap_shadow_color=opts.get("cap_shadow_color",
                                                  "black@0.7"),
                        cap_position=b.get("caption_position") or "lower",
                        cap_align=b.get("caption_align") or "center",
                        guides=bool(b.get("guides", True)), scale_to=540)
        data = base64.b64encode(tmp.read_bytes()).decode()
    except (RuntimeError, ValueError, OSError) as e:
        return jsonify(error=f"Could not draw it: {e}"[:300]), 500
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return jsonify(url=f"data:image/jpeg;base64,{data}", lines=info["lines"],
                   size=info["size"])


@app.post("/api/photo/read")
def photo_read():
    """How dark the chosen photos are, and what would fix them.

    Measured rather than guessed at: ffmpeg reports the average luma, and a
    photo that sits low needs lifting whatever anyone thinks of it. No model
    involved, so it costs nothing and says the same thing twice.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    ids = [int(i) for i in (b.get("media") or [])]
    if not ids:
        return jsonify(error="Pick a photo first."), 400
    con = db()
    rows = [r for r in con.execute(
        "SELECT * FROM media WHERE id IN (%s) AND kind='image'"
        % ",".join("?" * len(ids)), ids)]
    if not rows:
        return jsonify(error="No photos in that selection."), 400
    lumas = [sl.mean_luma(MEDIA / r["path"]) for r in rows]
    lumas = [x for x in lumas if x is not None]
    if not lumas:
        return jsonify(error="Could not read them."), 500
    avg = sum(lumas) / len(lumas)
    # Mid grey sits near 110 of 255. Below about 85 a photo reads as murky.
    if avg < 70:
        s = dict(brightness=0.14, contrast=1.08, saturation=1.05)
        why = f"Dark ({avg:.0f} of 255). Lifted."
    elif avg < 88:
        s = dict(brightness=0.07, contrast=1.04, saturation=1.03)
        why = f"A little dark ({avg:.0f}). Lifted a touch."
    elif avg > 175:
        s = dict(brightness=-0.06, contrast=1.06, saturation=1.0)
        why = f"Bright ({avg:.0f}). Pulled back."
    else:
        s = dict(brightness=0.0, contrast=1.0, saturation=1.0)
        why = f"Well exposed ({avg:.0f}). Nothing needed."
    return jsonify(luma=round(avg, 1), suggest=s, why=why, photos=len(lumas))


@app.post("/api/caption/cards")
def caption_cards_api():
    """Draft the caption cards from the copy, as a starting point."""
    import writer, slideshow as sl
    b = request.get_json(force=True)
    copy = (b.get("copy") or "").strip()
    if not copy:
        return jsonify(error="Write the copy first."), 400
    prof = profiles().get(b.get("profile") or "") or {}
    try:
        out = writer.caption_cards(copy, prof, n=b.get("n") or 3,
                                   seconds=b.get("seconds"))
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"[:400]), 502
    # Spread them down the frame rather than stacking every card in one place.
    spots = ["lower", "middle", "upper", "lower", "middle", "upper"]
    cards = [dict(c, position=spots[i % len(spots)], align="center")
             for i, c in enumerate(out.get("cards") or [])]
    return jsonify(cards=cards, why=out.get("why", ""))


@app.post("/api/caption/fit")
def caption_fit():
    """How the caption actually lays out, and how long it needs to be read.

    The box can guess at where the lines will break, but a condensed face fits
    far more than a character count suggests, so the guess runs long and Auto
    shows a length the renderer would not pick. This measures it with the real
    font at the real size, which is the same call the render makes.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    text = (b.get("caption") or "").strip()
    if not text:
        return jsonify(lines=0, size=None, needs=0.0, text=[])
    w, h = sl.SHAPES.get(b.get("shape") or "portrait", sl.SIZE)
    face = sl.fonts().get(b.get("caption_font") or "") or sl.default_font()
    if not face:
        return jsonify(error="No usable font on this machine."), 500
    try:
        size = int(b.get("caption_size") or 0)
    except (TypeError, ValueError):
        size = 0
    lines, drawn = sl.caption_layout(text, face, size or max(28, int(h * 0.045)),
                                     w)
    try:
        fade = float(b.get("caption_fade", sl.CAP_FADE))
    except (TypeError, ValueError):
        fade = sl.CAP_FADE
    needs = sl.caption_seconds(len(lines), len(text.split()), fade=fade)
    return jsonify(lines=len(lines), size=drawn, needs=round(needs, 2),
                   text=lines)


@app.post("/api/compose/shorten")
def compose_shorten():
    """Cut the approved copy down to something X will take, link included."""
    import writer
    b = request.get_json(force=True)
    copy = (b.get("copy") or "").strip()
    if not copy:
        return jsonify(error="Write the copy first."), 400
    prof = profiles().get(b.get("profile") or "") or {}
    cfg = channels_cfg()
    lim = max_chars(cfg, b.get("channel") or "x") or 280
    try:
        out = writer.shorten(copy, prof, limit=lim)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"[:400]), 502
    text = (out.get("copy") or "").strip()
    n = body_length(text, "x")
    return jsonify(copy=text, why=out.get("why", ""), counts=n, limit=lim,
                   over=max(0, n - lim))


def credit_of(b, con=None):
    """The track credit for the corner, if one was asked for.

    The name comes from the compose box so a correction is used immediately,
    and falls back to whatever the track is called in the library.
    """
    style = (b.get("credit_style") or "off").lower()
    if style not in ("note", "music"):
        return {}
    title = (b.get("credit_title") or "").strip()
    if not title and b.get("audio") and con is not None:
        import slideshow as sl
        row = con.execute("SELECT original, title FROM media WHERE id=?",
                          (int(b["audio"]),)).fetchone()
        if row:
            title = (row["title"] or "").strip() or sl.track_title(row["original"])
    return dict(credit=title, credit_style=style) if title else {}


def cards_of(b):
    """The caption cards, in order, each with its own place on the picture.

    A plain `caption` is card one, so anything written before cards existed
    still means what it meant.
    """
    out = []
    for c in (b.get("cards") or []):
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        if not text:
            continue
        import slideshow as sl
        pos = c.get("position") or "lower"
        al = c.get("align") or "center"
        out.append(dict(text=text,
                        position=pos if pos in sl.POSITIONS else "lower",
                        align=al if al in sl.ALIGNS else "center"))
    if not out and (b.get("caption") or "").strip():
        out = [dict(text=b["caption"].strip(),
                    position=b.get("caption_position") or "lower",
                    align=b.get("caption_align") or "center")]
    return out


def fit_mode_of(b):
    """Whole photo over a blur, cropped to fill, or decided per photo."""
    v = (b.get("fit_mode") or setting("fit_mode", "blur")).lower()
    return v if v in ("blur", "fill", "auto") else "blur"


def grade_of(b):
    """The colour adjustments, clamped to what does not wreck a photo."""
    g = b.get("grade") or {}
    def num(k, default, lo, hi):
        try:
            return max(lo, min(float(g.get(k, default)), hi))
        except (TypeError, ValueError):
            return default
    out = dict(brightness=num("brightness", 0.0, -0.5, 0.5),
               contrast=num("contrast", 1.0, 0.5, 2.0),
               saturation=num("saturation", 1.0, 0.0, 3.0),
               warmth=num("warmth", 0.0, -0.5, 0.5))
    flat = (abs(out["brightness"]) < 1e-6 and abs(out["contrast"] - 1) < 1e-6
            and abs(out["saturation"] - 1) < 1e-6 and abs(out["warmth"]) < 1e-6)
    return None if flat else out


def caption_of(b):
    """How the cards should look, off the compose box.

    Styling is one set for the whole post: typeface, size, colour, shadow and
    how they arrive. Only where a card sits varies, which is what keeps a post
    looking like one post.
    """
    cards = cards_of(b)
    if not cards:
        return {}
    text = cards[0]["text"]
    import slideshow
    have = slideshow.fonts()
    face = have.get(b.get("caption_font") or "") or slideshow.default_font()
    try:
        size = int(b.get("caption_size") or 0) or None
    except (TypeError, ValueError):
        size = None
    try:
        shadow = max(0, min(int(b.get("caption_shadow", 4)), 40))
    except (TypeError, ValueError):
        shadow = 4
    try:
        cap_fade = max(0.0, min(float(b.get("caption_fade", 0.2)), 3.0))
    except (TypeError, ValueError):
        cap_fade = 0.2
    import slideshow as sl
    tr = (b.get("transition") or setting("transition", "fade")).lower()
    return dict(cards=cards, font=face, cap_size=size,
                cap_color=b.get("caption_color") or "white",
                cap_shadow=shadow, cap_fade=cap_fade,
                transition=tr if tr in sl.TRANSITIONS else "fade",
                snap=b.get("snap", True) is not False,
                cap_shadow_color=b.get("caption_shadow_color") or "black@0.7")


def max_chars(cfg, chan):
    """The ceiling on the body of a post for one channel.

    A channel may set its own, which wins; otherwise the platform's applies.
    That is how one account gets held to something tighter than the platform
    allows without repeating the number thirteen times.
    """
    ch = (cfg.get("channels") or {}).get(chan) or {}
    if ch.get("max_chars"):
        return ch["max_chars"]
    plat = ch.get("platform") or (chan or "").split("_")[0]
    return ((cfg.get("platforms") or {}).get(plat) or {}).get("max_chars")


LINK_RE = re.compile(r"https?://\S+")
# Punctuation that ends a sentence rather than a URL. Copy reads "see
# (https://x.com)." and the closing bracket is the writer's, not the link's.
LINK_TRAIL = ".,;:!?)]}>\"'"


def split_link(text, ch):
    """Pull the links out of the body, for the first comment or for nowhere.

    `links_in_first_comment` moves them one comment down. `drop_links` takes
    them out altogether, which is what LinkedIn has since 5 Oct 2026: it
    holds back posts that send people off the site, a link in the first
    comment included, and our own comment was being counted as engagement.
    The graphic still prints guavy.com/wire. Any other channel keeps its copy
    exactly as written.

    Returns (body, first_comment).
    """
    ch = ch or {}
    if not (ch.get("links_in_first_comment") or ch.get("drop_links")):
        return text, ""
    if not LINK_RE.search(text or ""):
        return text, ""
    urls = []

    def lift(m):
        url = m.group(0).rstrip(LINK_TRAIL)
        urls.append(url)
        return m.group(0)[len(url):]      # leave the writer's punctuation

    body = LINK_RE.sub(lift, text)
    body = re.sub(r"\(\s*\)|\[\s*\]", "", body)     # a bracket with nothing in it
    body = re.sub(r"[ \t]{2,}", " ", body)          # the gap the link left
    body = re.sub(r"[ \t]+([.,;:!?])", r"\1", body)  # punctuation left stranded
    body = re.sub(r"[ \t]+(?=\n|$)", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not re.search(r"\w", body):
        # The copy was the link and little else. An all-but-empty post with the
        # link underneath is worse than the reach penalty, so leave it alone.
        return text, ""
    if ch.get("drop_links"):
        return body, ""
    # Same link twice in the copy is still one comment.
    return body, "\n".join(dict.fromkeys(urls))


def body_length(text, plat):
    """How long a post counts as, on the platform it is going to.

    X wraps every link in t.co and charges 23 characters for it however long
    the real one is. This post's link is over a hundred characters, so counting
    raw length would refuse a post X would have taken happily.
    """
    n = len(text or "")
    if plat != "x":
        return n
    for u in LINK_RE.findall(text or ""):
        n += 23 - len(u)
    return n


def copy_for(b, plat):
    """The words that actually go out on one platform.

    X gets its own short version because 280 characters is not a trim of a
    long post, it is a different post. LinkedIn gets its own too, started from
    the main copy and then edited: what reads as a note to a professional
    audience there would read as throat-clearing anywhere else. Either one left
    empty falls back to the copy as written.
    """
    main = (b.get("copy") or "").strip()
    if plat == "x":
        return (b.get("copy_x") or "").strip() or main
    if plat == "linkedin":
        return (b.get("copy_linkedin") or "").strip() or main
    return main


def length_of(b):
    """The length asked for in the compose box, or None to let slideshow.plan
    choose. Kept here so the preview and the publish cannot disagree about it,
    which would render one video and post a different one."""
    try:
        v = float(b.get("length") or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def export_download(row, label=None):
    """Drop a published video into the downloads folder as an MP4.

    Personal Facebook profiles cannot be posted to by any API, so that upload
    is always by hand and the file has to be somewhere findable rather than
    buried in the library under a content hash. A .MOV off a phone is already
    H.264 in a different wrapper, so it is remuxed rather than re-encoded:
    same picture, same sound, seconds instead of minutes, no generation loss.

    Returns the path written, or None if there was nothing to write.
    """
    if not row or row.get("kind") != "video":
        return None
    src = MEDIA / row["path"]
    if not src.exists():
        return None
    try:
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-",
                  Path(row.get("original") or src.name).stem).strip("-")
    # The desk's own renders already end in a stamp and the name carries the
    # date, so drop the duplicate rather than reading it twice in Finder.
    stem = re.sub(r"-\d{8}-\d{6}$", "", stem) or "post"
    parts = [p for p in (slug(label or row.get("profile") or ""),
                         date.today().isoformat(), stem) if p]
    base = "-".join(parts)[:120]
    dest = DOWNLOADS / f"{base}.mp4"
    n = 2
    while dest.exists():
        dest = DOWNLOADS / f"{base}-{n}.mp4"
        n += 1

    if src.suffix.lower() == ".mp4":
        shutil.copyfile(src, dest)
        return dest

    import slideshow
    r = subprocess.run([slideshow.exe(), "-y", "-i", str(src), "-c", "copy",
                        "-movflags", "+faststart", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        # Whatever was in there does not fit an MP4 as-is. Re-encode instead.
        r = subprocess.run([slideshow.exe(), "-y", "-i", str(src),
                            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", str(dest)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            dest.unlink(missing_ok=True)
            return None
    return dest


def render_with_music(con, media, track_id, b):
    """Photos plus a track become one video; a video plus a track keeps its
    picture and gets new sound. Returns the new media row, or None if the
    selection is not something that can be scored."""
    import slideshow
    photos = all(m["kind"] == "image" for m in media)
    one_video = len(media) == 1 and media[0]["kind"] == "video"
    if not (photos or one_video):
        return None
    t = con.execute("SELECT * FROM media WHERE id=?", (track_id,)).fetchone()
    if not t:
        raise ValueError("that track is gone")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = MEDIA / media[0]["profile"]
    dest.mkdir(parents=True, exist_ok=True)
    start = float(b.get("audio_start") or 0)
    if one_video:
        name = f"dubbed-{stamp}.mp4"
        info = slideshow.dub(MEDIA / media[0]["path"], dest / name,
                             MEDIA / t["path"],
                             keep_original=bool(b.get("keep_original")),
                             start=start)
        info["photos"] = 0
        info["width"] = media[0]["width"]
        info["height"] = media[0]["height"]
    else:
        shape = b.get("shape") or "portrait"
        name = f"slideshow-{shape}-{stamp}.mp4"
        info = slideshow.build([MEDIA / m["path"] for m in media], dest / name,
                               MEDIA / t["path"],
                               size=slideshow.SHAPES.get(shape, slideshow.SIZE),
                               start=start, target=length_of(b),
                               mode=fit_mode_of(b), grading=grade_of(b),
                               **caption_of(b), **credit_of(b, con))

    blob = (dest / name).read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    # Every file that went into this one, so a photo that only ever reached
    # the world inside a slideshow still counts as having been out.
    made_from = json.dumps([m["id"] for m in media] + [t["id"]])
    cue = f", from {int(start // 60)}:{int(start % 60):02d}" if start else ""
    con.execute(
        """INSERT OR IGNORE INTO media (profile,campaign,path,original,kind,
           mime,bytes,sha256,width,height,seconds,source,added,note,made_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (media[0]["profile"], media[0]["campaign"],
         f"{media[0]['profile']}/{name}", name, "video", "video/mp4",
         len(blob), digest, info["width"], info["height"], info["seconds"],
         "rendered", datetime.now().isoformat(timespec="seconds"),
         (f"{info['photos']} photos, {info['width']}x{info['height']}, "
          f"with {t['original']}{cue}" if info["photos"]
          else f"{media[0]['original']} with {t['original']}{cue}"
               + ("" if info.get("replaced") else ", mixed under the original")),
         made_from))
    con.commit()
    out = dict(con.execute("SELECT * FROM media WHERE sha256=?",
                           (digest,)).fetchone())
    # How it was timed is not worth a column, but the dialog should be able to
    # say it out loud, so it rides along on the row.
    out["render"] = {k: info[k] for k in ("hold", "loops", "short_by", "photos",
                                         "caption_lines", "caption_size",
                                         "credit", "credit_fell_back",
                                         "cards", "mode", "filled", "blurred")
                     if k in info}
    return out


@app.post("/api/preview")
def preview():
    """Build the video without posting it, so you can watch it first."""
    b = request.get_json(force=True)
    ids = b.get("media") or []
    if not ids or not b.get("audio"):
        return jsonify(error="Pick the media and a track first."), 400
    con = db()
    found = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM media WHERE id IN (%s)" % ",".join("?" * len(ids)),
        [int(i) for i in ids])}
    media = [found[int(i)] for i in ids if int(i) in found]
    if not media:
        return jsonify(error="Those files are gone."), 404
    try:
        row = render_with_music(con, media, int(b["audio"]), b)
    except (RuntimeError, ValueError) as e:
        return jsonify(error=f"Could not build the video: {e}"[:400]), 500
    if not row:
        return jsonify(error="Music can go under photos, or over a single "
                             "video. That selection is neither."), 400
    return jsonify(id=row["id"], url=f"/media/{row['id']}/raw",
                   seconds=row["seconds"], width=row["width"],
                   height=row["height"], bytes=row["bytes"], note=row["note"],
                   **(row.get("render") or {}))


def shape_of(m):
    """portrait, landscape or square, from the file's own dimensions."""
    w, h = m.get("width") or 0, m.get("height") or 0
    if not w or not h:
        return None
    return "portrait" if h > w else "landscape" if w > h else "square"


def for_channel(media, want):
    """The files this channel should carry, out of everything on the post.

    A preference, not a filter. If the post has the shape the channel wants,
    it gets only that, because sending both crops put the same story in one
    post twice. If it does not, the channel gets what there is: a vertical
    short is still the post even on a channel that would rather be wide, and
    no picture is worse than the wrong proportion.

    A carousel of several files of the same shape stays whole.
    """
    if not want or len(media) < 2:
        return media
    fits = [m for m in media if shape_of(m) == want]
    if fits:
        return fits
    # Nothing in the preferred shape. Square counts for either, so it is the
    # next best thing before falling back to everything.
    square = [m for m in media if shape_of(m) == "square"]
    return square or media


# posts.asset is "market:SYMBOL#article_id". Three things are packed into one
# column, so everything that reads it goes through here rather than splitting
# it by hand. Getting that wrong put a uuid in a hashtag and hid every markets
# post from its own cadence.
def asset_parts(asset):
    """market, symbol, article_id out of a stored asset string."""
    head, _, article = str(asset or "").partition("#")
    market, _, symbol = head.partition(":")
    return market, symbol, article


def hashtags_for(profile, platform, instrument=None):
    """This platform's tags, plus the instrument's own if there is one.

    The voice block may carry a flat list, which is used everywhere, or a
    mapping of platform to list with an optional `default`. A tag is published
    text like any other, so nothing here names a direction or an outcome.
    """
    book = ((profile or {}).get("voice") or {}).get("hashtags")
    if not book:
        return []
    if isinstance(book, list):
        tags = list(book)
    else:
        tags = list(book.get(platform) or book.get("default") or [])

    # The instrument's own tag, from its brand scope so it is the name rather
    # than the ticker: #Gold, #Bitcoin, not #XAUUSD.
    if instrument:
        mkt, sym, _ = asset_parts(instrument)
        scope = brand_scope(mkt, sym) if sym else None
        name = (scope or {}).get("name") or sym
        if name:
            # A scope name is written for a human reading a caption, so it
            # carries things a hashtag should not: "BNB (Binance)" became
            # #BNBBinance and "Ripple XRP" would become #RippleXRP. Drop any
            # parenthetical, then take the first word, and fall back to the
            # ticker if that leaves nothing usable.
            clean = re.sub(r"\s*\([^)]*\)", "", name).strip()
            tag = re.sub(r"[^A-Za-z0-9]", "", clean) or re.sub(
                r"[^A-Za-z0-9]", "", str(sym))
            if tag and tag.lower() not in {x.lower() for x in tags}:
                tags.append(tag)
    return [x.lstrip("#") for x in tags if x]


@app.post("/api/compose")
def compose():
    """One composed post, fanned out to the accounts you picked.

    The same person is a different handle on each platform, so people are
    chosen by name here and resolved to the right handle per platform. A
    collaborator is only sent where the platform actually has co-authors.
    """
    b = request.get_json(force=True)
    con, cfg = db(), channels_cfg()
    ids = b.get("media") or []
    if not ids:
        return jsonify(error="No media selected."), 400
    _inst = b.get("instrument") or {}
    found = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM media WHERE id IN (%s)" % ",".join("?" * len(ids)),
        [int(i) for i in ids])}
    # SQL gives no order, and the order is the carousel.
    media = [found[int(i)] for i in ids if int(i) in found]
    if not media:
        return jsonify(error="Those files are gone."), 404

    copy = (b.get("copy") or "").strip()
    if not copy:
        return jsonify(error="Write the copy first."), 400

    roles = b.get("people") or {}          # {person name: "tag"|"collab"}
    # People are listed once, on the profile you were looking at. Resolve their
    # handles from there whatever account the post lands on.
    book = ((profiles().get(b.get("profile") or lead_profile(media)) or {})
            .get("handles") or {})
    accounts = b.get("accounts") or []
    when = b.get("when") or {}
    mode = (when.get("mode") or "auto").lower()
    if mode not in WHEN_MODES:
        return jsonify(error=f"Do not know when {mode!r} means."), 400
    # Photos plus a track become one video, because no platform will let an
    # API attach music to a still. A single video plus a track keeps its
    # picture and gets new sound. Photos alone stay a carousel.
    track_id = b.get("audio")
    if b.get("rendered"):
        # Already built for the preview, so use that rather than doing it twice.
        row = con.execute("SELECT * FROM media WHERE id=?",
                          (int(b["rendered"]),)).fetchone()
        if row:
            media = [dict(row)]
    elif track_id:
        try:
            row = render_with_music(con, media, int(track_id), b)
        except (RuntimeError, ValueError) as e:
            return jsonify(error=f"Could not build the video: {e}"[:400]), 500
        if row:
            media = [row]

    gid = hashlib.sha256(f"{ids}|{datetime.now()}".encode()).hexdigest()[:16]
    lead = media[0]
    prof_data = profiles().get(b.get("profile") or lead["profile"]) or {}
    made, skipped = [], []

    for a in accounts:
        chan, prof = a.get("channel"), a.get("profile") or lead["profile"]
        ch = (cfg.get("channels") or {}).get(chan) or {}
        plat = ch.get("platform") or (chan or "").split("_")[0]
        pspec = (cfg.get("platforms") or {}).get(plat) or {}

        # Each channel owns its own row, so the variant is resolved here rather
        # than again at push time. Checked before the slot is claimed: slot_for
        # inserts, and skipping after it would leave an empty slot behind.
        text = copy_for(b, plat)
        text, first = split_link(text, ch)
        lim = max_chars(cfg, chan)
        n = body_length(text, plat)
        if lim and n > lim:
            skipped.append(dict(channel=chan,
                                why=f"copy counts as {n} characters and "
                                    f"{plat} allows {lim}"))
            continue

        # Which instrument the post is about, when it is about one. Kept on
        # the row so the Posts list, the calendar and anything automated can
        # see it, and used for the instrument's own hashtag.
        inst = (b.get("instrument") or {})
        sym = (inst.get("symbol") or "").strip()
        mkt = (inst.get("market") or "").strip()
        scope = brand_scope(mkt, sym) if (mkt and sym) else None
        # "market:SYMBOL#article_id". The article is appended so a story can
        # be recognised later and never chosen twice.
        aid = (b.get("article_id") or "").strip()
        asset = f"{mkt}:{sym}" if sym else (f"{mkt}:" if mkt else None)
        if asset and aid:
            asset = f"{asset}#{aid}"
        label = (scope or {}).get("name") or sym or None

        # Each channel takes the crop it wants out of what the post carries.
        mine = for_channel(media, ch.get("prefer_shape"))
        head = mine[0] if mine else lead

        row = slot_for(con, cfg, prof, chan, head, mode,
                       when.get("date"), when.get("time"))
        if not row:
            skipped.append(dict(channel=chan, why="no posting day found"))
            continue

        tags, collabs = [], []
        for person, role in roles.items():
            if role not in ("tag", "collab"):
                continue
            h = None
            for entry in (book.get(plat) or []):
                if (entry.get("name") or entry.get("handle")) == person:
                    h = entry
                    break
            if not h:
                continue
            if role == "collab" and pspec.get("collab") and h.get("collab"):
                collabs.append(h["handle"])
            elif pspec.get("tag"):
                tags.append(h["handle"])

        # LinkedIn and X use hashtags too; the old rule left them bare. What
        # differs is how many, which the per-platform lists carry.
        tags_h = []
        if b.get("hashtags"):
            tags_h = hashtags_for(prof_data, plat, asset)

        import push_zernio as _pz
        headline = (b.get("title") or "").strip() or _pz.title_from(text)

        # Which instrument the post is about, when it is about one. Kept on
        # the row so the Posts list, the calendar and anything automated can
        # see it: the compose sheet knowing is no use once the sheet is shut.
        # A post that carries an article is a Wire post however it was made,
        # so it counts against the same pacing and can never be picked twice.
        camp = "markets" if (asset and "#" in asset) else camp
        con.execute("""UPDATE posts SET media_id=?, media_ids=?, group_id=?,
                       copy=?, first_comment=?, title=?, tags=?, collaborators=?,
                       hashtags=?, asset=COALESCE(?, asset),
                       asset_label=COALESCE(?, asset_label),
                       campaign=?,
                       state='approved', updated=? WHERE id=?""",
                    (head["id"], json.dumps([m["id"] for m in mine]), gid, text,
                     first, headline, json.dumps(tags), json.dumps(collabs),
                     json.dumps(tags_h), asset, label, camp,
                     datetime.now().isoformat(timespec="seconds"), row["id"]))
        made.append(dict(id=row["id"], channel=chan, profile=prof,
                         date=row["date"], time=row["time"], first_comment=first,
                         tags=tags, collaborators=collabs, hashtags=tags_h,
                         instrument=asset, shape=shape_of(head),
                         files=len(mine)))
    con.commit()

    # Anything with a video in it gets a copy in the downloads folder, named
    # so it can be found. Facebook personal profiles are hand-uploaded and this
    # is the file to upload. It is a copy: the library still holds the original.
    exported = None
    if made and lead.get("kind") == "video":
        try:
            got = export_download(
                lead, (profiles().get(lead["profile"]) or {}).get("label"))
            exported = str(got) if got else None
        except OSError as e:
            app.logger.warning("could not export %s: %s", lead.get("path"), e)

    pushed = []
    if made and b.get("push", True):
        for p in made:
            cmd = [sys.executable, "push_zernio.py", "push", "--id", str(p["id"])]
            if mode == "soon":
                cmd.append("--soon")
            if not is_live():
                cmd.append("--dry-run")
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               timeout=900)
            pushed.append(dict(id=p["id"], channel=p["channel"],
                               out=((r.stdout or "") + (r.stderr or "")).strip()[-500:]))
    return jsonify(group=gid, created=made, skipped=skipped, pushed=pushed,
                   live=is_live(), exported=exported)


@app.get("/api/fonts")
def font_list():
    """Whatever is installed on this machine. Nothing is bundled, so the list
    is different on a different computer and a caption rendered here cannot be
    assumed to render the same somewhere else."""
    import slideshow
    have = slideshow.fonts()
    picks = [n for n in slideshow.FONT_PICKS if n in have]
    best = slideshow.default_font()
    return jsonify(fonts=sorted(have), suggested=picks,
                   default=next((n for n, p in have.items() if p == best), None))


@app.post("/api/media/measure")
def media_measure():
    """Fill in dimensions for anything uploaded before the desk measured them."""
    import probe as _probe
    con = db()
    n = 0
    for r in con.execute("SELECT id, path FROM media WHERE width IS NULL").fetchall():
        w, h, secs = _probe.probe(MEDIA / r["path"])
        if w:
            con.execute("UPDATE media SET width=?, height=?, seconds=? WHERE id=?",
                        (w, h, secs, r["id"]))
            n += 1
    con.commit()
    return jsonify(measured=n)


@app.get("/api/zernio/queue")
def zernio_queue():
    """What Zernio actually holds. The desk's own record can drift from this,
    so this reads from the source rather than from desk.db."""
    try:
        raw = zernio_all("/posts", request.args.get("fresh") == "1")
    except RuntimeError as e:
        return jsonify(error=str(e)[:400]), 502
    alias = platform_alias()
    owner = account_owner()
    only = request.args.get("profile")
    origin = {r["zernio_id"]: r["profile"] for r in db().execute(
        "SELECT zernio_id, profile FROM posts "
        "WHERE zernio_id IS NOT NULL AND zernio_id != ''")}
    out = []
    for p in raw:
        if only:
            mine = origin.get(p.get("_id"))
            if not mine:
                for x in p.get("platforms") or []:
                    acct = x.get("accountId")
                    aid = acct.get("_id") if isinstance(acct, dict) else acct
                    mine = mine or (owner.get(aid) or {}).get("slug")
            if mine != only:
                continue
        out.append(dict(
            id=p.get("_id"), status=p.get("status"),
            scheduled=p.get("scheduledFor"), tz=p.get("timezone"),
            content=p.get("content") or "",
            created=p.get("createdAt"),
            media=[m.get("url") for m in (p.get("mediaItems") or [])],
            kinds=[m.get("type") for m in (p.get("mediaItems") or [])],
            platforms=[dict(
                platform=alias.get(x.get("platform"), x.get("platform")),
                name=(x.get("accountId") or {}).get("displayName") or "",
                status=x.get("status") or p.get("status"),
                url=x.get("platformPostUrl") or x.get("postUrl") or "",
                error=x.get("error") or x.get("errorMessage") or "")
                for x in (p.get("platforms") or [])]))
    # Published work lives on the Published tab. This one is the queue: what
    # is still coming, still going out, or went wrong.
    out = [p for p in out if (p.get("status") or "").lower() != "published"]
    out.sort(key=lambda p: p.get("scheduled") or "")
    return jsonify(posts=out, total=len(out))


def _norm(t):
    return " ".join((t or "").split())[:120].lower()


@app.post("/api/reconcile")
def reconcile():
    """Ask Zernio what really happened and write it back.

    The desk records what it sent. Zernio records what went out. Those drift:
    a push whose response shape we did not recognise leaves no id behind, and
    nothing ever tells the desk a scheduled post has since published.
    """
    con = db()
    try:
        # Pressed on purpose, so it asks for the truth rather than the copy.
        raw_live = zernio_all("/analytics", force=True)
        raw_queue = zernio_all("/posts", force=True)
    except RuntimeError as e:
        return jsonify(error=str(e)[:400]), 502

    seen = []
    alias = platform_alias()
    for p in raw_live:
        a = p.get("analytics") or {}
        seen.append(dict(id=p.get("_id"),
                         platform=alias.get(p.get("platform"), p.get("platform")),
                         content=p.get("content"), state="published",
                         url=p.get("platformPostUrl") or "",
                         stats={k: a.get(k) for k in
                                ("views", "impressions", "reach", "likes",
                                 "comments", "shares", "saves", "engagementRate")}))
    for p in raw_queue:
        for x in p.get("platforms") or []:
            seen.append(dict(id=p.get("_id"),
                             platform=alias.get(x.get("platform"),
                                                x.get("platform")),
                             content=p.get("content"),
                             state=(p.get("status") or "scheduled").lower(),
                             url=x.get("platformPostUrl") or ""))

    cfg = channels_cfg()
    matched = 0
    rows = con.execute("SELECT * FROM posts WHERE state IN "
                       "('approved','scheduled','publishing','published')"
                       ).fetchall()
    for r in rows:
        ch = (cfg.get("channels") or {}).get(r["channel"]) or {}
        plat = ch.get("platform") or r["channel"].split("_")[0]
        # Tags and hashtags are appended at push time, so what went out is the
        # copy plus extras. Match on the opening instead of the whole thing.
        want = _norm(r["copy"])[:60]
        hit = next((z for z in seen if z["platform"] == plat and want
                    and _norm(z["content"]).startswith(want)), None)
        if not hit:
            continue
        state = "published" if hit["state"] == "published" else r["state"]
        con.execute(
            "UPDATE posts SET zernio_id=?, post_url=?, state=?, stats=? "
            "WHERE id=?",
            (hit["id"], hit["url"], state,
             json.dumps(hit.get("stats")) if hit.get("stats") else None,
             r["id"]))
        matched += 1
    con.commit()
    return jsonify(checked=len(rows), matched=matched)


BLOG_FEED = "https://www.guavy.com/blog/feed.json"

# Claims that the site published before voice.claims existed. Applied once, on
# the row's first insert, so a later hand-edit in the desk is never overwritten.
BLOG_BLOCKED = {
    "guavy-new-article-sentiment-metadata":
        "Enumerates the scoring dimensions by name, which house terminology "
        "forbids.",
}


def blog_slug(url):
    """The last path segment, which is what the feed keys a post on."""
    return (urllib.parse.urlparse(url or "").path or "").rstrip("/").split("/")[-1]


@app.post("/api/blogs/refresh")
def blogs_refresh():
    """Re-read the JSON feed. New blogs are added, known ones are updated.

    Nothing is ever deleted here: a blog that falls out of the feed may still
    be attached to a post, and dropping the row would blank that post's asset.
    """
    req = urllib.request.Request(BLOG_FEED, headers={"User-Agent": "Guavynator"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            feed = json.load(r)
    except Exception as e:
        # Loudly, because a silent zero looks exactly like "no blogs yet".
        return jsonify(error=f"Could not read {BLOG_FEED}: {e}"[:400]), 502

    items = feed.get("items") or []
    if not items:
        return jsonify(error="The feed parsed but held no items. Treating that "
                             "as a fault rather than an empty blog."), 502

    con = db()
    now = datetime.now().isoformat(timespec="seconds")
    added = updated = 0
    for it in items:
        url = it.get("url") or it.get("id") or ""
        slug = blog_slug(url)
        if not slug:
            continue
        row = con.execute("SELECT id FROM blogs WHERE slug=?", (slug,)).fetchone()
        vals = (url, (it.get("title") or "").strip(), (it.get("summary") or "").strip(),
                it.get("image") or "", (it.get("date_published") or "")[:10])
        if row:
            con.execute("""UPDATE blogs SET url=?, title=?, summary=?, image=?,
                           published=? WHERE slug=?""", vals + (slug,))
            updated += 1
        else:
            blocked = BLOG_BLOCKED.get(slug)
            con.execute("""INSERT INTO blogs
                           (slug,url,title,summary,image,published,postable,why_not,added)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (slug,) + vals + (0 if blocked else 1, blocked, now))
            added += 1
    con.commit()
    return jsonify(added=added, updated=updated, total=len(items))


@app.post("/api/blogs/<int:bid>/postable")
def blog_postable(bid):
    """Let the desk overrule the built-in block, or add one of its own."""
    con = db()
    body = request.get_json(silent=True) or {}
    con.execute("UPDATE blogs SET postable=?, why_not=? WHERE id=?",
                (1 if body.get("postable") else 0,
                 (body.get("why_not") or "").strip() or None, bid))
    con.commit()
    return jsonify(ok=True)


# Where anything the desk draws for itself is filed. Generated artwork, the
# renders built from it and the reshapes in between all land here rather than
# loose among the pictures you chose yourself. Renamed here and it follows, so
# long as a folder of that name exists or can be made.
AUTO_FOLDER = os.environ.get("AUTO_FOLDER", "Auto Images")

# Sources the desk produces. A blog hero is fetched rather than drawn, so it
# stays where an upload would.
AUTO_SOURCES = {"gemini", "render", "reshaped", "scrim", "rendered"}


def auto_folder_id(con, prof):
    """The id of this profile's auto folder, making it if it is not there."""
    r = con.execute("SELECT id FROM folders WHERE profile=? AND name=?",
                    (prof, AUTO_FOLDER)).fetchone()
    if r:
        return r["id"]
    try:
        con.execute("INSERT INTO folders (profile,name,created) VALUES (?,?,?)",
                    (prof, AUTO_FOLDER,
                     datetime.now().isoformat(timespec="seconds")))
        con.commit()
    except sqlite3.IntegrityError:
        pass
    r = con.execute("SELECT id FROM folders WHERE profile=? AND name=?",
                    (prof, AUTO_FOLDER)).fetchone()
    return r["id"] if r else None


def _keep_image(con, prof, blob, mime, url, title, note, source):
    """Put a picture in the library and hand back the row, not a response.

    save_image() answers the browser directly, which is no use when the caller
    has two more images to render before it can say anything.
    """
    digest = hashlib.sha256(blob).hexdigest()
    have = con.execute("SELECT * FROM media WHERE sha256=? AND profile=?",
                       (digest, prof)).fetchone()
    if have:
        return dict(have)
    ext = mimetypes.guess_extension(mime) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    base = secure_filename(title or "blog")[:60] or "blog"
    name = f"{digest[:12]}-{base}{ext}"
    dest = MEDIA / prof
    dest.mkdir(parents=True, exist_ok=True)
    (dest / name).write_bytes(blob)
    import probe as _probe
    w, h, _ = _probe.probe(dest / name)
    folder = auto_folder_id(con, prof) if source in AUTO_SOURCES else None
    con.execute(
        """INSERT OR IGNORE INTO media (profile,campaign,path,original,kind,mime,
           bytes,sha256,width,height,url,title,note,source,folder_id,added)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (prof, None, f"{prof}/{name}", base + ext, "image", mime, len(blob),
         digest, w, h, url, title, note, source, folder,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    row = con.execute("SELECT * FROM media WHERE sha256=? AND profile=?",
                      (digest, prof)).fetchone()
    return dict(row) if row else None


@app.post("/api/blogs/<int:bid>/media")
def blog_media(bid):
    """The blog's hero image as a library row, so it composes like an upload.

    The feed already names the hero, so this downloads it rather than parsing
    the page again. A blog with no hero is not an error: it composes without
    one and the desk renders a graphic from the copy instead.
    """
    con = db()
    b = request.get_json(silent=True) or {}
    prof = (b.get("profile") or "").strip()
    if prof not in profiles():
        return jsonify(error="Choose a profile first."), 400
    blog = con.execute("SELECT * FROM blogs WHERE id=?", (bid,)).fetchone()
    if not blog:
        return jsonify(error="No such blog."), 404
    if not blog["postable"]:
        return jsonify(error="That blog is blocked. Allow it first."), 400

    have = con.execute("SELECT * FROM media WHERE url=? AND profile=?",
                       (blog["url"], prof)).fetchone()
    if have:
        return jsonify(media=dict(have), already=True)
    if not blog["image"]:
        return jsonify(media=None, no_image=True)

    try:
        req = urllib.request.Request(blog["image"],
                                     headers={"User-Agent": "Guavynator"})
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = r.read(40 * 1024 * 1024)
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
    except Exception as e:
        return jsonify(error=f"Could not fetch the hero image: {e}"[:300]), 502

    row = _keep_image(con, prof, blob, mime, blog["url"], blog["title"],
                      blog["summary"], "blog")
    return jsonify(media=row)


# Gemini draws the cards. Named here rather than hard-coded so a newer image
# model can be dropped in without touching the code. gemini-2.5-flash-image is
# the legacy one; the 3.1 Flash image model is the current workhorse.
GEMINI_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")
# The Interactions API, not generateContent. The older endpoint still answers,
# but this is the one the image models are documented against.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# The house style. Dark and editorial to sit with the blog heroes, abstract
# rather than literal, and above all carrying no numbers: a generated chart
# with invented prices on it would be a claim the source pack cannot support,
# which is the one thing voice.claims is there to stop.
GEMINI_TEAL = os.environ.get("GUAVY_TEAL", "#00d4b4")
# Written almost entirely as instructions to draw rather than a list of things
# to avoid. A long "no bulls, no arrows, no skylines" tail does the opposite of
# what it says: naming a thing puts it in the picture, and one run came back
# with a bull, three up-arrows, a rising chart and a skyline in a single frame.
# The ban that matters is expressed positively instead: purely abstract, no
# recognisable objects at all, which forbids the whole family in one clause.
# Two different bans got conflated here once, and the fix was to separate
# them. The one that matters is narrow and is about claims: no figures, no
# charts, no direction, nothing that implies a market outcome. The other was
# a panic after a run came back with a bull and three up-arrows, and banned
# every recognisable object. That second ban made the pictures safe and
# useless: abstract wallpaper that could sit on any post at all. A phone, a
# door, a crowd, a map make no claim about anybody's money, so they are
# allowed, and the image is now asked to be about what the post says.
GEMINI_STYLE = (
    "Medium: a flat two-dimensional editorial illustration, screenprinted, of "
    "the kind that runs beside a feature in a newspaper's business pages. "
    "Limited flat inks, visible paper grain, halftone and stipple for tone, "
    "confident hand-drawn line, a little misregistration between layers. "
    "Printed, never rendered: no 3D, no CGI, no photorealism, no gloss, no "
    "lens effects, no volumetric light. "
    "{palette} Generous negative space. Calm, confident, understated, a "
    "little witty. "
    "The instruments themselves are fair game and are usually the clearest "
    "way in: the published symbols and marks of crypto assets, the physical "
    "form of a commodity, the notes and symbols of a currency pair, the "
    "recognisable object of a listed company's business. Draw them as the "
    "subject of the picture, in this print style, not as a logo salad. "
    "It must not contain, because each of these states something about money "
    "that nobody can stand behind: any chart, graph, candlestick, ticker, "
    "price, percentage or numeral; any arrow, line or shape that implies a "
    "market rising or falling; bulls, bears or any animal standing in for a "
    "market; heaps of gold, cash or treasure; anyone looking delighted at a "
    "screen or celebrating a win. "
    "It carries no lettering of its own and no Guavy logo, because the desk "
    "adds those itself afterwards."
)

SCOPES = ROOT / "brand-scopes.yaml"
_SCOPE_CACHE = {}


def brand_scope(market, symbol):
    """How this instrument should look, or None if we have not said.

    Read fresh when the file changes, because adding a scope and having to
    restart the desk to see it is the kind of friction that stops anyone
    adding one.
    """
    if not SCOPES.exists():
        return None
    stamp = SCOPES.stat().st_mtime
    if _SCOPE_CACHE.get("stamp") != stamp:
        try:
            _SCOPE_CACHE["data"] = yaml.safe_load(SCOPES.read_text()) or {}
            _SCOPE_CACHE["stamp"] = stamp
        except yaml.YAMLError:
            return None
    data = _SCOPE_CACHE.get("data") or {}
    book = data.get(market) or {}
    if symbol in book:
        return book[symbol]
    # Case-insensitive, then through the alias table.
    lower = {k.lower(): k for k in book}
    key = lower.get(str(symbol).lower())
    if key:
        return book[key]
    alias = ((data.get("aliases") or {}).get(market) or {})
    hit = alias.get(symbol) or {k.lower(): v for k, v in alias.items()}.get(
        str(symbol).lower())
    return book.get(hit) if hit else None


def scope_clause(scope):
    """The instrument's own look, which overrides the house palette for the
    subject. The ground stays the theme's: a branded subject on Guavy's
    ground, not a picture drawn entirely in someone else's colours."""
    if not scope:
        return ""
    bits = [f"The subject is {scope.get('name') or ''}."]
    if scope.get("visual"):
        bits.append(f"Draw: {scope['visual']}.")
    if scope.get("mark"):
        bits.append(f"Its mark, if one appears, is {scope['mark']}.")
    if scope.get("colour"):
        bits.append(f"Use its own palette for the subject, {scope['colour']}, "
                    f"in place of the house teal. The ground stays as "
                    f"described below.")
    return " ".join(bits) + " "


GEMINI_RATIO = {"portrait": "9:16", "landscape": "16:9", "square": "1:1"}


def recompose_prompt(shape, scope, theme):
    """The ask for the second shape, drawn from the first.

    The scope goes again here. Left out, the palette's house teal was the only
    colour instruction the portrait got, and it pulled the instrument's own
    look back to generic teal: the LinkedIn landscape was on brand and the
    Instagram portrait made from it was not.
    """
    return (scope_clause(scope)
            + "Recompose the attached image as a "
            f"{'vertical' if shape == 'portrait' else 'square'} picture. "
            "Same artwork, same subject, same inks, same mood: this is the "
            "other crop of one image, not a new idea. Re-stage the "
            "composition for the new proportions rather than stretching, "
            "padding or cropping it, and keep the subject clear of the "
            "outer edges.\n\n" + GEMINI_STYLE.replace(
                "{palette}", PALETTES.get(theme, PALETTES["dark"])))

# The model drifts to a white page unless the ground is stated flatly and
# early, so each theme says it twice: what the ground is, and what it is not.
PALETTES = {
    "dark": (f"Palette: a deep near-black ground, almost the whole frame. Not "
             f"white, not cream, not a light background. One accent ink of "
             f"Guavy teal {GEMINI_TEAL} (RGB 0, 212, 180) at full strength "
             f"carrying the subject, everything else near-monochrome so the "
             f"teal reads."),
    "light": (f"Palette: a warm off-white paper ground, almost the whole "
              f"frame, like uncoated stock. One accent ink of Guavy teal "
              f"{GEMINI_TEAL} (RGB 0, 212, 180) at full strength carrying the "
              f"subject, with near-black line work. No dark background."),
}


def gemini_image(prompt, shape, key, timeout=120, ref=None):
    """One image back from Gemini, as raw bytes and its mime type.

    `ref` is (bytes, mime) of a picture to work from, which is how the second
    shape is made: the same scene recomposed rather than a fresh invention.
    """
    import base64
    parts = [{"type": "text", "text": prompt}]
    if ref:
        parts.append({"type": "image", "mime_type": ref[1],
                      "data": base64.b64encode(ref[0]).decode()})
    body = {
        "model": GEMINI_MODEL,
        "input": parts,
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": GEMINI_RATIO.get(shape, "1:1"),
            "image_size": os.environ.get("GEMINI_IMAGE_SIZE", "2K"),
        },
    }
    req = urllib.request.Request(
        GEMINI_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)

    node = out.get("interaction") if isinstance(out.get("interaction"), dict) else out

    # On the wire the picture arrives inside the model_output step, as
    # steps[].content[] with type "image". The docs describe an
    # `output_image` accessor, which is the SDK's convenience over this and is
    # not in the JSON, so read the steps and keep the accessor as a fallback.
    for step in (node.get("steps") or []):
        for part in (step.get("content") or []):
            if part.get("type") == "image" and part.get("data"):
                return (base64.b64decode(part["data"]),
                        part.get("mime_type") or part.get("mimeType")
                        or "image/jpeg")
    img = node.get("output_image") or node.get("outputImage") or {}
    if img.get("data"):
        return (base64.b64decode(img["data"]),
                img.get("mime_type") or img.get("mimeType") or "image/jpeg")

    # A refusal comes back as a normal 200 with prose where the picture should
    # be, so say what it actually said rather than "no image".
    said = node.get("output_text") or node.get("outputText") or ""
    if not said:
        said = " ".join(part.get("text") or ""
                        for step in (node.get("steps") or [])
                        for part in (step.get("content") or []))
    raise RuntimeError(str(said).strip()[:300] or "Gemini returned no image.")


@app.get("/api/scopes")
def api_scopes():
    """Every instrument the desk knows how to brand, per market.

    Feeds the instrument field in the compose sheet, so picking one is a
    choice from what exists rather than a guess at spelling.
    """
    if not SCOPES.exists():
        return jsonify(markets={})
    brand_scope("", "")          # warms the cache
    data = _SCOPE_CACHE.get("data") or {}
    out = {}
    for m in ("crypto", "stocks", "forex", "commodities"):
        book = data.get(m) or {}
        out[m] = sorted([dict(symbol=k, name=(v or {}).get("name") or k)
                         for k, v in book.items()],
                        key=lambda d: d["symbol"])
    # The alias table travels too. Without it the sheet says "no brand scope"
    # for WTI while the renderer happily draws it as Oil, which is a lie about
    # what is going to happen.
    return jsonify(markets=out, aliases=(data.get("aliases") or {}))


@app.post("/api/image/generate")
def image_generate():
    """Draw the post's picture with Gemini, portrait and landscape.

    The subject is the blog's title. The copy comes along as grounding so the
    picture is about what this post actually says rather than the company in
    general, but it is explicitly not to be rendered as words.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    prof = (b.get("profile") or "").strip()
    if prof not in profiles():
        return jsonify(error="Choose a profile first."), 400

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return jsonify(error="GEMINI_API_KEY is not set. Put it in .env and "
                             "restart the desk."), 400

    title = (b.get("title") or "").strip()
    copy = (b.get("copy") or "").strip()
    if not (title or copy):
        return jsonify(error="Nothing to draw from."), 400
    subject = title or copy[:160]

    scope = brand_scope(b.get("market") or "", b.get("symbol") or "")
    prompt = (scope_clause(scope)
              + "Illustrate a social post. The picture should be recognisably "
              "about what the post says: someone who reads the post and then "
              "looks at the picture should see the connection.\n\n"
              f"Headline: {subject}\n"
              + (f"Post: {copy[:600]}\n" if copy else "")
              + "\nFind the one idea in it and draw that, as a single clear "
                "image rather than a collage of several. Prefer the concrete "
                "thing the post is actually about over a metaphor about "
                "markets in general.\n\n"
              + GEMINI_STYLE.replace(
                    "{palette}",
                    PALETTES.get((b.get("theme") or "dark").lower(),
                                 PALETTES["dark"])))

    shapes = b.get("shapes") or ["portrait", "landscape"]
    # Landscape is drawn first and the other shapes are recomposed from it, so
    # the pair is one picture in two crops rather than two unrelated ones. It
    # leads because it holds the most scene: going wide-to-tall is a matter of
    # what to leave out, where tall-to-wide has to invent the sides.
    order = [s for s in ("landscape", "portrait", "square") if s in shapes]
    ref = None
    con, out, work = db(), [], Path(tempfile.mkdtemp(prefix="gencard-"))
    try:
        for shape in order:
            size = sl.SHAPES.get(shape)
            if not size:
                continue
            ask = prompt if ref is None else recompose_prompt(
                shape, scope, (b.get("theme") or "dark").lower())
            try:
                blob, mime = gemini_image(ask, shape, key, ref=ref)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                return jsonify(error=f"Gemini said {e.code}: {detail}"), 502
            except Exception as e:
                return jsonify(error=f"Could not draw the {shape}: "
                                     f"{e}"[:300]), 502

            # Whatever shape came back, the file the desk stores is the exact
            # size the platform wants, so nothing is cropped later by surprise.
            raw = work / f"raw-{shape}"
            raw.write_bytes(blob)
            exact = work / f"{shape}.jpg"
            try:
                sl.fit(raw, exact, size[0], size[1], mode="auto")
                final, fmime = exact.read_bytes(), "image/jpeg"
            except (RuntimeError, ValueError):
                final, fmime = blob, mime      # keep what Gemini gave us

            if ref is None:
                ref = (blob, mime)      # what the other shapes are made from

            row = _keep_image(con, prof, final, fmime, b.get("url") or None,
                              f"{shape} image", subject[:200], "gemini")
            if row:
                row["shape"] = shape
                out.append(row)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not out:
        return jsonify(error="Nothing was drawn."), 500
    return jsonify(media=out, model=GEMINI_MODEL)


# How far down a scrim takes the picture. Named rather than free-typed so the
# two shapes of one post always get the same treatment.
SCRIMS = {"light": 0.25, "medium": 0.45, "heavy": 0.62}

# The full lockup, mark and wordmark, white on transparent, supplied by
# Stephen from the brand files. The logo is also published as SVG, which is no
# use here: ffmpeg has no SVG decoder and refuses the stream outright.
BRAND_MARK = ROOT / "brand" / "logo-lockup.png"
MARK_WIDTH = 0.26           # of the frame width. A wide lockup, not a square mark.
MARK_SIDE = 0.055           # margin in from the edges, mark and url alike
# A tall picture is not shown tall in a feed. Instagram crops 9:16 to the
# middle 4:5 while people scroll (3:4 in its newer grid, which is taller, so
# 4:5 is the one to fit), and TikTok and Reels lay their own tabs and caption
# over the top and bottom. Corner furniture on a portrait sits inside the
# middle 4:5 band, or the logo, the instrument and the link are what get cut.
FEED_SAFE = 5 / 4           # height over width of the band that is always seen

# Everything small on the frame is sized off the SHORT edge, not the height.
# Both shapes are 1080 on their short edge, so a landscape and a portrait come
# out matching. Sized off the height they did not: landscape is 1080 tall
# against portrait's 1920, so the same fraction drew the corner text at a bit
# over half the size and the landscape looked starved.
URL_SIZE = 0.034            # of the short edge, when none is asked for
URL_PCT = (1.0, 6.0)        # what the url's own -/+ buttons may reach
TAG_SIZE = 0.040            # the instrument name, off the short edge
STAMP_SIZE = 0.027          # the date and time, under the instrument
SCORE_SIZE = 0.038          # the scoring lines, under the headline
SCORE_BUMP = 2              # points on top of that, asked for by eye

# Direction gets its own mark and colour, drawn separately from the words so it
# can be coloured on its own: ffmpeg gives one colour per drawtext. Space
# Grotesk has no triangles or filled circle, so the arrows are the real arrow
# glyphs and hold is a bullet, all of which it does carry.
MARKS = {"bullish": ("\u2191", "0x4ADE80"),     # up, green
         "bearish": ("\u2193", "0xF87171"),     # down, red
         "hold":    ("\u2022", "0xFBBF24")}     # bullet, amber

# How big the title starts, as a percent of frame height. slideshow only ever
# shrinks a caption to fit, never grows it, so the starting size is what
# decides how much of the frame the words fill. The old default of 4.5 left a
# headline sitting in the middle of a lot of empty picture.
TEXT_PCT = (3.0, 14.0)          # what the -/+ buttons may reach

# Space Grotesk ships as one variable font, and ffmpeg's drawtext cannot pick
# a weight off a variable axis: it renders the default instance whatever you
# ask for. So the weights are baked to static files once, with fontTools:
#   from fontTools.varLib.instancer import instantiateVariableFont
#   instantiateVariableFont(TTFont(src), {"wght": 700}, updateFontNames=False)
# fontTools is only needed to regenerate these, never to run the desk.
BRAND_FONTS = ROOT / "brand" / "fonts"
WEIGHTS = ("Light", "Regular", "Medium", "SemiBold", "Bold")


def face_for(name, weight):
    """A font file for the chosen face and weight.

    A baked brand weight wins. Otherwise a sibling static file of the same
    family is looked for, which is how the other families on this machine
    carry their weights, and failing that the face is used as it is.
    """
    import slideshow as sl
    have = sl.fonts()
    if (name or "").startswith("SpaceGrotesk"):
        f = BRAND_FONTS / f"SpaceGrotesk-{weight}.ttf"
        if f.exists():
            return str(f)
    if name and weight:
        stem = re.split(r"[-_]", name)[0]
        sib = have.get(f"{stem}-{weight}")
        if sib:
            return sib
    return have.get(name) or sl.default_font()


@app.post("/api/link/sections")
def link_sections():
    """The parts of a page, so a post can be about one of them.

    A page is usually several things, and the opening summary is only the
    first. Read live rather than stored, because a page changes and the
    library row is about the picture, not the prose.
    """
    import link as linkmod
    b = request.get_json(force=True)
    url = (b.get("url") or "").strip()
    if not url:
        return jsonify(error="No page to read."), 400
    try:
        page = linkmod.fetch(url)
    except Exception as e:
        return jsonify(error=f"Could not read that page: {e}"[:300]), 502

    parts = []
    if page.get("excerpt"):
        parts.append(dict(heading=page.get("title") or "", 
                          text=page["excerpt"], lead=True))
    parts += [dict(heading=s["heading"], text=s["text"], lead=False)
              for s in (page.get("sections") or []) if s.get("text")]
    if not parts:
        return jsonify(error="Nothing readable on that page."), 404
    return jsonify(sections=parts, url=page.get("url") or url)


@app.post("/api/media/render")
def media_render():
    """The posting copy of a picture: scrim laid down, title burned on.

    Two steps in one row, because they are never wanted apart. The source
    stays in the library untouched, so a regenerated title does not cost the
    artwork, and what the text sits on is exactly what goes out rather than a
    preview that only looks right in the desk.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM media WHERE id=?",
                      (int(b.get("media") or 0),)).fetchone()
    if not row:
        return jsonify(error="No such picture."), 404
    if row["kind"] != "image":
        return jsonify(error="That is not a photo."), 400

    text = (b.get("text") or "").strip()
    if not text:
        return jsonify(error="No title to draw. Write the copy first."), 400
    name = (b.get("scrim") or "medium").lower()
    amount = None if name == "none" else SCRIMS.get(name)
    if name != "none" and amount is None:
        return jsonify(error=f"Do not know the scrim {name!r}."), 400

    w, h = row["width"] or 0, row["height"] or 0
    size = (sl.SHAPES["portrait"] if h > w else
            sl.SHAPES["landscape"] if w > h else sl.SHAPES["square"])

    src = MEDIA / row["path"]
    work = Path(tempfile.mkdtemp(prefix="render-"))
    try:
        base = src
        if amount:
            dark = work / "dark.png"
            r = subprocess.run(
                [sl.exe(), "-y", "-i", str(src), "-vf",
                 f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{amount}:t=fill",
                 str(dark)], capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify(error=f"Could not darken it: "
                                     f"{r.stderr.strip()[-300:]}"), 500
            base = dark

        want = (b.get("font") or "").strip()
        if not want:
            # Whatever the profile calls its own, before slideshow's default.
            for prof in profiles().values():
                want = ((prof or {}).get("brand") or {}).get("font") or ""
                if want:
                    break
        weight = (b.get("weight") or "Medium")
        face = face_for(want, weight if weight in WEIGHTS else "Medium")
        try:
            pct = float(b.get("text_pct") or 8.6)
        except (TypeError, ValueError):
            pct = 8.6
        pct = max(TEXT_PCT[0], min(TEXT_PCT[1], pct))

        # A shadow is what makes a caption survive a busy picture. Dark is the
        # usual answer under white lettering; light is for the rare pale frame.
        shadow = (b.get("shadow") or "dark").lower()
        depth = 0 if shadow == "none" else max(3, int(size[1] * 0.004))
        shade = {"dark": "black@0.72", "light": "white@0.55"}.get(shadow, "black@0.72")

        titled = work / "titled.png"
        # Off the short edge, like the rest of the furniture. Sized off the
        # height, the same percentage drew a portrait headline nearly twice
        # the landscape one, which on a tall frame ran to four lines and
        # pushed the scoring down into the picture.
        cap_px = int(min(size) * pct / 100.0)
        where = b.get("position") or "middle"
        try:
            sl.still(base, titled, size=size, caption=text, font=face,
                     cap_size=cap_px,
                     cap_shadow=depth, cap_shadow_color=shade,
                     cap_position=where,
                     cap_align=b.get("align") or "center", guides=False)
        except (RuntimeError, ValueError) as e:
            return jsonify(error=f"Could not draw the title: {e}"[:300]), 500

        out = work / "render.jpg"
        w2, h2 = size
        short = min(w2, h2)          # what the small furniture is sized from
        pad = int(w2 * MARK_SIDE)
        # Pushed in from the top and bottom on a portrait, so the corners sit
        # inside what a feed actually shows. Nothing on a landscape.
        vin = max(0, int((h2 - w2 * FEED_SAFE) / 2)) if h2 > w2 else 0
        ins, filters, last = ["-i", str(titled)], [], "0:v"

        # The mark sits top right. The url, when it is wanted, sits bottom left,
        # so the two never crowd each other whatever the artwork does.
        if b.get("mark", True) and BRAND_MARK.exists():
            ins += ["-i", str(BRAND_MARK)]
            filters.append(f"[1:v]scale={int(w2 * MARK_WIDTH)}:-1[m]")
            filters.append(f"[{last}][m]overlay=x=W-w-{pad}:y={vin + pad}:"
                           f"format=auto[k]")
            last = "k"

        # The instrument, upper left. Same corner in both shapes, so a pair
        # reads as one set, and diagonally opposite the mark.
        tag = (b.get("label") or "").strip()
        if tag:
            tf = work / "tag.txt"
            tf.write_text(tag, encoding="utf-8")
            filters.append(
                f"[{last}]drawtext=fontfile='{face_for(want, 'Bold')}':"
                f"textfile='{tf}':expansion=none:"
                f"fontsize={int(short * TAG_SIZE)}:fontcolor=white@0.92:"
                f"shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"x={pad}:y={vin + pad}[t]")
            last = "t"

        # When, under the instrument. Tucked directly beneath it so the two
        # read as one label rather than two things in the same corner.
        stamp = (b.get("stamp") or "").strip()
        if stamp:
            sf = work / "stamp.txt"
            sf.write_text(stamp, encoding="utf-8")
            filters.append(
                f"[{last}]drawtext=fontfile='{face_for(want, 'Regular')}':"
                f"textfile='{sf}':expansion=none:"
                f"fontsize={int(short * STAMP_SIZE)}:fontcolor=white@0.72:"
                f"shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"x={pad}:y={vin + pad + int(short * TAG_SIZE * 1.45)}[w]")
            last = "w"

        # The scoring, directly under the headline. Where the headline ends is
        # worked out with the same two functions that drew it, rather than
        # guessed at: slideshow wraps and shrinks to fit, so the block's height
        # is only known after that has happened.
        scores = [s for s in (b.get("scores") or []) if str(s).strip()][:2]
        if scores:
            lines, drawn = sl.caption_layout(text, face, cap_px, w2)
            lh, top = sl.caption_box(len(lines), drawn, w2, h2, where)
            y = int(top + lh * len(lines) + h2 * 0.012)
            sz = int(short * SCORE_SIZE) + SCORE_BUMP
            sface = face_for(want, "SemiBold")
            mark, colour = MARKS.get((b.get("direction") or "").lower(),
                                     (None, None))

            for i, line in enumerate(scores):
                line = str(line)
                row_y = y + i * int(sz * 1.45)
                # The direction mark rides on whichever line names it, in its
                # own colour. Both halves are measured so the pair can be
                # centred as one: ffmpeg can centre a drawtext but not a pair.
                if mark and i == len(scores) - 1:
                    gap = int(sz * 0.38)
                    mw = sl.text_width(mark, sface, sz)
                    tw = sl.text_width(line, sface, sz)
                    x0 = int((w2 - (mw + gap + tw)) / 2)
                    mf = work / "mark.txt"
                    mf.write_text(mark, encoding="utf-8")
                    filters.append(
                        f"[{last}]drawtext=fontfile='{sface}':"
                        f"textfile='{mf}':expansion=none:fontsize={sz}:"
                        f"fontcolor={colour}:shadowcolor=black@0.6:"
                        f"shadowx=2:shadowy=2:x={x0}:y={row_y}[m{i}]")
                    last = f"m{i}"
                    sf = work / f"score{i}.txt"
                    sf.write_text(line, encoding="utf-8")
                    filters.append(
                        f"[{last}]drawtext=fontfile='{sface}':"
                        f"textfile='{sf}':expansion=none:fontsize={sz}:"
                        f"fontcolor=white@0.86:shadowcolor=black@0.6:"
                        f"shadowx=2:shadowy=2:x={x0 + mw + gap}:y={row_y}[s{i}]")
                    last = f"s{i}"
                    continue

                sf = work / f"score{i}.txt"
                sf.write_text(line, encoding="utf-8")
                filters.append(
                    f"[{last}]drawtext=fontfile='{sface}':"
                    f"textfile='{sf}':expansion=none:"
                    f"fontsize={sz}:fontcolor=white@0.86:"
                    f"shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                    f"x=(w-text_w)/2:y={row_y}[s{i}]")
                last = f"s{i}"

        url_text = (b.get("url_text") or "").strip()
        if url_text:
            # Through a file, so a url full of slashes and colons never has to
            # be escaped into a filter string.
            uf = work / "url.txt"
            uf.write_text(url_text, encoding="utf-8")
            face_small = face_for(want, "Medium")
            try:
                upct = float(b.get("url_pct") or URL_SIZE * 100)
            except (TypeError, ValueError):
                upct = URL_SIZE * 100
            upct = max(URL_PCT[0], min(URL_PCT[1], upct))
            filters.append(
                f"[{last}]drawtext=fontfile='{face_small}':"
                f"textfile='{uf}':expansion=none:"
                f"fontsize={int(short * upct / 100.0)}:fontcolor=white@0.78:"
                f"shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"x={pad}:y=h-th-{pad + vin}[u]")
            last = "u"

        cmd = [sl.exe(), "-y"] + ins
        if filters:
            cmd += ["-filter_complex", ";".join(filters), "-map", f"[{last}]"]
        cmd += ["-q:v", "3", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return jsonify(error=f"Could not finish the render: "
                                 f"{r.stderr.strip()[-300:]}"), 500

        made = _keep_image(con, row["profile"], out.read_bytes(), "image/jpeg",
                           row["url"], f"{row['title'] or 'photo'} (render)",
                           text, "render")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not made:
        return jsonify(error="Nothing came back."), 500
    made["from_media"] = row["id"]
    return jsonify(media=made)


@app.post("/api/media/scrim")
def media_scrim():
    """A darkened copy, so caption text has something to sit on.

    The generated art is bright enough that white lettering over it is hard
    work. Burning the scrim into its own library row rather than doing it in
    CSS means what the caption is drawn over is exactly what goes out, and the
    original is untouched next to it.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM media WHERE id=?",
                      (int(b.get("media") or 0),)).fetchone()
    if not row:
        return jsonify(error="No such picture."), 404
    if row["kind"] != "image":
        return jsonify(error="That is not a photo."), 400
    name = (b.get("amount") or "medium").lower()
    amount = SCRIMS.get(name)
    if amount is None:
        return jsonify(error=f"Do not know the scrim {name!r}."), 400

    src = MEDIA / row["path"]
    work = Path(tempfile.mkdtemp(prefix="scrim-"))
    try:
        out = work / "dark.jpg"
        r = subprocess.run(
            [sl.exe(), "-y", "-i", str(src), "-vf",
             f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{amount}:t=fill",
             "-q:v", "3", str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            return jsonify(error=f"Could not darken it: "
                                 f"{r.stderr.strip()[-300:]}"), 500
        made = _keep_image(con, row["profile"], out.read_bytes(), "image/jpeg",
                           row["url"], f"{row['title'] or 'photo'} ({name})",
                           row["note"], "scrim")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not made:
        return jsonify(error="Nothing came back."), 500
    return jsonify(media=made, amount=name)


@app.post("/api/media/reshape")
def media_reshape():
    """The same picture in the other orientation, as its own library row.

    A landscape hero is wrong for a vertical feed and a portrait card is wrong
    for LinkedIn, so each shape gets a file rather than letting the platform
    crop it. "blur" keeps every pixel over a blurred copy of itself; "fill"
    crops to the frame. Neither invents anything that was not in the photo.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM media WHERE id=?",
                      (int(b.get("media") or 0),)).fetchone()
    if not row:
        return jsonify(error="No such picture."), 404
    if row["kind"] != "image":
        return jsonify(error="That is not a photo."), 400
    shape = b.get("shape") or "portrait"
    size = sl.SHAPES.get(shape)
    if not size:
        return jsonify(error=f"Do not know the shape {shape!r}."), 400

    w, h = size
    work = Path(tempfile.mkdtemp(prefix="reshape-"))
    try:
        out = work / f"{shape}.jpg"
        try:
            sl.fit(MEDIA / row["path"], out, w, h,
                   mode=b.get("mode") or "auto")
        except (RuntimeError, ValueError) as e:
            return jsonify(error=f"Could not reshape it: {e}"[:300]), 500
        made = _keep_image(con, row["profile"], out.read_bytes(), "image/jpeg",
                           row["url"], f"{row['title'] or 'photo'} ({shape})",
                           row["note"], "reshaped")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not made:
        return jsonify(error="Nothing came back from the reshape."), 500
    made["shape"] = shape
    return jsonify(media=made)


@app.post("/api/blogs/render")
def blog_render():
    """Draw the copy onto a plain card, portrait and landscape.

    For a blog with no hero, and for any post that wants a graphic of its own
    words. The background is the profile's brand ink, so the two shapes read as
    one pair. slideshow.still() does the typesetting, which means the caption
    lands inside the same safe areas a video caption would.
    """
    import slideshow as sl
    b = request.get_json(force=True)
    prof = (b.get("profile") or "").strip()
    if prof not in profiles():
        return jsonify(error="Choose a profile first."), 400
    text = (b.get("text") or "").strip()
    if not text:
        return jsonify(error="Write the copy first, then draw it."), 400
    # Long copy typesets to a wall. The card carries the hook, not the post.
    if len(text) > 300:
        text = text[:297].rsplit(" ", 1)[0] + "..."

    ink = ((profiles().get(prof) or {}).get("brand") or {}).get("ink") or "#272C28"
    shapes = b.get("shapes") or ["portrait", "landscape"]
    con, out, work = db(), [], Path(tempfile.mkdtemp(prefix="blogcard-"))
    try:
        for shape in shapes:
            size = sl.SHAPES.get(shape)
            if not size:
                continue
            w, h = size
            bg = work / f"bg-{shape}.png"
            r = subprocess.run([sl.exe(), "-y", "-f", "lavfi", "-i",
                                f"color=c={ink.lstrip('#')}:s={w}x{h}",
                                "-frames:v", "1", str(bg)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify(error=f"Could not make the background: "
                                     f"{r.stderr.strip()[-300:]}"), 500
            card = work / f"card-{shape}.png"
            try:
                sl.still(bg, card, size=size, caption=text,
                         font=sl.default_font(), cap_position="middle",
                         cap_align="center", guides=False)
            except (RuntimeError, ValueError) as e:
                return jsonify(error=f"Could not draw the {shape} card: "
                                     f"{e}"[:300]), 500
            row = _keep_image(con, prof, card.read_bytes(), "image/png",
                              b.get("url") or None,
                              f"{shape} card", text, "rendered")
            if row:
                row["shape"] = shape
                out.append(row)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not out:
        return jsonify(error="Nothing was drawn."), 500
    return jsonify(media=out)


# The Guavy API. The desk reads the Wire through this, and it is a different
# key from the one my own tooling uses: this process has to authenticate for
# itself.
GUAVY_BASE = os.environ.get("GUAVY_API_BASE", "https://guavy.com/api/v2")

# How deep to read a symbol's feed when counting a day's articles. The Wire is
# busier than it looks: Gold alone ran 239 briefs in 24 hours, so a limit of 20
# reported 20 and meant nothing. Counting properly costs tokens, though, and a
# sweep asks every symbol in every market. 250 is deep enough to be true for
# most tickers and a count that hits the ceiling is reported with a "+" rather
# than pretending to be exact. Raise it with GUAVY_COUNT_DEPTH.
COUNT_DEPTH = int(os.environ.get("GUAVY_COUNT_DEPTH", "250"))

# The desk's names, and the API's. They are not the same words, and house
# style is "US Equities" where the API says "stocks".
MARKETS = [("crypto", "Crypto", "crypto"),
           ("stocks", "US Equities", "stocks"),
           ("forex", "FX", "forex"),
           ("commodities", "Commodities", "commodities")]


def markets_cfg():
    return (channels_cfg().get("markets") or {})


def market_mode(market):
    """auto or manual, per sub-tab. Stored in the desk, not the browser: a
    poller running with nobody watching has to be able to read it."""
    return setting(f"markets.mode.{market}", "manual")


@app.get("/api/markets")
def api_markets():
    """What the Markets tab is, before it has anything to show.

    Deliberately honest about not being wired up: the tab exists, its pacing
    is configured, and the one thing missing is a key.
    """
    cfg = markets_cfg()
    chans = cfg.get("channels") or {}
    con = db()
    only = request.args.get("profile") or ""
    tz = channels_cfg().get("timezone")
    now = datetime.now(ZoneInfo(tz) if (ZoneInfo and tz) else None)
    today = now.date().isoformat()

    # Pacing is per market per channel: each market carries the same allowance
    # and keeps its own timer, so four markets at one a day is four posts on
    # that account. Usage is therefore counted per (channel, market), and the
    # All view sums it. posts.asset holds "market:SYMBOL", which is where the
    # market comes from.
    keys = [k for k, _, _ in MARKETS]
    last, spent = {}, {}
    for r in con.execute("""SELECT channel, asset, MAX(updated) AS t,
                                   COUNT(*) AS n, date
                            FROM posts
                            WHERE campaign='markets' AND profile=?
                            GROUP BY channel, asset""", (only,)):
        mk = (r["asset"] or "").split(":")[0]
        if mk not in keys:
            continue
        prev = last.get((r["channel"], mk))
        if not prev or (r["t"] or "") > prev:
            last[(r["channel"], mk)] = r["t"]
    for r in con.execute("""SELECT channel, asset, COUNT(*) AS n FROM posts
                            WHERE campaign='markets' AND profile=? AND date=?
                            GROUP BY channel, asset""", (only, today)):
        mk = (r["asset"] or "").split(":")[0]
        if mk in keys:
            spent[(r["channel"], mk)] = spent.get((r["channel"], mk), 0) + r["n"]

    quiet = cfg.get("quiet_hours") or []
    in_quiet = False
    if len(quiet) == 2:
        a, b = int(quiet[0]), int(quiet[1])
        h = now.hour
        in_quiet = (a <= h or h < b) if a > b else (a <= h < b)

    accounts = ((profiles().get(only) or {}).get("zernio") or {}).get("accounts") or {}

    def state(chan, v, market):
        """One market's standing on one channel."""
        # A channel with no Zernio account cannot post at all, whatever the
        # pacing says. Reporting it as free made the table disagree with the
        # autoposter, which skips it.
        if accounts.get(chan) in (None, "", "TODO"):
            return dict(last=None, hours_since=None, ready_at=None,
                        used_today=0, blocked="not connected")
        if v.get("paused"):
            return dict(last=None, hours_since=None, ready_at=None,
                        used_today=0, blocked=str(v["paused"]))
        if v.get("only") and market not in v["only"]:
            return dict(last=None, hours_since=None, ready_at=None,
                        used_today=0, blocked=f"{', '.join(v['only'])} only")
        mine = market_spec(v, market)
        gap = float(mine.get("min_hours") or 0)
        cap = int(mine.get("per_day") or 0)
        used = spent.get((chan, market), 0)
        seen = last.get((chan, market))
        hours_since, ready_at = None, None
        if seen:
            try:
                then = datetime.fromisoformat(seen)
                hours_since = round((datetime.now() - then).total_seconds() / 3600, 1)
                ready_at = (then + timedelta(hours=gap)).strftime("%H:%M")
            except ValueError:
                pass
        # The channel-wide spacing, the same rule channel_due applies.
        spread = spread_hours(cfg, v)
        any_seen = max((t for (c, _m), t in last.items() if c == chan and t),
                       default=None)
        spaced = None
        if any_seen and spread:
            try:
                then = datetime.fromisoformat(any_seen)
                spaced = (datetime.now() - then).total_seconds() / 3600
                if spaced < spread:
                    nxt = (then + timedelta(hours=spread)).strftime("%H:%M")
                    ready_at = max(ready_at or "", nxt)
            except ValueError:
                spaced = None
        why = ("quiet hours" if in_quiet
               else "daily cap reached" if cap and used >= cap
               else f"too soon, {gap}h gap"
               if (hours_since is not None and hours_since < gap)
               else f"spacing, {spread:g}h between markets"
               if (spaced is not None and spaced < spread) else "")
        return dict(last=seen, hours_since=hours_since, ready_at=ready_at,
                    used_today=used, blocked=why, spread_hours=spread)

    out = []
    for chan, v in chans.items():
        per = {m: state(chan, v, m) for m in keys}
        free = [m for m in keys if not per[m]["blocked"]]
        # The All row is the sum of the allowances, not one of them.
        out.append(dict(
            channel=chan, per_market=per, **v,
            total=dict(per_day=sum(int(market_spec(v, m).get("per_day") or 0)
                                   for m in keys),
                       used_today=sum(p["used_today"] for p in per.values()),
                       free_markets=len(free), markets=len(keys),
                       blocked=("" if free else
                                "quiet hours" if in_quiet else
                                "every market is waiting"))))

    return jsonify(
        markets=[dict(key=k, label=lab, api=api, mode=market_mode(k))
                 for k, lab, api in MARKETS],
        channels=out,
        per_market_allowance=True,
        in_quiet=in_quiet,
        quiet_hours=cfg.get("quiet_hours") or [],
        connected=bool(os.environ.get("GUAVY_API_KEY")),
        base=GUAVY_BASE)


@app.post("/api/markets/<market>/mode")
def api_market_mode(market):
    if market not in {k for k, _, _ in MARKETS}:
        return jsonify(error=f"No such market {market!r}."), 404
    b = request.get_json(silent=True) or {}
    mode = "auto" if (b.get("mode") == "auto") else "manual"
    if mode == "auto" and not os.environ.get("GUAVY_API_KEY"):
        return jsonify(error="GUAVY_API_KEY is not set, so nothing can run "
                             "automatically yet."), 400
    set_setting(f"markets.mode.{market}", mode)
    return jsonify(ok=True, mode=mode)


def last_markets_ms(con, profile, channel=None):
    """When Markets last posted, in epoch milliseconds. The cadence floor and
    the "since" of a scan are both measured from here."""
    q = ("SELECT MAX(updated) AS t FROM posts WHERE campaign='markets' "
         "AND profile=?" + (" AND channel=?" if channel else ""))
    args = [profile] + ([channel] if channel else [])
    r = con.execute(q, args).fetchone()
    if not r or not r["t"]:
        return 0.0
    try:
        return datetime.fromisoformat(r["t"]).timestamp() * 1000
    except ValueError:
        return 0.0


SYNC_PAGE = int(os.environ.get("GUAVY_SYNC_PAGE", "100"))


def sync_symbol(con, market, symbol, deep=False):
    """Top up one symbol's feed. Returns how many articles were new.

    Reads down the feed until it reaches the newest article this symbol was
    already read to, which is the only honest definition of caught up. Testing
    "do we hold this article" instead stops at the first story that reached
    this symbol via another, which on a well-connected coin is immediately.
    """
    import guavy
    try:
        got = guavy.briefs(market, symbol, SYNC_PAGE)
    except guavy.GuavyError:
        return 0
    row = con.execute(
        "SELECT last_ts FROM symbol_sync WHERE market=? AND symbol=?",
        (market, symbol)).fetchone()
    mark = 0 if deep else int((row["last_ts"] if row else 0) or 0)

    now = datetime.now().isoformat(timespec="seconds")
    new, highest = 0, mark
    for b in got:
        aid = b.get("article_id")
        if not aid:
            continue
        ts = int(guavy.when(b))
        if ts and ts <= mark:
            break                      # this symbol has been read this far
        highest = max(highest, ts)
        if not con.execute("SELECT 1 FROM briefs WHERE article_id=?",
                           (aid,)).fetchone():
            con.execute(
                """INSERT OR IGNORE INTO briefs (article_id,market,title,body,
                   day,ts,clout,sentiment,speculation,fud_fomo,bias,tone,
                   impacted,seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (aid, market, b.get("title") or "", b.get("body") or "",
                 b.get("date") or "", ts,
                 b.get("clout"), b.get("sentiment"), b.get("speculation_score"),
                 b.get("fud_fomo_score"), b.get("fud_fomo_bias"), b.get("tone"),
                 json.dumps(b.get("impacted_coins") or []), now))
            new += 1
        # The link is written whether or not the article was new to us.
        con.execute("""INSERT OR IGNORE INTO brief_symbols
                       (article_id,market,symbol) VALUES (?,?,?)""",
                    (aid, market, symbol))

    if highest > mark:
        con.execute("""INSERT INTO symbol_sync (market,symbol,last_ts,updated)
                       VALUES (?,?,?,?)
                       ON CONFLICT(market,symbol) DO UPDATE SET
                         last_ts=excluded.last_ts, updated=excluded.updated""",
                    (market, symbol, highest, now))
    con.commit()
    return new


@app.post("/api/wire/sync")
def wire_sync():
    """Top up the mirror. One market, or all of them."""
    import guavy
    b = request.get_json(silent=True) or {}
    markets = ([b["market"]] if b.get("market") in guavy.MARKETS
               else list(guavy.MARKETS))
    deep = bool(b.get("deep"))
    con = db()
    added, t0 = {}, time.time()
    for m in markets:
        n = 0
        try:
            for s in guavy.symbols(m):
                n += sync_symbol(con, m, s, deep)
        except guavy.GuavyError as e:
            return jsonify(error=str(e)), 502
        added[m] = n
    set_setting("wire.synced", datetime.now().isoformat(timespec="seconds"))
    return jsonify(added=added, seconds=round(time.time() - t0, 1),
                   held=con.execute("SELECT COUNT(*) FROM briefs").fetchone()[0])


# The mirror only grows when something tops it up, and the autoposter reads
# nothing else. Left to the Sync button it went a day stale, every market read
# zero articles in the last 24 hours, and the picker was choosing from
# yesterday's leftovers. A full sync is about four minutes.
WIRE_EVERY = int(os.environ.get("WIRE_EVERY", "3600"))   # seconds


def wire_loop():
    """Sync the Wire whenever the mirror is older than WIRE_EVERY."""
    while True:
        try:
            if os.environ.get("GUAVY_API_KEY"):
                with app.app_context():
                    last = setting("wire.synced")
                    age = ((datetime.now() - datetime.fromisoformat(last))
                           .total_seconds() if last else None)
                if age is None or age >= WIRE_EVERY:
                    with app.test_request_context(json={}):
                        got = wire_sync()
                    body = got[0] if isinstance(got, tuple) else got
                    app.logger.info("wire sync: %s", body.get_json())
            # The auto-made pictures clean-up, once a day.
            with app.app_context():
                if setting("cleanup.last") != date.today().isoformat():
                    out = clean_auto_media(db())
                    set_setting("cleanup.last", date.today().isoformat())
                    app.logger.info("media cleanup: %s", out)
            # A follower reading once a day, whoever opens the Numbers tab.
            # The count was only recorded when someone looked, which left
            # holes in the history a chart of it has to step over.
            if os.environ.get("ZERNIO_API_KEY"):
                with app.app_context():
                    have = db().execute(
                        "SELECT 1 FROM followers WHERE day=? LIMIT 1",
                        (date.today().isoformat(),)).fetchone()
                if not have:
                    with app.test_request_context(query_string={"fresh": "1"}):
                        followers()
        except Exception as e:                       # never let the loop die
            try:
                app.logger.warning("wire sync loop: %s", e)
            except Exception:
                pass
        time.sleep(300)


def wire_counts(con, hours=24):
    """Articles per market in the window, straight out of the mirror."""
    cut = int((time.time() - hours * 3600) * 1000)
    return {r["market"]: r["n"] for r in con.execute(
        "SELECT market, COUNT(*) n FROM briefs WHERE ts>=? GROUP BY market",
        (cut,))}


@app.get("/api/markets/<market>/symbols")
def market_symbols(market):
    """Every ticker in a market, with how busy it has been and how it is drawn.

    Counted out of the mirror. A story touching three symbols counts once for
    each, deliberately: the question is how much has been written that touches
    this ticker.
    """
    import guavy
    if market not in guavy.MARKETS:
        return jsonify(error=f"No such market {market!r}."), 404
    con = db()
    try:
        syms = guavy.symbols(market)
    except guavy.GuavyError as e:
        return jsonify(error=str(e)), 502

    cut = int((time.time() - 24 * 3600) * 1000)
    counts = {r["symbol"]: r["n"] for r in con.execute(
        """SELECT bs.symbol, COUNT(*) n FROM brief_symbols bs
           JOIN briefs b ON b.article_id=bs.article_id
           WHERE bs.market=? AND b.ts>=? GROUP BY bs.symbol""", (market, cut))}
    newest = {}
    for r in con.execute(
            """SELECT bs.symbol, b.article_id, b.title, b.ts FROM brief_symbols bs
               JOIN briefs b ON b.article_id=bs.article_id
               WHERE bs.market=? ORDER BY b.ts""", (market,)):
        newest[r["symbol"]] = (r["article_id"], r["title"])

    out = []
    for s in syms:
        scope = brand_scope(market, s) or {}
        aid, title = newest.get(s, (None, ""))
        out.append(dict(
            symbol=s, name=scope.get("name") or s,
            scope=" ".join(x for x in (scope.get("visual"), scope.get("colour"))
                           if x) or "",
            has_scope=bool(scope), count24=counts.get(s, 0),
            latest=title or "",
            url=guavy.wire_url(market, aid, resolve=False) if aid else ""))
    out.sort(key=lambda d: (-d["count24"], d["symbol"]))
    return jsonify(market=market, symbols=out,
                   articles24=wire_counts(con).get(market, 0))


def _generate_art(profile, title, copy, market, symbol, theme, shapes):
    """The Gemini call, without the request around it."""
    import slideshow as sl
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    scope = brand_scope(market, symbol or "")
    prompt = (scope_clause(scope)
              + "Illustrate a social post. The picture should be recognisably "
              "about what the post says: someone who reads the post and then "
              "looks at the picture should see the connection.\n\n"
              f"Headline: {title}\n"
              + (f"Post: {copy[:600]}\n" if copy else "")
              + "\nFind the one idea in it and draw that, as a single clear "
                "image rather than a collage of several. Prefer the concrete "
                "thing the post is actually about over a metaphor about "
                "markets in general.\n\n"
              + GEMINI_STYLE.replace("{palette}", PALETTES.get(
                    theme, PALETTES["dark"])))

    order = [s for s in ("landscape", "portrait", "square") if s in shapes]
    ref, out = None, []
    con, work = db(), Path(tempfile.mkdtemp(prefix="autoart-"))
    try:
        for shape in order:
            size = sl.SHAPES.get(shape)
            if not size:
                continue
            ask = prompt if ref is None else recompose_prompt(shape, scope, theme)
            blob, mime = gemini_image(ask, shape, key, ref=ref)
            raw = work / f"raw-{shape}"
            raw.write_bytes(blob)
            exact = work / f"{shape}.jpg"
            try:
                sl.fit(raw, exact, size[0], size[1], mode="auto")
                final, fmime = exact.read_bytes(), "image/jpeg"
            except (RuntimeError, ValueError):
                final, fmime = blob, mime
            if ref is None:
                ref = (blob, mime)
            row = _keep_image(con, profile, final, fmime, None,
                              f"{shape} image", title[:200], "gemini")
            if row:
                out.append(row)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def _render_one(row, text, **opts):
    """One finished picture, the same call the sheet makes."""
    with app.test_request_context(json=dict(media=row["id"], text=text, **opts)):
        got = media_render()
    body = got[0] if isinstance(got, tuple) else got
    data = body.get_json() or {}
    return data.get("media")


def _compose_one(con, cfg, profile, chan, media, copy, asset, label, title,
                 send=True, at_minutes=None, campaign="markets",
                 links_to_comment=False):
    """Create the post row and hand it to Zernio. Returns the post id.

    `links_to_comment` puts the link in the first comment on a channel that
    otherwise drops links, which is how ads keep their tracked link on
    LinkedIn while Wire posts there carry none.
    """
    import push_zernio as _pz
    ch = (cfg.get("channels") or {}).get(chan) or {}
    plat = ch.get("platform") or chan.split("_")[0]
    mine = for_channel(media, ch.get("prefer_shape"))
    head = mine[0] if mine else media[0]

    rule = ch
    if links_to_comment and (ch.get("drop_links") or ch.get("links_in_first_comment")):
        rule = dict(ch, drop_links=False, links_in_first_comment=True)
    text, first = split_link(copy, rule)

    lim = max_chars(cfg, chan)
    if lim and body_length(text, plat) > lim:
        return None                      # too long for this channel, skip it

    # "soon" is two minutes out. A given offset pins the slot instead, which
    # is how a run of posts is staggered rather than landing together.
    if at_minutes is None:
        row = slot_for(con, cfg, profile, chan, head, "soon", None, None)
    else:
        z = ZoneInfo(cfg.get("timezone")) if (ZoneInfo and cfg.get("timezone")) else None
        when = datetime.now(z) + timedelta(minutes=int(at_minutes))
        row = slot_for(con, cfg, profile, chan, head, "man",
                       when.strftime("%Y-%m-%d"), when.strftime("%H:%M"))
    if not row:
        return None
    prof_data = profiles().get(profile) or {}
    tags_h = hashtags_for(prof_data, plat,
                          asset if campaign == "markets" else None)
    # campaign='markets' is what every gate keys on: the cadence, the daily
    # cap, the pacing table and the never-twice list all filter on it. A slot
    # made for another campaign arrives as that campaign, and left
    # that way the autoposter cannot see its own work and never stops.
    con.execute("""UPDATE posts SET media_id=?, media_ids=?, copy=?,
                   first_comment=?, title=?, hashtags=?, asset=?,
                   asset_label=?, campaign=?,
                   state='approved', updated=? WHERE id=?""",
                (head["id"], json.dumps([m["id"] for m in mine]), text,
                 first, title[:200], json.dumps(tags_h), asset, label, campaign,
                 datetime.now().isoformat(timespec="seconds"), row["id"]))
    con.commit()

    r = subprocess.run([sys.executable, "push_zernio.py", "push",
                        "--id", str(row["id"])]
                       + ([] if at_minutes is not None else ["--soon"])
                       + ([] if (send and is_live()) else ["--dry-run"]),
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    app.logger.info("autopost %s %s (send=%s): %s", chan, asset, send,
                    ((r.stdout or "") + (r.stderr or "")).strip()[-200:])
    return row["id"]


# ---------------------------------------------------------------- autopost
#
# The one path that reaches an audience with nobody watching, so the gates are
# named rather than implied. All of these must be true before anything goes:
#
#   the desk is Live          the same switch that arms a hand push
#   AUTOPOST is on            a separate switch, off unless set
#   the market is on auto     per sub-tab, stored in the desk not the browser
#   the channel is due        min_hours since its last, under per_day, awake
#   a story clears the bar    the picker can and does refuse
#
# Defaults for a post nobody is watching. The theme is mixed so a feed does
# not become a wall of one ground.
# Per theme, both medium since 5 Oct 2026. The light ground needed it for
# white lettering to read, and the dark graphics now match it. Kept per theme
# so the two can be set apart again from .env.
AUTO_SCRIM = {"dark": os.environ.get("AUTO_SCRIM_DARK", "medium"),
              "light": os.environ.get("AUTO_SCRIM_LIGHT", "medium")}
AUTO_TEXT_PCT = float(os.environ.get("AUTO_TEXT_PCT", "8.0"))
AUTO_WEIGHT = os.environ.get("AUTO_WEIGHT", "Bold")
AUTO_TICK = int(os.environ.get("AUTO_TICK", "120"))     # seconds between looks

# A ceiling on one build. Every stage has its own timeout, but a stage that
# stalls still holds the one-at-a-time lock, and a run of eight posts stopped
# dead at the fourth twice because of it. Measured end to end a build is about
# a minute; this is generous and still bounded.
AUTO_BUDGET = int(os.environ.get("AUTO_BUDGET", "420"))


class OutOfTime(RuntimeError):
    pass


def _budget(t0, stage):
    """Raise rather than start a stage there is no time left for."""
    if time.time() - t0 > AUTO_BUDGET:
        raise OutOfTime(f"ran out of time before {stage} "
                        f"({AUTO_BUDGET}s budget)")


def autopost_on():
    return setting("autopost", "0") == "1"


def auto_theme(channel):
    """Dark or light at random, per channel, never three alike in a row.

    It used to alternate on a count of every markets post. Fired across two
    channels in turn, that gave LinkedIn every light one and Instagram every
    dark one. Random per post, remembered per channel, mixes both feeds.
    """
    key = f"theme.{channel}"
    recent = [t for t in (setting(key) or "").split(",") if t]
    pick = random.choice(("dark", "light"))
    if len(recent) >= 2 and recent[-1] == recent[-2] == pick:
        pick = "light" if pick == "dark" else "dark"
    set_setting(key, ",".join((recent + [pick])[-2:]))
    return pick


def channel_due(con, cfg, chan, market, profile, now=None):
    """Whether this market may post to this channel right now, and why not."""
    spec = ((cfg.get("channels") or {}).get(chan)) or {}
    if not spec:
        return False, "no markets pacing for this channel"
    # In the table but held: shown so its pacing is ready, never fired.
    if spec.get("paused"):
        return False, str(spec["paused"])
    if spec.get("only") and market not in spec["only"]:
        return False, f"{', '.join(spec['only'])} only"
    # A channel with no Zernio account can be paced all it likes; the push
    # would skip it and the desk would be left holding a post that can never
    # go anywhere. x and facebook are configured but not connected.
    z = ((profiles().get(profile) or {}).get("zernio") or {}).get("accounts") or {}
    if z.get(chan) in (None, "", "TODO"):
        return False, "no Zernio account for this channel"
    tz = channels_cfg().get("timezone")
    now = now or datetime.now(ZoneInfo(tz) if (ZoneInfo and tz) else None)

    quiet = cfg.get("quiet_hours") or []
    if len(quiet) == 2:
        a, b = int(quiet[0]), int(quiet[1])
        if (a <= now.hour or now.hour < b) if a > b else (a <= now.hour < b):
            return False, "quiet hours"

    today = now.date().isoformat()
    mine = market_spec(spec, market)
    used = con.execute(
        """SELECT COUNT(*) FROM posts WHERE campaign='markets' AND profile=?
           AND channel=? AND date=? AND asset LIKE ?""",
        (profile, chan, today, f"{market}:%")).fetchone()[0]
    if used >= int(mine.get("per_day") or 0):
        return False, f"{used} posted today, cap is {mine.get('per_day')}"

    last = con.execute(
        """SELECT MAX(updated) t FROM posts WHERE campaign='markets'
           AND profile=? AND channel=? AND asset LIKE ?""",
        (profile, chan, f"{market}:%")).fetchone()["t"]
    if last:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
            if gap < float(mine.get("min_hours") or 0):
                return False, (f"{gap:.1f}h since the last, needs "
                               f"{mine.get('min_hours')}h")
        except ValueError:
            pass

    # Each market keeps its own timer, so after a quiet night all four came
    # due at once and landed on one account within the hour. The channel also
    # waits between any two markets posts: by default its per-market gap shared
    # out across the markets, so four markets on a 10h gap go every 2.5h.
    spread = spread_hours(cfg, spec)
    last_any = con.execute(
        """SELECT MAX(updated) t FROM posts WHERE campaign='markets'
           AND profile=? AND channel=?""", (profile, chan)).fetchone()["t"]
    if last_any and spread:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last_any)).total_seconds() / 3600
            if gap < spread:
                return False, (f"spacing: {gap:.1f}h since this channel's "
                               f"last market post, needs {spread:g}h")
        except ValueError:
            pass
    return True, ""


def market_spec(spec, market):
    """A channel's pacing as one market sees it.

    Every market carries the channel's allowance unless the channel gives it
    its own under `markets:`, which is how X runs half crypto: crypto gets a
    bigger daily cap and a shorter gap, the others a smaller cap and a longer
    one, and the channel-wide spacing still sets the overall rhythm.
    """
    out = {**spec, **(((spec or {}).get("markets") or {}).get(market) or {})}
    # A channel that carries some markets only (@GuavyForex) gives the rest
    # no allowance, so they never take its slots and its totals are its own.
    if spec.get("only") and market not in spec["only"]:
        out["per_day"] = 0
    return out


def market_order(con, cfg, chan, profile, markets):
    """Which market should take this channel's next slot, best first.

    The one furthest behind its share of the day goes first: a market's share
    is its daily cap over the channel's total, and its deficit is what that
    share says it should have posted by now less what it has. Ties go to the
    market that has waited longest. Plain "longest waiting first" let crypto
    on X fall behind all day and then post in a run at the end of it; this
    interleaves, so a half-crypto channel alternates, with never more than
    two crypto in a row.
    """
    spec = (cfg.get("channels") or {}).get(chan) or {}
    tz = channels_cfg().get("timezone")
    today = datetime.now(ZoneInfo(tz) if (ZoneInfo and tz) else None).date().isoformat()
    caps = {m: int(market_spec(spec, m).get("per_day") or 0) for m in markets}
    total = sum(caps.values()) or 1
    used = {m: con.execute(
        """SELECT COUNT(*) FROM posts WHERE campaign='markets' AND profile=?
           AND channel=? AND date=? AND asset LIKE ?""",
        (profile, chan, today, f"{m}:%")).fetchone()[0] for m in markets}
    n = sum(used.values())
    return sorted(markets, key=lambda m: (
        -(caps[m] / total * (n + 1) - used[m]),
        market_last(con, profile, chan, m)))


def spread_hours(cfg, spec):
    """Hours between any two markets posts on one channel."""
    if spec.get("spread_hours") is not None:
        return float(spec["spread_hours"])
    return round(float(spec.get("min_hours") or 0) / max(len(MARKETS), 1), 2)


def market_last(con, profile, chan, market):
    """When this market last posted to this channel, '' if never."""
    return con.execute(
        """SELECT MAX(updated) t FROM posts WHERE campaign='markets'
           AND profile=? AND channel=? AND asset LIKE ?""",
        (profile, chan, f"{market}:%")).fetchone()["t"] or ""


def auto_once(market, channel, profile, send=True, at_minutes=None,
              insist=False):
    """Find a story, draw it, write it, and send it if asked.

    `send=False` builds everything and stops before the push, so the whole
    pipeline can be exercised without reaching an account.
    """
    import guavy, writer
    con, cfg = db(), channels_cfg()
    t0 = time.time()

    m = markets_cfg()
    rank = m.get("rank") or {}
    since = (time.time() - float(m.get("window_hours") or 24) * 3600) * 1000
    # A dedicated feed (ticker_scope: channel) holds a ticker back against its
    # own posts only, for ticker_hours; every other channel against all of them
    # for a day. The picker sees the same history it is held to.
    mspec = ((m.get("channels") or {}).get(channel)) or {}
    scope = channel if mspec.get("ticker_scope") == "channel" else None
    posted_syms, _t = recent_posts(con, market,
                                   float(mspec.get("ticker_hours") or 24), scope)
    cands = shortlist(con, market, since, float(m.get("min_sentiment") or 0),
                      float(rank.get("sentiment", 0.6)),
                      float(rank.get("clout", 0.4)), skip_symbols=posted_syms)
    if not cands:
        return dict(skipped="nothing on the Wire above the floor that is not "
                            "a ticker already posted today")
    _budget(t0, "the picker")
    got = writer.pick(cands, market, profiles().get(profile) or {},
                      posted=recent_posts(con, market, 48, scope)[1])
    # The loop leaves a slot empty rather than post a weak story. A hand fire
    # passes `insist`, because someone has already decided to post.
    brief = chosen_brief(got, cands, insist=insist)
    if not brief:
        return dict(skipped="the picker turned down everything on the list")

    sym = brief.get("symbol") or ""
    scope = brand_scope(market, sym) or {}
    title = brief.get("title") or ""
    link = guavy.wire_url(market, brief["article_id"])
    # The words come from the published article the link points to, never
    # from the brief in the list feed. The two are different texts for one
    # article id: the brief on the Carney pipeline story said "$4 billion
    # initially", which the published article never says, and that went out
    # under a link to a page that contradicted it. No article, no post.
    try:
        full = guavy.article(market, brief["article_id"]) or {}
    except guavy.GuavyError as e:
        return dict(skipped=f"could not read the published article: {e}"[:200])
    paras = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p)).strip()
             for p in re.split(r"</p>\s*<p[^>]*>|<br\s*/?>",
                               full.get("body") or full.get("content") or "")]
    body = "\n\n".join(p for p in paras if p)
    if not body:
        return dict(skipped="the published article came back empty")

    impacted = [c for c in (brief.get("impacted") or []) if isinstance(c, dict)]
    hit = max(impacted, key=lambda c: float(c.get("confidence") or 0),
              default={}) if impacted else {}
    direction = (hit.get("direction") or "").lower()
    # Geometric shapes rather than arrows. U+2191 is correct and the desk draws
    # it fine, but it is missing from enough of the fonts these posts land in
    # to come out as a box. U+25B2 and friends are far better covered.
    mark = {"bullish": "\u25b2", "bearish": "\u25bc"}.get(direction, "\u25cf")
    label = (f"{sym}: {scope['name']}"
             if scope.get("name") and scope["name"].lower() != sym.lower()
             else (scope.get("name") or sym))

    scores = []
    if brief.get("sentiment") is not None:
        scores.append(f"Sentiment: {brief['sentiment']}  |  "
                      f"Clout: {brief.get('clout')}")
    word = {"bullish": "Bullish", "bearish": "Bearish"}.get(direction, "Hold")
    tail = f"{word}  |  Confidence: {hit.get('confidence')}" if hit else word
    scores.append(tail)

    copy = f"{label} {mark}: {body}\n\n{link}\n\n" + "\n".join(scores)
    # The full article does not fit X. The article's own headline does, and
    # it is still Guavy's published words rather than a model's précis of
    # them, so a short channel gets the headline, then the scores as room
    # allows.
    lim = max_chars(cfg, channel)
    plat = (((cfg.get("channels") or {}).get(channel) or {}).get("platform")
            or channel.split("_")[0])
    if lim and body_length(copy, plat) > lim:
        for tail in (scores, scores[-1:], []):
            short = (f"{label} {mark}: {title}\n\n{link}"
                     + ("\n\n" + "\n".join(tail) if tail else ""))
            if body_length(short, plat) <= lim:
                copy = short
                break

    # Artwork, branded and themed, then the render everything else uses.
    _budget(t0, "the artwork")
    theme = auto_theme(channel)
    try:
        art = _generate_art(profile, title, body, market, sym, theme,
                            ["portrait", "landscape"])
    except Exception as e:
        return dict(error=f"artwork failed: {e}"[:200])
    if not art:
        return dict(error="artwork produced nothing")

    stamp = datetime.now().strftime("%-I:%M %p %B %-d, %Y").replace("AM", "am").replace("PM", "pm")
    # The profile's own face, the same one the sheet uses. Left out, the
    # renderer falls back to slideshow's default face: condensed, all caps,
    # and missing the arrow glyphs, so the direction mark came out as a box.
    face = ((profiles().get(profile) or {}).get("brand") or {}).get("font") or ""

    _budget(t0, "the render")
    rendered = []
    for row in art:
        out = _render_one(row, title, scrim=AUTO_SCRIM.get(theme, "light"), weight=AUTO_WEIGHT,
                          text_pct=AUTO_TEXT_PCT, label=label, stamp=stamp,
                          direction=direction, scores=scores, font=face,
                          url_text=f"guavy.com/wire/{market}")
        if out:
            rendered.append(out)
    if not rendered:
        return dict(error="render produced nothing")

    _budget(t0, "the post")
    made = _compose_one(con, cfg, profile, channel, rendered, copy,
                        f"{market}:{sym}#{brief['article_id']}",
                        scope.get("name") or sym, title, send=send,
                        at_minutes=at_minutes)
    if not made:
        return dict(skipped="no slot free on that channel")
    return dict(posted=made, sent=bool(send), title=title, symbol=sym,
                theme=theme, why=got.get("why") or "", copy=copy,
                seconds=round(time.time() - t0, 1))


@app.post("/api/autopost")
def api_autopost():
    """The master switch, and a way to fire one slot by hand."""
    b = request.get_json(silent=True) or {}
    if "on" in b:
        if b.get("on") and not is_live():
            return jsonify(error="The desk is not Live, so nothing would "
                                 "reach Zernio. Arm it first."), 400
        set_setting("autopost", "1" if b.get("on") else "0")
    return jsonify(on=autopost_on(), live=is_live())


# One pipeline at a time. Sixteen requests arrived at this endpoint once and
# every one of them ran: the desk generated artwork, wrote posts and published
# eight of them before anyone could stop it.
_FIRING = threading.Lock()


@app.post("/api/markets/<market>/fire")
def market_fire(market):
    """Run one slot: find, draw, write, and send unless asked not to.

    `send` must be passed explicitly. Left out, this builds the post and stops
    before Zernio, which is what you want when you are checking whether the
    thing works rather than trying to publish.
    """
    import guavy
    if market not in guavy.MARKETS:
        return jsonify(error=f"No such market {market!r}."), 404
    b = request.get_json(silent=True) or {}
    prof = b.get("profile") or ""
    chan = b.get("channel")
    if not chan:
        return jsonify(error="Which channel?"), 400
    con, cfg = db(), markets_cfg()
    held = ((cfg.get("channels") or {}).get(chan) or {}).get("paused")
    if held:                       # force skips pacing, never a paused channel
        return jsonify(error=f"{chan} is paused: {held}"), 409
    if not b.get("force"):
        ok, why = channel_due(con, cfg, chan, market, prof)
        if not ok:
            return jsonify(error=f"Not due: {why}"), 409
    if not _FIRING.acquire(blocking=False):
        return jsonify(error="Already building a post. One at a time."), 429
    try:
        out = auto_once(market, chan, prof, send=bool(b.get("send")),
                        at_minutes=b.get("at_minutes"),
                        insist=bool(b.get("insist", True)))
    except OutOfTime as e:
        out = dict(error=str(e))
    finally:
        _FIRING.release()
    return jsonify(**out)


def auto_loop():
    """Look every couple of minutes, post at most one thing per look.

    One per tick on purpose: a burst of four markets landing together on one
    account reads as a bot, and a fault that would post once posts once.
    """
    while True:
        time.sleep(AUTO_TICK)
        try:
            with app.app_context():
                if not (autopost_on() and is_live()):
                    continue
                con, cfg = db(), markets_cfg()
                prof = next(iter(profiles()), "")
                # Every channel, the one that has waited longest first, and on
                # each the market furthest behind its share of the day.
                live = [k for k, _, _ in MARKETS if market_mode(k) == "auto"]
                chans = sorted((cfg.get("channels") or {}), key=lambda c: con.execute(
                    """SELECT COALESCE(MAX(updated), '') FROM posts WHERE
                       campaign='markets' AND profile=? AND channel=?""",
                    (prof, c)).fetchone()[0])
                pairs = [(key, chan) for chan in chans
                         for key in market_order(con, cfg, chan, prof, live)]
                done = False
                for key, chan in pairs:
                    ok, _why = channel_due(con, cfg, chan, key, prof)
                    if not ok:
                        continue
                    if not _FIRING.acquire(blocking=False):
                        break              # a hand-fired post is building
                    app.logger.info("autopost firing %s -> %s", key, chan)
                    try:
                        out = auto_once(key, chan, prof, send=True)
                        app.logger.info("autopost result: %s", out)
                    except Exception as e:
                        app.logger.warning("autopost failed: %s", e)
                    finally:
                        _FIRING.release()
                    done = True
                    break
                # Ads, when the Wire had nothing to send this look. One post
                # per look either way, so the two never land together.
                if not done and ads_mode() == "auto":
                    achans = sorted((ads_cfg().get("channels") or {}),
                                    key=lambda c: con.execute(
                        """SELECT COALESCE(MAX(updated), '') FROM posts WHERE
                           campaign='ads' AND profile=? AND channel=?""",
                        (prof, c)).fetchone()[0])
                    for chan in achans:
                        ok, _why = ad_due(con, chan, prof)
                        if not ok:
                            continue
                        if not _FIRING.acquire(blocking=False):
                            break
                        try:
                            out = ad_once(chan, prof, send=True)
                            app.logger.info("ad result: %s", out)
                        except Exception as e:
                            app.logger.warning("ad failed: %s", e)
                        finally:
                            _FIRING.release()
                        break
        except Exception as e:                       # never let the loop die
            try:
                app.logger.warning("autopost loop: %s", e)
            except Exception:
                pass


@app.get("/api/markets/counts")
def market_counts():
    """Articles in the last 24 hours per market, out of the mirror.

    A SQL query rather than a sweep of every symbol, so it is instant and it is
    a real count rather than whatever a read depth happened to reach.
    """
    con = db()
    return jsonify(counts=wire_counts(con),
                   held=con.execute("SELECT COUNT(*) FROM briefs").fetchone()[0],
                   synced=setting("wire.synced"))


@app.get("/api/markets/<market>/scan")
def market_scan(market):
    """What the Wire is carrying, best first.

    `since` defaults to whenever Markets last posted for this profile, which
    is the question the tab is actually asking: what has happened that we have
    not already said something about.
    """
    import guavy
    if market not in guavy.MARKETS:
        return jsonify(error=f"No such market {market!r}."), 404
    con = db()
    prof = request.args.get("profile") or ""
    since = request.args.get("since")
    since_ms = float(since) if since else last_markets_ms(con, prof)
    try:
        clout = float(request.args.get("min_clout") or 0)
        got = guavy.scan(market, since_ms, clout)
    except guavy.GuavyError as e:
        return jsonify(error=str(e)), 502
    return jsonify(market=market, since=since_ms, count=len(got),
                   briefs=got[:25])


SHORTLIST = int(os.environ.get("WIRE_SHORTLIST", "25"))


def chosen_brief(got, cands, insist=False):
    """The candidate the picker chose, or None.

    When it turns the whole list down, an unattended post stays unposted. A
    person asking by hand has already decided they want something, so
    `insist` takes the picker's least bad candidate instead, and failing that
    the strongest by score it did not reject.
    """
    by_id = {x.get("article_id"): x for x in cands}
    if not got.get("none") and got.get("article_id") in by_id:
        return dict(by_id[got["article_id"]], why=got.get("why") or "",
                    duplicates=got.get("duplicates") or [], judged=True)
    if not insist:
        return None
    pick = by_id.get(got.get("best_available"))
    if pick:
        return dict(pick, judged=True, weak=True,
                    why="Nothing on the list is clearly material. This is "
                        "the picker's best of what there is.")
    turned = {r.get("article_id") for r in (got.get("rejected") or [])}
    rest = [x for x in cands if x.get("article_id") not in turned] or cands
    return dict(rest[0], judged=False, weak=True,
                why="The picker chose nothing, so this is the strongest by "
                    "score.") if rest else None


def recent_posts(con, market, hours=24, channel=None):
    """Market posts that went out, or are going, in the last `hours`.

    Returns (symbols, titles). An article id alone was not enough: three
    outlets covering one Boeing contract are three ids, so the same story went
    out twice on one day and "Find another article" kept offering it back.
    """
    cut = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    syms, titles = set(), []
    q = """SELECT asset, title FROM posts WHERE campaign='markets'
           AND state != 'rejected' AND asset LIKE ? AND updated >= ?"""
    args = [f"{market}:%", cut]
    if channel:
        q += " AND channel=?"
        args.append(channel)
    for r in con.execute(q + " ORDER BY updated DESC", args):
        _m, sym, _a = asset_parts(r["asset"])
        if sym:
            syms.add(sym.upper())
        if r["title"] and r["title"] not in titles:
            titles.append(r["title"])
    return syms, titles


def shortlist(con, market, since_ms, min_sentiment, w_sent, w_clout, n=None,
              exclude=(), skip_symbols=()):
    """The strongest candidates out of the mirror, best score first.

    Scoring is the cheap half: how far the sentiment is from neutral, and how
    far the story travelled. It is deliberately not the whole answer, because
    a concert presale that mentions a card issuer scores beautifully on both.
    """
    cut = int(since_ms or 0)
    # Anything already posted is off the list for good. The desk records the
    # article on the post, so a story cannot come round again on a later scan
    # just because it is still the strongest thing on the Wire.
    # Any post carrying an article, whatever campaign it ended up on, so a
    # story stays spent even if it was composed by hand.
    spent = {asset_parts(r["asset"])[2] for r in con.execute(
        "SELECT asset FROM posts WHERE asset LIKE '%#%'") if r["asset"]}
    spent.discard("")
    rows = []
    for r in con.execute(
            """SELECT b.*, GROUP_CONCAT(bs.symbol) syms FROM briefs b
               JOIN brief_symbols bs ON bs.article_id=b.article_id
               WHERE b.market=? AND b.ts>? GROUP BY b.article_id""",
            (market, cut)):
        d = dict(r)
        if d.get("article_id") in spent or d.get("article_id") in exclude:
            continue
        if abs(float(d.get("sentiment") or 0)) < min_sentiment:
            continue
        d["weight"] = round(
            w_sent * min(abs(float(d.get("sentiment") or 0)) / 5.0, 1.0)
            + w_clout * min(float(d.get("clout") or 0) / 100.0, 1.0), 4)
        try:
            d["impacted"] = json.loads(d.get("impacted") or "[]")
        except ValueError:
            d["impacted"] = []
        d["symbol"] = (d.pop("syms", "") or "").split(",")[0]
        # A ticker already posted today is spent too: another outlet's take
        # on the same news is not a new story.
        if d["symbol"] and d["symbol"].upper() in skip_symbols:
            continue
        rows.append(d)
    rows.sort(key=lambda d: (d["weight"], d.get("ts") or 0), reverse=True)
    return rows[:(n or SHORTLIST)]


@app.post("/api/markets/<market>/article")
def market_article(market):
    """The best article already on the Wire, with its link.

    Nothing is written here and nothing is invented. The article exists, Guavy
    published it, and this picks the one worth posting about: highest clout
    since this profile last posted from Markets. The link travels with it,
    because some posts are the link.
    """
    import guavy
    if market not in guavy.MARKETS:
        return jsonify(error=f"No such market {market!r}."), 404
    b = request.get_json(silent=True) or {}
    con = db()
    prof = b.get("profile") or ""
    try:
        if b.get("article_id"):
            brief = next((x for x in guavy.scan(market, 0)
                          if x.get("article_id") == b["article_id"]), None)
        else:
            m = markets_cfg()
            rank = m.get("rank") or {}
            # A rolling window, not "since we last posted". Those were the
            # same thing when a repeat could only be avoided by refusing to
            # look back, but the article id is on the post now, so a story is
            # excluded because it went out rather than because of when it
            # arrived. The old rule meant one post blinded the desk to
            # everything already on the Wire, however good.
            window = float(m.get("window_hours") or 24)
            since = (time.time() - window * 3600) * 1000
            # The article already on screen is not a new one. Asking again
            # means "something else".
            posted_syms, posted_titles = recent_posts(con, market)
            skip = posted_syms | {str(x).upper()
                                  for x in (b.get("exclude_symbols") or [])}
            args = (con, market, since, float(m.get("min_sentiment") or 0),
                    float(rank.get("sentiment", 0.6)),
                    float(rank.get("clout", 0.4)))
            cands = shortlist(*args, exclude=set(b.get("exclude") or []),
                              skip_symbols=skip)
            if not cands:
                # Every ticker on the Wire has had its turn today. Asked by
                # hand, a repeat ticker beats nothing, but never one already
                # shown on this card.
                cands = shortlist(*args, exclude=set(b.get("exclude") or []),
                                  skip_symbols={str(x).upper() for x in
                                                (b.get("exclude_symbols") or [])})
            brief = None
            if cands and b.get("judge", True):
                # Scoring got us to a shortlist. Which of them actually
                # matters is a reading job, so a model reads them.
                try:
                    import writer
                    got = writer.pick(cands, market,
                                      profiles().get(prof) or {},
                                      posted=recent_posts(con, market, 48)[1])
                except Exception as e:
                    app.logger.warning("picker failed: %s", e)
                    got = {}
                # Asked by hand, so always come back with something.
                brief = chosen_brief(got, cands, insist=True)
            if not brief and cands:
                brief = dict(cands[0], judged=False)
            if not brief and not cands:
                held = con.execute(
                    "SELECT COUNT(*) FROM briefs WHERE market=? AND ts>?",
                    (market, int(since))).fetchone()[0]
                posted = con.execute(
                    "SELECT COUNT(*) FROM posts WHERE campaign='markets' "
                    "AND asset LIKE ?", (f"{market}:%#%",)).fetchone()[0]
                return jsonify(error=(
                    f"Nothing left to pick. {held} articles held for "
                    f"{market} in the last {window:g}h, {posted} already "
                    f"posted, and the rest are under the sentiment floor of "
                    f"{m.get('min_sentiment')}. Sync the Wire, widen "
                    f"window_hours, or lower min_sentiment in "
                    f"channels.yaml.")), 404
            if not brief:
                # Nothing strong enough. Say which gate closed, because
                # "nothing new" and "nothing that moved the needle" want
                # different answers from you.
                loose = guavy.top(market, last_markets_ms(con, prof), 0, 0)
                if loose:
                    return jsonify(error=(
                        f"Nothing above the bar. The best on the Wire scores "
                        f"sentiment {loose.get('sentiment')} and clout "
                        f"{loose.get('clout')}, under the floors of "
                        f"{m.get('min_sentiment')} and {floor:g} in "
                        f"channels.yaml.")), 404
        # The instrument the story is about, unless we have no scope for it,
        # in which case the feed it came from is a real symbol and probably
        # close enough to brand from.
        if brief:
            sym = brief.get("symbol")
            if not brand_scope(market, sym or "") and brief.get("found_under"):
                if brand_scope(market, brief["found_under"]):
                    brief = dict(brief, symbol=brief["found_under"])
        if not brief:
            return jsonify(error="Nothing new on the Wire above the clout "
                                 "floor. Try again later, or lower "
                                 "min_clout in channels.yaml."), 404
        # The published article, or nothing. The brief in the list feed is a
        # different text and can carry figures the article does not.
        try:
            full = guavy.article(market, brief["article_id"])
        except guavy.GuavyError as e:
            return jsonify(error=f"Could not read the published article: "
                                 f"{e}"[:300]), 502
    except guavy.GuavyError as e:
        return jsonify(error=str(e)), 502

    full = full or {}
    body = full.get("body") or full.get("content") or ""
    if not body.strip():
        return jsonify(error="The published article came back empty."), 502
    # The API carries no link of its own, so the link is the Wire's own page
    # for this article, which is where a post should point anyway: it is
    # Guavy's write-up, not the outlet's.
    try:
        link = guavy.wire_url(market, brief["article_id"])
    except Exception:
        link = ""
    # Two forms, for two jobs. The full one goes in the copy, where a reader
    # taps it and lands on the article. The short one is for the graphic: a
    # sixty-character deep link printed along the bottom of a picture is a mess
    # nobody types anyway, and the section address says where to go.
    short = f"guavy.com/wire/{market}"
    # Every dimension the Wire scored, passed through rather than picked over.
    # The choice of article is made on two of them, and being able to see the
    # rest is how you tell a good pick from a lucky one.
    # The Wire calls it impacted_coins; a row out of the mirror calls it
    # impacted, already parsed. Read either, or the scoring line on the
    # graphic loses its direction and confidence.
    impacted = []
    for c in (brief.get("impacted_coins") or brief.get("impacted") or []):
        if isinstance(c, str):
            impacted.append(dict(asset=c))
        elif isinstance(c, dict):
            impacted.append(dict(asset=c.get("asset"),
                                 direction=c.get("direction"),
                                 confidence=c.get("confidence"),
                                 timeframe=c.get("timeframe")))

    return jsonify(market=market, brief=brief,
                   article=dict(title=brief.get("title") or "",
                                body=re.sub(r"<[^>]+>", " ", body).strip(),
                                url=link,
                                url_short=short,
                                source=full.get("source") or full.get("publisher") or "",
                                market=market,
                                symbol=brief.get("symbol"),
                                found_under=brief.get("found_under"),
                                article_id=brief.get("article_id"),
                                date=brief.get("date"),
                                clout=brief.get("clout"),
                                sentiment=brief.get("sentiment"),
                                speculation=brief.get("speculation_score"),
                                fud_fomo=brief.get("fud_fomo_score"),
                                fud_fomo_bias=brief.get("fud_fomo_bias"),
                                tone=brief.get("tone"),
                                weight=brief.get("weight"),
                                why=brief.get("why") or "",
                                judged=bool(brief.get("judged")),
                                duplicates=len(brief.get("duplicates") or []),
                                symbols=brief.get("symbols") or [],
                                impacted=impacted,
                                scope=bool(brand_scope(market, brief.get("symbol") or ""))))


@app.get("/api/blogs")
def blogs():
    """Every known blog, newest first, each carrying how far it has got.

    A blog is attached to a post either as the post's media_url or as an asset
    named `blog:<slug>`, so both are checked. Published outranks scheduled: a
    blog posted twice shows the furthest state it reached.
    """
    con = db()
    only = request.args.get("profile")
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM blogs ORDER BY published DESC, id DESC")]

    q = ("SELECT state, COUNT(*) n FROM posts "
         "WHERE (media_url=? OR asset=?)" + (" AND profile=?" if only else "") +
         " GROUP BY state")
    for b in rows:
        args = [b["url"], "blog:" + b["slug"]] + ([only] if only else [])
        seen = {r["state"]: r["n"] for r in con.execute(q, args)}
        b["posts"] = sum(seen.values())
        b["state"] = ("published" if seen.get("published")
                      else "scheduled" if seen.get("scheduled") else None)
    return jsonify(blogs=rows, feed=BLOG_FEED)


@app.get("/api/followers")
def followers():
    """Current follower counts, and whatever history the desk has recorded."""
    con = db()
    try:
        raw = zernio_all("/accounts", request.args.get("fresh") == "1")
    except RuntimeError as e:
        return jsonify(error=str(e)[:400]), 502

    # Which profile each Zernio account belongs to, per profiles.yaml.
    owner = account_owner()
    only = request.args.get("profile")
    reach = account_ids(only) if only else None

    today = date.today().isoformat()
    alias = platform_alias()
    now = []
    for a in raw:
        aid = a.get("_id")
        who = owner.get(aid) or dict(slug="other", label="Not in profiles.yaml")
        n = a.get("followersCount")
        if n is not None:
            con.execute("""INSERT INTO followers (day,account_id,platform,name,profile,n)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(day,account_id) DO UPDATE SET n=excluded.n""",
                        (today, aid, a.get("platform"),
                         a.get("displayName") or "", who["slug"], n))
        if reach is not None and aid not in reach:
            continue
        now.append(dict(account_id=aid,
                        platform=alias.get(a.get("platform"), a.get("platform")),
                        name=a.get("displayName") or a.get("username") or "",
                        profile=who["slug"], profile_label=who["label"],
                        url=a.get("profileUrl") or "",
                        n=n, updated=a.get("followersLastUpdated")))
    con.commit()

    # History is rolled up through today's ownership, not whatever was stored
    # on the day. Moving an account between profiles is a correction, and a
    # correction must not read as five thousand people arriving overnight.
    con.executemany("UPDATE followers SET profile=? WHERE account_id=?",
                    [(v["slug"], k) for k, v in owner.items()])
    con.commit()

    hist, per = {}, {}
    for day, aid, n in con.execute("SELECT day, account_id, n FROM followers"):
        slug = (owner.get(aid) or {}).get("slug", "other")
        hist.setdefault(day, {})
        hist[day][slug] = hist[day].get(slug, 0) + (n or 0)
        per.setdefault(aid, {})[day] = n
    # One total per profile, scoped to the accounts of the profile in view.
    totals = {}
    for a in raw:
        aid = a.get("_id")
        if reach is not None and aid not in reach:
            continue
        w = owner.get(aid) or dict(slug="other", label="Unassigned")
        if w["slug"] not in totals:
            totals[w["slug"]] = dict(label=w["label"], n=0)
        totals[w["slug"]]["n"] += a.get("followersCount") or 0
    # profiles.yaml order, so the tiles do not shuffle between loads
    order = [k for k in profiles() if k in totals] + \
            [k for k in totals if k not in profiles()]
    return jsonify(accounts=now, history=hist, account_history=per,
                   totals=totals, order=order, days=len(hist))


def local_iso(stamp):
    """A Zernio UTC timestamp in the desk's own timezone, offset included.

    The page buckets by the first ten characters, so a post at 18:39 in
    Edmonton, which is 00:39 UTC, was being counted on the next day.
    """
    if not stamp:
        return stamp
    tz = channels_cfg().get("timezone")
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return (t.astimezone(ZoneInfo(tz)) if (ZoneInfo and tz) else t).isoformat()
    except ValueError:
        return stamp


@app.get("/api/zernio/published")
def zernio_published():
    """Everything live, with its engagement. Zernio syncs posts published
    natively too, so this covers work that never went through this desk."""
    force = request.args.get("fresh") == "1"
    try:
        raw = zernio_all("/analytics", force)
        # Analytics lags publication by a sync cycle, so a post that went out
        # an hour ago is live but unmeasured. Take those from /posts too, or
        # the tab looks like nothing happened.
        fresh = zernio_all("/posts", force)
    except RuntimeError as e:
        return jsonify(error=str(e)[:400]), 502
    # An analytics row has its own _id; the post it measures is latePostId.
    # Matching on _id never matched, so every measured post came back a second
    # time from /posts as an unmeasured "awaiting" copy, doubling every count.
    have = {p.get("latePostId") or p.get("_id") for p in raw}
    for p in fresh:
        if p.get("_id") in have:
            continue
        if (p.get("status") or "").lower() != "published":
            continue
        plats = p.get("platforms") or []
        raw.append(dict(p, analytics={}, platform=(plats[0] or {}).get("platform")
                        if plats else None,
                        publishedAt=p.get("scheduledFor"),
                        platformPostUrl=next(
                            (x.get("platformPostUrl") or x.get("postUrl")
                             for x in plats if x.get("platformPostUrl")
                             or x.get("postUrl")), ""),
                        awaiting=True))
    alias = platform_alias()
    owner = account_owner()
    only = request.args.get("profile")

    # Whose work a post is, in order of confidence: the profile that composed
    # it on the desk, and failing that, whoever owns the account.
    origin = {r["zernio_id"]: r["profile"] for r in db().execute(
        "SELECT zernio_id, profile FROM posts "
        "WHERE zernio_id IS NOT NULL AND zernio_id != ''")}

    out = []
    for p in raw:
        if (p.get("status") or "").lower() != "published":
            continue
        a = p.get("analytics") or {}
        who, aid = "", None
        for x in p.get("platforms") or []:
            acct = x.get("accountId")
            # /analytics puts the name on the platform entry; /posts nests the
            # whole account object under accountId. Read both.
            if isinstance(acct, dict):
                aid = aid or acct.get("_id")
                who = who or acct.get("displayName") or acct.get("username") or ""
            else:
                aid = aid or acct
            who = who or x.get("accountUsername") or x.get("accountName") or ""
        by_account = (owner.get(aid) or {}).get("slug")
        from_desk = origin.get(p.get("latePostId") or p.get("_id"))
        slug = from_desk or by_account
        if only and slug != only:
            continue
        out.append(dict(
            id=p.get("_id"),
            platform=alias.get(p.get("platform"), p.get("platform")),
            account=who, profile=slug, account_id=aid,
            owned=(slug == only) if only else True,
            attributed="desk" if from_desk else "account",
            when=local_iso(p.get("publishedAt") or p.get("scheduledFor")),
            content=p.get("content") or "", url=p.get("platformPostUrl") or "",
            thumb=p.get("thumbnailUrl") or "",
            external=bool(p.get("isExternal")),
            awaiting=bool(p.get("awaiting")),
            impressions=a.get("impressions"), reach=a.get("reach"),
            likes=a.get("likes"), comments=a.get("comments"),
            shares=a.get("shares"), saves=a.get("saves"),
            views=a.get("views"), clicks=a.get("clicks"),
            rate=a.get("engagementRate"), updated=a.get("lastUpdated")))
    # Which metrics each platform actually hands back. TikTok and YouTube
    # report views but never impressions or reach, and Facebook reports views
    # on barely a quarter of posts. Drawn as 0 that reads as "nobody saw it"
    # rather than "nobody counted it", which is a very different thing.
    reports = {}
    for p in raw:
        for x in p.get("platforms") or []:
            a = x.get("analytics") or {}
            plat = alias.get(x.get("platform"), x.get("platform"))
            seen = reports.setdefault(plat, {})
            for k in ("views", "impressions", "reach", "likes", "comments",
                      "shares", "saves"):
                if (a.get(k) or 0) > 0:
                    seen[k] = True

    out.sort(key=lambda p: p.get("when") or "", reverse=True)
    # The floor follows the data. Zernio backfilled about twelve weeks when the
    # accounts were connected, so the earliest post is a sync boundary, not the
    # start of anything. Hardcoding a date would go stale the moment it moves.
    days = [p["when"][:10] for p in out if p.get("when")]
    return jsonify(age=zernio_age("/analytics"), reports=reports,
                   posts=out, total=len(out),
                   earliest=min(days) if days else None)


def _ideas(con, prof):
    rows = con.execute("""SELECT * FROM suggestions WHERE profile=?
                          ORDER BY frozen DESC, id""", (prof,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["platforms"] = json.loads(d["platforms"] or "[]")
        except ValueError:
            d["platforms"] = []
        d["frozen"] = bool(d["frozen"])
        out.append(d)
    return out


@app.get("/api/suggestions")
def suggestions():
    """Whatever is on the board, frozen ones first."""
    return jsonify(ideas=_ideas(db(), request.args.get("profile") or ""))


@app.post("/api/suggestions/<int:sid>/freeze")
def suggestion_freeze(sid):
    """Frozen means it survives the next round and cannot be dismissed."""
    b = request.get_json(silent=True) or {}
    con = db()
    con.execute("UPDATE suggestions SET frozen=? WHERE id=?",
                (1 if b.get("frozen") else 0, sid))
    con.commit()
    return jsonify(ok=True)


@app.delete("/api/suggestions/<int:sid>")
def suggestion_drop(sid):
    con = db()
    row = con.execute("SELECT frozen FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify(error="No such suggestion."), 404
    if row["frozen"]:
        return jsonify(error="That one is frozen. Unfreeze it first."), 400
    con.execute("DELETE FROM suggestions WHERE id=?", (sid,))
    con.commit()
    return jsonify(ok=True)


@app.post("/api/suggest")
def suggest():
    """Ten more things you could post. Frozen ideas stay where they are."""
    import writer
    prof_slug = (request.get_json(silent=True) or {}).get("profile") or ""
    prof = profiles().get(prof_slug) or {}
    camps = [d.name for d in sorted((ROOT / "campaigns").glob("*"))
             if (d / "source-pack.md").exists()]
    con = db()
    keeping = [r["title"] for r in con.execute(
        "SELECT title FROM suggestions WHERE profile=? AND frozen=1",
        (prof_slug,)) if r["title"]]
    try:
        out = writer.suggest(prof_slug, prof, camps, channels_cfg(), keeping)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"[:400]), 502

    # Only the unfrozen are cleared, and only once the new ones are in hand:
    # a failed call should never cost you the board you already had.
    con.execute("DELETE FROM suggestions WHERE profile=? AND frozen=0",
                (prof_slug,))
    now = datetime.now().isoformat(timespec="seconds")
    for x in (out or []):
        con.execute("""INSERT INTO suggestions
                       (profile,title,idea,opening,needs,day,platforms,frozen,added)
                       VALUES (?,?,?,?,?,?,?,0,?)""",
                    (prof_slug, (x.get("title") or "").strip(),
                     (x.get("idea") or "").strip(),
                     (x.get("opening") or "").strip(),
                     (x.get("needs") or "").strip(),
                     (x.get("day") or "").strip(),
                     json.dumps(x.get("platforms") or []), now))
    con.commit()
    return jsonify(ideas=_ideas(con, prof_slug))


@app.get("/api/channels")
def api_channels():
    """Channel to platform, and the colour each platform is drawn in."""
    cfg = channels_cfg()
    plats = cfg.get("platforms") or {}
    out = {}
    for key, c in (cfg.get("channels") or {}).items():
        c = c or {}
        p = c.get("platform") or key.split("_")[0]
        out[key] = dict(platform=p, label=c.get("label") or key,
                        ink=(plats.get(p) or {}).get("ink", "#8A8F86"),
                        delivery=c.get("delivery") or "zernio",
                        handoff_to=c.get("handoff_to"),
                        # The compose sheet takes the link out of these
                        # channels' copy as you write, so you see what goes.
                        drop_links=bool(c.get("drop_links")),
                        max_chars=max_chars(cfg, key))
    return jsonify(channels=out, platforms=plats)


# ---------------------------------------------------------------- hand-off
#
# Some channels cannot be posted to by this system: an account that will never
# be connected to Zernio. Those channels are marked
# `delivery: email` in channels.yaml. The post is planned and approved here
# exactly like any other, then emailed to whoever will post it by hand.

def handoff_email(row):
    """Build the message. Returns (to, subject, body, media_path)."""
    ch = (channels_cfg().get("channels") or {}).get(row["channel"]) or {}
    to = ch.get("handoff_to")
    when = datetime.fromisoformat(f"{row['date']}T{row['time']}")
    nice = when.strftime("%A %-d %B at %H:%M")
    who = (ch.get("label") or row["channel"]).split("/")[-1].strip()

    parts = [f"Here is the post for {who}, planned for {nice}.", "",
             "Copy, ready to paste:", "", (row["copy"] or "").rstrip(), ""]
    if (row["first_comment"] or "").strip():
        parts += ["Put this in the first comment, not in the post itself. "
                  "A link in the body costs about half the reach.", "",
                  row["first_comment"].strip(), ""]

    media_path = None
    if row["media_id"]:
        m = db().execute("SELECT * FROM media WHERE id=?", (row["media_id"],)).fetchone()
        if m:
            media_path = MEDIA / m["path"]
            parts += [f"Attached: {m['original']}", ""]
    elif row["media_url"]:
        parts += [f"Media: {row['media_url']}", ""]

    for label, key in (("Tag", "tags"), ("Collaborators", "collaborators")):
        vals = json.loads(row[key] or "[]")
        if vals:
            parts += [f"{label}: {', '.join(vals)}", ""]

    parts += [f"Campaign: {row['campaign'].replace('-', ' ')}, {row['phase']} phase.",
              "", "Nothing is scheduled at our end. It goes out when you post it."]
    return to, f"To post on {who}: {when.strftime('%a %-d %b')}", "\n".join(parts), media_path


@app.route("/api/posts/<int:pid>/handoff", methods=["GET", "POST"])
def handoff(pid):
    row = db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="no such post"), 404
    ch = (channels_cfg().get("channels") or {}).get(row["channel"]) or {}
    if (ch.get("delivery") or "zernio") != "email":
        return jsonify(error="This channel posts through Zernio, not by email."), 400
    if row["state"] != "approved":
        return jsonify(error="Approve it first."), 400

    to, subject, body, media_path = handoff_email(row)
    preview = dict(to=to, subject=subject, body=body,
                   media=media_path.name if media_path else None)
    if request.method == "GET":
        return jsonify(preview)

    if not to or to == "TODO":
        return jsonify(error="No handoff_to address set for this channel "
                             "in channels.yaml.", **preview), 400
    host = os.environ.get("SMTP_HOST")
    if not host:
        return jsonify(error="SMTP_HOST is not set, so the desk cannot send it "
                             "itself. The message is ready below.", **preview), 501

    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    msg.set_content(body)
    if media_path and media_path.exists():
        kind, _, sub = (mimetypes.guess_type(media_path.name)[0]
                        or "application/octet-stream").partition("/")
        msg.add_attachment(media_path.read_bytes(), maintype=kind, subtype=sub,
                           filename=media_path.name)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as sm:
            sm.starttls()
            if os.environ.get("SMTP_USER"):
                sm.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            sm.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        return jsonify(error=f"Send failed: {e}", **preview), 502

    stamp = datetime.now().isoformat(timespec="seconds")
    db().execute("UPDATE posts SET handed_off=? WHERE id=?", (stamp, pid))
    db().commit()
    return jsonify(sent=True, to=to, at=stamp)


# ---------------------------------------------------------------- media
#
# The library is local and durable. It never needs to be publicly reachable:
# Zernio takes the bytes at push time via POST /v1/media/presign and hosts the
# delivery copy itself. That upload only survives 7 days, which is exactly why
# this folder is the copy that lasts.

EXT_KIND = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".heic": "image",
    ".mp4": "video", ".mov": "video", ".m4v": "video", ".webm": "video",
    ".mp3": "audio", ".m4a": "audio", ".aac": "audio", ".wav": "audio",
    ".flac": "audio", ".ogg": "audio", ".aif": "audio", ".aiff": "audio",
}


def kind_of(mime, filename=""):
    """Not every client sends a real content type. Fall back to the name."""
    for prefix in ("image", "video", "audio"):
        if (mime or "").startswith(prefix + "/"):
            return prefix
    return EXT_KIND.get(Path(filename or "").suffix.lower())


@app.get("/api/media")
def media_list():
    q, p = "SELECT * FROM media WHERE 1=1", []
    for field, arg in (("profile = ?", "profile"), ("campaign = ?", "campaign"),
                       ("kind = ?", "kind"), ("bucket = ?", "bucket")):
        v = request.args.get(arg)
        if v:
            q += f" AND {field}"
            p.append(v)
    rows = [dict(r) for r in db().execute(q + " ORDER BY id DESC", p)]
    # A song name for every track: whatever you typed, else the filename with
    # its tail of mixes, tempos and keys taken off.
    import slideshow as _sl
    for r in rows:
        if r.get("kind") == "audio":
            r["song"] = (r.get("title") or "").strip() or _sl.track_title(
                r.get("original") or "")

    # Which files have been out in the world. media_ids carries a carousel, so
    # a file counts as used even when it is not the one on media_id.
    # A slideshow goes out in place of the photos it was made from, so a photo
    # that has been seen by the world is not named on any post. Walk back
    # through what each render was built out of, or the Media tab calls a photo
    # unused while it is out there being watched.
    from_of = {}
    for r in db().execute(
            "SELECT id, made_from FROM media WHERE made_from IS NOT NULL"):
        try:
            from_of[r["id"]] = json.loads(r["made_from"] or "[]")
        except ValueError:
            pass

    def with_sources(ids, seen=None):
        seen = set() if seen is None else seen
        for i in list(ids):
            if i in seen:
                continue
            seen.add(i)
            with_sources(from_of.get(i, []), seen)
        return seen

    used, live = {}, {}
    for r in db().execute("SELECT media_id, media_ids, state FROM posts"):
        ids = set()
        if r["media_id"]:
            ids.add(r["media_id"])
        try:
            ids.update(json.loads(r["media_ids"] or "[]"))
        except ValueError:
            pass
        for i in with_sources(ids):
            used[i] = used.get(i, 0) + 1
            if r["state"] == "published":
                live[i] = live.get(i, 0) + 1
    for r in rows:
        r["posts"] = used.get(r["id"], 0)
        r["published"] = live.get(r["id"], 0)
    return jsonify(rows)


@app.post("/api/media")
def media_add():
    """Drag and drop, a paste, or an upload from a phone on the same network."""
    prof = (request.form.get("profile") or "").strip()
    if prof not in profiles():
        return jsonify(error="Choose a profile before uploading."), 400
    bucket = (request.form.get("bucket") or "library").strip() or "library"
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        return jsonify(error="No files arrived."), 400

    con = db()
    added, dupes, rejected = 0, 0, []
    dest = MEDIA / prof
    dest.mkdir(parents=True, exist_ok=True)

    for f in files:
        mime = f.mimetype or ""
        kind = kind_of(mime, f.filename)
        if kind and mime in ("", "application/octet-stream"):
            mime = mimetypes.guess_type(f.filename)[0] or f"{kind}/*"
        if not kind:
            rejected.append(f"{f.filename} ({mime or 'unknown type'})")
            continue

        # Stream to a temp file, hashing as we go, then decide where it lives.
        h = hashlib.sha256()
        tmp = Path(tempfile.mkstemp(dir=MEDIA)[1])
        size = 0
        with tmp.open("wb") as out:
            while chunk := f.stream.read(1024 * 1024):
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
        digest = h.hexdigest()

        if con.execute("SELECT 1 FROM media WHERE sha256=? AND profile=?",
                       (digest, prof)).fetchone():
            tmp.unlink(missing_ok=True)
            dupes += 1
            continue

        safe = secure_filename(f.filename) or f"{kind}.bin"
        name = f"{digest[:12]}-{safe}"
        shutil.move(str(tmp), dest / name)
        import probe as _probe
        w, h, secs = _probe.probe(dest / name)
        if kind == "audio":
            import slideshow
            w = h = None
            secs = slideshow.duration(dest / name)
        con.execute(
            """INSERT INTO media (profile,campaign,path,original,kind,mime,bytes,
               sha256,width,height,seconds,source,bucket,added)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prof, request.form.get("campaign") or None, f"{prof}/{name}",
             f.filename, kind, mime, size, digest, w, h, secs,
             request.form.get("source") or "drop", bucket,
             datetime.now().isoformat(timespec="seconds")))
        added += 1

    con.commit()
    return jsonify(added=added, duplicates=dupes, rejected=rejected)


@app.get("/api/media/<int:mid>/slots")
def media_slots(mid):
    """Open slots this file could fill: right profile, right type, still empty,
    and not in the past. Attaching one is what turns an asset into a post."""
    m = db().execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if not m:
        return jsonify(error="no such file"), 404
    q = """SELECT * FROM posts WHERE state='allocated' AND media_id IS NULL
           AND profile = ? AND date >= ? AND asset_type LIKE ?"""
    p = [m["profile"], date.today().isoformat(),
         "video_%" if m["kind"] == "video" else "image"]
    if m["campaign"]:
        q += " AND campaign = ?"
        p.append(m["campaign"])
    rows = db().execute(q + " ORDER BY date, time LIMIT 60", p).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/media/<int:mid>/attach")
def media_attach(mid):
    ids = (request.get_json(silent=True) or {}).get("posts") or []
    if not ids:
        return jsonify(error="No slots chosen."), 400
    con = db()
    if not con.execute("SELECT 1 FROM media WHERE id=?", (mid,)).fetchone():
        return jsonify(error="no such file"), 404
    n = 0
    for pid in ids:
        n += con.execute(
            """UPDATE posts SET media_id=? WHERE id=? AND state='allocated'
               AND media_id IS NULL""", (mid, int(pid))).rowcount
    con.commit()
    return jsonify(attached=n)


@app.post("/api/posts/<int:pid>/republish")
def republish(pid):
    """Push an edit through to Zernio.

    Zernio can replace the text of a published post, but it cannot move a
    scheduled one, so a changed time means cancelling and re-creating it. Both
    paths are here because from the desk it is one action: make it match.
    """
    import push_zernio as pz
    con = db()
    r = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not r:
        return jsonify(error="no such post"), 404
    if not r["zernio_id"]:
        return jsonify(error="This one is not at Zernio, so there is nothing "
                             "to update. Publish it instead."), 400
    if not is_live():
        return jsonify(error="The desk is not live, so nothing was changed "
                             "at Zernio."), 400

    if r["state"] == "published":
        try:
            pz.api("POST", f"/posts/{r['zernio_id']}/edit",
                   body={"content": r["copy"] or ""})
        except RuntimeError as e:
            return jsonify(error=f"Zernio refused the edit: {e}"[:300]), 502
        return jsonify(updated="text", note="Media and timing cannot change "
                                            "on a post that is already out.")

    try:
        pz.api("DELETE", f"/posts/{r['zernio_id']}")
    except RuntimeError as e:
        return jsonify(error=f"Could not cancel the old one: {e}"[:300]), 502
    con.execute("UPDATE posts SET zernio_id=NULL, post_url=NULL, state='approved' "
                "WHERE id=?", (pid,))
    con.commit()
    out = subprocess.run([sys.executable, "push_zernio.py", "push", "--id", str(pid)],
                         cwd=ROOT, capture_output=True, text=True, timeout=900)
    return jsonify(updated="rescheduled", ok=out.returncode == 0,
                   output=((out.stdout or "") + (out.stderr or ""))[-1500:])


@app.delete("/api/posts/<int:pid>")
def post_delete(pid):
    """Take a post off the schedule.

    A slot the allocator made goes back to being an empty slot, because the
    allocator still believes it exists. One this desk invented is removed.
    """
    con = db()
    row = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify(error="no such post"), 404
    if row["state"] in ("scheduled", "published"):
        return jsonify(error="Already at Zernio. Cancel it there first."), 400
    adhoc = "adhoc-" in (row["slot_key"] or "") or "picked-" in (row["slot_key"] or "")
    if adhoc:
        con.execute("DELETE FROM posts WHERE id=?", (pid,))
    else:
        con.execute("""UPDATE posts SET copy=NULL, first_comment=NULL, tags=NULL,
                       collaborators=NULL, hashtags=NULL, why=NULL, media_id=NULL,
                       group_id=NULL, state='allocated' WHERE id=?""",
                    (pid,))
    con.commit()
    return jsonify(deleted=pid, removed=adhoc)


@app.post("/api/posts/<int:pid>/detach")
def post_detach(pid):
    db().execute("UPDATE posts SET media_id=NULL WHERE id=? AND state='allocated'",
                 (pid,))
    db().commit()
    return jsonify(ok=True)


def grab_image(url, timeout=25):
    """Download a URL that points straight at a picture.

    Returns (blob, mime, final_url, name), or None when the URL is a page
    rather than an image. Only the headers are read in that case, so the cost
    of asking is a request and no body.
    """
    import link as linkmod
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": linkmod.UA,
                                               "Accept": "image/*,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            return None
        final = r.geturl()
        blob = r.read(60 * 1024 * 1024)
    # The last bit of the path is usually the picture's name, but plenty of
    # hosts end in a number or a hash. Anything that tells you nothing gets
    # the site's name instead, so the library stays readable.
    bits = urllib.parse.urlsplit(final)
    stem = Path(urllib.parse.unquote(bits.path.rstrip("/").split("/")[-1])).stem
    if len(stem) < 3 or re.fullmatch(r"[\d_-]+", stem) or re.fullmatch(r"[0-9a-f]{16,}", stem, re.I):
        stem = (bits.hostname or "image").replace("www.", "").split(".")[0]
    return blob, mime, final, (stem or "image")


def save_image(con, prof, b, blob, mime, url, title):
    """Put a downloaded picture in the library, the same as an uploaded one."""
    if not blob:
        return jsonify(error="That link gave back nothing."), 400
    digest = hashlib.sha256(blob).hexdigest()
    have = con.execute("SELECT * FROM media WHERE sha256=? AND profile=?",
                       (digest, prof)).fetchone()
    if have:
        return jsonify(added=dict(have), already=True)
    ext = mimetypes.guess_extension(mime) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    base = secure_filename(title)[:60] or "image"
    name = f"{digest[:12]}-{base}{ext}"
    dest = MEDIA / prof
    dest.mkdir(parents=True, exist_ok=True)
    (dest / name).write_bytes(blob)
    import probe as _probe
    w, h, _ = _probe.probe(dest / name)
    con.execute(
        """INSERT OR IGNORE INTO media (profile,campaign,path,original,kind,mime,
           bytes,sha256,width,height,url,title,source,added)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (prof, b.get("campaign") or None, f"{prof}/{name}", base + ext, "image",
         mime, len(blob), digest, w, h, url, title, "grab",
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    row = con.execute("SELECT * FROM media WHERE sha256=? AND profile=?",
                      (digest, prof)).fetchone()
    return jsonify(added=dict(row) if row else None)


@app.post("/api/media/link")
def media_link():
    """Add a web page: its feature image becomes the media, its opening
    becomes the caption, and the link goes at the end of the post."""
    import link as linkmod
    b = request.get_json(force=True)
    prof = (b.get("profile") or "").strip()
    if prof not in profiles():
        return jsonify(error="Choose a profile first."), 400
    raw_url = (b.get("url") or "").strip()
    if not raw_url:
        return jsonify(error="Paste a link first."), 400
    if "://" in raw_url and not raw_url.lower().startswith(("http://", "https://")):
        return jsonify(error="Only http and https links."), 400

    # A link straight to a picture is not a page, and asking a page reader to
    # parse a JPEG gets you an error rather than the picture. Check first.
    try:
        shot = grab_image(raw_url)
    except Exception as e:
        return jsonify(error=f"Could not fetch that: {e}"[:300]), 400
    if shot:
        return save_image(db(), prof, b, *shot)

    try:
        page = linkmod.fetch(raw_url)
    except Exception as e:
        return jsonify(error=f"Could not read that page: {e}"[:300]), 400
    if not page.get("image"):
        return jsonify(error="That page has no feature image, so there is "
                             "nothing to post with."), 400

    con = db()
    if con.execute("SELECT 1 FROM media WHERE url=? AND profile=?",
                   (page["url"], prof)).fetchone():
        return jsonify(error="That page is already in the library."), 400

    try:
        req = urllib.request.Request(page["image"],
                                     headers={"User-Agent": linkmod.UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = r.read(40 * 1024 * 1024)
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
    except Exception as e:
        return jsonify(error=f"Could not fetch the feature image: {e}"[:300]), 400

    ext = mimetypes.guess_extension(mime) or ".jpg"
    digest = hashlib.sha256(blob).hexdigest()
    base = secure_filename(page["title"] or "page")[:60] or "page"
    name = f"{digest[:12]}-{base}{ext}"
    dest = MEDIA / prof
    dest.mkdir(parents=True, exist_ok=True)
    (dest / name).write_bytes(blob)

    import probe as _probe
    w, h, _ = _probe.probe(dest / name)
    con.execute(
        """INSERT OR IGNORE INTO media (profile,campaign,path,original,kind,mime,
           bytes,sha256,width,height,url,title,note,source,added)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (prof, b.get("campaign") or None, f"{prof}/{name}",
         page["title"] or page["url"], "image", mime, len(blob), digest, w, h,
         page["url"], page["title"], page["excerpt"], "link",
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    row = con.execute("SELECT * FROM media WHERE sha256=?", (digest,)).fetchone()
    return jsonify(added=dict(row) if row else None)


@app.get("/media/<int:mid>/raw")
def media_raw(mid):
    row = db().execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if not row:
        abort(404)
    p = MEDIA / row["path"]
    if not p.exists():
        abort(404)
    return send_file(p, mimetype=row["mime"] or None,
                     download_name=row["original"], conditional=True)


@app.get("/api/folders")
def folder_list():
    """The folders on one profile, with how much is in each."""
    prof = request.args.get("profile") or ""
    bucket = request.args.get("bucket") or "library"
    rows = db().execute(
        """SELECT f.id, f.name, COUNT(m.id) AS n FROM folders f
           LEFT JOIN media m ON m.folder_id = f.id
           WHERE f.profile=? AND f.bucket=? GROUP BY f.id
           ORDER BY f.name COLLATE NOCASE""",
        (prof, bucket)).fetchall()
    loose = db().execute(
        """SELECT COUNT(*) FROM media WHERE profile=? AND folder_id IS NULL
           AND bucket=?""", (prof, bucket)).fetchone()[0]
    return jsonify(folders=[dict(r) for r in rows], loose=loose)


@app.post("/api/folders")
def folder_add():
    b = request.get_json(force=True)
    name = (b.get("name") or "").strip()
    prof = b.get("profile") or ""
    bucket = b.get("bucket") or "library"
    if not name:
        return jsonify(error="Give it a name."), 400
    if not prof:
        return jsonify(error="No profile."), 400
    con = db()
    have = con.execute("SELECT * FROM folders WHERE profile=? AND name=?",
                       (prof, name)).fetchone()
    if have and have["bucket"] != bucket:
        return jsonify(error=f"There is already a folder called {name} on the "
                             f"{'Ads' if have['bucket'] == 'ads' else 'Media'} "
                             f"tab."), 409
    if have:
        return jsonify(dict(have))            # naming it twice is not an error
    con.execute("INSERT INTO folders (profile,name,created,bucket) VALUES (?,?,?,?)",
                (prof, name, datetime.now().isoformat(timespec="seconds"), bucket))
    con.commit()
    return jsonify(dict(con.execute("SELECT * FROM folders WHERE profile=? AND name=?",
                                    (prof, name)).fetchone()))


@app.patch("/api/folders/<int:fid>")
def folder_rename(fid):
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify(error="Give it a name."), 400
    con = db()
    row = con.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    if not row:
        return jsonify(error="no such folder"), 404
    clash = con.execute(
        "SELECT id FROM folders WHERE profile=? AND name=? AND id<>?",
        (row["profile"], name, fid)).fetchone()
    if clash:
        return jsonify(error=f"There is already a folder called {name}."), 409
    con.execute("UPDATE folders SET name=? WHERE id=?", (name, fid))
    con.commit()
    return jsonify(dict(con.execute("SELECT * FROM folders WHERE id=?",
                                    (fid,)).fetchone()))


@app.delete("/api/folders/<int:fid>")
def folder_del(fid):
    """Remove the folder. What was in it goes loose, never away: a folder is a
    label, and deleting a label should not delete the thing it was on."""
    con = db()
    row = con.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    if not row:
        return jsonify(error="no such folder"), 404
    n = con.execute("SELECT COUNT(*) FROM media WHERE folder_id=?", (fid,)).fetchone()[0]
    con.execute("UPDATE media SET folder_id=NULL WHERE folder_id=?", (fid,))
    con.execute("DELETE FROM folders WHERE id=?", (fid,))
    con.commit()
    return jsonify(deleted=fid, loosed=n)


@app.post("/api/media/move")
def media_move():
    """Put files in a folder, or take them out of one. Bulk, because moving a
    selection one file at a time is how a media tab becomes unmanageable."""
    b = request.get_json(force=True)
    ids = [int(i) for i in (b.get("media") or [])]
    if not ids:
        return jsonify(error="Nothing to move."), 400
    fid = b.get("folder_id")
    con = db()
    if fid not in (None, "", 0):
        fid = int(fid)
        f = con.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not f:
            return jsonify(error="no such folder"), 404
        # A file belongs to a profile and so does a folder; crossing them would
        # put a file somewhere its own profile cannot see it.
        wrong = con.execute(
            "SELECT COUNT(*) FROM media WHERE id IN (%s) AND profile<>?"
            % ",".join("?" * len(ids)), (*ids, f["profile"])).fetchone()[0]
        if wrong:
            return jsonify(error="That folder belongs to another profile."), 400
    else:
        fid = None
    con.execute("UPDATE media SET folder_id=? WHERE id IN (%s)"
                % ",".join("?" * len(ids)), (fid, *ids))
    con.commit()
    return jsonify(moved=len(ids), folder_id=fid)


@app.patch("/api/media/<int:mid>")
def media_edit(mid):
    body = request.get_json(force=True)
    fields = {k: v for k, v in body.items()
              if k in ("note", "campaign", "profile", "title", "caption",
                       "folder_id", "made_from")}
    if not fields:
        return jsonify(error="nothing to change"), 400
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE media SET {sets} WHERE id=?", (*fields.values(), mid))
    db().commit()
    return jsonify(dict(db().execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()))


@app.delete("/api/media/<int:mid>")
def media_del(mid):
    con = db()
    row = con.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="no such file"), 404
    used = con.execute(
        """SELECT COUNT(*) FROM posts WHERE media_id=?
           AND state NOT IN ('allocated','rejected')""", (mid,)).fetchone()[0]
    if used:
        return jsonify(error=f"Attached to {used} post(s) that are past drafting. "
                             f"Detach it there first."), 400
    (MEDIA / row["path"]).unlink(missing_ok=True)
    con.execute("UPDATE posts SET media_id=NULL WHERE media_id=?", (mid,))
    con.execute("DELETE FROM media WHERE id=?", (mid,))
    con.commit()
    return jsonify(deleted=mid)


# ---------------------------------------------------------------- ads
#
# Ads are posted as they were made: the picture and the words on it, with the
# ad's own tracked link. Nothing is written here. The words come from the
# manifest the ads were built with (guavy-ads.json), so what the post says is
# exactly what the ad says, and the link carries the campaign that made it.

def ads_cfg():
    return channels_cfg().get("ads") or {}


def ads_mode():
    return setting("ads.mode", "manual")


def ad_spacing(spec, cfg):
    """Hours between two ads on one channel: set, or the waking day shared out."""
    if spec.get("min_hours") is not None:
        return float(spec["min_hours"])
    quiet = cfg.get("quiet_hours") or markets_cfg().get("quiet_hours") or [23, 7]
    a, b = int(quiet[0]), int(quiet[1])
    awake = (a - b) % 24 or 24
    return round(awake / max(int(spec.get("per_day") or 1), 1), 2)


def ad_key(ad):
    """What an ad is called in the post record: its group and variation.

    Not its row id. A new zip rebuilds every row, and keying history on the
    id would forget what ran yesterday, so an ad could repeat the next
    morning. Brand v2 is Brand v2 across uploads.
    """
    return f"ad:{ad['property']}:{ad['variation']}"


def _ad_row(con, ad):
    d = dict(ad)
    d["sizes"] = [dict(r) for r in con.execute(
        """SELECT size, width, height, media_id FROM ad_sizes WHERE ad_id=?
           ORDER BY width*1.0/height DESC, size""", (ad["id"],))]
    d["media"] = (dict(con.execute("SELECT * FROM media WHERE id=?",
                                   (ad["media_id"],)).fetchone() or {})
                  if ad["media_id"] else None)
    return d


def ad_copy(ad, plat, limit=None, link_below=False, no_link=False):
    """The ad's own words, in the order the ad reads, with its link last.

    A channel too short for all of it drops the sub-line first; the headline,
    the call to action and the link are the ad.
    """
    head = (ad.get("headline") or "").strip()
    sub = (ad.get("sub") or "").strip()
    cta = (ad.get("cta") or "").strip()
    url = (ad.get("url") or "").strip()
    tail = f"{cta}: {url}" if cta else url
    if link_below and url:
        # The link is lifted into the first comment, so the line it leaves
        # behind has to say where it went rather than end on a colon.
        tail = (f"{cta}: link in the first comment" if cta
                else "Link in the first comment") + f"\n{url}"
    if no_link:
        # No link, so no call to action either: "Sign Up" with nowhere to go
        # reads as a fault. The picture carries the button and guavy.com.
        tail = ""
    for parts in ((head, sub, tail), (head, tail),
                  ((ad.get("short_headline") or head).strip(), tail)):
        text = "\n\n".join(x for x in parts if x)
        if not limit or body_length(text, plat) <= limit:
            return text
    return None


def ad_due(con, chan, profile, now=None):
    """Whether an ad may go to this channel now, and why not."""
    cfg = ads_cfg()
    spec = ((cfg.get("channels") or {}).get(chan)) or {}
    if not spec:
        return False, "no ad pacing for this channel"
    if spec.get("paused"):
        return False, str(spec["paused"])
    z = ((profiles().get(profile) or {}).get("zernio") or {}).get("accounts") or {}
    if z.get(chan) in (None, "", "TODO"):
        return False, "not connected"
    tz = channels_cfg().get("timezone")
    now = now or datetime.now(ZoneInfo(tz) if (ZoneInfo and tz) else None)
    quiet = cfg.get("quiet_hours") or markets_cfg().get("quiet_hours") or []
    if len(quiet) == 2:
        a, b = int(quiet[0]), int(quiet[1])
        if (a <= now.hour or now.hour < b) if a > b else (a <= now.hour < b):
            return False, "quiet hours"
    used = con.execute(
        """SELECT COUNT(*) FROM posts WHERE campaign='ads' AND profile=?
           AND channel=? AND date=? AND state != 'rejected'""",
        (profile, chan, now.date().isoformat())).fetchone()[0]
    if used >= int(spec.get("per_day") or 0):
        return False, f"daily cap reached ({used})"
    gap = ad_spacing(spec, cfg)

    def since(sql, args):
        t = con.execute(sql, args).fetchone()[0]
        if not t:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(t)).total_seconds() / 3600
        except ValueError:
            return None
    h = since("""SELECT MAX(updated) FROM posts WHERE campaign='ads' AND
                 profile=? AND channel=? AND state != 'rejected'""", (profile, chan))
    if h is not None and h < gap:
        return False, f"spacing, {gap:g}h between ads"
    # Clear of anything else on the account, so an ad never lands on top of a
    # Wire post and reads as one double post.
    clear = float(cfg.get("clear_minutes") or 15) / 60
    h = since("""SELECT MAX(updated) FROM posts WHERE campaign != 'ads' AND
                 profile=? AND channel=? AND state IN ('scheduled','published',
                 'approved')""", (profile, chan))
    if h is not None and h < clear:
        return False, "too close to a Wire post"
    return True, ""


def pick_ad(con, chan, profile, exclude=()):
    """The active ad that has gone longest without running on this channel.

    Never one that ran there within `repeat_days`. Ties, which is every ad
    that has never run, are broken at random so a new batch does not go out
    in the order it was imported.
    """
    days = float(ads_cfg().get("repeat_days") or 3)
    cut = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = con.execute(
        """SELECT a.*, (SELECT MAX(p.updated) FROM posts p WHERE p.campaign='ads'
             AND p.channel=? AND p.asset='ad:'||a.property||':'||a.variation AND p.state != 'rejected')
             AS last_here
           FROM ads a WHERE a.profile=? AND a.active=1 AND a.media_id IS NOT NULL""",
        (chan, profile)).fetchall()
    ok = [r for r in rows if r["id"] not in exclude
          and (not r["last_here"] or r["last_here"] < cut)]
    if not ok:
        return None
    random.shuffle(ok)
    ok.sort(key=lambda r: r["last_here"] or "")
    return ok[0]


def ad_size_for(chan):
    """The size this channel posts ads in: chosen on the Ads tab, or the
    channel's default from channels.yaml."""
    spec = ((ads_cfg().get("channels") or {}).get(chan)) or {}
    return (setting(f"ads.size.{chan}") or spec.get("size") or "").lower()


def ad_media_for(con, ad, chan):
    """The picture of this ad to post on this channel: (row, size, exact).

    The chosen size if the ad was made in it; otherwise the size nearest it
    in shape, so a channel set to 1080x1350 still posts something sensible
    from a zip that only carries squares, and says so.
    """
    want = ad_size_for(chan)
    rows = con.execute("SELECT * FROM ad_sizes WHERE ad_id=?", (ad["id"],)).fetchall()
    pick, exact = None, False
    if rows:
        pick = next((r for r in rows if r["size"] == want), None)
        exact = pick is not None
        if not pick:
            try:
                ww, wh = (int(x) for x in want.split("x"))
                ratio = ww / wh
            except (ValueError, ZeroDivisionError):
                ratio = 1.0
            pick = min(rows, key=lambda r: abs((r["width"] or 1) / (r["height"] or 1) - ratio))
        media = con.execute("SELECT * FROM media WHERE id=?",
                            (pick["media_id"],)).fetchone()
        return media, pick["size"], exact
    media = con.execute("SELECT * FROM media WHERE id=?",
                        (ad["media_id"],)).fetchone() if ad["media_id"] else None
    size = f"{media['width']}x{media['height']}" if media else ""
    return media, size, size == want


def ad_text(ad, chan, cfg=None):
    """What an ad says on one channel: (body, first_comment, link_rule).

    The one place that decides, so the preview on the Ads tab is the post and
    not a guess at it. Per ad channel, `links: none` posts it without its
    link (X, where a link costs $0.20 a post); otherwise the link goes in the
    first comment where the channel keeps links out of the body, and in the
    body where it does not.
    """
    cfg = cfg or channels_cfg()
    ch = (cfg.get("channels") or {}).get(chan) or {}
    plat = ch.get("platform") or chan.split("_")[0]
    aspec = ((ads_cfg().get("channels") or {}).get(chan)) or {}
    none = str(aspec.get("links") or "").lower() == "none"
    below = not none and bool(ch.get("drop_links") or ch.get("links_in_first_comment"))
    copy = ad_copy(dict(ad), plat, max_chars(cfg, chan), link_below=below,
                   no_link=none)
    if copy is None:
        return None, "", not none
    rule = ch
    if not none and (ch.get("drop_links") or ch.get("links_in_first_comment")):
        rule = dict(ch, drop_links=False, links_in_first_comment=True)
    body, first = split_link(copy, rule)
    return body, first, not none


@app.get("/api/ads/<int:aid>/preview")
def ad_preview(aid):
    """Each channel's post for one ad, exactly as it would go out."""
    con = db()
    ad = con.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()
    if not ad:
        return jsonify(error="no such ad"), 404
    cfg = channels_cfg()
    accts = ((profiles().get(ad["profile"]) or {}).get("zernio") or {}).get("accounts") or {}
    out = []
    for chan, spec in (ads_cfg().get("channels") or {}).items():
        if accts.get(chan) in (None, "", "TODO"):
            continue                 # never posts there, so nothing to preview
        body, first, _ = ad_text(ad, chan, cfg)
        plat = ((cfg.get("channels") or {}).get(chan) or {}).get("platform") or chan
        media, size, exact = ad_media_for(con, ad, chan)
        out.append(dict(channel=chan, body=body, first_comment=first,
                        paused=bool(spec.get("paused")),
                        media_id=media["id"] if media else None, size=size,
                        exact=exact, wanted=ad_size_for(chan),
                        chars=body_length(body or "", plat),
                        limit=max_chars(cfg, chan)))
    return jsonify(ad=_ad_row(con, ad), channels=out)


def ad_once(chan, profile, send=True, at_minutes=None, ad_id=None):
    """Post one ad to one channel."""
    con, cfg = db(), channels_cfg()
    ch = (cfg.get("channels") or {}).get(chan) or {}
    plat = ch.get("platform") or chan.split("_")[0]
    if ad_id:
        ad = con.execute("SELECT * FROM ads WHERE id=? AND profile=?",
                         (int(ad_id), profile)).fetchone()
    else:
        ad = pick_ad(con, chan, profile)
    if not ad:
        return dict(skipped="no ad is free to run on this channel")
    media, _size, _exact = ad_media_for(con, ad, chan)
    if not media:
        return dict(skipped=f"ad {ad['id']} has no picture")
    body, first, keep_link = ad_text(ad, chan, cfg)
    if not body:
        return dict(skipped=f"ad {ad['id']} does not fit {plat}")
    # Handed over with the link back on the end of the body, so _compose_one
    # makes the same split again rather than this having to bypass it.
    copy = body + (f"\n{first}" if first else "")
    made = _compose_one(con, cfg, profile, chan, [dict(media)], copy,
                        ad_key(ad), ad["property_name"] or "Ad",
                        ad["headline"] or "", send=send, at_minutes=at_minutes,
                        campaign="ads", links_to_comment=keep_link)
    if not made:
        return dict(skipped="no slot free on that channel")
    return dict(posted=made, sent=bool(send), ad=ad["id"],
                headline=ad["headline"], channel=chan)


@app.get("/api/ads")
def ads_list():
    """Every ad on a profile, with where and when each has run."""
    prof = request.args.get("profile") or ""
    con = db()
    out = []
    for a in con.execute("SELECT * FROM ads WHERE profile=? ORDER BY property, "
                         "variation", (prof,)):
        d = _ad_row(con, a)
        runs = con.execute(
            """SELECT channel, COUNT(*) n, MAX(updated) last FROM posts
               WHERE campaign='ads' AND asset=? AND state != 'rejected'
               GROUP BY channel""", (ad_key(a),)).fetchall()
        d["runs"] = {r["channel"]: dict(n=r["n"], last=r["last"]) for r in runs}
        out.append(d)
    return jsonify(ads=out, mode=ads_mode())


@app.post("/api/ads/import")
def ads_import():
    """Replace every ad with the set in one upload.

    The upload is a .zip of the ads folder: guavy-ads.json and the pictures in
    their group folders. A loose folder or a .json with its pictures works the
    same way. Nothing is touched until the upload has been read and found to
    hold a manifest; then the old ads, their pictures and their folders go,
    and the new set is built in their place. History survives, because it is
    kept by group and variation rather than by row: Brand v2 that ran
    yesterday is still Brand v2 today. A picture a waiting post still needs
    is kept.
    """
    import zipfile, io
    prof = request.form.get("profile") or ""
    if prof not in profiles():
        return jsonify(error="Choose a profile first."), 400
    manifest, pics = None, {}

    def take(name, read):
        nonlocal manifest
        base = Path(name).name
        if not base or base.startswith(".") or "__MACOSX" in name:
            return
        if base.lower().endswith(".json"):
            try:
                manifest = json.loads(read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"{base} is not readable JSON: {e}")
        elif base.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "webp"):
            pics[base] = read()

    try:
        for f in request.files.getlist("files"):
            if (f.filename or "").lower().endswith(".zip"):
                try:
                    z = zipfile.ZipFile(io.BytesIO(f.read()))
                except zipfile.BadZipFile:
                    return jsonify(error=f"{f.filename} is not a readable zip."), 400
                for info in z.infolist():
                    if not info.is_dir():
                        take(info.filename, lambda i=info: z.read(i))
            else:
                take(f.filename or "", f.read)
    except ValueError as e:
        return jsonify(error=str(e)[:300]), 400
    if manifest is None:
        return jsonify(error="No ads manifest (guavy-ads.json) in that upload. "
                             "Nothing was changed."), 400
    items = manifest if isinstance(manifest, list) else (
        next((v for v in manifest.values() if isinstance(v, list)), []))
    if not items:
        return jsonify(error="The manifest lists no ads. Nothing was changed."), 400

    con = db()
    today = date.today().isoformat()
    # Pictures a post that has not gone out yet still points at.
    keep = set()
    for r in con.execute("""SELECT media_id, media_ids FROM posts WHERE state IN
                            ('allocated','drafted','approved') OR (state='scheduled'
                            AND date >= ?)""", (today,)):
        if r["media_id"]:
            keep.add(r["media_id"])
        try:
            keep.update(json.loads(r["media_ids"] or "[]"))
        except ValueError:
            pass
    old = con.execute("SELECT COUNT(*) FROM ads WHERE profile=?", (prof,)).fetchone()[0]
    removed = 0
    for m in con.execute("SELECT id, path FROM media WHERE profile=? AND bucket='ads'",
                         (prof,)).fetchall():
        if m["id"] in keep:
            con.execute("UPDATE media SET folder_id=NULL WHERE id=?", (m["id"],))
            continue
        (MEDIA / m["path"]).unlink(missing_ok=True)
        con.execute("UPDATE posts SET media_id=NULL WHERE media_id=?", (m["id"],))
        con.execute("DELETE FROM media WHERE id=?", (m["id"],))
        removed += 1
    con.execute("DELETE FROM ad_sizes WHERE ad_id IN (SELECT id FROM ads "
                "WHERE profile=?)", (prof,))
    con.execute("DELETE FROM ads WHERE profile=?", (prof,))
    con.execute("DELETE FROM folders WHERE profile=? AND bucket='ads'", (prof,))
    con.commit()

    made, missing = 0, []
    con.execute("DELETE FROM ad_sizes WHERE ad_id NOT IN (SELECT id FROM ads)")
    for it in items:
        sizes = it.get("sizes") or []
        rel = (sizes[0] or {}).get("file") if sizes else ""
        group = it.get("propertyName") or it.get("property") or "Ads"
        got = []                  # (size, width, height, media id) per picture
        for sz in sizes:
            f = (sz or {}).get("file") or ""
            blob = pics.get(Path(f).name)
            if not blob:
                missing.append(f or f"{group} v{it.get('variation')}")
                continue
            row = _keep_image(con, prof, blob,
                              mimetypes.guess_type(f)[0] or "image/png", None,
                              Path(f).stem, it.get("headline"), "ad")
            if not row:
                continue
            con.execute("UPDATE media SET bucket='ads', folder_id=?, original=? "
                        "WHERE id=?", (_ads_folder(con, prof, group),
                                       Path(f).name, row["id"]))
            label = (sz.get("size") or f"{row['width']}x{row['height']}").lower()
            got.append((label, row["width"], row["height"], row["id"]))
        # The tile shows the square one where there is one: it is the shape
        # the grid is laid out in.
        mid = next((g[3] for g in got if g[1] and g[1] == g[2]),
                   got[0][3] if got else None)
        con.execute(
            """INSERT INTO ads (profile, property, variation, added, media_id,
               property_name, audience, headline, short_headline, sub, cta, url,
               file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prof, it.get("property"), it.get("variation"),
             datetime.now().isoformat(timespec="seconds"), mid, group,
             it.get("audience"), it.get("headline"), it.get("shortHeadline"),
             it.get("sub"), it.get("cta"),
             it.get("propertyUtm") or it.get("propertyUrl"), rel))
        aid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        for label, w, h, m in got:
            con.execute("INSERT OR REPLACE INTO ad_sizes (ad_id,size,width,height,"
                        "media_id) VALUES (?,?,?,?,?)", (aid, label, w, h, m))
        made += 1
    con.commit()
    return jsonify(replaced=old, added=made, missing=missing,
                   pictures_removed=removed, total=len(items))


def _ads_folder(con, prof, name):
    """The ads-shelf folder for one group of ads, made on first use."""
    row = con.execute("SELECT * FROM folders WHERE profile=? AND name=?",
                      (prof, name)).fetchone()
    if row and row["bucket"] == "ads":
        return row["id"]
    if row:                       # a Media folder has the name; keep them apart
        name = f"{name} ads"
        row = con.execute("SELECT * FROM folders WHERE profile=? AND name=?",
                          (prof, name)).fetchone()
        if row:
            return row["id"]
    con.execute("INSERT INTO folders (profile,name,created,bucket) VALUES (?,?,?,'ads')",
                (prof, name, datetime.now().isoformat(timespec="seconds")))
    return con.execute("SELECT id FROM folders WHERE profile=? AND name=?",
                       (prof, name)).fetchone()[0]


@app.patch("/api/ads/<int:aid>")
def ad_edit(aid):
    b = request.get_json(force=True)
    fields = {k: b[k] for k in ("active", "headline", "sub", "cta", "url",
                                "short_headline") if k in b}
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    if not fields:
        return jsonify(error="nothing to change"), 400
    con = db()
    con.execute("UPDATE ads SET " + ", ".join(f"{k}=?" for k in fields)
                + " WHERE id=?", (*fields.values(), aid))
    con.commit()
    return jsonify(dict(con.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()))


@app.delete("/api/ads/<int:aid>")
def ad_delete(aid):
    """Take an ad off the list. Its picture stays in the library, and what it
    already posted stays in the record."""
    con = db()
    con.execute("DELETE FROM ads WHERE id=?", (aid,))
    con.commit()
    return jsonify(deleted=aid)


@app.post("/api/ads/mode")
def ads_mode_set():
    b = request.get_json(silent=True) or {}
    mode = b.get("mode")
    if mode not in ("auto", "manual"):
        return jsonify(error="auto or manual"), 400
    if mode == "auto" and not is_live():
        return jsonify(error="The desk is not Live, so nothing would reach "
                             "Zernio. Arm it first."), 400
    set_setting("ads.mode", mode)
    return jsonify(mode=mode)


@app.get("/api/ads/pacing")
def ads_pacing():
    """The ads pacing table: per channel, the allowance and where it stands."""
    prof = request.args.get("profile") or ""
    con, cfg = db(), ads_cfg()
    tz = channels_cfg().get("timezone")
    now = datetime.now(ZoneInfo(tz) if (ZoneInfo and tz) else None)
    out = []
    for chan, spec in (cfg.get("channels") or {}).items():
        ok, why = ad_due(con, chan, prof, now)
        used = con.execute(
            """SELECT COUNT(*) FROM posts WHERE campaign='ads' AND profile=?
               AND channel=? AND date=? AND state != 'rejected'""",
            (prof, chan, now.date().isoformat())).fetchone()[0]
        last = con.execute(
            """SELECT p.updated, a.headline FROM posts p LEFT JOIN ads a
               ON p.asset='ad:'||a.property||':'||a.variation WHERE p.campaign='ads' AND p.profile=?
               AND p.channel=? AND p.state != 'rejected'
               ORDER BY p.updated DESC LIMIT 1""", (prof, chan)).fetchone()
        gap = ad_spacing(spec, cfg)
        nxt = None
        if last and last["updated"]:
            try:
                nxt = (datetime.fromisoformat(last["updated"])
                       + timedelta(hours=gap)).strftime("%H:%M")
            except ValueError:
                pass
        out.append(dict(channel=chan, per_day=int(spec.get("per_day") or 0),
                        paused=bool(spec.get("paused")),
                        spacing=gap, used_today=used, due=ok, blocked=why,
                        last=last["updated"] if last else None,
                        last_headline=last["headline"] if last else None,
                        next_at=nxt))
    sizes = [dict(size=r["size"], ads=r["n"]) for r in con.execute(
        """SELECT s.size, COUNT(DISTINCT s.ad_id) n FROM ad_sizes s
           JOIN ads a ON a.id=s.ad_id WHERE a.profile=? GROUP BY s.size
           ORDER BY s.size""", (prof,))]
    total = con.execute("SELECT COUNT(*) FROM ads WHERE profile=?", (prof,)).fetchone()[0]
    have = {x["size"]: x["ads"] for x in sizes}
    for c in out:
        c["size"] = ad_size_for(c["channel"])
        c["size_ads"] = have.get(c["size"], 0)
    return jsonify(channels=out, mode=ads_mode(), sizes=sizes, total_ads=total,
                   repeat_days=float(cfg.get("repeat_days") or 3),
                   quiet_hours=cfg.get("quiet_hours")
                   or markets_cfg().get("quiet_hours") or [])


@app.post("/api/ads/size")
def ads_size_set():
    """Which size of ad a channel posts."""
    b = request.get_json(silent=True) or {}
    chan, size = b.get("channel"), (b.get("size") or "").lower()
    if chan not in (ads_cfg().get("channels") or {}):
        return jsonify(error="No ad pacing for that channel."), 400
    if not re.fullmatch(r"\d+x\d+", size):
        return jsonify(error="A size looks like 1080x1350."), 400
    set_setting(f"ads.size.{chan}", size)
    return jsonify(channel=chan, size=size)


@app.post("/api/ads/fire")
def ads_fire():
    """Post one ad now, to one channel: the next in rotation, or a chosen one."""
    b = request.get_json(silent=True) or {}
    prof, chan = b.get("profile") or "", b.get("channel")
    if not chan:
        return jsonify(error="Which channel?"), 400
    held = ((ads_cfg().get("channels") or {}).get(chan) or {}).get("paused")
    if held:
        return jsonify(error=f"{chan} is paused: {held}"), 409
    if not b.get("force"):
        ok, why = ad_due(db(), chan, prof)
        if not ok:
            return jsonify(error=f"Not due: {why}"), 409
    if not _FIRING.acquire(blocking=False):
        return jsonify(error="Already building a post. One at a time."), 429
    try:
        out = ad_once(chan, prof, send=bool(b.get("send")),
                      at_minutes=b.get("at_minutes"), ad_id=b.get("ad"))
    finally:
        _FIRING.release()
    return jsonify(**out)


# ------------------------------------------------------------ media cleanup
#
# Every Wire post draws two pictures and renders two more, so the library grew
# by hundreds of files a week that nobody would open again. Once a post has
# gone, Zernio holds its own copy, so the desk's is only needed while the post
# is still waiting to go out.

AUTO_KEEP_DAYS = float(os.environ.get("AUTO_KEEP_DAYS", "7"))


def auto_media_to_clean(con, days=AUTO_KEEP_DAYS):
    """Auto-made pictures older than `days` that no waiting post still needs."""
    cut = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    today = date.today().isoformat()
    keep = set()
    for r in con.execute(
            """SELECT media_id, media_ids FROM posts WHERE state IN
               ('allocated','drafted','approved')
               OR (state='scheduled' AND date >= ?)""", (today,)):
        if r["media_id"]:
            keep.add(r["media_id"])
        try:
            keep.update(json.loads(r["media_ids"] or "[]"))
        except ValueError:
            pass
    marks = ",".join("?" * len(AUTO_SOURCES))
    return [dict(r) for r in con.execute(
        f"""SELECT id, path, bytes FROM media WHERE source IN ({marks})
            AND bucket != 'ads' AND added < ?""", (*AUTO_SOURCES, cut))
            if r["id"] not in keep]


def clean_auto_media(con, days=AUTO_KEEP_DAYS):
    gone = auto_media_to_clean(con, days)
    for r in gone:
        (MEDIA / r["path"]).unlink(missing_ok=True)
        con.execute("UPDATE posts SET media_id=NULL WHERE media_id=?", (r["id"],))
        con.execute("DELETE FROM media WHERE id=?", (r["id"],))
    con.commit()
    return dict(removed=len(gone), mb=round(sum(r["bytes"] or 0 for r in gone) / 1e6, 1))


@app.post("/api/media/cleanup")
def media_cleanup():
    """What the daily clean-up would remove, or remove it now with run=true."""
    b = request.get_json(silent=True) or {}
    con = db()
    if b.get("run"):
        out = clean_auto_media(con)
        set_setting("cleanup.last", date.today().isoformat())
        return jsonify(**out)
    gone = auto_media_to_clean(con)
    return jsonify(would_remove=len(gone), days=AUTO_KEEP_DAYS,
                   mb=round(sum(r["bytes"] or 0 for r in gone) / 1e6, 1),
                   last=setting("cleanup.last"))


# ---------------------------------------------------------------- page

@app.get("/")
def index():
    """The page, with the caption timings baked in.

    The box has to predict the length the renderer will pick, or Auto says one
    thing and the file comes out another. The numbers come from slideshow.py so
    there is only one place to change them.
    """
    import slideshow as sl
    timing = dict(delay=sl.CAP_DELAY, step=sl.CAP_STEP, fade=sl.CAP_FADE,
                  read=sl.CAP_READ, minRead=sl.CAP_MIN_READ, single=sl.SINGLE,
                  per=sl.PER, autoMax=sl.AUTO_MAX, max=sl.MAX)
    # The page is built fresh on every request and changes whenever app.py
    # does, so there is nothing worth caching and a stale one is confusing:
    # a reload that quietly serves yesterday's JavaScript looks like a bug in
    # the desk rather than in the browser.
    r = Response(PAGE.replace("/*TIMING*/null", json.dumps(timing)),
                 mimetype="text/html")
    r.headers["Cache-Control"] = "no-store, must-revalidate"
    return r


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guavynator Release Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600&display=swap" rel="stylesheet">
<style>
:root{
  --case:#1F2320; --panel:#272C28; --raised:#2F352F;
  --rule:#3D443E; --ink:#E5E3DC; --dim:#949C90; --faint:#6B7369;
  --ok:#7FA07A; --no:#C4634A;
  --sched:#5FD35A; --pub:#5B9BD5;
  --brand:#E0A33E;                 /* replaced per profile from profiles.yaml */
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--case);color:var(--ink);
  font:400 16px/1.5 Barlow,system-ui,sans-serif;font-variant-numeric:tabular-nums}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.01em}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
:focus-visible{outline:2px solid var(--ok);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

.wrap{max-width:1180px;margin:0 auto;padding:0 24px 80px}
header{display:flex;align-items:baseline;gap:20px;flex-wrap:wrap;
  padding:26px 0 18px;border-bottom:1px solid var(--rule)}
header h1{font-size:23px}
header .sub{color:var(--dim);font-size:15px}
header .spacer{flex:1}

nav{display:flex;gap:2px;margin:0 0 26px}
nav button{padding:11px 18px 9px;color:var(--dim);border-bottom:2px solid transparent;
  font-family:'Barlow Condensed',Barlow,sans-serif;font-size:19px;font-weight:600;
  letter-spacing:.02em}
nav button:hover{color:var(--ink)}
nav button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--brand)}
nav .count{color:var(--case);background:var(--ok);border-radius:9px;
  padding:1px 7px;font-size:13px;margin-left:7px;vertical-align:1px}

.act{border:1px solid var(--rule);background:var(--raised);padding:7px 15px;
  border-radius:3px;font-size:15px}
.act:hover{border-color:var(--dim)}
.act.go{background:var(--ok);color:#17201A;border-color:var(--ok);font-weight:600}
.act.no{color:var(--no);border-color:#4A3330}
.act:disabled{opacity:.4;cursor:default}

/* --- load strip: the three campaign curves crossing --- */
.strip{border:1px solid var(--rule);background:var(--panel);padding:20px 22px 14px;
  margin-bottom:22px}
.strip h2{font-size:16px;margin-bottom:3px}
.strip p{margin:0 0 18px;color:var(--dim);font-size:15px;max-width:62ch}
.strip p.sum{max-width:none}
.barwrap{overflow-x:auto;padding-bottom:2px}
.bars{display:flex;align-items:flex-end;gap:2px;height:132px;position:relative}
/* Faint horizontal rules at each tick, behind the bars, so heights can be
   compared across dates. The axis that names them sits to the right. */
.bars .grid{position:absolute;left:0;right:0;height:0;
  border-top:1px solid var(--rule);opacity:.55;pointer-events:none;z-index:0}
.bars .bar{position:relative;z-index:1}
.chartrow{display:flex;align-items:flex-start;gap:8px}
/* ---- ads tab ---- */
.adgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.adcard{border:1px solid var(--rule);background:var(--panel);display:flex;flex-direction:column;
  cursor:pointer}
.adcard:hover{border-color:var(--dim)}
#ads.over{outline:2px dashed var(--brand);outline-offset:6px}
/* Pacing tables fold up under their heading. Open unless you closed it. */
details.pace>summary{list-style:none;cursor:pointer}
details.pace>summary::-webkit-details-marker{display:none}
details.pace>summary .chev{display:inline-block;width:14px;color:var(--dim);
  transition:transform .15s;margin-right:4px}
details.pace[open]>summary .chev{transform:rotate(90deg)}
details.pace>summary:hover h2{color:var(--ink)}
details.pace>summary:focus-visible{outline:2px solid var(--brand);outline-offset:3px}
/* The ad, full size, beside what each channel will post. */
.admodal{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.62);
  display:flex;align-items:center;justify-content:center;padding:24px}
.admodal[hidden]{display:none}
.admodal .box{background:var(--panel);border:1px solid var(--rule);max-width:1100px;
  width:100%;max-height:calc(100vh - 48px);overflow:auto;display:grid;
  grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:0}
.admodal .pic{background:#000;display:flex;align-items:center;justify-content:center}
.admodal .pic img{max-width:100%;max-height:calc(100vh - 50px);width:auto;height:auto;display:block}
.admodal .txt{padding:20px 22px;display:flex;flex-direction:column;gap:14px}
.admodal .top{display:flex;align-items:flex-start;gap:12px}
.admodal .top h3{margin:0;font-size:18px;line-height:1.3;flex:1}
.admodal .grp{font-size:12px;color:var(--dim);letter-spacing:.04em;text-transform:uppercase}
.admodal .post{border:1px solid var(--rule);padding:10px 12px}
.admodal .post h4{margin:0 0 6px;font-size:13px;display:flex;gap:8px;align-items:center}
.admodal .post h4 .sub{font-weight:400}
.admodal .post pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit;
  font-size:14px;line-height:1.45}
.admodal .post .fc{margin-top:8px;font-size:13px;color:var(--dim);word-break:break-all}
@media (max-width:760px){.admodal .box{grid-template-columns:1fr}}
.adcard.off{opacity:.5}
/* Every tile the same square frame, the picture whole inside it, so a mix of
   square, 4:5 and 9:16 lines up in rows. */
.adcard .adpic{position:relative;aspect-ratio:1/1;background:#000;overflow:hidden}
.adcard .adpic img{position:absolute;inset:0;margin:auto;max-width:100%;max-height:100%;display:block}
.adcard .body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.adcard .grp{font-size:12px;color:var(--dim);letter-spacing:.04em;text-transform:uppercase}
.adcard .hl{font-weight:600;font-size:15px;line-height:1.3}
.adcard .sb{font-size:13px;color:var(--dim);line-height:1.4}
.adcard .cta{font-size:13px}
.adcard .adlink{color:var(--dim);text-decoration:none;margin-left:4px}
.adcard .adlink:hover{color:var(--ink);text-decoration:underline}
.adcard .runs{font-size:12px;color:var(--dim);margin-top:auto}
.adcard .row{display:flex;align-items:center;gap:8px;font-size:13px}
.chartrow .barwrap{flex:1;min-width:0}
.yaxis{position:relative;width:46px;flex:none;height:132px;color:var(--dim);
  font-size:12px;font-family:'Barlow Condensed',sans-serif;letter-spacing:.02em}
.yaxis span{position:absolute;left:0;transform:translateY(50%);white-space:nowrap}
.bar{flex:1;min-width:0;height:100%;display:flex;flex-direction:column-reverse;
  gap:1px;border-radius:1px}
.bar span{display:block;min-height:2px}
.bar:hover{background:rgba(229,227,220,.07)}
/* Labels sit centred over their column and are allowed to overflow it, so a
   narrow day column does not clip "14 Sep" down to "09". */
.axis{display:flex;gap:2px;margin-top:9px;color:var(--dim);font-size:13px;
  font-family:'Barlow Condensed',sans-serif;letter-spacing:.02em;height:16px}
.axis div{flex:1;min-width:0;position:relative}
.axis div b{position:absolute;left:50%;transform:translateX(-50%);
  font-weight:600;white-space:nowrap}
.tip{position:fixed;z-index:60;pointer-events:none;background:var(--raised);
  border:1px solid var(--rule);border-radius:3px;padding:10px 13px;font-size:14px;
  box-shadow:0 6px 22px rgba(0,0,0,.45);min-width:150px}
.tip{max-width:400px}
.tip h4{margin:0 0 7px;font-size:14px;font-weight:600}
.tip .meta{color:var(--faint);font-size:13px;margin-bottom:8px}
.tip .body{white-space:pre-wrap;color:var(--ink);line-height:1.5;
  max-height:340px;overflow:hidden}
.tip .none{color:var(--dim);font-style:italic}
.tipshot{margin:0 0 9px;border:1px solid var(--rule);border-radius:3px;
  overflow:hidden;background:var(--case);max-height:150px;display:flex;
  align-items:center;justify-content:center}
.tipshot img,.tipshot video{max-width:100%;max-height:150px;display:block}
.tipeng{margin:0 0 9px;padding-bottom:8px;border-bottom:1px solid var(--rule);
  white-space:normal}
.tip .r{display:flex;align-items:center;gap:8px;margin-top:3px;color:var(--dim)}
.tip .r b{width:9px;height:9px;border-radius:2px;flex:none}
.tip .r span{flex:1}
.tip .r i{font-style:normal;width:38px;text-align:right;color:var(--ink)}
.tip .r.hdr i{color:var(--faint);font-size:12px}
.tip .r.hdr{margin-bottom:2px}
.tip .r.on{background:rgba(229,227,220,.09);border-radius:2px;
  margin:0 -5px;padding:1px 5px}
.tip .r.on span{color:var(--ink)}
.tip .said{margin-top:8px;padding-top:8px;border-top:1px solid var(--rule);
  color:var(--dim);font-size:13px;line-height:1.45}
.tip .said div{margin-top:3px}
.tip .said .more{color:var(--faint)}
.tip .tot{margin-top:8px;padding-top:7px;border-top:1px solid var(--rule);
  color:var(--ink);font-weight:600}

.keys{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px;font-size:14px;
  color:var(--dim)}
.keys b{display:inline-block;width:9px;height:9px;margin-right:6px;border-radius:1px}

.seg{display:flex;gap:0;border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.seg button{padding:5px 13px;color:var(--dim);font-size:14px;
  border-right:1px solid var(--rule)}
.seg button:last-child{border-right:0}
.seg button:hover{color:var(--ink)}
.seg button.on{background:var(--raised);color:var(--ink);font-weight:600}
.seg button.wide{min-width:58px;color:var(--ink);cursor:default}
.chars.radio{display:flex;align-items:center;gap:5px;cursor:pointer}
.chars.radio input{margin:0}
.striphead{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:3px}
.striphead h2{flex:none}
.striphead .spacer{flex:1}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-bottom:22px}
.tile{background:var(--panel);padding:16px 18px}
.tile .n{font-size:34px;font-weight:600;line-height:1.1}
.tile .l{color:var(--dim);font-size:14px;margin-top:3px}
.tile.hot .n{color:var(--ok)}

table{width:100%;border-collapse:collapse;font-size:15px}
th{text-align:left;font-weight:600;color:var(--dim);font-size:14px;
  padding:8px 10px;border-bottom:1px solid var(--rule)}
td{padding:9px 10px;border-bottom:1px solid #23282400;
  border-bottom:1px solid #2C312D;vertical-align:top}

/* --- calendar --- */
/* The month fits the window. Seven columns, a header row and six week rows
   that share whatever height is left, so the calendar never scrolls the page. */
/* The grid used to be squeezed into the viewport: six rows inside
   calc(100vh - 268px). On a short window that left a cell about one chip tall,
   and the rest scrolled away inside it with no scrollbar drawn to say so. A
   day with five posts looked like a day with one. Rows now have a real minimum
   and the page scrolls instead of each cell scrolling in secret. */
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.dow{background:var(--panel);padding:7px 9px;color:var(--dim);font-size:13px;
  font-family:'Barlow Condensed',sans-serif;font-weight:600}
.day{background:var(--panel);padding:5px 6px;display:flex;
  flex-direction:column;overflow:hidden}
.day .chips{flex:1;min-height:0}
.day.out{background:#22262300;background:#232723}
.day .d{color:var(--faint);font-size:13px;margin-bottom:4px;flex:none}
.day.today .d{color:var(--ink);font-weight:600}
.day.today{box-shadow:inset 0 0 0 1px var(--ink)}
.chip{display:block;width:100%;text-align:left;font-size:13px;line-height:1.35;
  padding:3px 6px;margin-bottom:2px;border-radius:2px;border-left:3px solid;
  background:var(--raised);color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
/* Outside the chips, so it cannot scroll out of sight along with the posts it
   is there to tell you about. */
.day .more{display:block;width:100%;text-align:left;font-size:12px;flex:none;
  padding:2px 6px;color:var(--brand);text-decoration:underline}
.chip:hover{background:#39403A}
.chip.appr{opacity:1}
.chip.alloc{opacity:.5;font-style:italic}
/* What has already happened recedes. Near-white is for what is still to come,
   so a glance at the month reads as the work ahead rather than a wall of
   equally loud entries. The post is still there and still clickable. */
.chip.past{color:var(--dim)}
.chip.past:hover{color:var(--ink)}
.chip.rej{opacity:.35;text-decoration:line-through}
.calbar{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.calbar .spacer{flex:1}
.calbar h2{font-size:19px;min-width:180px}

/* --- post detail --- */
.sheet{position:fixed;inset:0;background:rgba(15,17,15,.72);display:none;
  z-index:20;padding:36px 20px;overflow:auto}
.sheet.on{display:block}
.card{max-width:660px;margin:0 auto;background:var(--panel);
  border:1px solid var(--rule);padding:24px}
.card .meta{color:var(--dim);font-size:14px;margin-bottom:16px;line-height:1.7}
.card .meta b{color:var(--ink);font-weight:500}
.card .prev{background:var(--case);border:1px solid var(--rule);border-radius:3px;
  overflow:hidden;margin-bottom:6px;max-height:240px;display:flex;
  align-items:center;justify-content:center}
.card .prev img,.card .prev video{max-width:100%;max-height:240px;display:block}
.when{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.when input[type=date],.when input[type=time]{width:auto;flex:none}
.when label{margin:0;display:flex;align-items:center;gap:6px;color:var(--ink)}
.soon{border:1px solid var(--rule);border-radius:3px;padding:9px 12px;
  background:var(--case);margin-top:9px;font-size:14px;color:var(--dim)}
.soon.on{border-color:var(--brand);color:var(--ink)}
.why{color:var(--faint);font-size:13px;margin-top:6px;font-style:italic}
label{display:block;color:var(--dim);font-size:14px;margin:14px 0 5px}
textarea,input[type=text]{width:100%;background:var(--case);color:var(--ink);
  border:1px solid var(--rule);border-radius:3px;padding:10px 12px;font:inherit}
textarea{min-height:150px;resize:vertical;line-height:1.55}
.chars{color:var(--faint);font-size:13px;margin-top:5px}
.chars.over{color:var(--no)}
.warn{border-left:3px solid var(--no);background:#2E2724;padding:10px 13px;
  font-size:15px;margin:14px 0}
.rowb{display:flex;gap:9px;margin-top:22px;flex-wrap:wrap}
.rowb .spacer{flex:1}
.state{display:inline-block;font-size:13px;padding:2px 8px;border-radius:2px;
  border:1px solid var(--rule);color:var(--dim);font-family:'Barlow Condensed',sans-serif;
  font-weight:600}
.empty{color:var(--dim);padding:44px 0;text-align:center}
.empty b{display:block;color:var(--ink);font-weight:500;margin-bottom:5px}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
  background:var(--raised);border:1px solid var(--rule);padding:11px 18px;
  border-radius:3px;z-index:40;display:none}
.toast.on{display:block}

/* --- profile picker --- */
.ent{display:flex;align-items:center;gap:9px}
.ent .sw{width:11px;height:11px;border-radius:2px;background:var(--brand);flex:none}
select{background:var(--raised);color:var(--ink);border:1px solid var(--rule);
  border-radius:3px;padding:6px 10px;font:inherit;max-width:100%}
.ent select{font-family:'Barlow Condensed',Barlow,sans-serif;font-size:19px;
  font-weight:600;letter-spacing:.02em}
.todo{color:var(--no);font-size:14px}
.act.live{display:inline-flex;align-items:center;gap:8px}
.act.live .dot{width:9px;height:9px;border-radius:50%;background:var(--faint);
  flex:none;transition:background .15s}
.act.live[aria-pressed=true]{border-color:var(--no);color:var(--ink)}
.act.live[aria-pressed=true] .dot{background:var(--no);
  box-shadow:0 0 0 3px rgba(196,99,74,.22)}

/* --- the big preview --- */
.viewer{position:fixed;inset:0;z-index:40;display:none}
.viewer.on{display:block}
.viewer .vshade{position:absolute;inset:0;background:rgba(10,12,10,.88)}
.viewer .vbody{position:relative;max-width:min(560px,92vw);margin:3vh auto;
  display:flex;flex-direction:column;gap:10px}
.viewer .vwrap{background:#000;border-radius:4px;overflow:hidden}
.viewer video{max-height:82vh;width:100%;display:block}
/* --- caption cards --- */
.cardbar{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px}
.cardtab{display:inline-flex;align-items:center;gap:6px;
  border:1px solid var(--rule);background:var(--panel);border-radius:3px;
  padding:5px 11px;font-size:14px;color:var(--dim)}
.cardtab:hover{color:var(--ink);border-color:var(--dim)}
.cardtab.on{color:var(--ink);border-color:var(--brand);background:var(--raised)}
.cardtab.add{border-style:dashed;color:var(--faint)}
.cardtab .fn{color:var(--faint);font-size:12px}
.caprow input[type=range]{width:96px;accent-color:var(--brand)}
.tile.quiet .n{color:var(--faint)}
.tile.quiet .l{color:var(--faint)}
/* A post on this profile's account that somebody else wrote. Dashed, because
   it takes up the slot but is not this profile's to edit lightly. */
/* --- folders --- */
.folders{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 10px}
.fold{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--rule);
  background:var(--panel);border-radius:3px;padding:6px 11px;font-size:14px;
  color:var(--dim);transition:border-color .12s,background .12s,color .12s}
.fold:hover{color:var(--ink);border-color:var(--dim)}
.fold.on{color:var(--ink);border-color:var(--brand);background:var(--raised)}
/* A folder the drag is over. Solid enough to be obvious with a tile in hand. */
.fold.over{border-color:var(--ok);background:var(--raised);color:var(--ink);
  box-shadow:inset 0 0 0 1px var(--ok)}
.fold .fn{color:var(--faint);font-size:13px}
.fold.on .fn{color:var(--dim)}
.fold .fx{color:var(--faint);font-size:13px;padding:0 2px;cursor:pointer}
.fold .fx:hover{color:var(--ink)}
.fold .fx.x:hover{color:var(--no)}
.fold.add{border-style:dashed;color:var(--faint)}
.fold.add:hover{color:var(--ink)}
.mi[draggable=true]{cursor:grab}
.mi[draggable=true]:active{cursor:grabbing}
hr.rule{border:0;border-top:1px solid var(--rule);margin:20px 0 18px;opacity:.7}
.uprow input[type=text]{flex:1;min-width:220px;width:auto;padding:7px 10px;
  font-size:15px}
/* The tab itself is the drop target, so it needs room to be dropped on and a
   way to say it is ready.

   Never set `display` here. Tabs are switched with the hidden attribute, and
   an author rule beats the browser's [hidden]{display:none} whatever its
   specificity, so `display:block` pinned the Media tab open: every other tab
   still drew, underneath it, below 60vh of photos. It reads as tabs that do
   nothing. Anything sized here is guarded on :not([hidden]) for the same
   reason. */
#up:not([hidden]){min-height:60vh}
#up{border:1px dashed transparent;border-radius:4px;padding:2px;
  transition:border-color .12s,background .12s}
#up.hot{border-color:var(--brand);background:var(--raised)}
.uprow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 14px}
.uprow .chars{min-width:260px}
/* --- media --- */
.drop{border:1px dashed var(--rule);background:var(--panel);border-radius:3px;
  padding:34px 24px;text-align:center;margin-bottom:22px}
.drop.hot{border-color:var(--brand);background:var(--raised)}
.drop b{display:block;font-size:19px;margin-bottom:6px}
.drop p{margin:0 auto;color:var(--dim);font-size:15px;max-width:60ch}
.drop .lan{margin-top:14px;color:var(--faint);font-size:14px}
.drop .lan code{color:var(--dim);font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:14px}
/* Small tiles are for finding a file among many, so the description box and
   the metadata line come off and only the picture and its name remain. */
.grid.small{grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:9px}
.grid.small .th{height:78px}
.grid.small .b{padding:5px 6px;font-size:12px}
.grid.small .m,.grid.small .desc,.grid.small .linky,.grid.small .qn{display:none}
.mlist td{vertical-align:middle}
.mlist tr{cursor:pointer}
.mlist tr:hover{background:var(--panel)}
.mlist tr.sel{outline:2px solid var(--brand);outline-offset:-2px}
.mlist .lthumb{width:64px}
.mlist .lthumb img,.mlist .lthumb video{width:56px;height:40px;object-fit:cover;
  display:block;border-radius:2px;background:#000}
.mlist .n{font-size:14px}
.mlist th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
.mlist th.sortable:hover{color:var(--ink)}
.mlist th.sortable.on{color:var(--ink)}
.mlist th .arr{font-size:10px;margin-left:5px;color:var(--brand)}
.mi{background:var(--panel);border:2px solid var(--rule);border-radius:3px;
  overflow:hidden;display:flex;flex-direction:column}
.mi .th{height:138px;background:var(--case);display:block}
.mi .th img,.mi .th video{width:100%;height:100%;object-fit:cover;display:block}
.mi .b{padding:10px 11px;font-size:14px;flex:1;display:flex;flex-direction:column}
.mi .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mi .m{color:var(--faint);font-size:13px;margin-top:3px}
.mi input[type=text]{margin-top:8px;padding:5px 8px;font-size:14px}
.mi{cursor:pointer;transition:border-color .12s}
.mi:hover{border-color:var(--dim)}
.mi.sel{border-color:var(--brand)}
.mi.live{border-color:var(--ok)}
.mi.live.sel{border-color:var(--brand);box-shadow:inset 0 0 0 2px var(--ok)}
.mi .live{color:var(--ok);font-size:12px;margin-top:3px}
.mi .th{position:relative}
.mi .note{display:flex;align-items:center;justify-content:center;height:100%;
  font-size:42px;color:var(--brand)}
.mi .tick{position:absolute;top:7px;right:7px;width:22px;height:22px;
  border-radius:50%;border:1px solid var(--rule);background:rgba(20,22,20,.75);
  color:var(--case);display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700}
.mi.sel .tick{background:var(--brand);border-color:var(--brand)}
/* Blogs. Same card as media, because a blog behaves like an upload, with two
   border states the media grid does not have: bright green once a post
   carrying it is scheduled, blue once one is published. Both beat the hover
   and selection borders, because how far a blog has got is the thing you scan
   the grid for. */
.mi.sched{border-color:var(--sched)}
.mi.pub{border-color:var(--pub)}
.mi .badge{font-size:12px;margin-top:4px;font-weight:600}
.mi.sched .badge{color:var(--sched)}
.mi.pub .badge{color:var(--pub)}
.mi.blocked{opacity:.62}
.mi.blocked .th{filter:grayscale(1)}
.mi .whynot{color:#D9A441;font-size:12px;line-height:1.35;margin-top:5px}
.mi .dt{color:var(--faint);font-size:13px;margin-top:3px}
.mi .sum{color:var(--dim);font-size:13px;line-height:1.4;margin-top:6px;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  overflow:hidden}
.slots2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.slot2{background:var(--case);border:2px solid var(--rule);border-radius:3px;
  padding:9px;display:flex;flex-direction:column;gap:7px}
.slot2.has{border-color:var(--ok)}
.slot2 .slab{font-size:13px;color:var(--ink);font-weight:600}
.slot2 .slab span{display:block;color:var(--faint);font-weight:400;font-size:12px}
.slot2 img{width:100%;height:132px;object-fit:contain;background:#000;display:block}
.slot2 .slotempty{height:132px;display:flex;align-items:center;
  justify-content:center;color:var(--faint);font-size:13px;
  border:1px dashed var(--rule)}
.slot2 .slotm{color:var(--faint);font-size:12px}
.slot2 .slotacts{margin-top:auto;display:flex;gap:7px}
.pickgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));
  gap:8px;margin:0 0 12px;max-height:230px;overflow-y:auto;padding:9px;
  background:var(--case);border:1px solid var(--rule);border-radius:3px}
.pickgrid img{width:100%;height:68px;object-fit:cover;border:2px solid transparent;
  border-radius:2px;cursor:pointer;display:block}
.pickgrid img:hover{border-color:var(--brand)}
select.slim{max-width:150px;padding:5px 7px;font-size:13px}
.bloghead{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.mi .acts{display:flex;align-items:center;gap:10px;margin-top:auto;padding-top:9px}
.mi .acts .act{padding:4px 10px;font-size:13px}
.linky2{background:none;border:0;padding:0;font-size:13px;color:var(--faint);
  cursor:pointer;font-family:inherit}
.linky2:hover{color:var(--no)}
.mi .th{cursor:pointer}
.mi .linky{display:block;margin-top:7px;font-size:12px}
.mi .linky:hover{color:var(--ok)}
.dims{display:flex;flex-wrap:wrap;gap:6px;margin:9px 0 0}
.dim{background:var(--case);border:1px solid var(--rule);border-radius:3px;
  padding:3px 8px;font-size:12px;color:var(--ink);white-space:nowrap}
.dim b{color:var(--faint);font-weight:400;margin-right:6px}
.dim.hot{border-color:var(--ok);color:var(--ok)}
.dim.hot b{color:var(--ok)}
.dim.cold{border-color:var(--no);color:var(--no)}
.dim.cold b{color:var(--no)}
.dim.warn{border-color:#D9A441;color:#D9A441}
.dim.warn b{color:#D9A441}
.symtab{margin:4px 0 6px}
.symtab td,.symtab th{padding:5px 9px;font-size:13px}
.symtab a{color:var(--brand);text-decoration:none;border-bottom:1px solid var(--rule)}
.symtab a:hover{border-bottom-color:var(--brand)}
.scope{position:relative;cursor:pointer;color:var(--dim)}
.scope:hover{color:var(--ink)}
.spop{display:none;position:absolute;top:20px;left:0;z-index:60;
  width:min(420px,66vw);background:var(--raised);border:1px solid var(--rule);
  border-radius:3px;padding:11px 13px;font-size:13px;line-height:1.5;
  color:var(--ink);white-space:pre-wrap;box-shadow:0 10px 26px rgba(0,0,0,.45)}
.scope:hover .spop{display:block}
.dim.quiet{color:var(--faint);border-color:var(--rule)}
/* The Auto switch borrows the Live button's look, in its own colour so the
   two are never mistaken for each other at a glance. */
#autobtn[aria-pressed=true]{border-color:#D9A441;color:#D9A441}
#autobtn[aria-pressed=true] .dot{background:#D9A441}
#autobtn:disabled{opacity:.45;cursor:not-allowed}
.mktabs{margin:0 0 16px}
.mktabs button{padding:6px 15px}
.idea.kept{border-color:var(--ok)}
.idea.kept .inum{color:var(--ok);background:none}
.qn{color:#D9A441;font-size:12px;line-height:1.35;margin-top:5px}
.linkrow{display:flex;gap:7px;max-width:460px;margin:14px auto 0}
.linkrow input{flex:1}
.mi .linky{color:var(--brand);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.mi .desc{margin-top:8px;min-height:0;padding:6px 8px;font-size:13px;
  line-height:1.4;resize:vertical;cursor:text}
.uphead{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.uptoggle{font-family:'Barlow Condensed',Barlow,sans-serif;font-size:19px;
  font-weight:600;letter-spacing:.02em;color:var(--ink);padding:0;
  display:flex;align-items:center;gap:8px}
.uptoggle .caret{color:var(--dim);font-size:14px}
.uptoggle:hover .caret{color:var(--ink)}
.uphead .spacer{flex:1}
.selbar{display:flex;align-items:center;gap:12px;background:var(--panel);
  border:1px solid var(--brand);border-radius:3px;padding:11px 14px;
  margin:0 0 16px;position:sticky;top:0;z-index:5}
.selbar .spacer{flex:1}
.shots{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.shot{background:var(--case);border:1px solid var(--rule);border-radius:3px;
  overflow:hidden;max-width:100%}
.shot{position:relative}
.shot img,.shot video{max-height:190px;max-width:100%;display:block}
.shotbar{display:flex;align-items:center;gap:4px;justify-content:center;
  padding:5px 4px;background:var(--raised);border-top:1px solid var(--rule)}
.shotbar button{color:var(--dim);padding:1px 7px;font-size:14px;line-height:1.2}
.shotbar button:hover:not(:disabled){color:var(--ink)}
.shotbar button:disabled{opacity:.3;cursor:default}
.shotbar span{font-size:13px;color:var(--faint);min-width:14px;text-align:center}
.shotbar .x{color:var(--no);margin-left:3px}

/* --- video player: one bar wherever a video is actually watched, so a
   rendered preview and an uploaded file behave the same. Native controls
   are off; these are ours. --- */
.vwrap{position:relative;max-width:100%}
.vwrap video{display:block;cursor:pointer}
.vbar{display:flex;align-items:center;gap:8px;padding:6px 9px;
  background:var(--raised);border-top:1px solid var(--rule)}
.vbar button{color:var(--dim);padding:1px 5px;font-size:14px;line-height:1.2}
.vbar button:hover{color:var(--ink)}
.vbar .vtime{font-size:12px;color:var(--faint);white-space:nowrap}
/* The line along the bottom. Grab it anywhere. */
.vseek{-webkit-appearance:none;appearance:none;width:auto;flex:1;min-width:60px;
  height:14px;margin:0;padding:0;background:none;cursor:pointer}
.vseek::-webkit-slider-runnable-track{height:3px;border-radius:2px;
  background:var(--rule)}
.vseek::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:11px;height:11px;margin-top:-4px;border:0;border-radius:50%;
  background:var(--brand)}
.vseek::-moz-range-track{height:3px;border-radius:2px;background:var(--rule)}
.vseek::-moz-range-progress{height:3px;border-radius:2px;background:var(--brand)}
.vseek::-moz-range-thumb{width:11px;height:11px;border:0;border-radius:50%;
  background:var(--brand)}
/* The post detail caps its preview at 240px total, and the bar lives inside
   that cap, so the picture gives up the bar's height rather than clipping it. */
.card .prev .vwrap video{max-height:206px}
.prow{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:14px}
.pn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr{display:flex;align-items:center;gap:5px;margin:0;color:var(--dim);font-size:13px}
.pr input{accent-color:var(--brand)}
.psearch{display:flex;gap:7px;margin:0 0 10px}
.psearch input[type=text]{flex:1}
.psearch select{width:auto;flex:none}
.ctabs{margin:0 0 9px;gap:2px}
.ctabs button{padding:6px 13px 5px;font-size:15px;position:relative}
.ctabs .dot{display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--brand);margin-left:6px;vertical-align:2px}
.lnk{color:var(--brand);padding:0;font-size:13px;text-decoration:underline}
.wherebox{margin-top:10px}
.wherebox img{max-width:100%;max-height:420px;display:block;border-radius:3px;
  border:1px solid var(--rule)}
.credit{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:2px}
.credit .cr{display:flex;align-items:center;gap:6px;margin:0;color:var(--dim);
  font-size:14px;white-space:nowrap;cursor:pointer}
.credit .cr.on{color:var(--ink)}
.credit .cr input{accent-color:var(--brand)}
.credit .song{flex:1;min-width:160px;width:auto;padding:5px 8px;font-size:14px}
/* --- caption controls --- */
.capbox{min-height:0;height:74px;line-height:1.4}
.caprow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:8px}
.caprow select{width:auto;flex:1 1 190px;min-width:150px;max-width:280px}
.caprow .cl{display:flex;align-items:center;gap:6px;margin:0;
  color:var(--dim);font-size:13px;white-space:nowrap}
.caprow .cl input[type=number]{width:66px;background:var(--case);
  color:var(--ink);border:1px solid var(--rule);border-radius:3px;
  padding:5px 7px;font:inherit;font-size:14px}
.caprow .cl input[type=color]{width:34px;height:28px;padding:0;cursor:pointer;
  background:var(--case);border:1px solid var(--rule);border-radius:3px}
.cue{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:2px}
.cue audio{height:34px;flex:1;min-width:200px}
.mi .rm{color:var(--no);font-size:13px;margin-top:9px;text-align:left;padding:0}
.pick{margin-top:9px;border-top:1px solid var(--rule);padding-top:9px;
  max-height:230px;overflow-y:auto}
.pick .none{color:var(--faint);font-size:13px;margin-bottom:7px}
.slot{display:flex;align-items:center;gap:7px;font-size:13px;padding:3px 0;
  cursor:pointer;margin:0}
.slot .sub{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.slot input{accent-color:var(--brand)}
.slot.dim{opacity:.45}
.zq{background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--rule);
  border-radius:3px;padding:14px 16px;margin-bottom:12px}
.zq.ok{border-left-color:var(--ok)}
.eng{white-space:nowrap;font-size:13px;color:var(--dim)}
.eng span{margin-right:9px}
.eng .rate{color:var(--ok);font-weight:600}
.grow{font-size:13px;margin-top:4px;color:var(--faint)}
.grow.up{color:var(--ok)}
.grow.down{color:var(--no)}
/* The per-account follower list is read at a glance, so it runs two steps
   larger than the other legends. */
.folllist{font-size:16px;gap:18px}
.folllist b{width:11px;height:11px}
.folllist span span{font-size:15px}
.folllist a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule)}
.folllist a:hover{color:var(--ok);border-bottom-color:var(--ok)}
.folllist .d{font-style:normal;font-size:14px;margin-left:5px;color:var(--faint)}
.folllist .d.up{color:var(--ok)}
.folllist .d.down{color:var(--no)}
/* Seven across, always on one line. They shrink rather than wrap. */
.tiles.row7{grid-template-columns:repeat(7,minmax(0,1fr))}
.tiles.row7 .tile{padding:14px 10px}
.tiles.row7 .n{font-size:23px;letter-spacing:-.01em}
.tiles.row7 .l{font-size:12px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
@media(max-width:760px){.tiles.row7{grid-template-columns:repeat(4,minmax(0,1fr))}}
.pstick{position:sticky;top:0;z-index:6;background:var(--case);
  padding:10px 0 8px;border-bottom:1px solid var(--rule)}
.pstick .filters{margin-bottom:0}
.dates{display:flex;align-items:center;gap:7px}
.dates input[type=date]{width:auto;padding:5px 8px;font-size:14px}
table.sticky thead th{position:sticky;top:var(--htop,44px);z-index:5;
  background:var(--case);box-shadow:inset 0 -1px 0 var(--rule)}
.idea{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:15px 17px;margin-bottom:12px}
.inum{width:24px;height:24px;border-radius:50%;background:var(--raised);
  color:var(--dim);display:inline-flex;align-items:center;justify-content:center;
  font-size:13px;flex:none}
.quote{border-left:2px solid var(--brand);padding-left:11px;color:var(--ink);
  font-size:14px;line-height:1.5;margin:9px 0}
.zq.no{border-left-color:var(--no)}
.zbody{display:flex;gap:13px;margin:10px 0}
.zthumb{width:104px;height:104px;object-fit:cover;border-radius:3px;flex:none;
  background:var(--case)}
.ztext{white-space:pre-wrap;font-size:14px;line-height:1.5;flex:1;min-width:0}
.zplats{display:flex;flex-wrap:wrap;gap:8px}
.zp{display:flex;align-items:center;gap:7px;font-size:13px;padding:4px 10px;
  border:1px solid var(--rule);border-radius:999px}
.zp b{width:9px;height:9px;border-radius:2px;display:inline-block}
.zp a{color:var(--ok)}
.filters{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:16px}
.fchip{display:flex;align-items:center;gap:6px;margin:0;padding:5px 11px;
  border:1px solid var(--rule);border-radius:999px;font-size:14px;
  color:var(--dim);cursor:pointer}
.fchip.on{color:var(--ink);border-color:var(--dim)}
.fchip input{accent-color:var(--brand);margin:0}
.fchip .sub{color:var(--faint);font-size:13px}
.fchip .fn{color:var(--faint);font-size:13px}
.cmds{white-space:nowrap}
.bin,.pen{color:var(--faint);font-size:16px;padding:2px 5px;line-height:1}
.bin:hover{color:var(--no)}
.pen:hover{color:var(--ink)}
.tabs{display:flex;gap:2px;margin:4px 0 14px;flex-wrap:wrap}
.tabs button{padding:6px 12px;border:1px solid var(--rule);border-radius:3px;
  color:var(--dim);font-size:14px}
.tabs button.on{background:var(--raised);color:var(--ink);border-color:var(--dim)}
.opt{display:block;padding:9px 12px;border:1px solid var(--rule);border-radius:3px;
  margin-top:7px;color:var(--dim);cursor:pointer}
.opt.on{border-color:var(--brand);color:var(--ink)}
.opt input{accent-color:var(--brand);margin-right:8px}
.mi .rm:hover{text-decoration:underline}
.dcard{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:18px 20px;margin-bottom:14px}
.dcard.dirty{border-color:var(--brand)}
.dhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.dhead b{font-weight:600}
.dsub{color:var(--dim);font-size:14px;margin-bottom:12px}
.sw2{width:10px;height:10px;border-radius:2px;flex:none}
.dcard textarea{min-height:132px}
.dcard label{margin-top:12px}
.chipset{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.chip2{border:1px solid var(--rule);background:var(--case);color:var(--dim);
  border-radius:999px;padding:4px 12px;font-size:14px}
.chip2:hover{color:var(--ink);border-color:var(--dim)}
.chip2.on{background:var(--brand);border-color:var(--brand);color:#17201A;font-weight:600}
.hprev{background:var(--case);border:1px solid var(--rule);border-radius:3px;
  padding:13px 15px;margin:14px 0 0;font:400 14px/1.55 ui-monospace,Menlo,monospace;
  white-space:pre-wrap;overflow-x:auto;color:var(--dim)}
/* The voice line: one sentence, with the rest a hover away. The popup is
   positioned against the mark rather than the header, so a long voice does not
   push the live button off the end. */
.vtip{display:inline-flex;align-items:center;justify-content:center;
  width:16px;height:16px;border:1px solid var(--rule);border-radius:50%;
  font-size:11px;color:var(--dim);cursor:pointer;position:relative;
  margin-left:5px;vertical-align:middle}
.vtip:hover,.vtip:focus{color:var(--ink);border-color:var(--dim);outline:none}
.vpop{display:none;position:absolute;top:22px;left:-8px;z-index:60;
  width:min(460px,72vw);background:var(--raised);border:1px solid var(--rule);
  border-radius:3px;padding:12px 14px;font-size:13px;line-height:1.5;
  color:var(--ink);white-space:pre-wrap;text-align:left;cursor:pointer;
  box-shadow:0 10px 26px rgba(0,0,0,.45)}
.artlink{font-size:12px;color:var(--brand);text-decoration:none;
  border-bottom:1px solid var(--rule);max-width:330px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.artlink:hover{border-bottom-color:var(--brand)}
.vhint{display:block;margin-top:10px;padding-top:9px;font-size:12px;
  color:var(--faint);border-top:1px solid var(--rule);white-space:normal}
.vtip:hover .vpop,.vtip:focus .vpop{display:block}
.bar2{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.bar2 .spacer{flex:1}
@media(max-width:720px){.cal{font-size:13px;height:auto}
  .day{min-height:88px}.calbar h2{min-width:0}}
</style></head><body>
<div class="wrap">
<header>
  <h1>Guavynator</h1>
  <span class="ent"><span class="sw"></span><select id="prof"></select></span>
  <span class="sub" id="voice"></span>
  <span class="spacer"></span>
  <button class="act live" id="livebtn" aria-pressed="false">
    <span class="dot"></span><span id="livetxt">Not live</span></button>
  <button class="act live" id="autobtn" aria-pressed="false">
    <span class="dot"></span><span id="autotxt">Auto off</span></button>
  <button class="act" id="pushbtn">Unsent</button>
</header>

<nav role="tablist">
  <button role="tab" aria-selected="false" data-t="sug">Suggested</button>
  <button role="tab" aria-selected="true"  data-t="mkt">Wire</button>
  <button role="tab" aria-selected="false" data-t="ads">Ads<span class="count" id="ac">0</span></button>
  <button role="tab" aria-selected="false" data-t="up">Media<span class="count" id="mc">0</span></button>
  <button role="tab" aria-selected="false" data-t="blog">Blogs<span class="count" id="bc">0</span></button>
  <button role="tab" aria-selected="false" data-t="list">To Be Posted<span class="count" id="wc">0</span></button>
  <button role="tab" aria-selected="false" data-t="cal">Calendar</button>
  <button role="tab" aria-selected="false" data-t="zq">Queued</button>
  <button role="tab" aria-selected="false" data-t="done">Published</button>
  <button role="tab" aria-selected="false" data-t="foll">Numbers</button>
</nav>

<section id="sug" hidden></section>
<section id="mkt"></section>
<section id="ads" hidden></section>
<section id="up" hidden></section>
<section id="blog" hidden></section>
<section id="list" hidden></section>
<section id="cal" hidden></section>
<section id="zq" hidden></section>
<section id="done" hidden></section>
<section id="foll" hidden></section>
</div>

<div class="viewer" id="viewer"></div>
<div class="sheet" id="sheet"><div class="card" id="card"></div></div>
<div class="toast" id="toast"></div>
<div class="tip" id="tip" hidden></div>

<script>
const $=s=>document.querySelector(s), api=(u,o)=>fetch(u,o).then(r=>r.json());
let CAMPS=[], ACCTS=[], GROUP=[], TAB=0;
let SUM={}, POSTS=[], MEDIA=[], PROFS={}, PROF='', CHAN={}, PLAT={},
    LIVE=false, MONTH=new Date(), CUR=null;
const CAL_CHIPS=3;    // chips a month cell can show without clipping
/* Published posts stay on the calendar by default, because what went out and
   when is most of what a calendar is for. They can be hidden when you are
   working on what is still to come. Hidden, not deleted: the row carries the
   live URL and the numbers it earned. */
let CALPUB=localStorage.getItem('desk.calpub')!=='0';
let CALVIEW=localStorage.getItem('desk.calview')||'month';
let PASTPOSTS=false;
/* The local date, not the UTC one. toISOString() rolls over at midnight in
   London, so from 6pm Mountain the desk called it tomorrow: the calendar boxed
   the wrong day and the Posts count dropped anything scheduled for today. */
const iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${
  String(d.getDate()).padStart(2,'0')}`;
const TODAY=()=>iso(new Date());
/* Slot colour is the platform, not the campaign. Set in channels.yaml. */
const cink=c=>(CHAN[c]||{}).ink||'#8A8F86';
const NICE={allocated:'Not written',drafted:'Waiting on you',approved:'Approved',
  rejected:'Rejected',scheduled:'At Zernio',published:'Published',failed:'Failed'};
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const title=s=>s.replace(/-/g,' ').replace(/\b\w/g,c=>c.toUpperCase());

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');
  clearTimeout(t._x);t._x=setTimeout(()=>t.classList.remove('on'),2600);}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.setAttribute('aria-selected',x===b));
  ['sug','mkt','ads','up','blog','list','cal','zq','done','foll']
    .forEach(id=>$('#'+id).hidden = id!==b.dataset.t);
  if(b.dataset.t==='zq') drawQueue();
  if(b.dataset.t==='done') drawPublished();
  if(b.dataset.t==='sug') drawSuggest();
  if(b.dataset.t==='foll') drawFollowers();
  if(b.dataset.t==='blog') drawBlogs();
  if(b.dataset.t==='mkt') drawMarkets();
  if(b.dataset.t==='ads') drawAds();
  if(b.dataset.t==='up') drawUpload('library');
});

let AUTOPOST=false;
async function paintLive(){
  const r=await api('/api/live'); LIVE=r.live;
  const b=$('#livebtn');
  b.setAttribute('aria-pressed',LIVE?'true':'false');
  $('#livetxt').textContent=LIVE?'Live':'Not live';
  b.title=LIVE?'Posts really go to Zernio. Click to disarm.'
    :(r.key?'Nothing can be posted. Click to go live.'
           :'No API key in .env, so nothing can go live.');
  await paintAuto();
}

/* The master switch for unattended posting. Separate from Live on purpose:
   Live means a push you pressed really goes; this means the desk may press it
   for you. Live can be on by itself, which is the normal working state. */
async function paintAuto(){
  const r=await api('/api/autopost',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});
  AUTOPOST=!!(r&&r.on);
  const b=$('#autobtn');
  b.setAttribute('aria-pressed',AUTOPOST?'true':'false');
  $('#autotxt').textContent=AUTOPOST?'Auto on':'Auto off';
  b.title=AUTOPOST
    ? 'The desk posts by itself when a market is on Auto and a slot comes due. '
      +'Click to stop.'
    :(LIVE?'The desk will not post by itself. Click to arm it.'
          :'Go Live first: nothing would reach Zernio.');
  b.disabled=!LIVE&&!AUTOPOST;
  if(typeof MKT!=='undefined'&&MKT) drawMarkets();
}

$('#autobtn').onclick=async()=>{
  if(!AUTOPOST && !confirm(
      'Arm automatic posting?\n\nWhen a market is set to Auto and its channel '
      +'comes due, the desk will pick a story, draw the artwork, write the post '
      +'and send it to Zernio with nobody watching.\n\nIt posts at most one '
      +'thing every couple of minutes, and only within the pacing in '
      +'channels.yaml.')) return;
  const r=await api('/api/autopost',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on:!AUTOPOST})});
  if(r.error){toast(r.error);return;}
  await paintAuto();
  toast(r.on?'Automatic posting armed':'Automatic posting off');
};
$('#livebtn').onclick=async()=>{
  if(!LIVE && !confirm('Go live?\n\nApproved posts will actually be sent to '
    +'Zernio from now on. While not live, every push is a dry run.')) return;
  const r=await api('/api/live',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({live:!LIVE})});
  if(r.error){toast(r.error);return;}
  await paintLive(); toast(r.live?'Live':'Not live');
};

async function boot(){
  PROFS = await api('/api/profiles');
  await paintLive();
  const ch = await api('/api/channels'); CHAN = ch.channels||{}; PLAT = ch.platforms||{};
  const keys=Object.keys(PROFS), sel=$('#prof');
  PROF = localStorage.getItem('desk.profile');
  if(!PROFS[PROF]) PROF = keys[0]||'';
  /* After PROF is known, never before. Asked with an empty profile the desk
     answers with every account it has, so a fresh load offered the other
     profile's accounts until you touched the picker. */
  ACCTS = await api('/api/accounts?profile='+encodeURIComponent(PROF));
  sel.innerHTML = keys.map(k=>
    `<option value="${k}"${k===PROF?' selected':''}>${esc(PROFS[k].label||k)}</option>`).join('');
  sel.onchange=async()=>{
    PROF=sel.value; localStorage.setItem('desk.profile',PROF);
    /* Everything Zernio-side is per profile too, so drop the caches. */
    ACCTS = await api('/api/accounts?profile='+encodeURIComponent(PROF));
    PUB=null; FOLL=null; QUEUE=null; IDEAS=null; PFILTER=null; FFROM=null;
    FACCTS=null;
    SEL=[]; TRACK=null; tint();
    await load();
    /* load() only redraws the always-on views. Whatever tab you are actually
       looking at has to be told as well, or it keeps the last profile's data. */
    const open=document.querySelector('nav button[aria-selected=true]');
    const t=open&&open.dataset.t;
    if(t==='done') drawPublished(true);
    else if(t==='foll') drawFollowers(true);
    else if(t==='zq') drawQueue(true);
    else if(t==='sug') IDEAS=null, drawSuggest();
    else if(t==='blog') drawBlogs(true);
    else if(t==='mkt') drawMarkets(true);
  };
  tint(); await load();
  /* The Wire is the tab you arrive on, so it has to be drawn rather than wait
     for a click that never comes. */
  drawMarkets();
}

/* Brand colour and voice come straight out of profiles.yaml. */
function tint(){
  const e=PROFS[PROF]||{}, v=e.voice||{}, r=(v.register||'').trim();
  document.documentElement.style.setProperty('--brand',(e.brand||{}).ink||'#E0A33E');
  const todo = !r || r==='TODO';

  /* The register grew from "warm, chatty, specific" into a paragraph, which is
     too much to carry in the header. The opening sentence is the summary; the
     rest is on hover, and copyable, because it is the thing you paste into
     whatever else is writing in this voice. */
  VOICEFULL = [r, (v.person||'').trim()&&`Person: ${(v.person||'').trim()}`]
    .filter(Boolean).join('\n\n');
  const flat = r.replace(/\s+/g,' ').trim();
  const stop = flat.search(/[.!?](\s|$)/);
  const first = stop>0 ? flat.slice(0,stop+1) : flat;

  $('#voice').innerHTML = todo
    ? `<span class="todo">No voice defined. Fill this profile in in profiles.yaml.</span>`
    : `${esc(first)}
       <span class="vtip" tabindex="0" role="button" title="Click to copy"
         aria-label="The whole voice. Click to copy."
         onclick="copyVoice()" onkeydown="if(event.key==='Enter')copyVoice()"
         >?<span class="vpop">${esc(VOICEFULL)}
         <span class="vhint">click to copy</span></span></span>`;
}

let VOICEFULL='';
async function copyVoice(){
  if(!VOICEFULL){toast('Nothing to copy');return;}
  try{
    await navigator.clipboard.writeText(VOICEFULL);
    toast('Voice copied');
  }catch(_){
    /* Clipboard access is refused outside a secure context, and this desk is
       plain http on the machine it runs on. Fall back to the old trick. */
    const ta=document.createElement('textarea');
    ta.value=VOICEFULL; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); toast('Voice copied'); }
    catch(e){ toast('Could not copy'); }
    ta.remove();
  }
}

async function load(){
  const q='?profile='+encodeURIComponent(PROF);
  SUM = await api('/api/summary'+q);
  POSTS = await api('/api/posts'+q);
  MEDIA = await api('/api/media'+q);
  await loadFolders();
  /* What is still to come. Anything published, rejected, or already in the
     past is not waiting on anyone. */
  $('#wc').textContent = POSTS.filter(p=>p.copy && p.date>=TODAY()
    && WAITING(p)).length;
  const byBucket=b=>MEDIA.filter(m=>(m.bucket||'library')===b).length;
  $('#mc').textContent = byBucket('library');
  $('#ac').textContent = ADS?ADS.length:byBucket('ads');
  drawUpload(); drawList(); drawCal();
}

/* ---- media ---- */
const KB=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':
  n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB';

/* An ordered list, not a set: the order you pick them in is the order they
   appear in a carousel, and you can shuffle it in the compose box. */
let SEL=[];
const selHas=id=>SEL.indexOf(id)>=0;
let TRACK=null, KEEPORIG=false, SHAPE='portrait', TRACKSTART=0, PREVIEW=null;
/* How long the rendered post runs. null lets the desk pick: 7s for one
   photo, 3s each for a run. Photos cycle to fill anything longer. */
let LEN=null;
/* One post, three pieces of writing. The main copy goes everywhere that can
   take it. X needs its own, because 280 characters is a different post rather
   than a trimmed one. LinkedIn starts as a copy of the main copy and is then
   edited, usually with a paragraph in front, so the news reads as a note to a
   professional audience there without dragging that framing onto Instagram.
   Either one left empty falls back to the main copy. */
let XDRAFT='', LIDRAFT='', CTAB='copy';
const COPYTABS=[{k:'copy',label:'Copy'},{k:'x',label:'X'},
                {k:'li',label:'LinkedIn'}];
/* Words on the picture. Blank means none, which is the default. */
let CAP='', CAPFONT='', CAPSIZE=null, CAPCOL='#FFFFFF',
    CAPSHADOW=4, CAPSHCOL='#000000', CAPFADE=0.2,
    CAPPOS='lower', CAPALIGN='center', CAPFRAME=null;
/* Naming the track in the corner. Off unless asked for. The name is kept on
   the track itself, so correcting it once is enough. */
let CREDIT='off', SONG='';
/* The captions, in order. Each card is its own moment on the picture: its own
   words, its own place. Styling stays one set for the whole post. */
let CARDS=[], CARDI=0, TRANS=null, SNAP=true, FITMODE=null;
let GRADE={brightness:0,contrast:1,saturation:1,warmth:0};
let DEFAULTS={transition:'fade',fit_mode:'blur'};
let VIEWER=null;
/* Filled in by index() from slideshow.py, so the length the box predicts is
   the length the renderer actually picks. */
const TIMING=/*TIMING*/null;
let FONTS=null;   /* filled once from /api/fonts */

/* Build it now so you can watch it before it goes anywhere. The file this
   makes is the one that publishes: nothing is rendered twice. */
async function makePreview(){
  rememberCopy();
  const msg=$('#pvmsg'); if(msg) msg.textContent='Building…';
  const r=await api('/api/preview',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:SEL, audio:TRACK, audio_start:TRACKSTART,
      shape:SHAPE, keep_original:KEEPORIG, length:LEN, ...renderOpts()})});
  if(r.error){ if(msg) msg.textContent=r.error; toast('Could not build it'); return; }
  PREVIEW=r; paintCompose(); await load();
}

/* Which platforms the accounts you have picked actually land on, so a tab for
   somewhere you are not posting can be marked as such. */
function wantPlats(){
  return new Set([...WANT].map(k=>(CHAN[k.split('|')[0]]||{}).platform));
}
/* True when a LinkedIn channel you have picked moves body links into the first
   comment, which all three of them do. */
function linkFirst(){
  return [...WANT].map(k=>CHAN[k.split('|')[0]]||{})
    .some(c=>c.platform==='linkedin');
}
function wants(tab){
  const p=wantPlats();
  return tab==='x' ? p.has('x')
       : tab==='li' ? p.has('linkedin')
       : [...p].some(x=>x!=='x'&&x!=='linkedin');
}
function setCTab(k){
  rememberCopy(); CTAB=k;
  /* Opening LinkedIn for the first time starts you from the copy rather than
     a blank box, because a LinkedIn post here is the same news with a
     paragraph in front of it, not a different one. */
  if(k==='li' && !LIDRAFT.trim() && (CDRAFT||'').trim()) LIDRAFT=forLi(CDRAFT);
  paintCompose();
}
function pullCopy(which){
  rememberCopy();
  if(which==='li') LIDRAFT=forLi(CDRAFT); else XDRAFT=forX(CDRAFT);
  paintCompose();
}
/* X and LinkedIn post without links (channels.yaml drop_links): X charges
   $0.20 a post that carries one, and LinkedIn holds back posts that send
   people off the site. The desk strips them when it sends; doing it here as
   well means the X and LinkedIn tabs show what will actually go out. */
const noLink=c=>!!(CHAN[c]||{}).drop_links;
function stripLinks(t){
  return (t||'').replace(/https?:\/\/\S+/g,'').replace(/[ \t]+$/gm,'')
    .replace(/\n{3,}/g,'\n\n').trim();
}
const forX=t=>noLink('x')?stripLinks(t):(t||'');
const forLi=t=>noLink('linkedin')?stripLinks(t):(t||'');
function tabValue(){ return {copy:CDRAFT||'', x:XDRAFT, li:LIDRAFT}[CTAB]; }
function tabPlaceholder(){
  return {copy:'Write the post, or describe the picture on the tile and it '
            +'lands here.',
          x:'The short version. Leave it empty and X gets the main copy.',
          li:'The LinkedIn version. Leave it empty and LinkedIn gets the main '
            +'copy.'}[CTAB];
}
/* The ceiling that actually applies to whatever is in the box. */
function tabLimit(){
  const lim=p=>(PLAT[p]||{}).max_chars;
  if(CTAB==='x') return {n:lim('x'), who:'X'};
  if(CTAB==='li') return {n:lim('linkedin'), who:'LinkedIn'};
  /* The main copy goes to several places at once, so it is held to the
     tightest of the ones you are actually posting to. */
  const named=[...wantPlats()].filter(p=>p&&p!=='x'&&p!=='linkedin'&&lim(p));
  if(!named.length) return {n:null};
  const who=named.reduce((a,b)=>lim(a)<=lim(b)?a:b);
  return {n:lim(who), who:(PLAT[who]||{}).label||who};
}
/* X charges 23 characters for any link however long it is, so the raw length
   of a post carrying a hundred character URL is not what X will count. */
function xLen(t){
  return (t||'').length + ((t||'').match(/https?:\/\/\S+/g)||[])
    .reduce((n,u)=>n+23-u.length, 0);
}
function tabCount(v){
  const {n:lim, who}=tabLimit();
  const n = CTAB==='x' ? xLen(v) : v.length;
  if(CTAB==='x'){
    const l=lim||280;
    const raw = v.length!==n ? ` (${v.length} typed, the link counts as 23)` : '';
    if(!v.length){
      const m=xLen(CDRAFT||'');
      return `Empty, so X gets the main copy: ${m} characters`
        +(m>l?`, which is over the ${l} limit and would be refused`:'');
    }
    return `${n} characters${raw} · X allows ${l}${n>l?'. Over.':''}`;
  }
  if(!v.length&&CTAB==='li'){
    const m=(CDRAFT||'').length, l=lim||3000;
    return `Empty, so LinkedIn gets the main copy: ${m} characters`
      +(m>l?`, over the ${l} limit`:'');
  }
  return `${v.length} characters`
    +(lim?` · ${who} allows ${lim}${v.length>lim?'. Over.':''}`:'');
}

/* --- caption cards --------------------------------------------------- */
const card=()=>CARDS[CARDI]||null;
function cardsOpts(){
  const live=CARDS.filter(c=>(c.text||'').trim());
  if(!live.length) return {cards:[]};
  return {cards:live.map(c=>({text:c.text.trim(),position:c.position,
                             align:c.align}))};
}
function addCard(text){
  /* A new card starts where the last one did not, so a sequence spreads down
     the frame instead of stacking in one place. */
  const spots=['lower','middle','upper'];
  const pos=spots[CARDS.length%spots.length];
  CARDS.push({text:text||'',position:pos,align:'center'});
  CARDI=CARDS.length-1; dropPreview(); CAPFRAME=null; paintCompose();
}
function delCard(i){
  CARDS.splice(i,1);
  CARDI=Math.max(0,Math.min(CARDI,CARDS.length-1));
  dropPreview(); CAPFRAME=null; measureCards(); paintCompose();
}
function moveCard(i,d){
  const j=i+d; if(j<0||j>=CARDS.length) return;
  [CARDS[i],CARDS[j]]=[CARDS[j],CARDS[i]];
  CARDI=j; dropPreview(); CAPFRAME=null; paintCompose();
}
function pickCard(i){ rememberCard(); CARDI=i; paintCompose(); }
function rememberCard(){
  const box=$('#cap'); const c=card();
  if(box&&c) c.text=box.value;
}
function setCardText(v){
  const c=card(); if(!c) return;
  c.text=v; dropPreview(); CAPFRAME=null;
  const note=$('#capnote'); if(note) note.textContent=capNote();
  measureCards();
}
function setCardProp(k,v){
  const c=card(); if(!c) return;
  c[k]=v; dropPreview(); CAPFRAME=null; paintCompose();
}
/* Every card has to be readable, so the post has to cover all of them. */
function capSeconds(){
  return CARDS.reduce((n,c)=>n+cardSeconds(c),0);
}
function cardSeconds(c){
  const txt=(c.text||'').trim(); if(!txt) return 0;
  const fit=CAPFIT[capKey(c)];
  if(fit) return fit.needs;
  const lines=guessLines(txt);
  const words=txt.split(/\s+/).filter(Boolean).length;
  return TIMING.delay+(lines-1)*TIMING.step+CAPFADE
       + Math.max(words/TIMING.read, TIMING.minRead);
}
function guessLines(txt){
  const per = SHAPE==='landscape' ? 46 : 30;
  return txt.split('\n').map(x=>x.trim()).filter(Boolean)
    .reduce((n,l)=>n+Math.max(1,Math.ceil(l.length/per)),0);
}
/* What each card gets of the post, mirroring slideshow.card_times(). */
function cardSpans(){
  const live=CARDS.filter(c=>(c.text||'').trim());
  if(!live.length) return [];
  const total=LEN||autoSeconds();
  const needs=live.map(c=>Math.max(cardSeconds(c),0.1));
  const scale=total/needs.reduce((a,b)=>a+b,0);
  const ends=[]; let acc=0;
  needs.forEach(n=>{acc+=n*scale; ends.push(acc);});
  ends[ends.length-1]=total;
  const hold=holdSeconds();
  if(SNAP&&hold>0.01){
    for(let i=0;i<ends.length-1;i++){
      const want=Math.round(ends[i]/hold)*hold;
      const lo=(i?ends[i-1]:0)+1.2, hi=ends[i+1]-1.2;
      if(want>=lo&&want<=hi) ends[i]=want;
    }
  }
  let start=0;
  return ends.map(e=>{const span=[start,e]; start=e; return span;});
}
function holdSeconds(){
  const n=photoCount(); if(n<1) return 0;
  const total=LEN||autoSeconds();
  return n===1 ? total : total/n;
}

/* Words on the picture. These autoplay muted on every feed the desk posts
   to, so for a sound-off viewer the caption is the whole message. It sits
   above the bottom quarter on vertical: that band is where the platform
   draws its own caption, username and buttons, and anything under there is
   covered on Reels and TikTok. Blank leaves the picture alone. */
/* Everything the layout depends on. When this changes, what the server told
   us about the last one no longer applies. */
/* Everything a card's layout depends on. When any of it changes, what the
   server measured for the old one no longer applies. */
function capKey(c){ return JSON.stringify([c.text,CAPFONT,CAPSIZE,SHAPE]); }
let CAPFIT={};          /* key -> {lines, size, needs}, measured by the server */
let CAPFITWAIT=null;

/* How many lines a card really draws. A line you did not break gets wrapped at
   render time, so counting the ones you typed under-reports it, and guessing
   from character counts over-reports it: a condensed face fits far more across
   than the count suggests. So the server measures it with the real font, and
   the guess only covers the moment before that comes back. */
function cardLines(c){
  const fit=CAPFIT[capKey(c)];
  return fit ? fit.lines : guessLines(c.text||'');
}
function capLines(){ return CARDS.reduce((n,c)=>n+cardLines(c),0); }
/* Ask the renderer how each card really lays out, a moment after you stop
   typing. One call per card that has not been measured yet. */
function measureCards(){
  clearTimeout(CAPFITWAIT);
  CAPFITWAIT=setTimeout(async()=>{
    for(const c of CARDS){
      const txt=(c.text||'').trim(); if(!txt) continue;
      const key=capKey(c); if(CAPFIT[key]) continue;
      const r=await api('/api/caption/fit',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({caption:txt, caption_font:CAPFONT,
          caption_size:CAPSIZE, shape:SHAPE, caption_fade:CAPFADE})});
      if(r.error) continue;
      CAPFIT[key]={lines:r.lines, size:r.size, needs:r.needs, text:r.text};
    }
    const note=$('#capnote'); if(note) note.textContent=capNote();
    paintLenSeg();
  },300);
}
/* Only the length row, so the Auto label can follow without a full repaint. */
function paintLenSeg(){
  const seg=$('#lenseg'); if(!seg) return;
  const b=seg.querySelector('button');
  if(b) b.textContent=`Auto \u00b7 ${secs(autoSeconds())}`;
}
function photoCount(){
  return SEL.map(id=>MEDIA.find(m=>m.id===id))
    .filter(m=>m&&m.kind==='image').length;
}
/* What Auto actually comes out as, cards included. Takes the count so the note
   and the button cannot end up describing different selections. */
function autoSeconds(shots){
  const n = shots===undefined ? photoCount() : shots;
  const natural = n<=1 ? TIMING.single : Math.min(n*TIMING.per, TIMING.autoMax);
  return Math.min(Math.max(natural, capSeconds()), TIMING.max);
}
const secs=n=>`${Math.round(n*10)/10}s`;
function capNote(){
  const live=CARDS.filter(c=>(c.text||'').trim());
  if(!live.length) return 'Nothing is drawn on the picture. These autoplay '
    +'muted, so a line or two here is often the only thing a viewer reads.';
  const spans=cardSpans();
  const total=LEN||autoSeconds();
  const tight=live.map((c,i)=>spans[i]&&(spans[i][1]-spans[i][0])<cardSeconds(c)-0.05)
    .filter(Boolean).length;
  if(live.length===1){
    const c=live[0], lines=cardLines(c);
    const typed=(c.text||'').split('\n').filter(x=>x.trim()).length;
    const wrapped=lines>typed?` (${typed} typed, wrapping to ${lines})`:'';
    return `One card, ${lines} line${lines===1?'':'s'}${wrapped}. It needs `
      +`${secs(cardSeconds(c))} to be readable, and it has ${secs(total)}.`;
  }
  return `${live.length} cards across ${secs(total)}: `
    +live.map((c,i)=>spans[i]?`${secs(spans[i][1]-spans[i][0])}`:'?').join(', ')
    +'. '+(tight
      ? `${tight} of them ${tight===1?'is':'are'} short of reading time. Add `
        +'seconds or cut words.'
      : 'Each has time to be read.');
}
function creditOpts(){
  return CREDIT==='off' ? {credit_style:'off'}
       : {credit_style:CREDIT, credit_title:songName()};
}
function songName(){
  if(SONG.trim()) return SONG.trim();
  const t=MEDIA.find(m=>m.id===TRACK);
  return (t&&t.song)||'';
}
function setCredit(v){ CREDIT=v; dropPreview(); rememberCopy(); paintCompose(); }
function setSong(v){ SONG=v; dropPreview(); }
/* Correcting the name saves it on the track, so the next post starts right. */
async function saveSong(){
  const t=MEDIA.find(m=>m.id===TRACK); if(!t) return;
  const v=SONG.trim();
  if(v===((t.title||'').trim()||t.song)) return;
  await api(`/api/media/${TRACK}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({title:v})});
  t.title=v; t.song=v||t.song; toast('Saved to the track');
}
/* Styling is one set for the whole post; only where a card sits varies. */
/* The compose sheet is a working space, so the preview in it is small. This
   is the same file at the size you would actually watch it. */
function openViewer(url){
  VIEWER=url;
  const el=$('#viewer');
  el.innerHTML=`<div class="vshade" onclick="closeViewer()"></div>
    <div class="vbody">
      ${vplayer(url)}
      <div class="rowb">
        <span class="chars" style="flex:1">${PREVIEW
          ? `${PREVIEW.width}&times;${PREVIEW.height} \u00b7 ${PREVIEW.seconds}s
             \u00b7 ${KB(PREVIEW.bytes)}. This exact file is what posts.` : ''}</span>
        <button class="act" onclick="closeViewer()">Close</button>
      </div>
    </div>`;
  el.classList.add('on');
  const v=el.querySelector('video'); if(v) v.play().catch(()=>{});
}
function closeViewer(){
  const el=$('#viewer');
  const v=el.querySelector('video'); if(v) v.pause();
  el.classList.remove('on'); el.innerHTML=''; VIEWER=null;
}

function fitNote(){
  const m=FITMODE||DEFAULTS.fit_mode;
  if(m==='blur') return 'Every pixel kept, the gap filled with a blurred copy. ';
  if(m==='fill') return 'Cropped to the frame. Nothing is letterboxed and the '
    +'edges are lost. ';
  return 'Filled where little would be lost, whole over a blur where a lot '
    +'would. Mixed sets end up looking deliberate either way. ';
}
/* Drafts the cards from the copy. Asked for, not automatic: these are the
   words on the picture and you should read them before they go out. */
async function draftCards(){
  rememberCopy();
  if(!(CDRAFT||'').trim()){toast('Write the copy first');return;}
  const why=$('#cardwhy'); if(why) why.textContent='Writing\u2026';
  const r=await api('/api/caption/cards',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({copy:CDRAFT, profile:PROF, n:3,
      seconds:LEN||autoSeconds()})});
  if(r.error){ if(why) why.textContent=r.error; toast('Could not write them'); return; }
  CARDS=(r.cards||[]).map(c=>({text:c.text,position:c.position||'lower',
                               align:c.align||'center'}));
  CARDI=0; CAPFIT={}; dropPreview(); CAPFRAME=null; measureCards(); paintCompose();
  const w=$('#cardwhy'); if(w) w.textContent=r.why||'';
  toast(`${CARDS.length} cards`);
}

function capOpts(){
  const c=cardsOpts();
  if(!c.cards.length) return {cards:[]};
  return {...c, caption_font:CAPFONT, caption_size:CAPSIZE,
          caption_color:CAPCOL, caption_shadow:CAPSHADOW,
          caption_shadow_color:CAPSHCOL, caption_fade:CAPFADE,
          transition:TRANS||DEFAULTS.transition, snap:SNAP};
}
function renderOpts(){
  return {...capOpts(), ...creditOpts(),
          fit_mode:FITMODE||DEFAULTS.fit_mode, grade:GRADE};
}
/* Draws one frame with a card already up and the platform's own bands shaded,
   so you can see where the words land without rendering a video. */
async function showWhere(){
  const c=card();
  if(!c||!(c.text||'').trim()){toast('Write the caption first');return;}
  const btn=$('#whereb'); if(btn){btn.disabled=true; btn.textContent='Drawing\u2026';}
  const r=await api('/api/caption/frame',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:SEL, shape:SHAPE, caption:c.text,
      caption_position:c.position, caption_align:c.align,
      caption_font:CAPFONT, caption_size:CAPSIZE, caption_color:CAPCOL,
      caption_shadow:CAPSHADOW, caption_shadow_color:CAPSHCOL,
      fit_mode:FITMODE||DEFAULTS.fit_mode, grade:GRADE})});
  if(r.error){ toast(r.error); if(btn){btn.disabled=false;
    btn.textContent='Show me where';} return; }
  CAPFRAME=r.url; paintCompose();
}
function hideWhere(){ CAPFRAME=null; paintCompose(); }
function capSet(k,v){
  ({font:()=>CAPFONT=v, size:()=>CAPSIZE=v?+v:null, col:()=>CAPCOL=v,
    shadow:()=>CAPSHADOW=+v, shcol:()=>CAPSHCOL=v, fade:()=>CAPFADE=+v,
    trans:()=>TRANS=v, snap:()=>SNAP=v, fitmode:()=>FITMODE=v,
    pos:()=>setCardProp('position',v), align:()=>setCardProp('align',v)})[k]();
  if(k==='font'||k==='size') CAPFIT={};     /* measured at the old face */
  dropPreview(); CAPFRAME=null; rememberCopy(); measureCards(); paintCompose();
}
function setGrade(k,v){
  GRADE[k]=+v; dropPreview(); CAPFRAME=null; paintCompose();
}
function resetGrade(){
  GRADE={brightness:0,contrast:1,saturation:1,warmth:0};
  dropPreview(); CAPFRAME=null; paintCompose();
}
/* A dark photo is worth lifting, and the desk can tell without being asked. */
async function suggestGrade(){
  if(!SEL.length){toast('Pick a photo first');return;}
  const r=await api('/api/photo/read',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:SEL})});
  if(r.error){toast(r.error);return;}
  GRADE={...GRADE, ...r.suggest};
  dropPreview(); CAPFRAME=null; paintCompose();
  toast(r.why||'Adjusted');
}
/* The font list is whatever is installed on this machine, so it is read once
   and reused rather than shipped with the desk. */
async function loadDefaults(){
  const r=await api('/api/defaults');
  if(!r.error) DEFAULTS=r;
}
/* Change it here and it is the default for every post after this one. */
async function saveDefault(k,v){
  DEFAULTS[k]=v;
  await api('/api/defaults',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({[k]:v})});
  toast('Saved as the default');
}
let SCOPES={markets:{}};
async function loadScopes(){
  if(Object.keys(SCOPES.markets||{}).length) return SCOPES;
  SCOPES=await api('/api/scopes');
  paintCompose();
  return SCOPES;
}

async function loadFonts(){
  if(FONTS) return FONTS;
  FONTS = await api('/api/fonts');
  /* The profile's own face wins over the desk's default. */
  const want=((PROFS[PROF]||{}).brand||{}).font;
  if(!CAPFONT)
    CAPFONT = (want && (FONTS.fonts||[]).includes(want) ? want : null)
           || FONTS.default || (FONTS.suggested||[])[0] || '';
  paintCompose();
  return FONTS;
}
function fontOptions(){
  if(!FONTS) return `<option>Reading the machine&hellip;</option>`;
  const pick=(FONTS.suggested||[]), all=(FONTS.fonts||[]);
  const opt=n=>`<option value="${esc(n)}"${n===CAPFONT?' selected':''}
    >${esc(n)}</option>`;
  return (pick.length?`<optgroup label="Reads well on a phone">
      ${pick.map(opt).join('')}</optgroup>`:'')
    + `<optgroup label="Everything installed (${all.length})">
      ${all.map(opt).join('')}</optgroup>`;
}

/* How long a photo post runs, and what that buys.

   A still stretched across a whole track is dead air: nobody watches sixty
   seconds of one photograph, and a feed that measures watch-through reads
   that as a post nobody stayed for. Short and looping beats long and
   trailing off, because a replay counts. So the desk picks 7s for a single
   photo and 3s each for a run, and anything longer cycles the photos
   instead of holding them. */
function setLen(v){
  LEN = (v===null||LEN===v) ? null : v;   // press the same one again for Auto
  dropPreview(); rememberCopy(); paintCompose();
}
function lenNote(n){
  if(!n) return '';
  if(LEN===null){
    const a=autoSeconds(n), lifted=capSeconds()>0.001
      &&a>(n<=1?TIMING.single:Math.min(n*TIMING.per,TIMING.autoMax));
    if(lifted) return `Auto: ${secs(a)}, which is what the caption needs to be `
      +'read. Take the length down and the words get cut off; shorten the '
      +'caption and this follows it.';
    return n===1
      ? `Auto: ${secs(a)}. Long enough to read the photo and catch the hook, `
        +'short enough to come round twice in a scroll.'
      : `Auto: ${TIMING.per} seconds each, ${secs(a)} in all.`;
  }
  if(n===1) return `One photo held for ${LEN}s.`;
  const passes=Math.max(1,Math.round(LEN/(n*3)));
  return passes>1
    ? `${n} photos, round ${passes} times in ${LEN}s. Each photo holds `
      +`${(LEN/(passes*n)).toFixed(1)}s, so it loops on the cut.`
    : `${n} photos in ${LEN}s, ${(LEN/n).toFixed(1)}s each.`;
}

/* Any change to the inputs makes the preview stale. */
function dropPreview(){ PREVIEW=null; }
const mmss=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;

/* Every video the desk asks you to watch gets the same four controls: play,
   pause, back to the start, and a line you can drag. Handlers walk up from
   the element that was clicked, so nothing needs an id and a repaint of the
   sheet cannot leave a stale wire behind. */
function vplayer(src){
  return `<div class="vwrap">
    <video src="${src}" preload="metadata" playsinline
      onloadedmetadata="vtick(this)" ontimeupdate="vtick(this)"
      onplay="vtick(this)" onpause="vtick(this)" onended="vtick(this)"
      onclick="vtoggle(this)"></video>
    <div class="vbar">
      <button class="vplay" title="Play" onclick="vtoggle(vof(this))"
        >&#9654;</button>
      <button title="Back to the start" onclick="vhome(vof(this))"
        >&#9198;</button>
      <input class="vseek" type="range" min="0" max="1000" step="1" value="0"
        title="Drag to move through it"
        onpointerdown="this.dataset.drag='1'"
        onpointerup="this.dataset.drag=''"
        onpointercancel="this.dataset.drag=''"
        oninput="vseek(vof(this),this.value)">
      <span class="vtime">0:00</span>
    </div>
  </div>`;
}
/* The video belonging to whichever control you touched. */
const vof=el=>el.closest('.vwrap').querySelector('video');
function vtoggle(v){ v.paused ? v.play() : v.pause(); }
function vhome(v){ v.currentTime=0; vtick(v); }
function vseek(v,val){
  const d=v.duration;
  if(!isFinite(d)||!d) return;
  v.currentTime=d*(val/1000); vtick(v);
}
/* One paint for the whole bar. Metadata can be missing on the first call, so
   duration is never assumed. */
function vtick(v){
  const bar=v.closest('.vwrap').querySelector('.vbar'), d=v.duration,
        known=isFinite(d)&&d>0, at=v.currentTime||0,
        seek=bar.querySelector('.vseek'), play=bar.querySelector('.vplay'),
        going=!v.paused&&!v.ended;
  /* Leave the handle alone while it is being dragged, or playback fights it. */
  if(known&&!seek.dataset.drag) seek.value=Math.round(1000*at/d);
  bar.querySelector('.vtime').textContent=mmss(at)+(known?` / ${mmss(d)}`:'');
  play.innerHTML=going?'&#10074;&#10074;':'&#9654;';
  play.title=going?'Pause':'Play';
}
/* Take the cue from wherever the track is playing. */
function cueHere(){
  const a=$('#cueplay'); if(!a) return;
  TRACKSTART=Math.max(0, Math.round(a.currentTime*10)/10);
  dropPreview(); rememberCopy(); paintCompose();
  toast(`Track starts ${mmss(TRACKSTART)} in`);
}
/* Folders are a shelf, not a filing system: one level, per profile, and a file
   that is in none of them is loose rather than lost. FOLDER is what the grid is
   filtered to: null for everything, 'loose' for the unfiled, or an id. */
let FOLDERS=[], LOOSE=0, FOLDER=null, DRAGGING=null, DROPON=null;
let UPOPEN=localStorage.getItem('desk.upopen')!=='0';
let SHOWLIVE=true, SHOWNEW=true;
/* One box for both kinds of link. A page and a picture are never the same URL,
   so there is nothing to choose between: the button just says which one it has
   got. */
const IMAGE_URL=/\.(jpe?g|png|gif|webp|avif|heic|bmp)(\?|#|$)/i;
function paintFetch(){
  const b=$('#fetchb'), u=($('#lurl')||{}).value||'';
  if(b) b.textContent=IMAGE_URL.test(u.trim()) ? 'Grab the image' : 'Fetch';
}

/* The desk keeps its own copy of every file, so deleting here never touches
   whatever you uploaded from. */
async function delSelected(){
  const ids=[...SEL]; if(TRACK) ids.push(TRACK);
  if(!ids.length) return;
  const names=ids.map(i=>(MEDIA.find(m=>m.id===i)||{}).original||i);
  if(!confirm(`Delete ${ids.length} file${ids.length===1?'':'s'} from the library?`
    +`\n\n${names.join('\n')}\n\nThe desk's copy goes. Your original file is`
    +` untouched, and anything already posted stays up.`)) return;
  let gone=0; const kept=[];
  for(const id of ids){
    const r=await fetch(`/api/media/${id}`,{method:'DELETE'}).then(r=>r.json());
    if(r.error) kept.push(`${(MEDIA.find(m=>m.id===id)||{}).original}: ${r.error}`);
    else gone++;
  }
  SEL=[]; TRACK=null;
  toast(kept.length?`${gone} deleted, ${kept.length} kept`:`${gone} deleted`);
  if(kept.length) alert(kept.join('\n\n'));
  load();
}
/* If the photos themselves lean one way, say so rather than let a whole set
   get letterboxed by a default. */
function shapeHint(sel){
  const land=sel.filter(m=>m.width&&m.height&&m.width>m.height).length;
  const port=sel.filter(m=>m.width&&m.height&&m.height>m.width).length;
  if(land&&land===sel.length&&SHAPE!=='landscape')
    return ' All of these photos are landscape.';
  if(port&&port===sel.length&&SHAPE!=='portrait')
    return ' All of these photos are portrait.';
  return '';
}
/* Warn before rendering, not after: a short track leaves the tail silent. */
function trackShort(v){
  const t=MEDIA.find(m=>m.id===TRACK);
  if(!t||!t.seconds||!v||!v.seconds) return '';
  const gap=Math.round((v.seconds-t.seconds)*10)/10;
  return gap>0.5?` The track is ${gap}s shorter than the video, so the last
    ${gap}s will be silent.`:'';
}
/* The last row you clicked without shift. Shift-clicking another one takes
   everything between the two, in the order they are on screen, which is what
   the ordering of a carousel should follow. */
let MANCHOR=null;

function toggleSel(id, ev){
  const m=MEDIA.find(x=>x.id===id);
  if(m&&m.kind==='audio'){ TRACK = TRACK===id?null:id; drawUpload(); return; }

  if(ev&&ev.shiftKey&&MANCHOR!==null&&MANCHOR!==id){
    const order=(MSHOWN||[]).map(x=>x.id);
    const a=order.indexOf(MANCHOR), b=order.indexOf(id);
    if(a>=0&&b>=0){
      const run=order.slice(Math.min(a,b), Math.max(a,b)+1);
      /* Add what is missing rather than replacing the selection: a range is
         usually a second handful on top of a first. Audio is skipped, since
         a track is a different kind of choice. */
      run.forEach(x=>{
        const r=MEDIA.find(y=>y.id===x);
        if(r&&r.kind!=='audio'&&!selHas(x)) SEL.push(x);
      });
      dropPreview(); drawUpload();
      return;
    }
  }

  /* Clicking a photo off takes every copy of it, which is what "not this one"
     means. Copies are made deliberately, in the compose strip. */
  if(selHas(id)) SEL=SEL.filter(x=>x!==id); else SEL.push(id);
  MANCHOR=id;
  dropPreview(); drawUpload();
}
/* These work on where a photo sits, not which photo it is, because the same
   photo may now appear more than once. */
function moveSel(i,d){
  const j=i+d;
  if(i<0||i>=SEL.length||j<0||j>=SEL.length)return;
  SEL.splice(j,0,SEL.splice(i,1)[0]);
  rememberCopy(); paintCompose();
}
function dropSel(i){
  if(i<0||i>=SEL.length)return;
  SEL.splice(i,1);
  rememberCopy();
  SEL.length?paintCompose():closeSheet();
  drawUpload();
}
/* The same photo twice, so it can come back later in the run. Right-click a
   shot or press the button; the copy lands next to the original, ready to be
   moved somewhere else in the story. */
function dupSel(i){
  if(i<0||i>=SEL.length)return;
  SEL.splice(i+1,0,SEL[i]);
  dropPreview(); rememberCopy(); paintCompose(); drawUpload();
  toast('Used again. Move it where you want it.');
}

/* Nothing here is re-encoded, so the file you drop is the file the platforms
   get. That makes the source the only thing that decides quality, and this is
   where you find out before it goes out rather than after. */
function qualityNote(m){
  if(!m.width||!m.height) return '';
  const short=Math.min(m.width,m.height), vertical=m.height>m.width;
  const bits=[];
  if(m.kind==='video'&&short<1080)
    bits.push(`${short}p source. Reels and TikTok encode to 1080 wide.`);
  if(m.kind==='video'&&!vertical)
    bits.push('Landscape. Vertical feeds will crop or letterbox it.');
  return bits.length?`<div class="qn">${esc(bits.join(' '))}</div>`:'';
}

async function loadFolders(){
  const r=await api('/api/folders?profile='+encodeURIComponent(PROF));
  FOLDERS=r.folders||[]; LOOSE=r.loose||0;
  if(FOLDER!==null&&FOLDER!=='loose'&&!FOLDERS.some(f=>f.id===FOLDER)) FOLDER=null;
}
function setFolder(f){ FOLDER=f; drawUpload(); }
async function newFolder(){
  const name=(prompt('Name the folder')||'').trim();
  if(!name) return;
  const r=await api('/api/folders',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name, profile:PROF})});
  if(r.error){toast(r.error);return;}
  await loadFolders(); FOLDER=r.id; drawUpload(); toast(`Made ${name}`);
}
async function renameFolder(id){
  const f=FOLDERS.find(x=>x.id===id); if(!f) return;
  const name=(prompt('Rename the folder', f.name)||'').trim();
  if(!name||name===f.name) return;
  const r=await api(`/api/folders/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  if(r.error){toast(r.error);return;}
  await loadFolders(); drawUpload();
}
async function dropFolder(id){
  const f=FOLDERS.find(x=>x.id===id); if(!f) return;
  if(f.n&&!confirm(`Delete ${f.name}? The ${f.n} file${f.n===1?'':'s'} in it `
    +'stay in the library, just loose.')) return;
  const r=await api(`/api/folders/${id}`,{method:'DELETE'});
  if(r.error){toast(r.error);return;}
  if(FOLDER===id) FOLDER=null;
  await loadFolders(); await load();
  toast(r.loosed?`${r.loosed} file${r.loosed===1?'':'s'} loose again`:'Deleted');
}
/* Dragging one of a selection moves the lot, because moving forty photos one
   at a time is the problem folders are here to solve. */
function dragIds(id){
  return selHas(id)&&SEL.length>1 ? SEL.slice() : [id];
}
function startDrag(e,id){
  DRAGGING=dragIds(id);
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/desk-media', JSON.stringify(DRAGGING));
  /* Some browsers refuse a drag with no plain payload. */
  e.dataTransfer.setData('text/plain', DRAGGING.join(','));
}
function endDrag(){ DRAGGING=null; DROPON=null; clearOver(); }
const isMediaDrag=e=>[...(e.dataTransfer&&e.dataTransfer.types||[])]
  .includes('text/desk-media');
function clearOver(){
  document.querySelectorAll('.fold.over').forEach(x=>x.classList.remove('over'));
}
/* The highlight goes on the element itself rather than through a repaint.
   Redrawing the bar under a drag replaces the very node the pointer is over,
   which in a browser cancels the drag mid-gesture. */
function overFolder(e,el,key){
  if(!isMediaDrag(e)) return;
  e.preventDefault(); e.dataTransfer.dropEffect='move';
  DROPON=key; el.classList.add('over');
}
function leaveFolder(el,key){
  if(DROPON===key) DROPON=null;
  el.classList.remove('over');
}
async function dropInto(e,key){
  if(!isMediaDrag(e)) return;
  e.preventDefault();
  let ids=DRAGGING;
  try{ ids=JSON.parse(e.dataTransfer.getData('text/desk-media')); }catch(_){}
  DROPON=null; DRAGGING=null; clearOver();
  if(!ids||!ids.length) return;
  const r=await api('/api/media/move',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:ids, folder_id:key==='loose'?null:key})});
  if(r.error){toast(r.error);return;}
  await loadFolders(); await load();
  const where=key==='loose'?'out of the folder'
    :`into ${(FOLDERS.find(f=>f.id===key)||{}).name||'the folder'}`;
  toast(`${r.moved} file${r.moved===1?'':'s'} ${where}`);
}
/* Only the bar, so a drag hovering a folder does not repaint the whole grid
   underneath it and drop the drag. */
function paintFolderBar(){
  const el=$('#folderbar'); if(el) el.innerHTML=folderBar();
}
function folderBar(){
  const chip=(key,label,n,extra='')=>{
    /* Single quotes on purpose: the attribute is delimited by double ones, so
       JSON.stringify('loose') ends it early and the handler becomes rubbish. */
    const k=key===null?'null':typeof key==='string'?`'${key}'`:String(key);
    return `<button class="fold${FOLDER===key?' on':''}"
      onclick="setFolder(${k})"
      ondragover="overFolder(event,this,${k})"
      ondragleave="leaveFolder(this,${k})"
      ondrop="dropInto(event,${k})"
      >${label} <span class="fn">${n}</span>${extra}</button>`;
  };
  return chip(null,'All',MEDIA.filter(m=>(m.bucket||'library')==='library').length)
    + chip('loose','Loose',LOOSE)
    + FOLDERS.map(f=>chip(f.id,esc(f.name),f.n,
        `<span class="fx" title="Rename"
           onclick="event.stopPropagation();renameFolder(${f.id})">&#9998;</span>
         <span class="fx x" title="Delete"
           onclick="event.stopPropagation();dropFolder(${f.id})">&times;</span>`)).join('')
    + `<button class="fold add" onclick="newFolder()">+ Folder</button>`;
}

/* How the library is shown. Three views over the same rows: big tiles to
   judge a picture by, small tiles to find one among two hundred, and a list to
   read the facts. Remembered, because it is a working preference rather than a
   per-visit choice. */
/* Two shelves of one library: the working media, and the ads. They share
   uploads, folders, selection, the compose sheet and the push path, because an
   ad is a picture or a video like any other. What they do not share is how you
   are looking at them, so the view, sort, filter and search are kept per
   shelf. BUCKET is whichever tab is on screen. */
let BUCKET='library';
const SHELF={
  library:{el:'#up', title:'Media', view:'big', sort:'added-desc', kind:'all', q:''},
  ads:{el:'#ads', title:'Ads', view:'big', sort:'added-desc', kind:'all', q:''},
};
for(const [k,s] of Object.entries(SHELF)){
  s.view=localStorage.getItem(`desk.${k}.view`)||s.view;
  s.sort=localStorage.getItem(`desk.${k}.sort`)||s.sort;
  s.kind=localStorage.getItem(`desk.${k}.kind`)||s.kind;
}
const shelf=()=>SHELF[BUCKET];
let MSHOWN=[];
function setMView(v){shelf().view=v;localStorage.setItem(`desk.${BUCKET}.view`,v);drawUpload();}
function setMSort(v){shelf().sort=v;localStorage.setItem(`desk.${BUCKET}.sort`,v);drawUpload();}
function setMKind(v){shelf().kind=v;localStorage.setItem(`desk.${BUCKET}.kind`,v);drawUpload();}
function setMQ(v){shelf().q=v;drawUpload();}

const MSORTS=[
  ['added-desc','Newest'], ['added-asc','Oldest'],
  ['name-asc','Name A-Z'], ['name-desc','Name Z-A'],
  ['bytes-desc','Largest'], ['bytes-asc','Smallest'],
  ['kind-asc','Kind'], ['used-desc','Most used'],
];

/* A sortable column head. Clicking it sorts by that column, and clicking the
   same one again turns it round. Name starts A to Z and the numbers start at
   the big end, because that is what each is usually wanted for. */
function mhead(label,key){
  const [cur,dir]=shelf().sort.split('-');
  const on=cur===key;
  const next=on?(dir==='asc'?'desc':'asc')
             :(key==='name'||key==='kind'?'asc':'desc');
  return `<th class="sortable${on?' on':''}"
    onclick="setMSort('${key}-${next}')"
    title="Sort by ${label.toLowerCase()}">${label}<span class="arr">${
      on?(dir==='asc'?'&#9650;':'&#9660;'):''}</span></th>`;
}

function sortMedia(rows){
  const [key,dir]=shelf().sort.split('-');
  const s=dir==='asc'?1:-1;
  const val=m=>({
    added:m.added||'', name:(m.original||'').toLowerCase(),
    bytes:m.bytes||0, kind:m.kind||'',
    used:(m.published||0)+(m.posts||0),
  })[key];
  return rows.slice().sort((a,b)=>{
    const x=val(a), y=val(b);
    return x<y?-s:x>y?s:0;
  });
}

/* Free text over the things you would actually remember about a file: what it
   was called, what you wrote about it, and where it came from. */
function matchMedia(m){
  if(shelf().kind!=='all'&&m.kind!==shelf().kind) return false;
  const q=shelf().q.trim().toLowerCase();
  if(!q) return true;
  return [m.original,m.note,m.title,m.url,m.source]
    .some(v=>(v||'').toLowerCase().includes(q));
}

function drawUpload(which){
  if(which) BUCKET=which;
  const s=shelf();
  const label=esc((PROFS[PROF]||{}).label||PROF);
  /* Only this shelf's files. Everything downstream, selection included, works
     on the same rows it always did. */
  const ALL=MEDIA.filter(m=>(m.bucket||'library')===BUCKET);
  const nLive=ALL.filter(m=>m.published).length;
  const inFolder=m=>FOLDER===null ? true
    : FOLDER==='loose' ? !m.folder_id : m.folder_id===FOLDER;
  const shown=sortMedia(ALL.filter(m=>(m.published?SHOWLIVE:SHOWNEW)
    &&inFolder(m)&&matchMedia(m)));
  /* Held for shift-click, which needs to know what a range means: the rows as
     they are sorted and filtered right now, not the library order. */
  MSHOWN=shown;
  const kinds={};
  ALL.forEach(m=>{kinds[m.kind]=(kinds[m.kind]||0)+1;});

  const rowList=m=>`
    <tr class="${selHas(m.id)?'sel':''}" onclick="toggleSel(${m.id},event)"
      draggable="true" ondragstart="startDrag(event,${m.id})" ondragend="endDrag()">
      <td class="lthumb">${m.kind==='video'
        ? `<video src="/media/${m.id}/raw#t=0.5" preload="metadata" muted playsinline></video>`
        : m.kind==='audio' ? `<span class="note">&#9834;</span>`
        : `<img src="/media/${m.id}/raw" alt="" loading="lazy">`}</td>
      <td><div class="n">${esc(m.original)}</div>
        ${m.note?`<div class="sub">${esc((m.note||'').slice(0,90))}</div>`:''}</td>
      <td>${m.kind}</td>
      <td>${m.width?`${m.width}&times;${m.height}`:m.seconds?`${m.seconds}s`:'—'}</td>
      <td>${KB(m.bytes||0)}</td>
      <td>${esc((m.added||'').slice(0,10))}</td>
      <td>${m.published?`<span class="dim hot">published${m.published>1
        ?' &times;'+m.published:''}</span>`:m.posts?'scheduled':'—'}</td>
    </tr>`;

  const items=shown.map(m=>`
    <div class="mi${selHas(m.id)||TRACK===m.id?' sel':''}${
      m.published?' live':''}" onclick="toggleSel(${m.id},event)"
      draggable="true" ondragstart="startDrag(event,${m.id})"
      ondragend="endDrag()">
      <div class="th">
        ${m.kind==='video'
          ? `<video src="/media/${m.id}/raw#t=0.5" preload="metadata" muted playsinline></video>`
          : m.kind==='audio' ? `<span class="note">&#9834;</span>`
          : `<img src="/media/${m.id}/raw" alt="${esc(m.original)}" loading="lazy">`}
        <span class="tick">${m.kind==='audio'?(TRACK===m.id?'&#9834;':'')
          :selHas(m.id)?String(SEL.indexOf(m.id)+1)
            +(SEL.filter(x=>x===m.id).length>1
              ? `&times;${SEL.filter(x=>x===m.id).length}` : '')
          :''}</span>
      </div>
      <div class="b">
        <div class="n" title="${esc(m.original)}">${esc(m.original)}</div>
        <div class="m">${m.kind} · ${KB(m.bytes||0)}${
          m.width?` · ${m.width}&times;${m.height}`:''}${
          m.seconds?` · ${m.seconds}s`:''}</div>
        ${m.url?`<div class="m linky" title="${esc(m.url)}">&#128279;
          ${esc((m.url||'').replace(/^https?:\/\//,'').slice(0,34))}</div>`:''}
        ${m.published?`<div class="live">Published${m.published>1
          ?` &times;${m.published}`:''}</div>`
          :m.posts?`<div class="m">scheduled</div>`:''}
        ${qualityNote(m)}
        <textarea class="desc" rows="2" placeholder="Describe this. What is in it?"
          draggable="false" ondragstart="event.stopPropagation()"
          onclick="event.stopPropagation()"
          onchange="mnote(${m.id},this.value)">${esc(m.note||'')}</textarea>
      </div>
    </div>`).join('');

  const picked=SEL.length+(TRACK?1:0);
  $(s.el).innerHTML=`
    <div class="uphead">
      <b>${esc(s.title)}</b>
      <span class="spacer"></span>
      <span class="sub">${ALL.length} file${ALL.length===1?'':'s'} ·
        ${KB(ALL.reduce((a,m)=>a+(m.bytes||0),0))}</span>
    </div>
    <div class="chars" style="margin:-6px 0 14px">Drop files anywhere on this
      tab. Filed under <b>${label}</b>${BUCKET==='ads'?' as an ad':''}, on this
      machine. Or copy an image and
      press <b>&#8984;V</b>. From a phone, open
      <code>${esc(location.host)}</code> on the same wifi.</div>
    ${ALL.length?`<div class="folders" id="folderbar">${folderBar()}</div>
      <div class="chars" style="margin:-4px 0 12px">Drag files onto a folder to
        file them, or onto Loose to take them out. Dragging one of a selection
        moves all of it.</div>`:''}
    ${ALL.length&&nLive?`<div class="filters">
      <label class="fchip${SHOWLIVE?' on':''}">
        <input type="checkbox" ${SHOWLIVE?'checked':''}
          onchange="SHOWLIVE=this.checked;drawUpload()">
        Published <span class="fn">${nLive}</span></label>
      <label class="fchip${SHOWNEW?' on':''}">
        <input type="checkbox" ${SHOWNEW?'checked':''}
          onchange="SHOWNEW=this.checked;drawUpload()">
        Not published <span class="fn">${ALL.length-nLive}</span></label>
    </div>`:''}
    ${ALL.length?`<div class="rowb" style="margin:0 0 12px">
      <span class="seg">${[['big','big tiles'],['small','small'],['list','list']]
        .map(([k,l])=>`<button class="${s.view===k?'on':''}"
        onclick="setMView('${k}')">${l}</button>`).join('')}</span>
      <input type="search" value="${esc(s.q)}" placeholder="Search name, note or link"
        oninput="setMQ(this.value)" style="width:210px">
      <select class="slim" onchange="setMKind(this.value)">
        <option value="all"${s.kind==='all'?' selected':''}>every kind</option>
        ${Object.keys(kinds).sort().map(k=>`<option value="${k}"
          ${s.kind===k?' selected':''}>${k} (${kinds[k]})</option>`).join('')}
      </select>
      <select class="slim" onchange="setMSort(this.value)">
        ${MSORTS.map(([k,l])=>`<option value="${k}"${s.sort===k?' selected':''}
          >${l}</option>`).join('')}
      </select>
      <span class="chars" style="flex:1">${shown.length} of ${ALL.length}
        shown</span>
    </div>`:''}
    <div class="uprow">
      <button class="act" onclick="$('#file').click()">Upload ${
        BUCKET==='ads'?'ads':'files'}</button>
      <input type="text" id="lurl" placeholder="Paste a blog post, or a link straight to an image"
        oninput="paintFetch()"
        onkeydown="if(event.key==='Enter')addLink()">
      <button class="act" id="fetchb" onclick="addLink()">Fetch</button>
    </div>
    <input type="file" id="file" multiple accept="image/*,video/*,audio/*" hidden>
    <div class="lan" id="lmsg"></div>
    <hr class="rule">
    ${picked?`<div class="selbar">
      <b>${SEL.length} selected${TRACK?', with music':''}</b>
      <button class="act" onclick="SEL=[];TRACK=null;drawUpload()">Clear</button>
      <button class="act no" onclick="delSelected()">Delete</button>
      <span class="spacer"></span>
      ${SEL.length?`<button class="act go" onclick="openCompose()">Create post</button>`
        :`<span class="sub">Pick a photo or video to post it with</span>`}
    </div>`:''}
    ${shown.length?(s.view==='list'
      ? `<table class="tbl mlist"><thead><tr><th></th>
          ${mhead('Name','name')}${mhead('Kind','kind')}
          <th>Size</th>${mhead('Bytes','bytes')}${mhead('Added','added')}
          ${mhead('Use','used')}</tr></thead>
          <tbody>${shown.map(rowList).join('')}</tbody></table>`
      : `<div class="grid${s.view==='small'?' small':''}">${items}</div>`)
      :`<div class="empty"><b>${!ALL.length?(BUCKET==='ads'?'No ads yet.':'Nothing uploaded yet.')
        :FOLDER!==null&&FOLDER!=='loose'?'This folder is empty.'
        :FOLDER==='loose'?'Everything is filed.'
        :(s.q||s.kind!=='all')?'Nothing matches that search.'
        :'Nothing matches those filters.'}</b>${ALL.length
        ?(FOLDER!==null?'Drag files onto it from All.':'')
        :'Drop files above, or open this page on your phone.'}</div>`}`;

  const d=$('#up');
  if(!d) return;
  $('#file').onchange=e=>up(e.target.files);
  /* The whole tab takes a drop, not a letterbox at the top of it. A file
     dragged from the desktop is an upload; a tile dragged from the grid is a
     move between folders, and must not be handed to up(). */
  const fromOutside=e=>[...(e.dataTransfer&&e.dataTransfer.types||[])]
    .includes('Files');
  ['dragenter','dragover'].forEach(n=>d.addEventListener(n,e=>{
    if(!fromOutside(e))return;
    e.preventDefault();d.classList.add('hot');}));
  ['dragleave','drop'].forEach(n=>d.addEventListener(n,e=>{
    if(!fromOutside(e))return;
    e.preventDefault();
    /* Moving between children of the tab is not leaving it. */
    if(n==='dragleave'&&d.contains(e.relatedTarget))return;
    d.classList.remove('hot');}));
  d.addEventListener('drop',e=>{ if(fromOutside(e)) up(e.dataTransfer.files); });
}

/* One person, many platforms. Grouped by name so you pick the person, and the
   right handle for each platform is resolved when it publishes. */
let PQ='', PQPLAT='instagram';

/* One row per person, filtered by the search box. */
function peopleRows(ppl){
  const q=PQ.trim().toLowerCase().replace(/^@/,'');
  const hit=ppl.filter(p=>!q
    || p.name.toLowerCase().includes(q)
    || Object.values(p.on).some(h=>h.handle.toLowerCase().includes(q)));
  if(!hit.length) return `<div class="none">Nobody by that name.
    Pick a platform and press Add to put <b>${esc(PQ.trim())}</b> on the list.</div>`;
  return hit.map(p=>`<div class="prow">
    <span class="pn">${esc(p.name)}<span class="sub"> ${esc(p.role||'')}</span></span>
    ${['no','tag','collab'].map(r=>`<label class="pr">
      <input type="radio" name="r-${esc(p.name).replace(/\W/g,'')}"
        ${(ROLES[p.name]||'no')===r?'checked':''}
        onchange="ROLES['${esc(p.name)}']='${r}'">${
          r==='no'?'No':r==='tag'?'Tag':'Collab'}</label>`).join('')}
  </div>`).join('');
}
function paintPeople(){
  const el=$('#plist'); if(el) el.innerHTML=peopleRows(people());
}

/* No API anywhere can search Instagram or Facebook for a handle, so this
   takes you at your word and remembers it for next time. */
async function addHandle(){
  const h=PQ.trim(); if(!h) return;
  const plat=$('#pqplat')?$('#pqplat').value:PQPLAT;
  PQPLAT=plat;
  const r=await api('/api/handles',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({handle:h, platform:plat, profile:PROF})});
  if(r.error){toast(r.error);return;}
  PROFS = await api('/api/profiles');
  ROLES[r.added]='tag';
  PQ='';
  toast(`${r.added} added to ${(PLAT[plat]||{}).label||plat}`);
  paintCompose();
}

function people(){
  const h=(PROFS[PROF]||{}).handles||{}, by={};
  for(const [plat,list] of Object.entries(h)){
    (list||[]).forEach(x=>{
      if(!x||!x.handle)return;
      const k=x.name||x.handle;
      by[k]=by[k]||{name:k,role:x.role||'',on:{}};
      by[k].on[plat]={handle:x.handle,collab:!!x.collab};
    });
  }
  return Object.values(by).sort((a,b)=>a.name.localeCompare(b.name));
}

/* A pasted image arrives as a blob called image.png, or nothing at all. */
function named(f,i){
  if(f.name && f.name!=='image.png' && f.name!=='blob') return f;
  const ext=(f.type.split('/')[1]||'png').replace('jpeg','jpg').replace('quicktime','mov');
  const st=new Date().toISOString().replace(/[-:T]/g,'').slice(0,14);
  return new File([f],`pasted-${st}${i?'-'+(i+1):''}.${ext}`,{type:f.type});
}

document.addEventListener('paste',e=>{
  if($('#media').hidden) return;
  if(/^(INPUT|TEXTAREA|SELECT)$/.test((e.target||{}).tagName||'')) return;
  const files=[...((e.clipboardData||{}).items||[])]
    .filter(i=>i.kind==='file').map(i=>i.getAsFile())
    .filter(f=>f&&(f.type.startsWith('image/')||f.type.startsWith('video/')));
  if(!files.length) return;
  e.preventDefault();
  up(files.map(named),'paste');
});

/* A web page becomes a piece of media: its feature image, its opening as the
   caption, and the link itself on the end of the post. */
async function addLink(){
  const box=$('#lurl'), msg=$('#lmsg');
  const u=(box.value||'').trim();
  if(!u){box.focus();return;}
  msg.textContent='Reading that page…';
  const r=await api('/api/media/link',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:u, profile:PROF})});
  if(r.error){msg.textContent=r.error;return;}
  box.value=''; msg.textContent='';
  toast(`Added "${(r.added&&r.added.title)||'page'}"`);
  await load();
}

async function up(files,source){
  if(!files||!files.length)return;
  const fd=new FormData();
  [...files].forEach(f=>fd.append('file',f));
  fd.append('profile',PROF);
  fd.append('source',source||'drop');
  /* Whichever shelf you are looking at is the one you are dropping onto. */
  fd.append('bucket',BUCKET);
  toast(`Copying ${files.length} file${files.length===1?'':'s'}…`);
  const r=await fetch('/api/media',{method:'POST',body:fd}).then(r=>r.json());
  if(r.error){toast(r.error);return;}
  const bits=[`${r.added} added`];
  if(r.duplicates) bits.push(`${r.duplicates} already in the library`);
  if((r.rejected||[]).length) bits.push(`${r.rejected.length} not a photo or video`);
  toast(bits.join(', '));
  load();
}
async function mnote(id,v){
  await api(`/api/media/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({note:v})});
  toast('Saved');
}
async function mcamp(id,v){
  await api(`/api/media/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({campaign:v||null})});
  toast(v?'Filed under '+title(v):'Release cleared'); load();
}

/* Pick the accounts, not the dates. Each account's next free slot is used. */
function maccts(id){
  const box=document.getElementById('mu-'+id);
  if(box.dataset.open==='1'){box.innerHTML='';box.dataset.open='0';return;}
  box.dataset.open='1';
  box.innerHTML=`<div class="pick">
    <div class="none">Which accounts should this go to?</div>
    ${ACCTS.map(a=>`<label class="slot">
      <input type="checkbox" value="${a.channel}" data-prof="${a.profile}"
        ${a.on?' checked':''}>
      <span style="color:${a.ink}">&#9632;</span>
      <span>${esc(a.label)}</span>
      <span class="sub">${esc(a.profile_label)}${
        a.free_slots?'':' · new slot'}</span></label>`).join('')}
    <button class="act go" style="margin-top:10px;width:100%"
      onclick="mcreate(${id})">Create draft post</button>
    <div class="none" id="cprog-${id}" style="margin-top:8px"></div></div>`;
}

async function mcreate(id){
  const box=document.getElementById('mu-'+id);
  const chans=[...box.querySelectorAll('input:checked')]
    .map(i=>({channel:i.value, profile:i.dataset.prof}));
  if(!chans.length){toast('Pick at least one account');return;}
  const prog=document.getElementById('cprog-'+id);
  prog.textContent='Creating slots…';
  const r=await api(`/api/media/${id}/create-draft`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({accounts:chans})});
  if(r.error){prog.textContent=r.error;return;}
  const made=r.created||[];
  for(let i=0;i<made.length;i++){
    prog.textContent=`Writing ${i+1} of ${made.length}, ${made[i].channel}…`;
    const w=await api(`/api/posts/${made[i].id}/draft`,{method:'POST'});
    if(w.error) prog.textContent=`${made[i].channel}: ${w.error}`;
  }
  const skip=(r.skipped||[]).map(s=>s.channel).join(', ');
  prog.textContent=`${made.length} drafted${skip?`. Skipped: ${skip}`:''}.`;
  toast(`${made.length} draft${made.length===1?'':'s'} written`);
  box.dataset.open='0';
  await load();
}

/* Kept for the older per-slot path. */
async function mslots(id){
  const box=document.getElementById('mu-'+id);
  if(box.dataset.open==='1'){box.innerHTML='';box.dataset.open='0';return;}
  const rows=await api(`/api/media/${id}/slots`);
  box.dataset.open='1';
  if(!rows.length){
    box.innerHTML=`<div class="pick"><div class="none">No open slots take this
      yet. Set the release above, or the allocator has nothing free of this
      type.</div></div>`;
    return;
  }
  box.innerHTML=`<div class="pick">
    <div class="none">${rows.length} open slot${rows.length===1?'':'s'} could
      take this. Tick the ones you want it in.</div>
    ${rows.map(p=>`<label class="slot">
      <input type="checkbox" value="${p.id}">
      <span style="color:${cink(p.channel)}">&#9632;</span>
      <span>${p.date} ${p.time}</span>
      <span class="sub">${esc(p.channel_label.split('/')[0].trim())} ·
        ${esc(p.phase)}</span></label>`).join('')}
    <button class="act go" style="margin-top:9px;width:100%"
      onclick="mattach(${id})">Attach to selected</button></div>`;
}
async function mattach(id){
  const box=document.getElementById('mu-'+id);
  const ids=[...box.querySelectorAll('input:checked')].map(i=>+i.value);
  if(!ids.length){toast('Nothing ticked');return;}
  const r=await api(`/api/media/${id}/attach`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({posts:ids})});
  if(r.error){toast(r.error);return;}
  toast(`Attached to ${r.attached} slot${r.attached===1?'':'s'}. Ready to write.`);
  box.dataset.open='0'; load();
}
/* Zernio is the truth about what went out. This copies it back. */
async function syncZernio(){
  toast('Asking Zernio…');
  const r=await api('/api/reconcile',{method:'POST'});
  if(r.error){toast(r.error);return;}
  toast(r.matched?`${r.matched} of ${r.checked} matched up`
        :`Nothing to reconcile`);
  /* Reconcile already pulled Zernio fresh, so drop what the tabs are holding
     and let them re-read it: the server answers from what it just fetched. */
  PUB=null; FOLL=null; QUEUE=null;
  load();
}

async function delPost(id){
  const p=POSTS.find(x=>x.id===id);
  if(!confirm(`Take this off the schedule?\n\n${p.channel_label}, ${p.date} ${p.time}`
    +`\n\nThe slot goes back to being empty. Nothing is deleted at Zernio.`))return;
  const r=await fetch(`/api/posts/${id}`,{method:'DELETE'}).then(r=>r.json());
  if(r.error){toast(r.error);return;}
  toast('Off the schedule'); load();
}

async function mdetach(pid){
  await api(`/api/posts/${pid}/detach`,{method:'POST'});
  toast('Detached'); load();
}

async function mdel(id){
  const r=await fetch(`/api/media/${id}`,{method:'DELETE'}).then(r=>r.json());
  if(r.error){toast(r.error);return;}
  toast('Removed'); load();
}

/* ---- overview ---- */
/* Buckets are computed in UTC so they do not drift with the local timezone,
   and anchored to a fixed Monday so a fortnight means the same fortnight
   whatever the data happens to start on. */
const DAY=864e5, WEEK=7*DAY, MON_EPOCH=Date.UTC(2026,0,5);
const i2t=s=>{const a=s.split('-').map(Number);return Date.UTC(a[0],a[1]-1,a[2]);};
const t2i=t=>new Date(t).toISOString().slice(0,10);

function bstart(iso,per){
  if(per==='day')   return iso;
  if(per==='month') return iso.slice(0,7)+'-01';
  const t=i2t(iso), mon=t-((new Date(t).getUTCDay()+6)%7)*DAY;
  if(per==='week')  return t2i(mon);
  return t2i(MON_EPOCH+Math.floor((mon-MON_EPOCH)/WEEK/2)*2*WEEK);
}
function bnext(iso,per){
  if(per==='month'){const [y,m]=iso.split('-').map(Number);
    return t2i(Date.UTC(m===12?y+1:y,m===12?0:m,1));}
  return t2i(i2t(iso)+(per==='day'?DAY:per==='week'?WEEK:2*WEEK));
}
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const dm=iso=>{const d=new Date(i2t(iso));
  return `${d.getUTCDate()} ${MON[d.getUTCMonth()]}`;};

function blabel(iso,per){
  const d=new Date(i2t(iso));
  if(per==='month') return `${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
  return dm(iso);
}
/* What span of dates a bar actually covers, for the hover. */
function brange(iso,per){
  if(per==='day') return new Date(i2t(iso))
    .toLocaleString('en-CA',{weekday:'long',timeZone:'UTC'})+', '+dm(iso);
  if(per==='month') return new Date(i2t(iso))
    .toLocaleString('en-CA',{month:'long',year:'numeric',timeZone:'UTC'});
  return dm(iso)+' to '+dm(t2i(i2t(bnext(iso,per))-DAY));
}
/* ---- draft posts: the review queue ---- */
/* Unsaved edits survive a reload of the list, so approving one card does not
   throw away what you were typing in another. */
let DIRTY={};
const dv=(id,k,fallback)=>((DIRTY[id]||{})[k]!==undefined?DIRTY[id][k]:fallback);

function dmark(id,k,v){
  (DIRTY[id]=DIRTY[id]||{})[k]=v;
  const c=document.getElementById('d-'+id); if(c)c.classList.add('dirty');
}

/* A person is a different handle on each platform, so handles are looked up by
   the post's platform, and collaborators only where the platform allows them. */
function handlesFor(p,kind){
  const plat=(CHAN[p.channel]||{}).platform;
  if(kind==='collab' && !(PLAT[plat]||{}).collab) return [];
  if(kind==='tag' && !(PLAT[plat]||{}).tag) return [];
  return (((PROFS[p.profile]||{}).handles||{})[plat]||[])
    .filter(h=>h && h.handle && h[kind]);
}
const hlist=(p,key)=>{
  const d=DIRTY[p.id]||{};
  return d[key]!==undefined?d[key]:JSON.parse(p[key]||'[]');
};
function chips(p,key,list){
  const on=new Set(hlist(p,key));
  return list.map(h=>`<button type="button" class="chip2${on.has(h.handle)?' on':''}"
    onclick="dtoggle(${p.id},'${key}','${esc(h.handle)}',this)"
    title="${esc(h.note||h.role||'')}">${esc(h.handle)}${
      h.name?' <span style="opacity:.7">'+esc(h.name)+'</span>':''}</button>`).join('');
}
function dtoggle(id,key,handle,el){
  const p=POSTS.find(x=>x.id===id), cur=new Set(hlist(p,key));
  cur.has(handle)?cur.delete(handle):cur.add(handle);
  dmark(id,key,[...cur]);
  el.classList.toggle('on');
}

function dcard(p){
  const cp=dv(p.id,'copy',p.copy||''), fc=dv(p.id,'fc',p.first_comment||'');
  const col=handlesFor(p,'collab'), tg=handlesFor(p,'tag');
  return `<div class="dcard${DIRTY[p.id]?' dirty':''}" id="d-${p.id}">
    <div class="dhead">
      <span class="sw2" style="background:${cink(p.channel)}"></span>
      <b>${esc(p.channel_label)}</b>
      <span class="spacer"></span>
      <span class="state">${p.date} ${p.time}</span>
    </div>
    <div class="dsub">${esc(title(p.campaign))} · ${esc(p.phase)} phase ·
      ${esc(p.asset_label||'no asset')}</div>
    <textarea id="d-cp-${p.id}" oninput="dmark(${p.id},'copy',this.value);dcount(${p.id})"
      >${esc(cp)}</textarea>
    <div class="chars" id="d-cc-${p.id}"></div>
    <label>First comment</label>
    <input type="text" id="d-fc-${p.id}" value="${esc(fc)}"
      oninput="dmark(${p.id},'fc',this.value)">
    <label>Media</label>
    <select id="d-ml-${p.id}" onchange="dmark(${p.id},'ml',this.value)">${mopts(p)}</select>
    ${col.length?`<label>Collaborators</label>
      <div class="chipset">${chips(p,'collaborators',col)}</div>`:''}
    ${tg.length?`<label>Tag</label>
      <div class="chipset">${chips(p,'tags',tg)}</div>`:''}
    <div class="rowb">
      <button class="act" onclick="dsave(${p.id})">Save</button>
      <span class="spacer"></span>
      <button class="act no" onclick="ddecide(${p.id},'reject')">Reject</button>
      <button class="act go" onclick="ddecide(${p.id},'approve')">Approve</button>
    </div>
  </div>`;
}

function dcount(id){
  const p=POSTS.find(x=>x.id===id), ta=document.getElementById('d-cp-'+id),
        c=document.getElementById('d-cc-'+id);
  if(!p||!ta||!c)return;
  const n=ta.value.length, lim=p.max_chars;
  c.textContent=lim?`${n} of ${lim}`:
    `${n} characters${n>210?' · past the fold at 210':''}`;
  c.classList.toggle('over',!!lim&&n>lim);
}

function hcard(p){
  const ch=CHAN[p.channel]||{};
  return `<div class="dcard">
    <div class="dhead"><span class="sw2" style="background:${cink(p.channel)}"></span>
      <b>${esc(p.channel_label)}</b><span class="spacer"></span>
      <span class="state">${p.date} ${p.time}</span></div>
    <div class="dsub">Approved. This account is not connected, so the post goes by
      email to ${esc(ch.handoff_to&&ch.handoff_to!=='TODO'?ch.handoff_to:
      'nobody yet, set handoff_to in channels.yaml')} to be posted by hand.</div>
    <div class="rowb">
      <button class="act" onclick="hprev(${p.id})">Preview</button>
      <span class="spacer"></span>
      <button class="act go" onclick="hsend(${p.id})">Send</button>
    </div>
    <pre class="hprev" id="h-${p.id}" hidden></pre>`;
}
async function hprev(id){
  const r=await api(`/api/posts/${id}/handoff`), el=document.getElementById('h-'+id);
  el.textContent=`To: ${r.to&&r.to!=='TODO'?r.to:'(not set)'}\nSubject: ${r.subject||''}\n\n`
    +(r.body||r.error||'')+(r.media?`\n\n[attached: ${r.media}]`:'');
  el.hidden=!el.hidden;
}
async function hsend(id){
  const r=await api(`/api/posts/${id}/handoff`,{method:'POST'});
  if(r.sent){toast('Emailed to '+r.to); load(); return;}
  toast(r.error||'Could not send');
  const el=document.getElementById('h-'+id);
  el.textContent=`${r.error||''}\n\nTo: ${r.to&&r.to!=='TODO'?r.to:'(not set)'}\n`
    +`Subject: ${r.subject||''}\n\n${r.body||''}`
    +(r.media?`\n\n[attached: ${r.media}]`:'');
  el.hidden=false;
}

function rcard(p){
  const c=(p.copy||'');
  return `<div class="dcard">
    <div class="dhead"><span class="sw2" style="background:${cink(p.channel)}"></span>
      <b>${esc(p.channel_label)}</b><span class="spacer"></span>
      <span class="state">${p.date} ${p.time}</span></div>
    <div class="dsub">Approved, waiting for its slot.
      ${esc(c.slice(0,120))}${c.length>120?'…':''}</div>
    <div class="rowb">
      <span class="spacer"></span>
      <button class="act" onclick="sendPost(${p.id},false)">Schedule for its slot</button>
      <button class="act go" onclick="sendPost(${p.id},true)">Send in 2 min</button>
    </div>
    <pre class="hprev" id="s-${p.id}" hidden></pre>`;
}
async function sendPost(id,soon){
  if(LIVE && soon && !confirm('Send this in 2 minutes?\n\nIt goes to Zernio now '
    +'and fires two minutes later. You can still cancel it there within that '
    +'window.')) return;
  toast(LIVE?'Sending…':'Dry run…');
  const r=await api('/api/push',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confirm:true,id:id,soon:!!soon})});
  const el=document.getElementById('s-'+id);
  if(el){el.textContent=(r.live?'':'NOT LIVE. Nothing was sent. This is what '
    +'would have gone:\n\n')+(r.output||r.error||'');el.hidden=false;}
  if(r.ok&&r.live){toast('Scheduled'); load();}
  else if(r.ok){toast('Dry run only, the desk is not live');}
  else toast(r.error||'Did not go through, see below');
}

function gcard(g){
  const p=g[0], m=MEDIA.find(x=>x.id===p.media_id)||{};
  return `<div class="dcard" style="cursor:pointer" onclick="open_(${p.id})">
    <div class="dhead">
      ${g.map(x=>`<span class="sw2" style="background:${cink(x.channel)}"
        title="${esc(x.channel_label)}"></span>`).join('')}
      <b>${esc(m.original||p.asset_label||'asset')}</b>
      <span class="spacer"></span>
      <span class="state">${g.length} accounts</span></div>
    <div class="dsub">${g.map(x=>esc((CHAN[x.channel]||{}).label||x.channel)).join(', ')}
      · ${esc(title(p.campaign))}</div>
    <div class="dsub" style="color:var(--dim);margin-top:8px">${
      esc((p.copy||'').slice(0,150))}${(p.copy||'').length>150?'…':''}</div>
    <div class="rowb"><span class="spacer"></span>
      <button class="act">Open all ${g.length}</button></div></div>`;
}

function wcard(p){
  const m=MEDIA.find(x=>x.id===p.media_id)||{};
  return `<div class="dcard">
    <div class="dhead"><span class="sw2" style="background:${cink(p.channel)}"></span>
      <b>${esc(p.channel_label)}</b><span class="spacer"></span>
      <span class="state">${p.date} ${p.time}</span></div>
    <div class="dsub">${esc(title(p.campaign))} · ${esc(p.phase)} phase ·
      ${esc(m.original||p.asset_label||'asset')}${m.note?`. "${esc(m.note)}"`:''}</div>
    <div class="rowb">
      <button class="act" onclick="mdetach(${p.id})">Detach asset</button>
      <span class="spacer"></span>
      <button class="act" onclick="open_(${p.id})">Write it</button>
    </div></div>`;
}

function drawDraft(){
  const rows=POSTS.filter(p=>p.state==='drafted')
    .sort((a,b)=>a.date.localeCompare(b.date)||a.time.localeCompare(b.time));
  const ho=POSTS.filter(p=>p.state==='approved'&&!p.handed_off
      &&(CHAN[p.channel]||{}).delivery==='email')
    .sort((a,b)=>a.date.localeCompare(b.date));
  const rd=POSTS.filter(p=>p.state==='approved'
      &&(CHAN[p.channel]||{}).delivery!=='email')
    .sort((a,b)=>a.date.localeCompare(b.date)||a.time.localeCompare(b.time));
  const wr=POSTS.filter(p=>p.state==='allocated'&&p.media_id)
    .sort((a,b)=>a.date.localeCompare(b.date)||a.time.localeCompare(b.time));
  /* One asset sent to five accounts is one thing to review, not five. */
  const groups=[];
  const byGid={};
  rows.forEach(p=>{
    if(!p.group_id){groups.push([p]);return;}
    if(!byGid[p.group_id]){byGid[p.group_id]=[];groups.push(byGid[p.group_id]);}
    byGid[p.group_id].push(p);
  });
  const queue = rows.length ? `
    <div class="bar2"><h2>Waiting on you</h2><span class="spacer"></span>
      <span class="sub">${groups.length} to review, ${rows.length} post${
        rows.length===1?'':'s'}</span></div>
    ${groups.map(g=>g.length>1?gcard(g):dcard(g[0])).join('')}`
    : `<div class="empty"><b>Nothing waiting.</b>
       Copy that has been written but not yet approved collects here.</div>`;
  $('#draft').innerHTML = (wr.length ? `
    <div class="bar2"><h2>Has an asset, needs copy</h2>
      <span class="spacer"></span><span class="sub">${wr.length}</span></div>
    ${wr.map(wcard).join('')}` : '') + queue + (rd.length ? `
    <div class="bar2" style="margin-top:26px"><h2>Approved, ready to send</h2>
      <span class="spacer"></span><span class="sub">${rd.length}</span></div>
    ${rd.map(rcard).join('')}` : '') + (ho.length ? `
    <div class="bar2" style="margin-top:26px"><h2>Ready to email</h2>
      <span class="spacer"></span><span class="sub">${ho.length}</span></div>
    ${ho.map(hcard).join('')}` : '');
  rows.forEach(p=>dcount(p.id));
}

async function dsave(id,quiet){
  const g=k=>document.getElementById(`d-${k}-${id}`), p=POSTS.find(x=>x.id===id);
  await api(`/api/posts/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({copy:g('cp').value, first_comment:g('fc').value,
      media_id:g('ml').value?+g('ml').value:null,
      tags:hlist(p,'tags'), collaborators:hlist(p,'collaborators')})});
  delete DIRTY[id];
  if(!quiet){toast('Saved'); load();}
}
async function ddecide(id,a){
  await dsave(id,true);
  const r=await api(`/api/posts/${id}/${a}`,{method:'POST'});
  if(r.error){toast(r.error); load(); return;}
  toast(a==='approve'?'Approved':'Rejected'); load();
}

/* ---- the Zernio queue ---- */
/* Read straight from Zernio, because this desk's record and theirs can
   disagree: anything posted from their dashboard never touches desk.db, and a
   push that failed halfway leaves the two out of step. */
let QUEUE=null;

const ZSTATE={scheduled:'Scheduled', publishing:'Going out now',
  published:'Published', processing:'Processing', failed:'Failed',
  partial:'Partly failed', draft:'Draft at Zernio'};

function zwhen(iso,tz){
  if(!iso)return 'no time set';
  const d=new Date(iso);
  return d.toLocaleString('en-CA',{weekday:'short',day:'numeric',month:'short',
    hour:'2-digit',minute:'2-digit',hour12:false})+(tz?` ${tz.split('/').pop()}`:'');
}

/* Markets. One function draws the tab, sub-tabs and all, so a market with a
   problem cannot blank its neighbours. The sub-tab is a filter inside this
   panel and never touches the panel's display: setting that on a tab section
   beats [hidden] and pins the tab open. */
let MKT=null, ART={}, MKTSYM=null, MKTAB='all', MCOUNT=null, MSYMS={};
/* Not remembered. The Wire opens on All every time, because the question you
   arrive with is "what is happening", not "what was I last looking at". */
function setMktab(k){MKTAB=k;drawMarkets();}

/* Which pacing tables are folded, per tab, so a redraw keeps your choice. */
const paceOpen=k=>{try{return localStorage.getItem('desk.pace.'+k)!=='closed';}catch(e){return true;}};
function paceSave(k,open){try{localStorage.setItem('desk.pace.'+k,open?'open':'closed');}catch(e){}}
const paceHead=(k,extra='',top=20)=>`<details class="pace" ${paceOpen(k)?'open':''}
  ontoggle="paceSave('${k}',this.open)"><summary class="bar2" style="margin-top:${top}px">
  <h2 style="font-size:19px"><span class="chev" aria-hidden="true">&#9656;</span>Pacing</h2>
  <span class="spacer"></span>${extra}</summary>`;

/* ---- ads ---- */
/* The ads tab is its own shelf: its own folders (one per group of ads), its
   own pacing, its own Auto switch. The words that go out are the ad's own,
   from the manifest it was built with, so nothing is edited here but the
   on/off switch. */
let ADS=null, ADPACE=null, ADGROUP=null;
let ADSIZE=(()=>{try{return localStorage.getItem('desk.ads.size')||null;}catch(e){return null;}})();
async function drawAds(force){
  const el=$('#ads');
  if(force||!ADS){
    const [a,p]=await Promise.all([
      api('/api/ads?profile='+encodeURIComponent(PROF)),
      api('/api/ads/pacing?profile='+encodeURIComponent(PROF))]);
    if(a.error||p.error){el.innerHTML=`<div class="empty"><b>${esc(a.error||p.error)}</b></div>`;return;}
    ADS=a.ads||[]; ADPACE=p;
    $('#ac').textContent=ADS.length;
  }
  const groups=[...new Set(ADS.map(x=>x.property_name||'Ads'))];
  if(ADGROUP&&!groups.includes(ADGROUP)) ADGROUP=null;
  /* One tile per picture: an ad in three sizes is three tiles. An ad from
     before sizes were kept has just its one picture. */
  const tiles=ADS.flatMap(x=>(x.sizes&&x.sizes.length?x.sizes
      :(x.media?[{size:`${x.media.width}x${x.media.height}`,media_id:x.media.id}]:[{size:'',media_id:null}]))
    .map(sz=>({ad:x,size:sz.size,media_id:sz.media_id})));
  const sizeList=[...new Set(tiles.map(t=>t.size).filter(Boolean))]
    .sort((a,b)=>{const r=s=>{const [w,h]=s.split('x').map(Number);return w/h;};return r(b)-r(a);});
  if(ADSIZE&&!sizeList.includes(ADSIZE)) ADSIZE=null;
  const inGroup=x=>!ADGROUP||(x.property_name||'Ads')===ADGROUP;
  const shown=tiles.filter(t=>inGroup(t.ad)&&(!ADSIZE||t.size===ADSIZE));
  const count=(g,sz)=>tiles.filter(t=>(!g||(t.ad.property_name||'Ads')===g)&&(!sz||t.size===sz)).length;
  const mode=ADPACE.mode||'manual', live=(ADPACE.channels||[]);
  const plabel=c=>esc((PLAT[c]||{}).label||c);
  const fmt=t=>t?esc(String(t).replace('T',' ').slice(5,16)):'never';
  const runs=x=>Object.entries(x.runs||{}).map(([c,r])=>`${plabel(c)} ${r.n}`).join(' · ');
  const lastRun=x=>{const t=Object.values(x.runs||{}).map(r=>r.last).sort().pop();
    return t?`last ${fmt(t)}`:'not run yet';};

  el.innerHTML=`
    <div class="bar2"><h2>Ads</h2><span class="spacer"></span>
      <span class="sub">${ADS.length} ad${ADS.length===1?'':'s'}${sizeList.length>1
        ?` in ${sizeList.length} sizes, ${tiles.length} pictures`:''} ·
        ${ADS.filter(x=>x.active).length} in rotation</span>
      <span class="seg">${['manual','auto'].map(k=>`<button class="${mode===k?'on':''}"
        onclick="setAdMode('${k}')" ${k==='auto'&&!LIVE?'disabled':''}
        title="${k==='auto'&&!AUTOPOST?'Set, but nothing fires until the Auto switch in the header is on':''}"
        >${k}</button>`).join('')}</span>
      <label class="act" title="Replaces every ad with the set in the zip. You can also drop the zip anywhere on this tab."
        >Upload .zip<input type="file" accept=".zip,application/zip"
        hidden onchange="adImport([...this.files]);this.value=''"></label>
      <button class="act" onclick="drawAds(true)">Refresh</button></div>

    ${paceHead('ads','',4)}
    <div class="chars" style="margin:-6px 0 9px">${mode==='auto'
      ? (AUTOPOST?'Ads are on automatic and will post within the pacing below.'
         :'<b>Ads are set to Auto, but nothing will fire.</b> Automatic posting is switched off in the header.')
      : 'Ads are on manual. Nothing posts by itself until this is set to auto.'}
      The ad that has gone longest without running on a channel goes next;
      none repeats on a channel within ${ADPACE.repeat_days} days. Quiet hours
      ${(ADPACE.quiet_hours||[]).join(' to ')||'not set'}. From <code>channels.yaml</code>.</div>
    <table class="tbl"><thead><tr><th>Channel</th><th>Size</th><th>Per day</th><th>Spacing</th>
      <th>Today</th><th>Last ad</th><th>Free to post</th><th></th></tr></thead>
      <tbody>${live.map(c=>`<tr>
        <td>${plabel(c.channel)}</td>
        <td>${adSizePick(c)}</td><td>${c.per_day}</td><td>${c.spacing}h</td>
        <td>${c.used_today} of ${c.per_day}</td>
        <td>${c.last?`${fmt(c.last)} <span class="sub">${esc((c.last_headline||'').slice(0,40))}</span>`:'never'}</td>
        <td>${c.due?'<span class="sub">now</span>'
          :`<span class="dim warn">${esc(c.blocked)}${c.next_at&&/spacing/.test(c.blocked)?`, ${c.next_at}`:''}</span>`}</td>
        <td>${c.blocked==='not connected'||c.paused?'':`<button class="act" onclick="adFire('${c.channel}')"
          ${LIVE?'':'disabled'}>Post one now</button>`}</td></tr>`).join('')}</tbody></table></details>

    <div class="folders" style="margin:20px 0 6px">
      <button class="fold${ADGROUP===null?' on':''}" onclick="setAdGroup(null)">All
        <span class="fn">${count(null,ADSIZE)}</span></button>
      ${groups.map(g=>`<button class="fold${ADGROUP===g?' on':''}"
        onclick="setAdGroup(${esc(JSON.stringify(g))})">${esc(g)}
        <span class="fn">${count(g,ADSIZE)}</span></button>`).join('')}
    </div>
    ${sizeList.length>1?`<div class="folders" style="margin:0 0 12px" aria-label="Size">
      <span class="sub" style="align-self:center;margin-right:2px">Size</span>
      <button class="fold${ADSIZE===null?' on':''}" onclick="setAdSize2(null)">All
        <span class="fn">${count(ADGROUP,null)}</span></button>
      ${sizeList.map(sz=>`<button class="fold${ADSIZE===sz?' on':''}"
        onclick="setAdSize2('${sz}')">${sz.replace('x','×')}
        <span class="fn">${count(ADGROUP,sz)}</span></button>`).join('')}
    </div>`:''}
    ${shown.length?`<div class="adgrid">${shown.map(({ad:x,size,media_id})=>`
      <div class="adcard${x.active?'':' off'}" onclick="openAd(${x.id},'${size}')">
        ${media_id?`<div class="adpic"><img src="/media/${media_id}/raw" alt="" loading="lazy"></div>`
          :'<div class="empty" style="aspect-ratio:1/1">No picture</div>'}
        <div class="body">
          <div class="grp">${esc(x.property_name||'')} · v${x.variation??''}
            ${size?` · ${size.replace('x','×')}`:''}</div>
          <div class="hl">${esc(x.headline||'')}</div>
          ${x.sub?`<div class="sb">${esc(x.sub)}</div>`:''}
          <div class="cta"><b>${esc(x.cta||'')}</b>
            ${x.url?`<a class="adlink" href="${esc(x.url)}" target="_blank" rel="noopener"
              onclick="event.stopPropagation()"
              title="${esc(x.url)}">${esc(x.url.replace(/^https?:\/\/(www\.)?/,'').split('?')[0])}
              &#8599;</a>`:''}</div>
          <div class="runs">${runs(x)?esc(runs(x))+' · ':''}${lastRun(x)}</div>
          <div class="row" onclick="event.stopPropagation()"><label><input type="checkbox" ${x.active?'checked':''}
            onchange="adActive(${x.id},this.checked)"> In rotation${(x.sizes||[]).length>1
              ?` <span class="sub">(all ${x.sizes.length} sizes)</span>`:''}</label></div>
        </div></div>`).join('')}</div>`
      :`<div class="empty"><b>No ads yet.</b> Drag the guavy-ads folder onto this tab.</div>`}`;
  wireAdDrop();
}
function setAdGroup(g){ADGROUP=g;drawAds();}
function setAdSize2(sz){ADSIZE=sz;try{sz?localStorage.setItem('desk.ads.size',sz)
  :localStorage.removeItem('desk.ads.size');}catch(e){}drawAds();}
/* The sizes the loaded ads come in, plus the channel's own choice even when
   no ad has it, so the table says what it wants and what it is making do with. */
function adSizePick(c){
  const sizes=(ADPACE.sizes||[]).map(x=>x.size);
  const opts=sizes.includes(c.size)||!c.size?sizes:[c.size,...sizes];
  const total=ADPACE.total_ads||0;
  const note=!c.size_ads?'<div class="sub">none in this set, nearest shape used</div>'
    :c.size_ads<total?`<div class="sub">${c.size_ads} of ${total} ads; the rest use the nearest shape</div>`:'';
  return `<select onchange="setAdSize('${c.channel}',this.value)" aria-label="Ad size for ${esc(c.channel)}">
    ${opts.map(sz=>`<option value="${sz}" ${sz===c.size?'selected':''}>${sz.replace('x','×')}</option>`).join('')}
    </select>${note}`;
}
async function setAdSize(chan,size){
  const r=await api('/api/ads/size',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({channel:chan,size})});
  if(r.error){toast(r.error);return;}
  toast(`${(PLAT[chan]||{}).label||chan} ads: ${size.replace('x','×')}`); drawAds(true);
}
async function openAd(id,size){
  let m=$('#admodal');
  if(!m){ m=document.createElement('div'); m.id='admodal'; m.className='admodal';
    m.hidden=true; m.setAttribute('role','dialog'); m.setAttribute('aria-modal','true');
    m.addEventListener('click',e=>{ if(e.target===m) closeAd(); });
    document.body.appendChild(m); }
  const r=await api(`/api/ads/${id}/preview`);
  if(r.error){toast(r.error);return;}
  const a=r.ad;
  const pic=((a.sizes||[]).find(x=>x.size===size)||{}).media_id||(a.media&&a.media.id);
  m.innerHTML=`<div class="box">
    <div class="pic">${pic?`<img src="/media/${pic}/raw" alt="${esc(a.headline||'')}">`:''}</div>
    <div class="txt">
      <div class="top"><div style="flex:1">
          <div class="grp">${esc(a.property_name||'')} · v${a.variation??''}${a.audience?` · ${esc(a.audience)}`:''}</div>
          <h3>${esc(a.headline||'')}</h3></div>
        <button class="act" onclick="closeAd()" aria-label="Close">&times;</button></div>
      ${(r.channels||[]).map(c=>`<div class="post">
        <h4><span style="color:${(PLAT[c.channel]||{}).ink||'#8A8F86'}">&#9632;</span>
          ${esc((PLAT[c.channel]||{}).label||c.channel)}
          <span class="sub">${c.paused?'paused, not posting yet':c.body?`${c.chars}${c.limit?` of ${c.limit}`:''} characters`:'does not fit'}
            · ${esc((c.size||'').replace('x','×'))}${c.exact?'':` (wanted ${esc((c.wanted||'').replace('x','×'))})`}</span></h4>
        <pre>${esc(c.body||'')}</pre>
        ${c.first_comment?`<div class="fc">First comment: ${esc(c.first_comment)}</div>`:''}
      </div>`).join('')}
    </div></div>`;
  m.hidden=false;
  document.addEventListener('keydown',adEsc);
}
function closeAd(){ const m=$('#admodal'); if(m) m.hidden=true;
  document.removeEventListener('keydown',adEsc); }
function adEsc(e){ if(e.key==='Escape') closeAd(); }
async function setAdMode(m){
  const r=await api('/api/ads/mode',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
  if(r.error){toast(r.error);return;}
  toast(m==='auto'?'Ads on automatic':'Ads on manual'); drawAds(true);
}
async function adActive(id,on){
  const r=await api(`/api/ads/${id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({active:on})});
  if(r.error){toast(r.error);return;}
  const a=ADS.find(x=>x.id===id); if(a)a.active=on?1:0; drawAds();
}
async function adFire(chan){
  if(!confirm(`Post the next ad in rotation to ${(PLAT[chan]||{}).label||chan} now?`))return;
  toast('Posting an ad…');
  const r=await api('/api/ads/fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({profile:PROF,channel:chan,send:true,force:true})});
  toast(r.error||r.skipped||`Sent: ${(r.headline||'').slice(0,50)}`); drawAds(true);
}
/* A dropped folder arrives as directory entries, not files, so walk it. */
async function filesFrom(dt){
  const out=[], walk=async(entry,path)=>{
    if(entry.isFile){ await new Promise(res=>entry.file(f=>{
      out.push(new File([f],path+f.name,{type:f.type})); res();},res)); }
    else if(entry.isDirectory){
      const rd=entry.createReader(); let batch;
      do{ batch=await new Promise(res=>rd.readEntries(res,()=>res([])));
          for(const e of batch) await walk(e,path+entry.name+'/'); }while(batch.length);
    }};
  const items=[...(dt.items||[])].map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  if(!items.length) return [...(dt.files||[])];
  for(const e of items) await walk(e,'');
  return out;
}
function wireAdDrop(){
  const d=$('#ads'); if(!d||d._wired)return; d._wired=true;
  ['dragenter','dragover'].forEach(n=>d.addEventListener(n,e=>{
    if(![...(e.dataTransfer.types||[])].includes('Files'))return;
    e.preventDefault(); d.classList.add('over');}));
  d.addEventListener('dragleave',e=>{ if(!d.contains(e.relatedTarget)) d.classList.remove('over');});
  d.addEventListener('drop',async e=>{
    if(![...(e.dataTransfer.types||[])].includes('Files'))return;
    e.preventDefault(); d.classList.remove('over');
    adImport(await filesFrom(e.dataTransfer));
  });
}
/* A zip, or a loose folder of the same thing. Either replaces the whole set,
   so it asks first; the server checks the upload holds a manifest before it
   removes anything. */
async function adImport(files){
  const keep=files.filter(f=>/\.(zip|json|png|jpe?g|webp)$/i.test(f.name));
  const zip=keep.find(f=>/\.zip$/i.test(f.name));
  if(!zip&&!keep.some(f=>/\.json$/i.test(f.name))){
    toast('Drop the ads .zip (it needs guavy-ads.json inside)');return;}
  const n=(ADS||[]).length;
  if(n&&!confirm(`Replace all ${n} ads with the set in ${zip?zip.name:'this folder'}?`))return;
  toast('Reading the ads…');
  const fd=new FormData(); fd.append('profile',PROF);
  (zip?[zip]:keep).forEach(f=>fd.append('files',f,f.name));
  const r=await api('/api/ads/import',{method:'POST',body:fd});
  if(r.error){toast(r.error);return;}
  toast(`${r.added} ads loaded`+(r.replaced?`, replacing ${r.replaced}`:'')
    +(r.missing&&r.missing.length?`. ${r.missing.length} without a picture`:''));
  ADS=null; ADGROUP=null; await load(); drawAds(true);
}

async function drawMarkets(force){
  const el=$('#mkt');
  if(MKT===null||force){
    el.innerHTML=`<div class="empty">Reading the Wire…</div>`;
    MKT=await api('/api/markets?profile='+encodeURIComponent(PROF));
  }
  if(MKT.error){
    el.innerHTML=`<div class="bar2"><h2>Wire</h2><span class="spacer"></span>
      <button class="act" onclick="drawMarkets(true)">Try again</button></div>
      <div class="empty"><b>Could not read the Wire.</b>${esc(MKT.error)}</div>`;
    return;
  }
  const mk=MKT.markets||[], live=MKT.connected;
  const on=mk.filter(m=>m.mode==='auto').length;

  const tabs=`<div class="seg mktabs">
    <button class="${MKTAB==='all'?'on':''}" onclick="setMktab('all')">All</button>
    ${mk.map(m=>`<button class="${MKTAB===m.key?'on':''}"
      onclick="setMktab('${m.key}')">${esc(m.label)}</button>`).join('')}</div>`;

  const shown=MKTAB==='all'?mk:mk.filter(m=>m.key===MKTAB);

  const head=`<div class="bar2"><h2>Wire</h2><span class="spacer"></span>
      <span class="sub">${MCOUNT&&MCOUNT.held
        ? `${MCOUNT.held} held${MCOUNT.synced
            ? `, synced ${esc(String(MCOUNT.synced).replace('T',' ').slice(5,16))}`:''}`
        : (live?`${on} of ${mk.length} on automatic`:'not connected')}</span>
      <button class="act" onclick="syncWire(this)" ${live?'':'disabled'}
        >Sync the Wire</button></div>
    ${tabs}
    ${live?'':`<div class="empty" style="margin-bottom:16px">
      <b>No Guavy key yet.</b> The desk cannot read the Wire until
      <code>GUAVY_API_KEY</code> is in <code>.env</code>. Everything below is
      configured and waiting on it.</div>`}`;

  const cards=`${shown.map(m=>`<div class="idea">
      <div class="dhead"><b>${esc(m.label)}</b><span class="spacer"></span>
        <span class="dim${MCOUNT&&MCOUNT[m.key]?'':' quiet'}"
          title="Articles on the Wire in the last 24 hours${
            MCOUNT&&MCOUNT.capped&&MCOUNT.capped[m.key]
              ? '. At least this many: the count stopped at the read depth.':''}"
          ><b>24h</b>${MCOUNT?((MCOUNT.counts||{})[m.key]??0):'&hellip;'}${
          MCOUNT&&MCOUNT.capped&&MCOUNT.capped[m.key]?'+':''}</span>
        <span class="seg">${['manual','auto'].map(k=>`<button
          class="${m.mode===k?'on':''}" onclick="setMode('${m.key}','${k}')"
          title="${k==='auto'&&!AUTOPOST
            ? 'Set, but nothing fires until the Auto switch in the header is on'
            : ''}"
          ${k==='auto'&&!live?'disabled':''}>${k}</button>`).join('')}</span>
      </div>
      <div class="dsub">Guavy calls this market <code>${esc(m.api)}</code></div>
      <div class="rowb" style="margin-top:10px">
        <button class="act" onclick="getArticle('${m.key}',this)"
          ${live?'':'disabled'}>${ART[m.key]?'Find another article'
            :'Find best article'}</button>
        <button class="act" onclick="postFromArticle('${m.key}')"
          ${live&&ART[m.key]?'':'disabled'}>Post from article</button>
        <span class="chars" style="flex:1">${ART[m.key]
          ? '' : 'Nothing picked yet. The article already exists on the Wire.'}</span>
      </div>
      ${ART[m.key]?dimensions(ART[m.key]):''}
      <div class="rowb" style="margin-top:9px">
        <button class="linky2" onclick="toggleSyms('${m.key}')"
          >${MSYMS[m.key]==='open'?'Hide ticker symbols':'[Ticker Symbols]'}</button>
        <span class="chars" style="flex:1"></span>
      </div>
      ${symbolTable(m.key)}
      ${ART[m.key]?`<div class="quote" style="margin-top:9px"
        ><b>${esc(ART[m.key].title)}</b><br>${esc(ART[m.key].body||'')}
        ${ART[m.key].url?`<br><a class="linky" href="${esc(ART[m.key].url)}"
          target="_blank" rel="noopener">${esc(ART[m.key].url)}</a>`
         :`<br><span class="qn">The Wire gave no link for this one.</span>`}
        </div>`:''}
    </div>`).join('')}`;

  const pacing=`${paceHead('wire',
      MKT.in_quiet?`<span class="dim warn"><b>now</b>quiet hours</span>`:'')}
    ${on&&!AUTOPOST?`<div class="chars" style="margin:-6px 0 12px">
      <b>${on} market${on===1?' is':'s are'} set to Auto, but nothing will
      fire.</b> Automatic posting is switched off in the header.</div>`:''}
    ${AUTOPOST?`<div class="chars" style="margin:-6px 0 12px">Automatic posting
      is armed. ${on?`${on} market${on===1?'':'s'} will post without being
      asked, within the pacing below.`:`No market is on Auto, so nothing will
      fire yet.`}</div>`:''}
    <div class="chars" style="margin:-4px 0 9px">${MKTAB==='all'
      ? `Every market carries the same allowance on a channel and keeps its own
         timer, so these totals are the four markets added together: one a day
         each is four a day on that account.`
      : `${esc((MKT.markets.find(x=>x.key===MKTAB)||{}).label||MKTAB)} only.
         Each of the other markets has the same allowance again, on its own
         timer.`}
      From <code>channels.yaml</code>. Quiet hours
      ${(MKT.quiet_hours||[]).join(' to ')||'not set'}.</div>
    <table class="tbl"><thead><tr><th>Channel</th><th>No sooner than</th>
      <th>${MKTAB==='all'?'Today, all markets':'Today'}</th>
      <th>Min clout</th><th>Last post</th><th>Free to post</th></tr></thead>
      <tbody>${(MKT.channels||[]).map(c=>{
        const all=MKTAB==='all', tot=c.total||{}, one=(c.per_market||{})[MKTAB]||{};
        const used=all?tot.used_today:one.used_today;
        const cap=all?tot.per_day:c.per_day;
        const blocked=all?tot.blocked:one.blocked;
        const split=all?Object.entries(c.per_market||{})
          .filter(([,v])=>v.used_today).map(([k,v])=>`${k} ${v.used_today}`)
          .join(', '):'';
        return `<tr>
        <td>${esc(c.channel)}</td>
        <td>${c.min_hours}h${!all&&one.hours_since!=null
          ? ` <span class="sub">(${one.hours_since}h ago)</span>`:''}</td>
        <td>${used} of ${cap}${split?` <span class="sub">(${esc(split)})</span>`:''}
          ${all?` <span class="sub">= ${c.per_day} &times; ${tot.markets}</span>`:''}</td>
        <td>${c.min_clout}</td>
        <td>${all
          ? (tot.free_markets===tot.markets?'<span class="sub">none yet</span>'
             :`<span class="sub">${tot.markets-tot.free_markets} waiting</span>`)
          : (one.last?esc(one.last):'never')}</td>
        <td>${blocked
          ? `<span class="dim warn">${esc(blocked)}${one.ready_at&&!all
              &&String(blocked).startsWith('too soon')
              ? ` until ${esc(one.ready_at)}`:''}</span>`
          : `<span class="dim hot">${all
              ? `${tot.free_markets} of ${tot.markets} markets`:'yes'}</span>`}</td>
        </tr>`;}).join('')}
      </tbody></table></details>`;

  /* On All, pacing leads: the question there is whether anything may go out at
     all, and four market cards in front of it is four scrolls of nothing. On a
     single market the card is the thing you came for, so it keeps the top. */
  el.innerHTML = MKTAB==='all' ? head+pacing+cards : head+cards+pacing;
  if(live) loadCounts();
}

/* Every score the Wire put on this article. Two of them decided the pick, so
   the rest are how you judge whether it was a good one: a shipbuilding story
   scored against natural gas reads very differently once you can see that its
   impacted assets are thin. */
function dimensions(a){
  const n=v=>(v===null||v===undefined||v==='')?'—':String(v);
  const sent=Number(a.sentiment);
  const tone=isNaN(sent)?'':(sent>0?'pos':sent<0?'neg':'flat');
  const cell=(k,v,cls)=>`<span class="dim${cls?' '+cls:''}"><b>${k}</b>${esc(n(v))}</span>`;
  return `<div class="dims">
    ${cell('sentiment', a.sentiment, sent>=3?'hot':sent<=-3?'cold':'')}
    ${tone?`<span class="dim"><b>direction</b>${tone}</span>`:''}
    ${cell('clout', a.clout)}
    ${cell('weight', a.weight)}
    ${cell('speculation', a.speculation)}
    ${cell('fud/fomo', a.fud_fomo)}
    ${cell('bias', a.fud_fomo_bias)}
    ${cell('tone', a.tone)}
    ${cell('date', a.date)}
    ${cell('instrument', a.symbol)}
    ${a.found_under&&a.found_under!==a.symbol
      ? cell('found under', a.found_under, 'warn') : ''}
    ${a.scope?'':`<span class="dim warn"><b>brand</b>no scope</span>`}
    ${(a.symbols||[]).length?`<span class="dim"><b>symbols</b>${
      esc((a.symbols||[]).join(' '))}</span>`:''}
    ${(a.impacted||[]).length?`<span class="dim"><b>impacted</b>${
      esc((a.impacted||[]).map(x=>x.direction
        ? `${x.asset} ${x.direction}${x.confidence!=null?'/'+x.confidence:''}`
        : x.asset).join(', '))}</span>`:''}
  </div>`;
}

/* The most important thing on the Wire since this profile last posted from
   Markets. Held per market so the four sub-tabs can each have one waiting. */
/* Everything already offered on a card, articles and tickers both, so "Find
   another article" means another story, not the same one from another outlet.
   Cleared when the page reloads. */
const SHOWN={};
async function getArticle(market,btn){
  if(btn){btn.disabled=true;btn.textContent='Reading the Wire…';}
  const seen=SHOWN[market]||(SHOWN[market]={ids:[],syms:[]});
  const now=ART[market];
  if(now){
    if(now.article_id&&!seen.ids.includes(now.article_id))seen.ids.push(now.article_id);
    if(now.symbol&&!seen.syms.includes(now.symbol))seen.syms.push(now.symbol);
  }
  const r=await api(`/api/markets/${market}/article`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({profile:PROF, exclude:seen.ids, exclude_symbols:seen.syms})});
  if(r.error){toast(r.error);MKT=null;await drawMarkets();return;}
  ART[market]=r.article;
  await drawMarkets();
  toast(`Picked: ${r.article.title.slice(0,50)}`);
}

/* Straight into the ordinary compose sheet, so a markets post is approved,
   scheduled and pushed exactly like every other post. */
function postFromArticle(market){
  const a=ART[market]; if(!a){toast('Generate an article first');return;}
  SEL=[]; BSRC=[];
  /* The headline is on the graphic, so repeating it as the first line of the
     post reads as a stutter. The copy is the body; the title still reaches the
     picture through BTITLE. */
  BSEED={copy:a.body||'',
         links:a.url?[a.url]:[],
         urlShort:a.url_short||'',
         title:a.title||'',
         fanout:true};
  BTITLE=a.title||'';
  /* The instrument travels with the post. Create image reads it to pick the
     brand scope, so a BTC story is drawn in Bitcoin's palette rather than the
     house teal. */
  /* The strongest impact the Wire named, which is what the direction and
     confidence on the graphic refer to. */
  const hit=(a.impacted||[]).filter(x=>x.direction)
    .sort((x,y)=>(y.confidence||0)-(x.confidence||0))[0]||{};
  MKSCORE={sentiment:a.sentiment, clout:a.clout,
           direction:hit.direction||'', confidence:hit.confidence,
           article_id:a.article_id||''};
  openCompose({market:a.market||market, symbol:a.symbol||'', scope:!!a.scope});
}

/* The tickers in a market, opened on demand: it asks the Wire once per symbol
   and there is no reason to spend that until you want to look. */
async function toggleSyms(market){
  if(MSYMS[market]==='open'){ delete MSYMS[market]; drawMarkets(); return; }
  MSYMS[market]='loading'; drawMarkets();
  const r=await api(`/api/markets/${market}/symbols`);
  if(r.error){ delete MSYMS[market]; toast(r.error); drawMarkets(); return; }
  MSYMS[market]='open'; MSYMS[market+':rows']=r.symbols||[];
  drawMarkets();
}

function symbolTable(market){
  if(MSYMS[market]==='loading')
    return `<div class="chars" style="margin:6px 0">Asking the Wire&hellip;</div>`;
  if(MSYMS[market]!=='open') return '';
  const rows=MSYMS[market+':rows']||[];
  if(!rows.length) return `<div class="chars">No tickers came back.</div>`;
  return `<table class="tbl symtab"><thead><tr>
      <th>Ticker</th><th>Name</th><th>Brand scope</th>
      <th style="width:70px">24h</th></tr></thead><tbody>
    ${rows.map(s=>`<tr>
      <td>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener"
        title="${esc(s.latest||'Open the newest article')}">${esc(s.symbol)}</a>`
        :esc(s.symbol)}</td>
      <td>${esc(s.name)}</td>
      <td>${s.has_scope
        ? `<span class="scope" onclick="copyScope('${esc(market)}','${esc(s.symbol)}')"
             title="Click to copy">${esc(s.scope.slice(0,52))}${
             s.scope.length>52?'…':''}<span class="spop">${esc(s.scope)}
             <span class="vhint">click to copy</span></span></span>`
        : `<span class="dim warn">none</span>`}</td>
      <td>${s.count24?`<span class="dim hot" title="${s.capped
        ?'At least this many':'In the last 24 hours'}">${s.count24}${
        s.capped?'+':''}</span>`:`<span class="sub">0</span>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

async function copyScope(market,symbol){
  const row=(MSYMS[market+':rows']||[]).find(x=>x.symbol===symbol);
  if(!row) return;
  try{ await navigator.clipboard.writeText(row.scope); toast('Scope copied'); }
  catch(_){ toast('Could not copy'); }
}

/* Top up the local copy. Cheap after the first run: each symbol is read until
   it meets an article already held, which is usually within one page. */
async function syncWire(btn){
  const tick=btn?startTimer(btn,'Syncing'):null;
  const r=await api('/api/wire/sync',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  if(tick) tick.stop('Sync the Wire');
  if(r.error){toast(r.error);return;}
  const n=Object.values(r.added||{}).reduce((a,b)=>a+b,0);
  MCOUNT=null; MSYMS={}; await drawMarkets();
  toast(`${n} new, ${r.held} held, ${r.seconds}s`);
}

/* The per-market counts, after the tab has painted. Asking every symbol in
   every market takes seconds, and the tab should not wait for it. */
async function loadCounts(){
  if(MCOUNT) return;
  const r=await api('/api/markets/counts');
  if(r&&r.counts){ MCOUNT=r; drawMarkets(); }
}

async function setMode(market,mode){
  const r=await api(`/api/markets/${market}/mode`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode})});
  if(r.error){toast(r.error);return;}
  MKT=null; await drawMarkets();
  toast(`${market} is ${mode}`);
}

/* Blogs. The whole tab is drawn here, so one bad row cannot blank it.

   A blog behaves like an upload: same card, same grid. What it adds is how far
   it has got. Bright green means a post carrying it is scheduled, blue means
   one is published, and a blog the claims rules will not let us quote is
   dimmed with the reason on the card rather than hidden, because a blog that
   silently never appears looks like a broken feed. */
let BLOGS=null, BSEL=[];
async function drawBlogs(force){
  const el=$('#blog');
  if(BLOGS===null||force){
    el.innerHTML=`<div class="empty">Reading the blog index…</div>`;
    BSEL=[];
    BLOGS=await api('/api/blogs?profile='+encodeURIComponent(PROF));
  }
  if(BLOGS.error){
    el.innerHTML=`<div class="bar2"><h2>Blogs</h2><span class="spacer"></span>
      <button class="act" onclick="drawBlogs(true)">Try again</button></div>
      <div class="empty"><b>Could not read the blog index.</b>${esc(BLOGS.error)}</div>`;
    return;
  }
  const rows=BLOGS.blogs||[];
  $('#bc').textContent=rows.length;
  const n=s=>rows.filter(b=>b.state===s).length;
  const bar=`<div class="bar2 bloghead"><h2>Blogs</h2><span class="spacer"></span>
    <span class="sub">${rows.length} from the feed${
      n('scheduled')?` · ${n('scheduled')} scheduled`:''}${
      n('published')?` · ${n('published')} published`:''}</span>
    ${BSEL.length?`<button class="act on" onclick="postBlogs()">Post ${
      BSEL.length} blog${BSEL.length===1?'':'s'}</button>`:''}
    <button class="act" onclick="refreshBlogs(this)">Re-read feed</button></div>`;
  if(!rows.length){
    el.innerHTML=bar+`<div class="empty"><b>No blogs yet.</b>
      Press Re-read feed to pull them from guavy.com.</div>`;
    return;
  }
  el.innerHTML=bar+`<div class="grid">`+rows.map(b=>{
    const cls=b.state==='published'?'pub':b.state==='scheduled'?'sched':'';
    const badge=b.state==='published'?`Published · ${b.posts} post${b.posts===1?'':'s'}`
      :b.state==='scheduled'?`Scheduled · ${b.posts} post${b.posts===1?'':'s'}`:'';
    const sel=BSEL.includes(b.id);
    /* Blocked blogs do not select. Allow it first, deliberately, and it
       becomes selectable like any other. */
    return `<div class="mi ${cls}${b.postable?'':' blocked'}${sel?' sel':''}"
      ${b.postable?`onclick="pickBlog(${b.id})"`:''}>
      <span class="th">${
        b.image?`<img src="${esc(b.image)}" alt="" loading="lazy">`
               :`<span class="note">¶</span>`}${
        b.postable?`<span class="tick">${sel?'✓':''}</span>`:''}</span>
      <div class="b">
        <div class="n" title="${esc(b.title)}">${esc(b.title)}</div>
        <div class="dt">${esc(b.published||'')}</div>
        ${badge?`<div class="badge">${badge}</div>`:''}
        ${b.summary?`<div class="sum">${esc(b.summary)}</div>`:''}
        ${b.postable?'':`<div class="whynot">Not postable.${
          b.why_not?' '+esc(b.why_not):''}</div>`}
        <a class="linky" href="${esc(b.url)}" target="_blank" rel="noopener"
           onclick="event.stopPropagation()"
           title="Read it on guavy.com">${esc(b.url.replace(/^https?:\/\//,''))}</a>
        <div class="acts">${b.postable
          ? `<button class="linky2" onclick="event.stopPropagation();blockBlog(${b.id})">Block</button>`
          : `<button class="act" onclick="allowBlog(${b.id})">Allow this</button>`}
        </div>
      </div></div>`;}).join('')+`</div>`;
}

/* Selection, the same gesture as the media grid: click the card, click again
   to drop it. The link inside stops propagation so reading a blog is never
   mistaken for choosing one. */
function pickBlog(id){
  const i=BSEL.indexOf(id);
  if(i<0) BSEL.push(id); else BSEL.splice(i,1);
  drawBlogs();
}

async function refreshBlogs(btn){
  if(btn){btn.disabled=true;btn.textContent='Reading…';}
  const r=await api('/api/blogs/refresh',{method:'POST'});
  if(r.error) toast(r.error);
  else toast(`${r.added} new, ${r.updated} updated`);
  BLOGS=null; await drawBlogs(true);
}

async function postBlogs(){
  if(!BSEL.length) return;
  const picked=BSEL.map(id=>(BLOGS.blogs||[]).find(x=>x.id===id)).filter(Boolean);
  if(!picked.length) return;
  toast('Fetching…');
  /* A blog with a hero becomes a library row, so from here on it is an
     upload like any other. One without composes bare, and Create image in
     the sheet draws the graphic from whatever copy you settle on. */
  const ids=[]; let bare=0;
  for(const b of picked){
    const r=await api(`/api/blogs/${b.id}/media`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profile:PROF})});
    if(r.error){toast(r.error);return;}
    if(r.media) ids.push(r.media.id); else bare++;
  }
  await load();                    // so both rows resolve against fresh MEDIA
  BSRC=ids; SEL=[];                // a hero is artwork: render it to post it
  /* The headline goes in front of the summary. The title drawn on the picture
     is the copy's first line, so seeding it this way makes the headline the
     title without breaking the rule that whatever is on the image is also in
     the post. */
  BSEED={copy:picked.map(b=>[(b.title||'').trim(),(b.summary||'').trim()]
                            .filter(Boolean).join('\n\n'))
                    .filter(Boolean).join('\n\n'),
         links:picked.map(b=>b.url).filter(Boolean)};
  BTITLE=picked.map(b=>b.title).filter(Boolean).join(' / ');
  openCompose();
  if(bare) toast(`${bare} blog${bare===1?' has':'s have'} no picture. `
    +`Use Create image.`);
}

/* paintCompose() rebuilds the whole sheet, which throws away focus and the
   caret. Anything slow enough that you would keep typing through it has to
   put them back, or a generation eats the sentence you were in the middle
   of. */
function repaintCompose(){
  const a=document.activeElement;
  const id=a&&a.id, s=a&&a.selectionStart, e=a&&a.selectionEnd;
  paintCompose();
  if(!id) return;
  const back=document.getElementById(id);
  if(!back) return;
  back.focus();
  if(s!=null&&back.setSelectionRange){ try{back.setSelectionRange(s,e);}catch(_){} }
}

/* Draw the copy onto a card, both shapes, and add them to this post. */
async function drawCards(btn){
  const text=(CDRAFT||'').trim();
  if(!text){toast('Write the copy first');return;}
  const tick=btn?startTimer(btn,'Drawing'):null;
  const r=await api('/api/image/generate',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({profile:PROF,copy:text,title:BTITLE||'',
                         theme:THEME,
                         market:(MKTSYM||{}).market||'',
                         symbol:(MKTSYM||{}).symbol||''})});
  if(r.error){toast(r.error);
    if(tick) tick.stop('Create image');
    return;}
  if(tick) tick.stop();
  await load();
  /* Portrait first: it is the shape every vertical channel wants, and the
     first file is the one a single-image post uses. */
  BSRC=(r.media||[]).sort((a,b)=>(a.shape==='portrait'?-1:1)).map(m=>m.id);
  repaintCompose();
  /* Straight on into the render: artwork with no title on it is never the
     thing you wanted, so making you press a second button is just a step. */
  if(titleText()) await renderPost();
  else toast(`${BSRC.length} drawn. Write the copy and they will be titled.`);
}

/* Allowing and blocking are separate named actions, never one toggle. A toggle
   means the same click does opposite things depending on state you cannot see,
   which is how a blog ends up blocked with the reason "Allow". */
function allowBlog(id){
  const b=(BLOGS.blogs||[]).find(x=>x.id===id); if(!b) return;
  setPostable(id,true,'',`Allowed. ${b.title.slice(0,40)}`);
}

function blockBlog(id){
  const b=(BLOGS.blogs||[]).find(x=>x.id===id); if(!b) return;
  const why=prompt(`Block "${b.title}"?\n\n`
    +`It stays in the list, dimmed, with your reason on the card.\n`
    +`Say why, or leave empty to cancel:`,'');
  if(why===null||!why.trim()) return;   // empty is a cancel, never a silent block
  setPostable(id,false,why.trim(),'Blocked');
}

function setPostable(id,postable,why_not,msg){
  api(`/api/blogs/${id}/postable`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({postable,why_not})})
    .then(()=>{BLOGS=null;drawBlogs(true);toast(msg);});
}

async function drawQueue(force){
  const el=$('#zq');
  if(QUEUE===null||force){
    el.innerHTML=`<div class="empty">Asking Zernio…</div>`;
    QUEUE=await api('/api/zernio/queue?profile='+encodeURIComponent(PROF));
  }
  if(QUEUE.error){
    el.innerHTML=`<div class="bar2"><h2>Zernio</h2><span class="spacer"></span>
      <button class="act" onclick="drawQueue(true)">Try again</button></div>
      <div class="empty"><b>Could not reach Zernio.</b>${esc(QUEUE.error)}</div>`;
    return;
  }
  const rows=QUEUE.posts||[];
  const bar=`<div class="bar2"><h2>At Zernio</h2><span class="spacer"></span>
    <span class="sub">${rows.length} of ${QUEUE.total||rows.length}</span>
    <button class="act" onclick="drawQueue(true)">Refresh</button></div>`;
  if(!rows.length){el.innerHTML=bar+
    `<div class="empty"><b>Nothing at Zernio.</b>
      Anything you publish from here will show up in this list.</div>`;return;}

  el.innerHTML=bar+rows.map(p=>{
    const st=(p.status||'').toLowerCase();
    const cls=st==='failed'||st==='partial'?'no':st==='published'?'ok':'';
    return `<div class="zq ${cls}">
      <div class="dhead">
        <span class="state">${esc(ZSTATE[st]||p.status||'?')}</span>
        <b>${esc(zwhen(p.scheduled,p.tz))}</b>
        <span class="spacer"></span>
        ${p.media.length?`<span class="sub">${p.kinds[0]||'media'}</span>`:''}
      </div>
      <div class="zbody">
        ${p.media.length&&p.kinds[0]==='video'
          ?`<video class="zthumb" src="${esc(p.media[0])}" preload="metadata" muted></video>`
          :p.media.length?`<img class="zthumb" src="${esc(p.media[0])}" alt="">`:''}
        <div class="ztext">${esc((p.content||'').slice(0,320))}${
          (p.content||'').length>320?'…':''}</div>
      </div>
      <div class="zplats">${p.platforms.map(x=>`
        <span class="zp" title="${esc(x.error||'')}">
          <b style="background:${(PLAT[x.platform]||{}).ink||'#8A8F86'}"></b>
          ${esc((PLAT[x.platform]||{}).label||x.platform)}
          <span class="sub">${esc(x.name)}</span>
          <span class="sub">${esc(ZSTATE[(x.status||'').toLowerCase()]||x.status||'')}</span>
          ${x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener">open</a>`:''}
        </span>`).join('')}</div>
      ${p.platforms.some(x=>x.error)?`<div class="warn">${
        esc(p.platforms.filter(x=>x.error).map(x=>x.platform+': '+x.error).join(' · '))
      }</div>`:''}
    </div>`;
  }).join('');
}

/* ---- what actually went out, and how it did ---- */
let PUB=null, POSTEDROWS=[];
const num=n=>n===null||n===undefined?'-':n>=1000?(n/1000).toFixed(1)+'k':String(n);
/* Rounded is fine in a crowded table cell. A headline number should be the
   number. */
const exact=n=>n===null||n===undefined?'-':Number(n).toLocaleString('en-CA');

let PFILTER=null, FOLL=null, PPERIOD=localStorage.getItem('desk.pperiod')||'week';
let PFROM=null, PTO=null;
/* The earliest post Zernio holds. Replaced from the data on first load. */
let FLOOR='2026-06-01';
/* Where both ranges open. Earlier is still selectable, back to FLOOR, but the
   months Zernio backfilled before Guavy posted from here are not the story. */
const START='2026-09-01';
const dflt=()=>FLOOR>START?FLOOR:START;
function setRange(){
  PFROM=$('#pfrom').value||FLOOR; PTO=$('#pto').value||TODAY(); drawPublished();
}
function resetRange(){ PFROM=dflt(); PTO=TODAY(); drawPublished(); }
const pkey=r=>r.platform+'|'+(r.account||'');

/* The timeline is drawn on both Published and Numbers, so its controls redraw
   whichever of the two is open. Each used to redraw only one of them, which
   left Day, Week and Month dead on Numbers and the metric dead on Published. */
function redrawTimeline(){
  const open=document.querySelector('nav button[aria-selected=true]');
  if(open&&open.dataset.t==='foll') drawFollowers(); else drawPublished();
}
function setPPeriod(p){PPERIOD=p;localStorage.setItem('desk.pperiod',p);redrawTimeline();}
function togglePF(k){PFILTER.has(k)?PFILTER.delete(k):PFILTER.add(k);drawPublished();}
function allPF(on){PFILTER=on?new Set((PUB.posts||[]).map(pkey)):new Set();drawPublished();}

async function drawPublished(force){
  const el=$('#done');
  if(PUB===null||force){
    el.innerHTML=`<div class="empty">Asking Zernio…</div>`;
    const q='?profile='+encodeURIComponent(PROF)+(force?'&fresh=1':'');
    /* Both are fetched before either is published to the globals. Assigning
       PUB and then awaiting again left a gap in which changing profile set it
       back to null, and the next line read it: the tab threw and drew
       nothing. */
    const [pub,foll]=[await api('/api/zernio/published'+q),
                      await api('/api/followers'+q)];
    if(PROF!==decodeURIComponent(q.slice(9).split('&')[0])) return;  /* moved on */
    PUB=pub||{error:'No answer from Zernio.'}; FOLL=foll;
    if(PUB.earliest){FLOOR=PUB.earliest; if(PFROM&&PFROM<FLOOR)PFROM=FLOOR;}
  }
  if(!PUB) return drawPublished(true);
  if(PUB.error){
    el.innerHTML=`<div class="bar2"><h2>Published</h2><span class="spacer"></span>
      <button class="act" onclick="drawPublished(true)">Try again</button></div>
      <div class="empty"><b>Could not reach Zernio.</b>${esc(PUB.error)}</div>`;return;
  }
  const every=(PUB.posts||[]).filter(r=>(r.when||'')>=FLOOR);
  if(PFILTER===null) PFILTER=new Set(every.map(pkey));
  if(PFROM===null){PFROM=dflt();PTO=TODAY();}
  const inRange=r=>{const d=(r.when||'').slice(0,10);
    return d>=PFROM&&d<=PTO;};
  const rows=every.filter(r=>PFILTER.has(pkey(r))&&inRange(r));
  POSTEDROWS=rows;

  /* one chip per account that has published something */
  const seen=new Set(), accts=[];
  every.forEach(r=>{const k=pkey(r); if(seen.has(k))return; seen.add(k);
    accts.push({key:k, platform:r.platform, account:r.account,
      n:every.filter(x=>pkey(x)===k).length});});
  accts.sort((a,b)=>(a.account||'').localeCompare(b.account||'')
    ||a.platform.localeCompare(b.platform));

  const tot=k=>rows.reduce((a,r)=>a+(r[k]||0),0);
  const bar=`<div class="bar2"><h2>Published</h2><span class="spacer"></span>
    <span class="sub">${rows.length} of ${every.length}${
      PUB.age==null?'':` &middot; asked Zernio ${PUB.age<5?'just now'
        :PUB.age<90?PUB.age+'s ago':Math.round(PUB.age/60)+'m ago'}`}</span>
    <button class="act" onclick="drawPublished(true)">Refresh</button>
    ${rows.length?`<button class="act" onclick="copyLinks()">Copy all links</button>`:''}
    </div>`;

  /* Sits directly above the table head, and both stick as you scroll. */
  const filters=`<div class="pstick">
    <div class="filters">
      ${accts.map(a=>`<label class="fchip${PFILTER.has(a.key)?' on':''}">
        <input type="checkbox" ${PFILTER.has(a.key)?'checked':''}
          onchange="togglePF('${a.key.replace(/'/g,"\\'")}')">
        <span style="color:${(PLAT[a.platform]||{}).ink||'#8A8F86'}">&#9632;</span>
        ${esc((PLAT[a.platform]||{}).label||a.platform)}
        <span class="sub">${esc(a.account)}</span>
        <span class="fn">${a.n}</span></label>`).join('')}
      ${accts.length>1?`<button class="act" onclick="allPF(${
        PFILTER.size<accts.length})">${
        PFILTER.size<accts.length?'All':'None'}</button>`:''}
      <span class="spacer"></span>
      <span class="dates">
        <input type="date" id="pfrom" value="${PFROM}" min="${FLOOR}"
          max="${TODAY()}" onchange="setRange()">
        <span class="sub">to</span>
        <input type="date" id="pto" value="${PTO}" min="${FLOOR}"
          max="${TODAY()}" onchange="setRange()">
        ${(PFROM!==dflt()||PTO!==TODAY())?
          `<button class="act" onclick="resetRange()">Reset</button>`:''}
      </span>
    </div>
  </div>`;

  el.innerHTML=bar+filters+
    (rows.length?pubTable(rows):
     `<div class="empty"><b>${every.length?'Nothing in that range, or nothing '
       +'matches those filters.':'Nothing has gone out yet.'}</b></div>`);
  stickHead();
}

/* Followers, how the posting has been paced, and what it earned. Kept apart
   from Published because that tab is a working list and this one is a read. */
let FFROM=null, FTO=null, FACCTS=null;
function toggleFAcct(id){
  FACCTS.has(id)?FACCTS.delete(id):FACCTS.add(id); drawFollowers();
}
function allFAccts(){
  FACCTS=new Set((PUB.posts||[]).map(r=>r.account_id).filter(Boolean));
  drawFollowers();
}
function setFRange(){
  FFROM=$('#ffrom').value||FLOOR; FTO=$('#fto').value||TODAY(); drawFollowers();
}
function resetFRange(){ FFROM=dflt(); FTO=TODAY(); drawFollowers(); }

async function drawFollowers(force){
  const el=$('#foll');
  if(PUB===null||FOLL===null||force){
    el.innerHTML=`<div class="empty">Asking Zernio…</div>`;
    const q='?profile='+encodeURIComponent(PROF)+(force?'&fresh=1':'');
    const [pub,foll]=[await api('/api/zernio/published'+q),
                      await api('/api/followers'+q)];
    if(PROF!==decodeURIComponent(q.slice(9).split('&')[0])) return;  /* moved on */
    PUB=pub||{error:'No answer from Zernio.'}; FOLL=foll;
    if(PUB.earliest){FLOOR=PUB.earliest; if(FFROM&&FFROM<FLOOR)FFROM=FLOOR;}
  }
  if(!PUB||!FOLL) return drawFollowers(true);
  if(FOLL.error||PUB.error){
    el.innerHTML=`<div class="bar2"><h2>Followers</h2><span class="spacer"></span>
      <button class="act" onclick="drawFollowers(true)">Try again</button></div>
      <div class="empty"><b>Could not reach Zernio.</b>
        ${esc(FOLL.error||PUB.error)}</div>`;return;
  }
  if(FFROM===null){FFROM=dflt();FTO=TODAY();}
  const all=(PUB.posts||[]).filter(r=>(r.when||'')>=FLOOR);

  /* Which accounts count toward this profile's numbers. Posting to someone
     else's account does not make their holiday photos your statistics, so
     these can be turned off one at a time. */
  const seen=new Set(), accts=[];
  all.forEach(r=>{
    if(!r.account_id||seen.has(r.account_id))return;
    seen.add(r.account_id);
    const meta=(FOLL.accounts||[]).find(a=>a.account_id===r.account_id)||{};
    accts.push({id:r.account_id, platform:r.platform,
      who:meta.profile_label||r.account||'', owned:!!r.owned,
      n:all.filter(x=>x.account_id===r.account_id).length});
  });
  accts.sort((a,b)=>(a.owned===b.owned?0:a.owned?-1:1)
    ||a.who.localeCompare(b.who)||a.platform.localeCompare(b.platform));
  /* Everything on by default. The two buttons below switch between this
     profile's own work and all of it in one click. */
  if(FACCTS===null) FACCTS=new Set(accts.map(a=>a.id));

  const rows=all.filter(r=>{const d=(r.when||'').slice(0,10);
    return d>=FFROM&&d<=FTO&&(!r.account_id||FACCTS.has(r.account_id));});
  const tot=k=>rows.reduce((a,r)=>a+(r[k]||0),0);
  el.innerHTML=`<div class="bar2"><h2>Audience</h2><span class="spacer"></span>
      <span class="dates">
        <input type="date" id="ffrom" value="${FFROM}" min="${FLOOR}"
          max="${TODAY()}" onchange="setFRange()">
        <span class="sub">to</span>
        <input type="date" id="fto" value="${FTO}" min="${FLOOR}"
          max="${TODAY()}" onchange="setFRange()">
        ${(FFROM!==dflt()||FTO!==TODAY())?
          `<button class="act" onclick="resetFRange()">Reset</button>`:''}
      </span>
      <button class="act" onclick="drawFollowers(true)">Refresh</button></div>`
    +followerBlock()
    +(accts.length>1?`<div class="filters">
      ${accts.map(a=>`<label class="fchip${FACCTS.has(a.id)?' on':''}">
        <input type="checkbox" ${FACCTS.has(a.id)?'checked':''}
          onchange="toggleFAcct('${a.id}')">
        <span style="color:${(PLAT[a.platform]||{}).ink||'#8A8F86'}">&#9632;</span>
        ${esc((PLAT[a.platform]||{}).label||a.platform)}
        <span class="sub">${esc(a.who)}</span>
        <span class="fn">${a.n}</span></label>`).join('')}
      <button class="act" onclick="allFAccts()">All</button>
    </div>`:'')
    +timeline(rows)
    +(rows.length?`<div class="bar2"><h2>What it earned</h2>
        <span class="spacer"></span>
        <span class="sub">${rows.length} of ${all.length} posts</span></div>`
        +statTiles(tot,rows)
      :`<div class="empty"><b>No posts in that range.</b></div>`);
  wirePubTip();
}

/* Which platforms in this selection never report a metric at all. A zero that
   means "not counted" has to look different from a zero that means "nobody
   looked", or you go off to check the platform and find numbers there. */
function silentOn(metric,rows){
  const seen=(PUB&&PUB.reports)||{};
  const plats=[...new Set(rows.map(r=>r.platform).filter(Boolean))];
  return plats.filter(p=>!((seen[p]||{})[metric]));
}
function statTiles(tot,rows){
  rows=rows||[];
  return `<div class="tiles row7">${[['Views','views'],
    ['Impressions','impressions'],['Reach','reach'],['Likes','likes'],
    ['Comments','comments'],['Shares','shares'],['Saves','saves']]
    .map(([l,k])=>{
      const n=tot(k), quiet=silentOn(k,rows);
      const none=!n&&quiet.length;
      return `<div class="tile${none?' quiet':''}"${quiet.length
        ? ` title="${esc(quiet.join(', '))} ${quiet.length===1?'does':'do'} not `
          +`report ${l.toLowerCase()} through Zernio"`:''}>
        <div class="n">${none?'&mdash;':exact(n)}</div>
        <div class="l">${l}${quiet.length&&n?' *':''}</div></div>`;
    }).join('')}</div>`;
}

/* Followers, and whether they are moving. Zernio reports today's number only,
   so the desk records one row per account per day and the change is measured
   against the earliest day it has. */
/* Where the growth actually is. Measured across the same days as the tiles,
   so the parts add up to the whole. */
function acctDelta(aid){
  const h=((FOLL||{}).account_history||{})[aid];
  if(!h) return null;
  const days=Object.keys(h).sort()
    .filter(x=>(!FFROM||x>=FFROM)&&(!FTO||x<=FTO));
  if(days.length<2) return null;
  return h[days[days.length-1]]-h[days[0]];
}

function followerBlock(){
  if(!FOLL||FOLL.error||!FOLL.accounts) return '';
  const totals=FOLL.totals||{}, order=FOLL.order||Object.keys(totals);
  const days=Object.keys(FOLL.history||{}).sort()
    .filter(d=>(!FFROM||d>=FFROM)&&(!FTO||d<=FTO));
  const first=days[0], last=days[days.length-1];
  const delta=slug=>{
    if(!first||first===last) return null;
    const a=(FOLL.history[first]||{})[slug], b=(FOLL.history[last]||{})[slug];
    return (a===undefined||b===undefined)?null:b-a;
  };
  const grand=Object.values(totals).reduce((a,x)=>a+x.n,0);
  const card=(label,n,slug,hot)=>{
    const d=slug?delta(slug):null;
    return `<div class="tile${hot?' hot':''}"><div class="n">${exact(n)}</div>
      <div class="l">${esc(label)}${slug===PROF?' · showing':''}</div>
      ${d===null?'':`<div class="grow ${d>0?'up':d<0?'down':''}">${
        d>0?'+':''}${d} since ${first.slice(5)}</div>`}</div>`;
  };
  return `<div class="bar2" style="margin-top:4px"><h2>Followers</h2>
      <span class="spacer"></span>
      <span class="sub">${days.length} day${days.length===1?'':'s'} recorded${
        days.length?` · ${first.slice(5)} to ${last.slice(5)}`:' in this range'}</span>
    </div>
    <div class="tiles">
      ${order.map(slug=>card(totals[slug].label,totals[slug].n,slug,
        slug===PROF)).join('')}
      ${order.length>1?`<div class="tile"><div class="n">${exact(grand)}</div>
        <div class="l">Everything</div></div>`:''}
    </div>
    <div class="keys folllist" style="margin:0 0 20px">
      ${FOLL.accounts.slice().sort((a,b)=>
          (order.indexOf(a.profile)-order.indexOf(b.profile))
          || (b.n||0)-(a.n||0)).map(a=>{
        const label=esc((PLAT[a.platform]||{}).label||a.platform);
        const d=acctDelta(a.account_id);
        return `<span><b style="background:${
            (PLAT[a.platform]||{}).ink||'#8A8F86'}"></b>
          ${a.url?`<a href="${esc(a.url)}" target="_blank" rel="noopener"
            title="Open ${esc(a.name)} on ${label}">${label}</a>`:label}
          <span style="color:var(--faint)">${esc(a.name)}</span>
          ${exact(a.n)}${d===null?'':`<i class="d${d>0?' up':d<0?' down':''}"
            >${d>0?'+':''}${d}</i>`}</span>`;}).join('')}
      ${days.length<2?`<span style="color:var(--faint)">Growth shows once the desk
        has two days of readings. Zernio keeps no history, so this starts today.</span>`:''}
    </div>`;
}

/* Published posts over time, stacked by platform. */
let PBUCKETS=[], PMETRIC=localStorage.getItem('desk.pmetric')||'posts';
const METRICS={posts:'Posts', views:'Views', impressions:'Impressions',
  reach:'Reach', likes:'Likes', followers:'Followers'};
/* Followers is a level, not a sum: a bar is each platform's count at the end
   of its period, stacked. The history lives with the Numbers tab, so the
   button only appears there. */
const onNumbers=()=>{const o=document.querySelector('nav button[aria-selected=true]');
  return !!(o&&o.dataset.t==='foll');};
const metricNow=()=>PMETRIC==='followers'&&!(onNumbers()&&FOLL&&FOLL.account_history)
  ?'posts':PMETRIC;
function setMetric(m){PMETRIC=m;localStorage.setItem('desk.pmetric',m);
  redrawTimeline();}

/* The first sentence, which is usually enough to recognise a post by. */
function opener(text){
  const t=(text||'').replace(/\s+/g,' ').trim();
  if(!t) return '';
  const m=t.match(/^.*?[.!?](?=\s|$)/);
  let s=(m?m[0]:t).trim();
  if(s.length>110) s=s.slice(0,110).replace(/\s\S*$/,'');
  return s.replace(/[.!?]+$/,'')+'…';
}

const niceDay=iso=>new Date(i2t(iso)).toLocaleString('en-CA',
  {day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});
const metricTotal=rows=>metricNow()==='posts'?rows.length
  :rows.reduce((a,r)=>a+(r[metricNow()]||0),0);

/* Followers per platform at the end of each period: the last reading on or
   before it, carried forward over days nobody took one. */
function follBuckets(keys){
  const per={};
  (FOLL.accounts||[]).forEach(a=>{
    const h=(FOLL.account_history||{})[a.account_id]||{};
    Object.entries(h).forEach(([d,n])=>{ if(n==null)return;
      ((per[a.platform]=per[a.platform]||{})[d]=((per[a.platform]||{})[d]||0)+n);});
  });
  const out={};
  keys.forEach(k=>{
    const end=t2i(i2t(bnext(k,PPERIOD))-DAY);
    Object.entries(per).forEach(([pl,h])=>{
      const ds=Object.keys(h).filter(d=>d<=end).sort();
      if(ds.length)(out[k]=out[k]||{})[pl]=h[ds[ds.length-1]];
    });
  });
  return out;
}
/* A round top for the scale and the ticks under it: 1, 2, 2.5 or 5 times a
   power of ten, about four steps. */
function niceScale(max){
  const raw=Math.max(1,max)/4, p=Math.pow(10,Math.floor(Math.log10(raw)));
  const step=[1,2,2.5,5,10].map(m=>m*p).find(s=>s>=raw);
  const top=Math.ceil(Math.max(1,max)/step)*step;
  const ticks=[]; for(let v=0;v<=top+1e-9;v+=step) ticks.push(+v.toFixed(6));
  return {top,ticks};
}

function timeline(rows){
  const seg=[['day','Day'],['week','Week'],['month','Month']].map(([k,l])=>
    `<button class="${PPERIOD===k?'on':''}" onclick="setPPeriod('${k}')">${l}</button>`
    ).join('');
  const metric=metricNow();
  const mseg=Object.entries(METRICS)
    .filter(([k])=>k!=='followers'||(onNumbers()&&FOLL&&FOLL.account_history))
    .map(([k,l])=>
    `<button class="${metric===k?'on':''}" onclick="setMetric('${k}')">${l}</button>`
    ).join('');
  if(!rows.length) return '';
  const buck={}, meta={};
  rows.forEach(r=>{
    const iso=(r.when||'').slice(0,10); if(!iso)return;
    const k=bstart(iso,PPERIOD);
    if(metric!=='followers'){
      const add=metric==='posts'?1:(r[metric]||0);
      (buck[k]=buck[k]||{})[r.platform]=(buck[k][r.platform]||0)+add;
    }
    const m=(meta[k]=meta[k]||{});
    const p=(m[r.platform]=m[r.platform]||{n:0,likes:0,comments:0,shares:0,
      views:0,impressions:0,reach:0,lines:[]});
    p.n+=1; p.likes+=r.likes||0; p.comments+=r.comments||0;
    p.shares+=r.shares||0; p.reach+=r.reach||0;
    p.views+=r.views||0; p.impressions+=r.impressions||0;
    const o=opener(r.content); if(o) p.lines.push(o);
  });
  const dates=rows.map(r=>(r.when||'').slice(0,10)).filter(Boolean).sort();
  if(!dates.length) return '';
  const keys=[]; let k=bstart(dates[0]<FLOOR?FLOOR:dates[0],PPERIOD);
  const last=bstart(dates[dates.length-1],PPERIOD);
  for(let i=0;i<800&&k<=last;i++){keys.push(k);k=bnext(k,PPERIOD);}
  if(metric==='followers') Object.assign(buck, follBuckets(keys));
  const order=Object.keys(PLAT).filter(pl=>metric==='followers'
    ? keys.some(x=>(buck[x]||{})[pl]) : rows.some(r=>r.platform===pl));
  const {top:max,ticks}=niceScale(Math.max(...keys.map(x=>
    Object.values(buck[x]||{}).reduce((a,b)=>a+b,0))));
  if(!Object.keys(buck).length) return '';
  const tickLabel=v=>v>=1e6?(v/1e6).toFixed(v%1e6?1:0)+'M'
    :v>=1e4?(v/1e3).toFixed(0)+'k':v>=1e3?(v/1e3).toFixed(v%1e3?1:0)+'k':String(v);
  const grid=ticks.slice(1).map(v=>
    `<i class="grid" style="bottom:${(v/max*128).toFixed(1)}px"></i>`).join('');
  const yax=ticks.map(v=>
    `<span style="bottom:${(v/max*128).toFixed(1)}px">${tickLabel(v)}</span>`).join('');
  const wide={day:7,week:16,month:30}[PPERIOD];
  PBUCKETS=keys.map(x=>({key:x,counts:buck[x]||{},meta:meta[x]||{},order,metric}));
  const bars=keys.map((x,i)=>{
    const c=buck[x]||{};
    return `<div class="bar" data-i="${i}">${order.filter(pl=>c[pl]).map(pl=>
      `<span data-pl="${pl}" style="height:${(c[pl]/max*128).toFixed(1)}px;
        background:${PLAT[pl].ink}"></span>`).join('')}</div>`;}).join('');
  const evy=Math.max(1,Math.ceil(keys.length/14));
  const ax=keys.map((x,i)=>`<div>${i%evy?'':`<b>${blabel(x,PPERIOD)}</b>`}</div>`).join('');
  return `<div class="strip">
    <div class="striphead"><h2>Timeline</h2><span class="spacer"></span>
      <div class="seg">${mseg}</div>
      <div class="seg">${seg}</div></div>
    <p class="sum">${metric==='followers'
       ? `Followers at the end of each ${PPERIOD}, stacked by platform. Now ${
          exact(Object.values(buck[keys[keys.length-1]]||{}).reduce((a,b)=>a+b,0))}`
       : `${esc(METRICS[metric])}: ${exact(metricTotal(rows))}`} -
       ${esc(niceDay((onNumbers()?FFROM:PFROM)||dflt()))} to ${
         esc(niceDay((onNumbers()?FTO:PTO)||TODAY()))}</p>
    <div class="chartrow">
      <div class="barwrap"><div style="min-width:${keys.length*wide}px">
        <div class="bars" id="pbars">${grid}${bars}</div>
        <div class="axis">${ax}</div></div></div>
      <div class="yaxis" aria-hidden="true">${yax}</div></div>
    <div class="keys">${order.map(pl=>
      `<span><b style="background:${PLAT[pl].ink}"></b>${esc(PLAT[pl].label)}</span>`
      ).join('')}</div></div>`;
}

function wirePubTip(){
  const wrap=$('#pbars'), t=$('#tip'); if(!wrap)return;
  const hide=()=>{t.hidden=true;};
  wrap.addEventListener('mousemove',e=>{
    const b=e.target.closest('.bar'); if(!b){hide();return;}
    const d=PBUCKETS[+b.dataset.i]; if(!d){hide();return;}
    /* Which slice of the stack the cursor is actually on. */
    const seg=e.target.closest('span[data-pl]');
    const on=seg?seg.dataset.pl:null;
    const sum=k=>Object.values(d.meta).reduce((a,m)=>a+(m[k]||0),0);
    const live=d.order.filter(pl=>(d.meta[pl]||{}).n);
    const posts=sum('n');
    const lines=on?((d.meta[on]||{}).lines||[]):[];
    if(d.metric==='followers'){
      const fl=d.order.filter(pl=>d.counts[pl]);
      const all=fl.reduce((a,pl)=>a+d.counts[pl],0);
      t.innerHTML=`<h4>${esc(brange(d.key,PPERIOD))}</h4>
        ${fl.map(pl=>`<div class="r${pl===on?' on':''}">
          <b style="background:${PLAT[pl].ink}"></b>
          <span>${esc(PLAT[pl].label)}</span><i>${exact(d.counts[pl])}</i></div>`).join('')
         ||'<div class="r"><span>No reading yet</span></div>'}
        ${fl.length?`<div class="tot">${exact(all)} followers</div>`:''}`;
      t.hidden=false;
      const r=t.getBoundingClientRect();
      t.style.left=Math.max(8,Math.min(e.clientX+16,innerWidth-r.width-8))+'px';
      t.style.top=Math.max(8,e.clientY-r.height-14)+'px';
      return;
    }
    t.innerHTML=`<h4>${esc(brange(d.key,PPERIOD))}</h4>
      ${live.length?`<div class="r hdr"><b></b><span></span>
        <i>posts</i><i>&#9654;</i><i>&#9829;</i><i>&#128172;</i></div>`:''}
      ${live.map(pl=>{const m=d.meta[pl]||{};
        return `<div class="r${pl===on?' on':''}">
          <b style="background:${PLAT[pl].ink}"></b>
          <span>${esc(PLAT[pl].label)}</span>
          <i>${m.n}</i><i>${num(m.views||0)}</i>
          <i>${num(m.likes||0)}</i><i>${num(m.comments||0)}</i>
        </div>`;}).join('')
       ||'<div class="r"><span>Nothing published</span></div>'}
      ${posts?`<div class="tot">${posts} post${posts===1?'':'s'} ·
        ${num(sum('views'))} views · ${num(sum('likes'))} likes ·
        ${num(sum('comments'))} comments</div>`:''}
      ${lines.length?`<div class="said">${lines.slice(0,3).map(l=>
        `<div>${esc(l)}</div>`).join('')}${lines.length>3
        ?`<div class="more">and ${lines.length-3} more</div>`:''}</div>`:''}`;
    t.hidden=false;
    const r=t.getBoundingClientRect();
    t.style.left=Math.max(8,Math.min(e.clientX+16,innerWidth-r.width-8))+'px';
    t.style.top=Math.max(8,e.clientY-r.height-14)+'px';
  });
  wrap.addEventListener('mouseleave',hide);
}

/* The table head parks itself directly under the filter row, whatever height
   the filters happen to wrap to. */
function stickHead(){
  const f=document.querySelector('#done .pstick');
  const t=document.querySelector('#done thead');
  if(!f||!t)return;
  t.style.setProperty('--htop', f.getBoundingClientRect().height+'px');
}
addEventListener('resize',()=>{ if(!$('#done').hidden) stickHead(); });

function pubTable(rows){
  return `<table class="sticky"><thead><tr>
      <th style="width:132px">When</th><th style="width:170px">Where</th><th>Copy</th>
      <th style="width:250px">Engagement</th><th style="width:60px"></th>
    </tr></thead><tbody>
    ${rows.map(r=>`<tr>
      <td>${esc(zwhen(r.when,null))}${r.external?
        '<div class="sub">posted natively</div>':''}${r.awaiting?
        '<div class="sub">numbers pending</div>':''}</td>
      <td><span style="color:${(PLAT[r.platform]||{}).ink||'#8A8F86'}">&#9632;</span>
        ${esc((PLAT[r.platform]||{}).label||r.platform)}
        <div class="sub">${esc(r.account)}</div></td>
      <td>${esc((r.content||'').slice(0,95))}${(r.content||'').length>95?'…':''}</td>
      <td class="eng">
        <span title="views">&#9654; ${num(r.views)}</span>
        <span title="impressions">&#128065; ${num(r.impressions)}</span>
        <span title="reach">&#8599; ${num(r.reach)}</span>
        <span title="likes">&#9829; ${num(r.likes)}</span>
        <span title="comments">&#128172; ${num(r.comments)}</span>
        <span title="shares">&#8631; ${num(r.shares)}</span>
        ${r.rate!==null&&r.rate!==undefined?
          `<span class="rate">${r.rate}%</span>`:''}
      </td>
      <td>${r.url?`<a href="${esc(r.url)}" target="_blank" rel="noopener">open</a>`
        :'<span style="color:var(--faint)">none</span>'}</td>
    </tr>`).join('')}</tbody></table>
    <div class="keys" style="margin-top:12px"><span style="color:var(--faint)">
      Numbers come from Zernio and lag the platforms by up to an hour.</span></div>`;
}

function copyLinks(){
  const has=POSTEDROWS.filter(r=>r.url);
  navigator.clipboard.writeText(has.map(r=>
    `${(PLAT[r.platform]||{}).label||r.platform}\t${r.url}`).join('\n'))
    .then(()=>toast(`${has.length} links copied`))
    .catch(()=>toast('Could not reach the clipboard'));
}

/* ---- ten things you could post ---- */
let IDEAS=null;

async function drawSuggest(force){
  const el=$('#sug');
  /* The board is kept in desk.db now, so it survives a reload and a reopened
     tab. Read it before offering to think, or a frozen idea would look lost
     until you asked for ten more. */
  if(IDEAS===null&&!force){
    IDEAS=await api('/api/suggestions?profile='+encodeURIComponent(PROF));
  }
  if(force){
    el.innerHTML=`<div class="bar2"><h2>Suggested</h2></div>
      <div class="empty">Thinking. This takes about a minute…</div>`;
    IDEAS=await api('/api/suggest',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profile:PROF})});
  }
  if(IDEAS.error){
    el.innerHTML=`<div class="bar2"><h2>Suggested</h2><span class="spacer"></span>
      <button class="act" onclick="drawSuggest(true)">Try again</button></div>
      <div class="empty"><b>Could not get suggestions.</b>${esc(IDEAS.error)}</div>`;
    return;
  }
  const ideas=IDEAS.ideas||[];
  const kept=ideas.filter(x=>x.frozen).length;
  if(!ideas.length){
    el.innerHTML=`<div class="bar2"><h2>Suggested</h2></div>
      <div class="empty"><b>Ten things you could post.</b>
        Built from your source packs, what you have already spent, and the voice
        rules. Takes about a minute.
        <div style="margin-top:16px">
          <button class="act go" onclick="drawSuggest(true)">Suggest 10 posts</button>
        </div></div>`;
    return;
  }
  el.innerHTML=`<div class="bar2"><h2>Suggested</h2><span class="spacer"></span>
      <span class="sub">${ideas.length} idea${ideas.length===1?'':'s'}${
        kept?` · ${kept} kept`:''}</span>
      <button class="act go" onclick="drawSuggest(true)">Suggest 10 more</button></div>
    ${kept?`<div class="chars" style="margin:-6px 0 12px">Kept ideas stay put
      when you ask for more, and the writer is told not to suggest them
      again.</div>`:''}
    ${ideas.map((x,i)=>`<div class="idea${x.frozen?' kept':''}">
      <div class="dhead"><span class="inum">${x.frozen?'&#10052;':i+1}</span>
        <b>${esc(x.title||'')}</b><span class="spacer"></span>
        ${(x.platforms||[]).map(p=>`<span class="zp">
          <b style="background:${(PLAT[p]||{}).ink||'#8A8F86'}"></b>
          ${esc((PLAT[p]||{}).label||p)}</span>`).join('')}</div>
      <div class="dsub">${esc(x.day||'')}</div>
      <div class="ztext" style="margin:9px 0">${esc(x.idea||'')}</div>
      ${x.opening?`<div class="quote">${esc(x.opening)}</div>`:''}
      ${x.needs?`<div class="qn">Needs: ${esc(x.needs)}</div>`:''}
      <div class="rowb" style="margin-top:10px">
        <button class="act" onclick="freezeIdea(${x.id},${x.frozen?0:1})"
          title="${x.frozen?'Let it be replaced by the next round'
                           :'Hold on to this one'}"
          >${x.frozen?'Unfreeze':'Freeze'}</button>
        ${x.frozen?'':`<button class="linky2" onclick="dropIdea(${x.id})"
          >Dismiss</button>`}
        <span class="chars" style="flex:1">${x.frozen
          ? 'Kept. It will not be replaced, and cannot be dismissed until you unfreeze it.'
          : ''}</span>
      </div>
    </div>`).join('')}`;
}

async function freezeIdea(id,on){
  const r=await api(`/api/suggestions/${id}/freeze`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({frozen:!!on})});
  if(r.error){toast(r.error);return;}
  IDEAS=null; await drawSuggest();
  toast(on?'Kept':'No longer kept');
}

async function dropIdea(id){
  const r=await api(`/api/suggestions/${id}`,{method:'DELETE'});
  if(r.error){toast(r.error);return;}
  IDEAS=null; await drawSuggest();
  toast('Dismissed');
}

/* ---- calendar ---- */
/* Which platforms are on screen, and their colours. Called by the load chart
   and by the calendar, so it lives outside both. */
function legend(){
  const seen=new Set(POSTS.map(p=>(CHAN[p.channel]||{}).platform).filter(Boolean));
  return Object.entries(PLAT).filter(([k])=>seen.has(k)).map(([k,v])=>
    `<span><b style="background:${v.ink}"></b>${esc(v.label||k)}</span>`).join('');
}

function calSpan(){
  /* Returns the first cell, how many cells, and the heading, for the view. */
  const y=MONTH.getFullYear(), m=MONTH.getMonth(), d=MONTH.getDate();
  if(CALVIEW==='day'){
    return {start:new Date(y,m,d), n:1, cols:1, rows:1,
      title:MONTH.toLocaleString('en-CA',
        {weekday:'long',day:'numeric',month:'long',year:'numeric'})};
  }
  if(CALVIEW==='week'){
    const s=new Date(y,m,d); s.setDate(d-((s.getDay()+6)%7));
    const e=new Date(s); e.setDate(s.getDate()+6);
    const f=x=>x.toLocaleString('en-CA',{day:'numeric',month:'short'});
    return {start:s, n:7, cols:7, rows:1,
      title:`${f(s)} to ${f(e)}, ${e.getFullYear()}`};
  }
  const first=new Date(y,m,1), s=new Date(first);
  s.setDate(1-((first.getDay()+6)%7));
  return {start:s, n:42, cols:7, rows:6,
    title:MONTH.toLocaleString('en-CA',{month:'long',year:'numeric'})};
}

function drawCal(){
  const sp=calSpan(), m=MONTH.getMonth(), today=TODAY();
  const dows=CALVIEW==='day'?'':['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    .map(d=>`<div class="dow">${d}</div>`).join('');
  let cells='';
  for(let i=0;i<sp.n;i++){
    const d=new Date(sp.start); d.setDate(sp.start.getDate()+i);
    const day=iso(d);
    const mine=POSTS.filter(p=>p.date===day
      &&(CALPUB||p.state!=='published'));
    /* A month cell is about three chips tall. It used to scroll, with no sign
       there was anything below the fold, so a day with five posts looked like
       a day with three. */
    const room=CALVIEW==='month'?CAL_CHIPS:mine.length;
    const over=mine.length-room;
    const chips=mine.slice(0,room).map(p=>{
      const k=[p.state==='approved'||p.state==='scheduled'?'appr':
               p.state==='rejected'?'rej':p.state==='allocated'?'alloc':'',
               /* Published, or simply gone by. Either way it is not ahead. */
               p.state==='published'||p.date<today?'past':''].filter(Boolean).join(' ');
      return `<button class="chip ${k}" data-p="${p.id}"
        style="border-left-color:${cink(p.channel)}"
        onclick="open_(${p.id})">${p.time} ${esc(
          p.channel_label.split('/')[0].trim())}${
          p.media_id?' <b style="color:var(--ok)">&#9679;</b>':''}</button>`;
    }).join('');
    const out=CALVIEW==='month'&&d.getMonth()!==m?'out':'';
    const lab=CALVIEW==='day'
      ? '' : `<div class="d">${d.getDate()}</div>`;
    const more=over>0?`<button class="more" onclick="calDay('${day}')"
      >and ${over} more</button>`:'';
    cells+=`<div class="day ${out} ${day===today?'today':''}">
      ${lab}<div class="chips">${chips||
        '<div class="d" style="opacity:.45">nothing</div>'}</div>${more}</div>`;
  }
  const seg=[['month','Month'],['week','Week'],['day','Day']].map(([k,l])=>
    `<button class="${CALVIEW===k?'on':''}" onclick="setCal('${k}')">${l}</button>`
  ).join('');
  const done=POSTS.filter(p=>p.state==='published').length;
  $('#cal').innerHTML=`
    <div class="calbar">
      <button class="act" onclick="mv(-1)">Previous</button>
      <h2>${esc(sp.title)}</h2>
      <button class="act" onclick="mv(1)">Next</button>
      <button class="act" onclick="today_()">Today</button>
      <span class="spacer"></span>
      ${done?`<label class="opt" style="margin:0">
        <input type="checkbox" ${CALPUB?'checked':''} onchange="calPub(this.checked)">
        Published (${done})</label>`:''}
      <div class="seg">${seg}</div>
    </div>
    <div class="cal" id="calgrid"
      style="grid-template-columns:repeat(${sp.cols},1fr);
             grid-template-rows:${dows?'auto ':''}repeat(${sp.rows},minmax(${
               CALVIEW==='month'?'136px':'420px'},auto))"
      >${dows}${cells}</div>
    <div class="keys"><span style="color:var(--faint)">Colour is the platform.
      Hover a slot to read it. A green dot means an asset is attached.</span>
      ${legend()}</div>`;
  wireCalTip();
}
function setCal(v){CALVIEW=v;localStorage.setItem('desk.calview',v);drawCal();}
function calPub(on){
  CALPUB=on; localStorage.setItem('desk.calpub',on?'1':'0'); drawCal();
}
/* Open one day in full, from a cell that could not show everything. */
function calDay(day){
  const a=day.split('-').map(Number);
  MONTH=new Date(a[0],a[1]-1,a[2]);
  setCal('day');
}
function today_(){MONTH=new Date();drawCal();}

function mv(n){
  const d=new Date(MONTH);
  if(CALVIEW==='month') MONTH=new Date(d.getFullYear(),d.getMonth()+n,1);
  else {d.setDate(d.getDate()+n*(CALVIEW==='week'?7:1)); MONTH=d;}
  drawCal();
}

/* Hovering a slot shows the post itself, which is the only way a month view
   can stay this compact and still be readable. */
function slotTip(p){
  const body=(p.copy||'').trim();
  const m=MEDIA.find(x=>x.id===p.media_id);
  const shot=m?`<div class="tipshot">${m.kind==='video'
      ?`<video src="/media/${m.id}/raw#t=0.5" preload="metadata" muted></video>`
      :`<img src="/media/${m.id}/raw" alt="">`}</div>`:'';
  const bits=[esc(title(p.campaign)), esc(p.phase)+' phase',
              esc(p.asset_label||'no asset')]
             .filter(Boolean).join(' · ');
  return `<h4><span style="color:${cink(p.channel)}">&#9632;</span>
      ${esc(p.channel_label)}</h4>
    <div class="meta">${p.date} at ${p.time} · ${NICE[p.state]||p.state}${
      p.handed_off?' · emailed':''}<br>${bits}</div>
    ${shot}
    ${statLine(p)}
    ${body?`<div class="body">${esc(body.length>700?body.slice(0,700)+'…':body)}</div>`
          :`<div class="none">Not written yet.</div>`}
    ${p.first_comment?`<div class="meta" style="margin:8px 0 0">First comment: ${
      esc(p.first_comment)}</div>`:''}`;
}
/* Once a post has gone out, how it did is the useful thing to see on it. */
function statLine(p){
  let s=null;
  try{ s=JSON.parse(p.stats||'null'); }catch(e){}
  if(!s) return '';
  const cell=(icon,label,v)=>v===null||v===undefined?'':
    `<span title="${label}">${icon} ${num(v)}</span>`;
  const row=[cell('&#9654;','views',s.views),
             cell('&#128065;','impressions',s.impressions),
             cell('&#8599;','reach',s.reach),
             cell('&#9829;','likes',s.likes),
             cell('&#128172;','comments',s.comments),
             cell('&#8631;','shares',s.shares)].filter(Boolean).join('');
  if(!row) return '';
  return `<div class="eng tipeng">${row}${
    s.engagementRate!==null&&s.engagementRate!==undefined?
    `<span class="rate">${s.engagementRate}%</span>`:''}</div>`;
}

function wireCalTip(){
  const grid=$('#calgrid'), t=$('#tip');
  if(!grid)return;
  const hide=()=>{t.hidden=true;};
  grid.addEventListener('mousemove',e=>{
    const c=e.target.closest('.chip');
    if(!c){hide();return;}
    const p=POSTS.find(x=>x.id===+c.dataset.p);
    if(!p){hide();return;}
    t.innerHTML=slotTip(p); t.hidden=false;
    const r=t.getBoundingClientRect();
    t.style.left=Math.max(8,Math.min(e.clientX+16,innerWidth-r.width-8))+'px';
    t.style.top=Math.max(8,Math.min(e.clientY+16,innerHeight-r.height-8))+'px';
  });
  grid.addEventListener('mouseleave',hide);
}

/* ---- posts ---- */
/* Which accounts the filter is showing. Null means "everything", which is
   also what a newly appearing account joins as. */
let FILTER=null;
const akey=p=>p.channel+'|'+p.profile;

function toggleFilter(k){
  if(FILTER.has(k)) FILTER.delete(k); else FILTER.add(k);
  drawList();
}
function allFilters(on){
  FILTER = on ? new Set(POSTS.map(akey)) : new Set();
  drawList();
}

/* Once Zernio has a post, this desk is no longer the thing holding it up.
   `scheduled` means accepted into Zernio's queue and `published` means it went,
   so neither belongs on a list of what is still waiting here. `failed` stays:
   it came back, and it is waiting on you.

   Queued and Published are where a post lives after this, and both read from
   Zernio rather than from desk.db, so nothing is lost by dropping it here. */
const WAITING = p => !['rejected','scheduled','published'].includes(p.state);

function drawList(){
  const t=TODAY();
  const all=POSTS.filter(p=>WAITING(p)&&(p.copy||p.media_id));

  /* One entry per account that actually has posts. */
  const accts=[];
  const seen=new Set();
  all.forEach(p=>{
    const k=akey(p);
    if(seen.has(k))return;
    seen.add(k);
    const a=ACCTS.find(x=>x.channel===p.channel&&x.profile===p.profile);
    accts.push({key:k, ink:cink(p.channel),
      label:(CHAN[p.channel]||{}).label||p.channel,
      who:(a&&a.profile_label)||(PROFS[p.profile]||{}).label||p.profile,
      n:all.filter(x=>akey(x)===k).length});
  });
  accts.sort((a,b)=>a.who.localeCompare(b.who)||a.label.localeCompare(b.label));
  if(FILTER===null) FILTER=new Set(accts.map(a=>a.key));
  else accts.forEach(a=>{if(!seen.has(a.key))FILTER.add(a.key);});

  const bar2=accts.length>1?`<div class="filters">
    ${accts.map(a=>`<label class="fchip${FILTER.has(a.key)?' on':''}">
      <input type="checkbox" ${FILTER.has(a.key)?'checked':''}
        onchange="toggleFilter('${a.key}')">
      <span style="color:${a.ink}">&#9632;</span>
      ${esc(a.label)} <span class="sub">${esc(a.who)}</span>
      <span class="fn">${a.n}</span></label>`).join('')}
    <button class="act" onclick="allFilters(${FILTER.size<accts.length})">${
      FILTER.size<accts.length?'All':'None'}</button>
  </div>`:'';
  const shown=all.filter(p=>FILTER.has(akey(p)));
  const past=shown.filter(p=>p.date<t).length;
  const rows=(PASTPOSTS?shown:shown.filter(p=>p.date>=t))
    .sort((a,b)=>a.date.localeCompare(b.date)||a.time.localeCompare(b.time));
  const bar=`<div class="bar2"><h2>${PASTPOSTS?'Everything waiting':'To be posted'}</h2>
    <span class="spacer"></span>
    <button class="act" onclick="syncZernio()">Sync with Zernio</button>
    ${past?`<button class="act" onclick="PASTPOSTS=!PASTPOSTS;drawList()">${
      PASTPOSTS?'Hide':'Show'} ${past} past</button>`:''}</div>`;
  if(!rows.length){ $('#list').innerHTML=bar+bar2+
    `<div class="empty"><b>${all.length?'Nothing matches those filters.'
      :'Nothing waiting.'}</b>${all.length?''
      :'Anything already at Zernio is in Queued, and anything that went out '
       +'is in Published.'}</div>`; return;}
  $('#list').innerHTML=bar+bar2+`<table><thead><tr>
    <th style="width:108px">When</th><th>Account</th><th>Copy</th>
    <th style="width:120px">State</th><th style="width:66px"></th></tr></thead><tbody>
    ${rows.map(p=>`<tr style="cursor:pointer" onclick="open_(${p.id})">
      <td>${p.date}<br><span style="color:var(--faint)">${p.time}</span></td>
      <td><span style="color:${cink(p.channel)}">&#9632;</span>
          ${esc(p.channel_label)}</td>
      <td>${p.copy?esc(p.copy.slice(0,90))+(p.copy.length>90?'…':''):
           '<span style="color:var(--faint)">'+esc(p.asset_label||'')+'</span>'}</td>
      <td><span class="state">${p.handed_off?'Emailed':NICE[p.state]||p.state}</span>
        ${p.post_url?`<div><a href="${esc(p.post_url)}" target="_blank"
          rel="noopener">open</a></div>`:''}</td>
      <td class="cmds">
        <button class="pen" title="Edit this post"
          onclick="event.stopPropagation();open_(${p.id})">&#9998;</button>
        <button class="bin" title="Take this off the schedule"
          onclick="event.stopPropagation();delPost(${p.id})">&#128465;</button></td>
    </tr>`).join('')}</tbody></table>`;
}

/* ---- detail ---- */
let PUBLISHED=false;
let ROLES={}, WANT=new Set(), COMPOSE=false, BSEED=null, BTITLE='', BSRC=[];

/* `mkt` is the instrument this post is about, when it came from the Markets
   tab: {market, symbol, scope}. Everything else opens without one, and it is
   cleared here so a brand scope cannot leak from a Markets post onto the next
   thing you compose from Media or Blogs. */
function openCompose(mkt){
  COMPOSE=true;
  MKTSYM=mkt||null;
  PUBLISHED=false;
  WHASH=true;
  if(!mkt) MKSCORE=null;        // not a Wire post, so no scoring to show
  ROLES={}; PREVIEW=null; TITLE=null; PICKING=false;
  SECTS=null; SECTI=0; URLTEXT=null;
  WANT=new Set(ACCTS.filter(a=>a.on).map(a=>a.channel+'|'+a.profile));
  /* Your description of the picture is the starting point, not a separate
     field you fill in twice. Edit it here and the post is yours.

     A page fetched by URL carries its headline in `title` and its opening in
     `note`, and the headline leads. It is what the title drawn on the picture
     is taken from, and seeding only the excerpt meant the graphic got the
     first 120 characters of the summary, cut off mid-sentence. An uploaded
     photo has no title, so for those nothing changes. */
  const picked=SEL.map(id=>MEDIA.find(m=>m.id===id)).filter(Boolean);
  CDRAFT = picked.map(m=>[(m.title||'').trim(),(m.note||'').trim()]
                        .filter(Boolean).join('\n\n'))
                 .filter(Boolean).join('\n\n');
  /* A link belongs at the end, after whatever you have to say about it. */
  const links=[...new Set(picked.map(m=>m.url).filter(Boolean))];
  if(links.length) CDRAFT = (CDRAFT ? CDRAFT + '\n\n' : '') + links.join('\n');
  /* A blog seeds the sheet from the feed's own summary and link, because the
     media row may not exist yet, or may never exist if the blog has no hero. */
  PAGEURL=findPageUrl();
  if(BSEED){
    CDRAFT=[BSEED.copy,BSEED.links.join('\n')].filter(Boolean).join('\n\n');
    /* The instrument and its direction lead the post. Done after MKTSYM is
       set, since the prefix is built from it. */
    const pre=postPrefix();
    if(pre) CDRAFT=pre+CDRAFT;
    /* The same four the graphic carries, at the foot of the text. A reader on
       a feed that has collapsed the image, or using a screen reader, gets the
       scoring either way. */
    const sc=scoreLines();
    if(sc.length) CDRAFT=`${CDRAFT}\n\n${sc.join('\n')}`;
    /* A Wire post goes to more than one account, and each keeps its own copy.
       Seeding both from the body means the variants are there to edit rather
       than empty boxes you have to remember to fill. X still has to be cut to
       280, which its own tab does. */
    if(BSEED.fanout){ XDRAFT=forX(CDRAFT); LIDRAFT=forLi(CDRAFT); FANOUT=true; }
    /* The graphic gets the short form, the copy keeps the deep link. Still
       editable: clearing the field falls back to following the page. */
    if(BSEED.urlShort) URLTEXT=BSEED.urlShort;
    BSEED=null;
  }
  /* A caption written against a photo comes back with it. */
  /* A caption written against a photo comes back with it. Stored as one
     string, so it arrives as card one. */
  const kept = picked.map(m=>(m.caption||'').trim()).find(Boolean) || '';
  CARDS = kept ? [{text:kept,position:'lower',align:'center'}] : [];
  CARDI = 0; CAPFIT = {};
  loadFonts(); loadScopes(); loadDefaults();
  paintCompose();
  $('#sheet').classList.add('on');
  /* A Wire post is seeded with the same words on every channel, and X cannot
     hold them. Cut it once, here, rather than leaving a variant that is
     visibly too long for you to notice later. */
  if(FANOUT){ FANOUT=false; fitXCopy(); }
}

/* The two shapes this post can go out in. Portrait is what a vertical feed
   wants and landscape is what LinkedIn wants, so a post that has both can send
   each platform the one that fits instead of letting it crop. A slot knows
   which of the selected files is that shape: taller than wide is portrait,
   wider than tall is landscape, square counts for neither. */
function shapeOf(m){
  if(!m||!m.width||!m.height) return null;
  return m.height>m.width?'portrait':m.width>m.height?'landscape':'square';
}
function slotFor(sel,shape){ return sel.find(m=>shapeOf(m)===shape)||null; }

/* The title drawn on the picture is the copy's opening line, never a field of
   its own. Anything burned into an image is a published statement a reader
   cannot check against context, so it has to be text that is already in the
   post and has already been read. Deriving it makes that true by
   construction rather than by remembering. */
function copyTitle(){
  const first=((CDRAFT||'').trim().split(/\n+/)[0]||'').trim();
  if(!first) return '';
  const stop=first.search(/[.!?](\s|$)/);
  const s=stop>20?first.slice(0,stop+1):first;
  return s.length>120?s.slice(0,117).replace(/\s+\S*$/,'')+'\u2026':s;
}
/* TITLE null means "follow the copy". Type in the field and it holds your
   words instead; empty it and it goes back to following. */
function titleText(){
  if(TITLE!==null) return TITLE;
  /* A Wire post knows its headline outright, and it is no longer the first
     line of the copy. Anything else still takes the copy's opening. */
  return (BTITLE||'').trim() || copyTitle();
}

let SCRIM=localStorage.getItem('desk.scrim')||'medium', TITLE=null,
    TPCT=parseFloat(localStorage.getItem('desk.tpct'))||8.6,
    WEIGHT=localStorage.getItem('desk.weight')||'Medium',
    SHADOW=localStorage.getItem('desk.shadow')||'dark', PICKING=false,
    SHOWURL=localStorage.getItem('desk.showurl')||'on', URLTEXT=null,
    UPCT=parseFloat(localStorage.getItem('desk.upct'))||2.6,
    THEME=localStorage.getItem('desk.theme')||'dark';
/* Scrim, face and title all change what the finished picture looks like, so
   each of them redraws it rather than leaving a stale render behind that no
   longer matches the controls above it. Only once there is something to
   redraw: before the first render they just set the choice. */
function setScrim(k){
  SCRIM=k; localStorage.setItem('desk.scrim',k);
  if(SEL.length) renderPost(); else repaintCompose();
}
function setRenderFont(v){
  CAPFONT=v;
  if(SEL.length) renderPost(); else repaintCompose();
}
const WEIGHTS=['Light','Regular','Medium','SemiBold','Bold'];
function nudgeSize(d){
  TPCT=Math.round(Math.max(3,Math.min(14,TPCT+d))*10)/10;
  localStorage.setItem('desk.tpct',TPCT);
  if(SEL.length) renderPost(); else repaintCompose();
}
function setWeight(w){
  WEIGHT=w; localStorage.setItem('desk.weight',w);
  if(SEL.length) renderPost(); else repaintCompose();
}
function setShadow(k){
  SHADOW=k; localStorage.setItem('desk.shadow',k);
  if(SEL.length) renderPost(); else repaintCompose();
}
function setShowUrl(k){
  SHOWURL=k; localStorage.setItem('desk.showurl',k);
  if(SEL.length) renderPost(); else repaintCompose();
}
/* The ground the artwork is drawn on. Only affects the next generation, so
   there is nothing to redraw: the pictures already made keep their own. */
function setTheme(k){
  THEME=k; localStorage.setItem('desk.theme',k); repaintCompose();
}
/* Shown, not linked: the bare address without the scheme or a trailing slash,
   which is what reads on a picture. URLTEXT null means "follow the page"; type
   in the field and it holds your words instead, which is how a post points at
   a short vanity address rather than the deep link it was built from. */
function urlOnArt(){
  if(SHOWURL!=='on') return '';
  if(URLTEXT!==null) return URLTEXT;
  const u=pageUrl(); if(!u) return '';
  return u.replace(/^https?:\/\//i,'').replace(/\/$/,'');
}
function urlChanged(){
  if(URLTEXT!==null && !URLTEXT.trim()) URLTEXT=null;   // emptied, follow again
  if(SEL.length) renderPost(); else repaintCompose();
}
function nudgeUrl(d){
  UPCT=Math.round(Math.max(1,Math.min(6,UPCT+d))*10)/10;
  localStorage.setItem('desk.upct',UPCT);
  if(SEL.length) renderPost(); else repaintCompose();
}

/* Artwork can also come off disk or out of the library. Either way it lands
   in the sources row and gets the same scrim, title and mark as a generated
   one, so a hand-made picture posts exactly like a drawn one. */
async function artUpload(files){
  if(!files||!files.length) return;
  const before=new Set(MEDIA.map(m=>m.id));
  await up(files,'drop');
  await load();
  const fresh=MEDIA.filter(m=>m.kind==='image'&&!before.has(m.id)).map(m=>m.id);
  if(fresh.length) BSRC=[...new Set([...BSRC,...fresh])];
  repaintCompose();
  if(fresh.length&&titleText()) renderPost();
}
function addArt(id){
  BSRC=[...new Set([...BSRC,id])];
  PICKING=false;
  repaintCompose();
  if(titleText()) renderPost();
}
function titleChanged(){
  if(TITLE!==null && !TITLE.trim()) TITLE=null;   // emptied, so follow again
  if(SEL.length) renderPost(); else repaintCompose();
}

/* Which instrument this post is about. Filled in when the post came from the
   Markets tab, and editable always: the Wire's guess at the asset is a
   confidence score, not a fact, and a story about Bitcoin that arrived under
   XRP's feed should be correctable here rather than drawn in the wrong
   colours. Empty means no instrument, and the artwork uses house style. */
/* The same resolution the renderer does: the symbol as written, then
   case-insensitively, then through the alias table. WTI is Oil, XAUUSD is
   Gold, and the sheet should say so rather than claiming there is no scope. */
function resolveScope(market,symbol){
  const book=(SCOPES.markets||{})[market]||[];
  if(!symbol) return null;
  const want=String(symbol).toLowerCase();
  let hit=book.find(x=>x.symbol.toLowerCase()===want);
  if(hit) return hit;
  const alias=((SCOPES.aliases||{})[market])||{};
  const key=Object.keys(alias).find(k=>k.toLowerCase()===want);
  if(!key) return null;
  const target=String(alias[key]).toLowerCase();
  return book.find(x=>x.symbol.toLowerCase()===target)||null;
}

function instrumentRow(){
  const m=(MKTSYM&&MKTSYM.market)||'', s=(MKTSYM&&MKTSYM.symbol)||'';
  const book=(SCOPES.markets||{})[m]||[];
  const known=resolveScope(m,s);
  return `<div class="rowb" style="margin:2px 0 10px">
    <span class="chars">Instrument</span>
    <select class="slim" onchange="setInstrument(this.value,null)">
      <option value=""${m?'':' selected'}>no market</option>
      ${['crypto','stocks','forex','commodities'].map(k=>`<option value="${k}"
        ${m===k?'selected':''}>${k==='stocks'?'US Equities':
          k==='forex'?'FX':k[0].toUpperCase()+k.slice(1)}</option>`).join('')}
    </select>
    <input list="scopelist" value="${esc(s)}" placeholder="BTC, Gold, EUR…"
      oninput="setInstrument(null,this.value)" style="width:150px"
      ${m?'':'disabled'}>
    <datalist id="scopelist">${book.map(x=>`<option value="${esc(x.symbol)}"
      >${esc(x.name)}</option>`).join('')}</datalist>
    <span class="chars" style="flex:1">${
      !m ? 'Not a markets post. Artwork uses house style.'
      : known ? `Artwork will be drawn in ${esc(known.name)}&rsquo;s palette.`
      : s ? 'No brand scope for that one, so house style is used.'
      : 'Pick one to brand the artwork.'}</span>
  </div>`;
}

function setInstrument(market,symbol){
  MKTSYM=MKTSYM||{market:'',symbol:''};
  if(market!==null){ MKTSYM.market=market; MKTSYM.symbol=''; }
  if(symbol!==null) MKTSYM.symbol=symbol;
  if(!MKTSYM.market&&!MKTSYM.symbol) MKTSYM=null;
  repaintCompose();
}

function shapeSlots(){
  const src=BSRC.map(id=>MEDIA.find(m=>m.id===id)).filter(Boolean);
  const title=titleText();
  const one=(shape,label,hint)=>{
    const m=slotFor(src,shape);
    const other=shape==='portrait'?'landscape':'portrait';
    const from=slotFor(src,other);
    return `<div class="slot2${m?' has':''}">
      <div class="slab">${label}<span>${hint}</span></div>
      ${m?`<img src="/media/${m.id}/raw" alt="">
           <div class="slotm">${m.width}&times;${m.height}</div>`
         :`<div class="slotempty">nothing this shape yet</div>`}
      <div class="slotacts">
        <button class="act" onclick="makeShape('${shape}',this)"
          ${from?'':'disabled'} title="${from?`Reshape the ${other} one`
            :`Add a ${other} picture first`}">From ${other}</button>
      </div></div>`;
  };
  return `<div class="bar2" style="margin:2px 0 8px">
      <h2 style="font-size:19px">Artwork</h2><span class="spacer"></span>
      <button class="act" onclick="drawCards(this)"
        ${(CDRAFT||'').trim()||BTITLE?'':'disabled'}>Create image</button>
      <span class="seg">${['dark','light'].map(k=>`<button class="${
        THEME===k?'on':''}" onclick="setTheme('${k}')"
        title="The ground the artwork is drawn on">${k}</button>`).join('')}</span>
      <button class="act" onclick="$('#artup').click()">Upload image</button>
      <button class="act" onclick="PICKING=!PICKING;repaintCompose()"
        >${PICKING?'Close library':'Select image'}</button>
      <input id="artup" type="file" accept="image/*" multiple hidden
        onchange="artUpload(this.files)">
    </div>
    ${PICKING?`<div class="pickgrid">${MEDIA.filter(m=>m.kind==='image'
        &&!BSRC.includes(m.id)).map(m=>`<img src="/media/${m.id}/raw"
        onclick="addArt(${m.id})" title="${esc(m.original||'')}">`).join('')
        ||`<div class="chars">Nothing in the library yet.</div>`}</div>`:''}
    <div class="slots2">
      ${one('portrait','Portrait','1080&times;1920 · Instagram, Reels')}
      ${one('landscape','Landscape','1920&times;1080 · LinkedIn, YouTube')}
    </div>
    <div class="bar2" style="margin:14px 0 8px">
      <h2 style="font-size:19px">Ready to post</h2><span class="spacer"></span>
      <span class="chars">Scrim</span>
      <span class="seg">${['none','light','medium','heavy'].map(k=>`<button
        class="${SCRIM===k?'on':''}" onclick="setScrim('${k}')">${k}</button>`
        ).join('')}</span>
      <label class="chars radio"><input type="radio" name="showurl"
        ${SHOWURL==='on'?'checked':''} onchange="setShowUrl('on')"> Link on</label>
      <label class="chars radio"><input type="radio" name="showurl"
        ${SHOWURL==='off'?'checked':''} onchange="setShowUrl('off')"> off</label>
      ${pageUrl()?`<a class="artlink" href="${esc(pageUrl())}" target="_blank"
        rel="noopener" title="Open the article">${
          esc(pageUrl().replace(/^https?:\/\//i,'').slice(0,46))}${
          pageUrl().replace(/^https?:\/\//i,'').length>46?'…':''}</a>`:''}
      <button class="act" onclick="renderPost(this)"
        ${src.length&&title?'':'disabled'}>${SEL.length?'Redraw':'Render'}</button>
    </div>
    ${SHOWURL==='on'?`<div class="rowb" style="margin:0 0 9px">
      <input id="rurl" type="text" placeholder="the address printed along the
        bottom" value="${esc(urlOnArt())}" oninput="URLTEXT=this.value"
        onchange="urlChanged()" style="flex:1;min-width:220px"
        title="What is printed on the picture. The post keeps its own link.">
      <span class="chars">Link size</span>
      <span class="seg"><button onclick="nudgeUrl(-0.2)"
        title="Smaller">&minus;</button><button class="wide"
        >${UPCT.toFixed(1)}%</button><button onclick="nudgeUrl(0.2)"
        title="Bigger">+</button></span>
    </div>`:''}
    <div class="rowb" style="margin:0 0 9px">
      <select class="slim" onchange="setRenderFont(this.value)"
        title="The face the title is drawn in">${fontOptions()}</select>
      <span class="seg">${WEIGHTS.map(w=>`<button class="${WEIGHT===w?'on':''}"
        onclick="setWeight('${w}')">${w}</button>`).join('')}</span>
      <span class="chars">Size</span>
      <span class="seg"><button onclick="nudgeSize(-0.5)"
        title="Smaller">&minus;</button><button class="wide"
        >${TPCT.toFixed(1)}%</button><button onclick="nudgeSize(0.5)"
        title="Bigger">+</button></span>
      <span class="chars">Shadow</span>
      <span class="seg">${[['none','off'],['dark','dark'],['light','light']]
        .map(([k,l])=>`<button class="${SHADOW===k?'on':''}"
        onclick="setShadow('${k}')">${l}</button>`).join('')}</span>
      <span class="chars" style="flex:1"></span>
    </div>
    <input id="rtitle" type="text" placeholder="Title drawn on the picture"
      value="${esc(title)}" oninput="TITLE=this.value"
      onchange="titleChanged()" style="width:100%;margin-bottom:5px">
    <div class="chars" style="margin:0 0 9px">${TITLE===null
      ? (BTITLE ? `The article's own headline. Type here to override it.`
                : `Following the copy's first line. Type here to override it.`)
      : `Your words, not the copy's. <a href="#" onclick="TITLE=null;
         titleChanged();return false">Follow the copy again</a> &mdash; and
         check this line is in the post too.`}</div>`;
}

/* Whichever page this post came from, if any. A blog opened the sheet with
   its own link, and a fetched page carries it on the media row. */
let SECTS=null, SECTI=0, PAGEURL=null;
/* Captured when the sheet opens and kept for as long as it is open. Working
   it out from the selection does not survive Create image: a generated
   picture has no url of its own, so the moment the artwork was replaced the
   page behind the post was forgotten and the button disappeared. */
function pageUrl(){ return PAGEURL; }
function findPageUrl(){
  if(BSEED&&(BSEED.links||[]).length) return BSEED.links[0];
  const m=SEL.concat(BSRC).map(id=>MEDIA.find(x=>x.id===id)).filter(Boolean)
             .find(x=>x.url);
  return m?m.url:null;
}

/* Step through the page a part at a time. The heading leads, so the title
   drawn on the picture follows it like any other copy. */
async function otherPart(btn){
  const url=pageUrl();
  if(!url){toast('No page behind this post');return;}
  if(!SECTS){
    if(btn){btn.disabled=true;btn.textContent='Reading…';}
    const r=await api('/api/link/sections',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
    if(r.error){toast(r.error);repaintCompose();return;}
    SECTS=r.sections; SECTI=-1;
  }
  SECTI=(SECTI+1)%SECTS.length;
  const s=SECTS[SECTI];
  CDRAFT=[s.heading,s.text,url].filter(Boolean).join('\n\n');
  TITLE=null;                       // follow the copy again, on its new first line
  repaintCompose();
  toast(`Part ${SECTI+1} of ${SECTS.length}`);
}

/* A counting clock on a slow button. A minute of a disabled button with no
   sign of life reads as a hang; a number that moves reads as work. */
function startTimer(btn, label){
  const t0=Date.now();
  btn.disabled=true;
  const paint=()=>{
    const s=Math.floor((Date.now()-t0)/1000);
    btn.textContent=`${label} ${String(Math.floor(s/60)).padStart(2,'0')}:${
      String(s%60).padStart(2,'0')}`;
  };
  paint();
  const id=setInterval(paint,1000);
  return {stop(text){clearInterval(id);btn.disabled=false;
                     if(text) btn.textContent=text;}};
}

let WHASH=true;
/* Hashtags are appended by push_zernio at push time, per platform, so they are
   not in the copy box and were invisible until something went out. This shows
   what each account will get. */
function hashtagPreview(){
  const book=((PROFS[PROF]||{}).voice||{}).hashtags;
  if(!book) return 'No hashtags set for this profile in profiles.yaml.';
  const inst=(MKTSYM&&MKTSYM.symbol)
    ? (resolveScope(MKTSYM.market||'',MKTSYM.symbol)||{}).name||MKTSYM.symbol : '';
  const extra=inst?inst.replace(/[^A-Za-z0-9]/g,''):'';
  const plats=[...new Set([...WANT].map(k=>(CHAN[k.split('|')[0]]||{}).platform))]
    .filter(Boolean);
  if(!plats.length) return 'Pick an account to see its hashtags.';
  return plats.map(pl=>{
    const list=Array.isArray(book)?book:(book[pl]||book.default||[]);
    const all=[...list];
    if(extra&&!all.some(x=>x.toLowerCase()===extra.toLowerCase())) all.push(extra);
    return `<b>${esc(pl)}</b> ${esc(all.map(h=>'#'+h).join(' '))}`;
  }).join('<br>');
}

/* What a Wire post opens with: the instrument, where the Wire thinks it is
   pointing, then the story. "BA: Boeing ↑: Boeing has won…" so a reader
   scanning a feed knows what it is about before reading a word of it. */
/* Geometric shapes, not arrows: U+2191 is the right character and the desk
   draws it, but it is missing from enough of the fonts a post lands in to show
   as a box. */
const TREND_MARK={bullish:'\u25b2', bearish:'\u25bc', neutral:'\u25cf',
                  hold:'\u25cf'};
function postPrefix(){
  const label=instrumentLabel();
  if(!label) return '';
  const dir=((MKSCORE&&MKSCORE.direction)||'').toLowerCase();
  const mark=TREND_MARK[dir]||'';
  return `${label}${mark?' '+mark:''}: `;
}

/* The scoring a Wire article arrived with, kept for the graphic. Null for
   anything not from the Wire, which then gets no scoring lines. */
let MKSCORE=null;

/* "9:41 am July 13, 2026". The moment the post is scheduled for when you have
   set one, otherwise now: the graphic is dated when it goes out, not when the
   artwork happened to be drawn. */
function stampText(){
  let d=new Date();
  if(WHEN==='man'&&$('#dd')&&$('#dd').value){
    const t=($('#tt')&&$('#tt').value)||'09:00';
    const parsed=new Date(`${$('#dd').value}T${t}`);
    if(!isNaN(parsed)) d=parsed;
  }
  const time=d.toLocaleTimeString('en-CA',
    {hour:'numeric',minute:'2-digit',hour12:true}).toLowerCase().replace(/\s/,' ');
  const date=d.toLocaleDateString('en-CA',
    {month:'long',day:'numeric',year:'numeric'});
  return `${time} ${date}`;
}

/* Two lines under the headline. Split so the first carries what was measured
   and the second what it points at, and dropped entirely when the Wire did
   not give us the numbers. */
function scoreLines(){
  const s=MKSCORE; if(!s) return [];
  const bits1=[], bits2=[];
  if(s.sentiment!=null&&s.sentiment!=='') bits1.push(`Sentiment: ${s.sentiment}`);
  if(s.clout!=null&&s.clout!=='') bits1.push(`Clout: ${s.clout}`);
  const dir=(s.direction||'').toLowerCase();
  if(dir) bits2.push(dir==='bullish'?'Bullish':dir==='bearish'?'Bearish':'Hold');
  if(s.confidence!=null&&s.confidence!=='') bits2.push(`Confidence: ${s.confidence}`);
  return [bits1.join('  |  '), bits2.join('  |  ')].filter(Boolean);
}

/* What goes in the upper left: the instrument's own name, not its ticker, so
   a picture says "Crude Oil" rather than "WTI". Nothing when the post is not
   about an instrument. */
/* "BA: Boeing" rather than either alone: the ticker is what a reader scans
   for and the name is what they recognise. Collapsed to one when the two are
   the same word, because "Gold: Gold" helps nobody. */
function instrumentLabel(){
  if(!MKTSYM||!MKTSYM.symbol) return '';
  const sym=String(MKTSYM.symbol).trim();
  const known=resolveScope(MKTSYM.market||'', sym);
  const name=(known&&known.name)||'';
  if(!name||name.toLowerCase()===sym.toLowerCase()) return name||sym;
  return `${sym}: ${name}`;
}

/* Scrim and title, burned in. What comes out of here is what posts. */
async function renderPost(btn){
  const src=BSRC.map(id=>MEDIA.find(m=>m.id===id)).filter(Boolean);
  const title=titleText();
  if(!src.length||!title){toast('Need artwork and copy first');return;}
  if(btn){btn.disabled=true;btn.textContent='Rendering…';}
  const old=SEL.slice();
  const made=[];
  for(const m of src){
    const r=await api('/api/media/render',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({media:m.id,text:title,scrim:SCRIM,font:CAPFONT,
                           text_pct:TPCT,weight:WEIGHT,shadow:SHADOW,
                           url_text:urlOnArt(),url_pct:UPCT,
                           label:instrumentLabel(),stamp:stampText(),
                           scores:scoreLines(),
                           direction:(MKSCORE&&MKSCORE.direction)||''})});
    if(r.error){toast(r.error);repaintCompose();return;}
    made.push(r.media);
  }
  /* A redraw would otherwise leave every earlier attempt in the library.
     Only renders are cleared, never the artwork they came from. */
  const keep=new Set(made.map(m=>m.id));
  for(const id of old){
    const m=MEDIA.find(x=>x.id===id);
    if(m&&m.source==='render'&&!keep.has(id))
      await api(`/api/media/${id}`,{method:'DELETE'});
  }
  await load();
  /* Portrait first: a single-image post uses the first file, and every
     vertical channel wants that one. */
  SEL=made.sort((a,b)=>(a.height>a.width?-1:1)).map(m=>m.id);
  repaintCompose();
  toast(`${made.length} rendered`);
}

/* Build the missing shape out of the one that is there. */
async function makeShape(shape,btn){
  const src=BSRC.map(id=>MEDIA.find(m=>m.id===id)).filter(Boolean);
  const other=shape==='portrait'?'landscape':'portrait';
  const from=slotFor(src,other);
  if(!from){toast(`No ${other} picture to work from`);return;}
  if(btn){btn.disabled=true;btn.textContent='Reshaping…';}
  const r=await api('/api/media/reshape',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:from.id,shape})});
  if(r.error){toast(r.error);repaintCompose();return;}
  await load();
  BSRC=[...new Set([...BSRC,r.media.id])];
  repaintCompose();
  toast(`${shape} made`);
}

function paintCompose(){
  const sel=SEL.map(id=>MEDIA.find(m=>m.id===id)).filter(Boolean);
  const tracks=MEDIA.filter(m=>m.kind==='audio');
  const allPhotos=sel.length>0&&sel.every(m=>m.kind==='image');
  const oneVideo=sel.length===1&&sel[0].kind==='video';
  const canScore=allPhotos||oneVideo;
  if(!canScore){ TRACK=null; TRACKSTART=0; }
  const ppl=people();
  const LINKFIRST=linkFirst()&&/https?:\/\//.test(LIDRAFT||CDRAFT||'');
  const showPeople=$('#wcollab')&&$('#wcollab').checked
                 ||$('#wtag')&&$('#wtag').checked;
  $('#card').innerHTML=`
    <h2>New post</h2>
    <div class="meta">${sel.length} file${sel.length===1?'':'s'} ·
      ${esc((PROFS[PROF]||{}).label||PROF)}</div>

    ${instrumentRow()}
    ${shapeSlots()}

    ${sel.length?'':`<div class="chars" style="margin:0 0 9px">Nothing
      rendered yet, so this post has no picture.</div>`}
    <div class="shots">${sel.map((m,i)=>`<div class="shot"
        oncontextmenu="event.preventDefault();dupSel(${i})">
      ${m.kind==='video'
        ?vplayer(`/media/${m.id}/raw`)
        :`<img src="/media/${m.id}/raw" alt="${esc(m.original)}">`}
      <div class="shotbar">
        <button ${i===0?'disabled':''} onclick="moveSel(${i},-1)"
          title="earlier">&#9664;</button>
        <span>${i+1}</span>
        <button ${i===sel.length-1?'disabled':''} onclick="moveSel(${i},1)"
          title="later">&#9654;</button>
        <button onclick="dupSel(${i})"
          title="use it again">&#10697;</button>
        ${sel.length>1?`<button class="x" onclick="dropSel(${i})"
          title="remove">&times;</button>`:''}
      </div></div>`).join('')}
    </div>
    ${sel.length>1?`<div class="chars">Left to right is the order they appear
      in the carousel. The first one is the cover.</div>`:''}
    ${canScore&&!tracks.length?`<label>Music</label>
      <div class="chars">No audio in the library yet. Drop an MP3, M4A or WAV
        on the Media tab and it will appear here.</div>`:''}
    ${canScore&&tracks.length?`<label for="trk">Music</label>
      <select id="trk" onchange="TRACK=this.value?+this.value:null;
        dropPreview();rememberCopy();paintCompose()">
        <option value="">${oneVideo?"Keep the video's own sound"
          :`No music, post ${sel.length>1?'as a carousel':'as a photo'}`}</option>
        ${tracks.map(t=>`<option value="${t.id}"${TRACK===t.id?' selected':''}
          >${esc(t.original)}${t.seconds?` · ${Math.round(t.seconds)}s`:''}</option>`
        ).join('')}
      </select>
      ${TRACK?`<label>Name the track on the picture</label>
        <div class="credit">
          ${[['off','Do not'],['note','\u266a Title'],['music','Music: Title']]
            .map(([v,l])=>`<label class="cr${CREDIT===v?' on':''}">
              <input type="radio" name="credit" value="${v}"
                ${CREDIT===v?'checked':''} onchange="setCredit('${v}')">
              ${l}</label>`).join('')}
          ${CREDIT!=='off'?`<input type="text" class="song" value="${esc(songName())}"
            placeholder="Song title" oninput="setSong(this.value)"
            onchange="saveSong()">`:''}
        </div>
        <div class="chars">${CREDIT==='off'
          ? 'Small text in the top corner, clear of the platform\'s header.'
          : `Drawn small in the top left. The name is remembered on the track,
             so correcting it here is enough.${PREVIEW&&PREVIEW.credit_fell_back
             ? ' No font on this machine has a music note, so it reads "Music:".'
             : ''}`}</div>
        <label>Where in the track</label>
        <div class="cue">
          <audio id="cueplay" controls preload="metadata"
            src="/media/${TRACK}/raw"></audio>
          <button class="act" onclick="cueHere()">Start here</button>
          ${TRACKSTART?`<button class="act" onclick="TRACKSTART=0;dropPreview();
            rememberCopy();paintCompose()">From the top</button>`:''}
        </div>
        <div class="chars">${TRACKSTART
          ? `Starts ${mmss(TRACKSTART)} in.`
          : 'Starts at the beginning.'} Play it, and press Start here at the
          moment you want the post to begin.</div>
        ${oneVideo?'':`<label>Captions on the picture</label>
          ${CARDS.length?`<div class="cardbar">${CARDS.map((c,i)=>`
            <button class="cardtab${i===CARDI?' on':''}" onclick="pickCard(${i})"
              >${i+1}${(c.text||'').trim()?'':' <span class="fx">empty</span>'}
              ${cardSpans()[i]?`<span class="fn">${
                secs(cardSpans()[i][1]-cardSpans()[i][0])}</span>`:''}</button>`
            ).join('')}
            <button class="cardtab add" onclick="addCard()">+ Card</button>
          </div>`:`<div class="rowb" style="margin-bottom:8px">
            <button class="act" onclick="addCard()">Add a caption</button>
            <button class="act" onclick="draftCards()"
              ${(CDRAFT||'').trim()?'':'disabled'}>Write them from the copy</button>
            <span class="chars" id="cardwhy" style="flex:1"></span>
          </div>`}
          ${CARDS.length?`
          <textarea id="cap" rows="3" class="capbox" placeholder="What this card says. One line per line."
            oninput="setCardText(this.value)">${esc((card()||{}).text||'')}</textarea>
          <div class="chars" id="capnote">${capNote()}</div>
          <div class="caprow">
            <label class="cl">Where
              <select onchange="capSet('pos',this.value)">${
                [['top','Top'],['upper','Upper third'],['middle','Middle'],
                 ['lower','Lower third']].map(([v,l])=>
                `<option value="${v}"${(card()||{}).position===v?' selected':''}
                  >${l}</option>`).join('')}</select></label>
            <label class="cl">Justify
              <select onchange="capSet('align',this.value)">${
                [['left','Left'],['center','Centred'],['right','Right']]
                .map(([v,l])=>`<option value="${v}"${
                  (card()||{}).align===v?' selected':''}>${l}</option>`
                ).join('')}</select></label>
            <button class="act" id="whereb" onclick="showWhere()"
              >Show me where</button>
            <span class="spacer"></span>
            <button class="act" onclick="moveCard(${CARDI},-1)"
              ${CARDI?'':'disabled'} title="earlier">&#9664;</button>
            <button class="act" onclick="moveCard(${CARDI},1)"
              ${CARDI<CARDS.length-1?'':'disabled'} title="later">&#9654;</button>
            <button class="act no" onclick="delCard(${CARDI})">Remove</button>
          </div>
          ${CARDS.length>1?`<div class="rowb" style="margin-top:8px">
            <button class="act" onclick="draftCards()"
              ${(CDRAFT||'').trim()?'':'disabled'}>Rewrite from the copy</button>
            <span class="chars" id="cardwhy" style="flex:1"></span>
          </div>`:''}
          ${CAPFRAME?`<div class="wherebox">
            <img src="${CAPFRAME}" alt="where the caption lands">
            <div class="chars">Red is the platform's furniture, and the box is
              your text. This frame is drawn for checking, never posted.
              <button class="lnk" onclick="hideWhere()">Hide it</button></div>
          </div>`:''}
          <label>How the words look</label>
          <div class="caprow">
            <select title="Typeface" onchange="capSet('font',this.value)"
              onfocus="loadFonts()">${fontOptions()}</select>
            <label class="cl">Size
              <input type="number" min="20" max="200" step="2"
                placeholder="auto" value="${CAPSIZE||''}"
                onchange="capSet('size',this.value)"></label>
            <label class="cl">Colour
              <input type="color" value="${CAPCOL}"
                onchange="capSet('col',this.value)"></label>
            <label class="cl">Shadow
              <input type="number" min="0" max="40" step="1" value="${CAPSHADOW}"
                onchange="capSet('shadow',this.value)"></label>
            <label class="cl">Shadow colour
              <input type="color" value="${CAPSHCOL}"
                onchange="capSet('shcol',this.value)"></label>
            <label class="cl">Arrive
              <select onchange="capSet('fade',this.value)">${
                [[0,'Pop in'],[0.2,'Quick fade'],[0.45,'Slow fade']].map(([v,l])=>
                `<option value="${v}"${CAPFADE==v?' selected':''}>${l}</option>`
                ).join('')}</select></label>
          </div>
          ${CARDS.length>1?`<div class="caprow">
            <label class="cl">Between cards
              <select onchange="capSet('trans',this.value)">${
                [['cut','Cut'],['fade','Fade'],['cross','Cross-fade']]
                .map(([v,l])=>`<option value="${v}"${
                  (TRANS||DEFAULTS.transition)===v?' selected':''}>${l}</option>`
                ).join('')}</select></label>
            <button class="lnk" onclick="saveDefault('transition',
              TRANS||DEFAULTS.transition)">Make that the default</button>
            <label class="cl"><input type="checkbox" ${SNAP?'checked':''}
              onchange="capSet('snap',this.checked)"> Change on a photo</label>
          </div>
          <div class="chars">A card change landing at the same moment as the
            picture reads as deliberate. Off, the cards divide the time by how
            long each takes to read.</div>`:''}
          <div class="chars">Every position is held clear of the bands the
            platform draws its own caption, name and buttons over, so Top means
            under the header rather than against the edge.</div>`:''}
          <label>How long</label>
          <div class="seg" id="lenseg">${[[null,`Auto · ${secs(autoSeconds())}`],
            [7,'7s'],[10,'10s'],[15,'15s'],
            [20,'20s'],[30,'30s']].map(([v,l])=>
            `<button class="${LEN===v?'on':''}" onclick="setLen(${v})"
              >${l}</button>`).join('')}</div>
          <div class="chars">${lenNote(sel.length)}</div>`}
        <div class="rowb" style="margin-top:10px">
          <button class="act" onclick="makePreview()">${PREVIEW
            ? 'Build it again' : 'Preview the video'}</button>
          <span class="chars" id="pvmsg" style="flex:1">${PREVIEW
            ? `${PREVIEW.width}&times;${PREVIEW.height} · ${PREVIEW.seconds}s ·
               ${KB(PREVIEW.bytes)}. This exact file is what posts.${
               PREVIEW.short_by?` The track ran out ${PREVIEW.short_by}s early,
               so it stops there.`:''}` : ''}</span>
        </div>
        ${PREVIEW?`<div class="shot" style="margin-top:9px"
            ondblclick="openViewer('${PREVIEW.url}')">
          ${vplayer(PREVIEW.url)}
          <div class="shotbar"><button onclick="openViewer('${PREVIEW.url}')"
            >Open it bigger</button></div>
        </div>`:''}`:''}
      ${TRACK&&!oneVideo?`<label>How the photos fill the frame</label>
        <div class="seg">${[['blur','Whole photo'],['fill','Fill the frame'],
          ['auto','Fill where it fits']].map(([k,l])=>
          `<button class="${(FITMODE||DEFAULTS.fit_mode)===k?'on':''}"
            onclick="capSet('fitmode','${k}')">${l}</button>`).join('')}</div>
        <div class="chars">${fitNote()}
          <button class="lnk" onclick="saveDefault('fit_mode',
            FITMODE||DEFAULTS.fit_mode)">Make that the default</button></div>
        <label>Colour</label>
        <div class="caprow">
          ${[['brightness','Brightness',-0.5,0.5,0.02],
             ['contrast','Contrast',0.5,2,0.05],
             ['saturation','Colour',0,2,0.05],
             ['warmth','Warmth',-0.5,0.5,0.02]].map(([k,l,lo,hi,st])=>
            `<label class="cl">${l}
              <input type="range" min="${lo}" max="${hi}" step="${st}"
                value="${GRADE[k]}" oninput="setGrade('${k}',this.value)"></label>`
            ).join('')}
          <button class="act" onclick="suggestGrade()">Read the photos</button>
          <button class="act" onclick="resetGrade()">Reset</button>
        </div>
        <div class="chars">Applied to every photo in the post. Read the photos
          measures how dark they are and lifts them if they need it, which is
          usually what a black dog in shade needs.</div>
        <label>Shape</label>
        <div class="seg">${[['portrait','Portrait','1080 x 1920'],
          ['landscape','Landscape','1920 x 1080'],
          ['square','Square','1080 x 1080']].map(([k,l,d])=>
          `<button class="${SHAPE===k?'on':''}" title="${d}"
            onclick="SHAPE='${k}';dropPreview();rememberCopy();paintCompose()"
            >${l}</button>`
          ).join('')}</div>
        <div class="chars">${SHAPE==='portrait'
          ?'Fills the screen on Reels, TikTok and Shorts.'
          :SHAPE==='landscape'?'For YouTube and Facebook. Reels will letterbox it.'
          :'Instagram feed. Safe everywhere, ideal nowhere.'}${shapeHint(sel)}</div>`:''}
      ${TRACK&&oneVideo?`
        <label class="opt${KEEPORIG?' on':''}" style="margin-top:8px">
          <input type="checkbox" ${KEEPORIG?'checked':''}
            onchange="KEEPORIG=this.checked;dropPreview();rememberCopy();paintCompose()">
          Keep the original sound underneath, quieter</label>
        <div class="chars">${KEEPORIG?'The track sits over the original audio.'
          :'The original audio is replaced.'} The picture is copied through
          untouched, so no quality is lost.${trackShort(sel[0])}</div>`
      :TRACK?`<div class="chars">These ${sel.length} photo${sel.length===1?'':'s'}
        become one video with that track under them. Nothing is cropped: each one
        is fitted whole over a blurred copy of itself.</div>`
        :tracks.length?'':`<div class="chars">Upload an audio file and it appears
        here. No API can reach a platform's music library, so the track has to be
        rendered into the file.</div>`}`:''}

    <label>Your copy</label>
    <div class="tabs ctabs">${COPYTABS.map(t=>`
      <button class="${CTAB===t.k?'on':''}" onclick="setCTab('${t.k}')"
        >${t.label}${wants(t.k)?'<span class="dot"></span>':''}</button>`).join('')}
    </div>
    <textarea id="ccopy" placeholder="${esc(tabPlaceholder())}"
      >${esc(tabValue())}</textarea>
    <div class="chars" id="ccnt"></div>
    ${pageUrl()?`<div class="rowb" style="margin-top:8px">
      <button class="act" onclick="otherPart(this)">Different</button>
      <span class="chars" style="flex:1">${SECTS
        ? `Part ${SECTI+1} of ${SECTS.length} from the page. Press again for
           the next one.`
        : `Takes another section of the page this came from, headline and all,
           and puts it in the copy.`}</span>
    </div>`:''}
    ${CTAB==='x'?`<div class="rowb" style="margin-top:8px">
      <button class="act" onclick="shortenForX()"
        ${(CDRAFT||'').trim()?'':'disabled'}>Summarise for X</button>
      <button class="act" onclick="pullCopy('x')"
        ${(CDRAFT||'').trim()?'':'disabled'}>Copy the copy across</button>
      <span class="chars" id="cwhy" style="flex:1"></span>
    </div>
    <div class="chars">${noLink('x')?'X posts go without a link: X charges $0.20 a post that carries one. The graphic prints guavy.com.'
      :'Keeps the link and the voice, cuts the rest to fit.'}
      Nothing is added that the copy did not already say.</div>`:''}
    ${CTAB==='li'?`<div class="rowb" style="margin-top:8px">
      <button class="act" onclick="pullCopy('li')"
        ${(CDRAFT||'').trim()?'':'disabled'}>Copy the copy across again</button>
      <span class="chars" style="flex:1">Started from your copy. Put the
        preface on the front and edit the rest however LinkedIn needs it.</span>
    </div>
    ${noLink('linkedin')?`<div class="chars">LinkedIn posts go without a link:
      it holds back posts that send people off the site.</div>`
      :LINKFIRST?`<div class="chars">The link comes out of the body and goes in
      the first comment, so say where it is rather than "read more here".</div>`
      :''}`:''}
    ${CTAB==='copy'?`<div class="rowb" style="margin-top:8px">
      <button class="act" onclick="claudeCompose()">Write this up for me</button>
      <span class="chars" id="cwhy" style="flex:1"></span>
    </div>`:''}

    <div class="rowb" style="margin:14px 0 0">
      <label class="opt" style="flex:1;margin:0">
        <input type="checkbox" id="wtag" ${showPeople&&$('#wtag')&&$('#wtag').checked?'checked':''}
          onchange="rememberCopy();paintCompose()"> Tag people</label>
      <label class="opt" style="flex:1;margin:0">
        <input type="checkbox" id="wcollab" ${$('#wcollab')&&$('#wcollab').checked?'checked':''}
          onchange="rememberCopy();paintCompose()"> Collaborate</label>
    </div>
    ${showPeople?`<div class="pick" style="max-height:none">
      <div class="none">Collaborators only apply where the platform has them,
        which right now is Instagram. Everyone else is tagged in the copy.</div>
      <div class="psearch">
        <input type="text" id="pq" placeholder="Search, or type a new handle"
          value="${esc(PQ)}" oninput="PQ=this.value;paintPeople()">
        ${PQ.trim()?`<select id="pqplat">${
          Object.keys(PLAT).map(k=>`<option value="${k}"${
            k===PQPLAT?' selected':''}>${esc(PLAT[k].label)}</option>`).join('')}
          </select>
          <button class="act" onclick="addHandle()">Add</button>`:''}
      </div>
      <div id="plist">${peopleRows(ppl)}</div>
    </div>`:''}

    <label>Post to</label>
    <div class="pick" style="max-height:none">
      ${ACCTS.map(a=>{const k=a.channel+'|'+a.profile;return `<label class="slot">
        <input type="checkbox" ${WANT.has(k)?'checked':''}
          onchange="this.checked?WANT.add('${k}'):WANT.delete('${k}')">
        <span style="color:${a.ink}">&#9632;</span>
        <span>${esc(a.label)}</span>
        <span class="sub">${esc(a.profile_label)}${
          a.free_slots?'':' · new slot'}</span></label>`;}).join('')}
    </div>

    <label>When</label>
    <label class="opt on" id="o-auto"><input type="radio" name="w" checked
      onchange="pickWhen('auto')"> Next available slot on each platform</label>
    <label class="opt" id="o-man"><input type="radio" name="w"
      onchange="pickWhen('man')"> A date and time I choose</label>
    <div class="when" id="manbox" hidden>
      <input type="date" id="dd" value="${TODAY()}">
      <input type="time" id="tt" value="09:00">
      <span class="chars">America/Edmonton</span>
    </div>
    <label class="opt" id="o-soon"><input type="radio" name="w"
      onchange="pickWhen('soon')"> Two minutes from now</label>

    <label class="opt" style="margin-top:14px"><input type="checkbox" id="whash"
      ${WHASH?'checked':''} onchange="WHASH=this.checked;repaintCompose()">
      Add hashtags for each platform when it publishes</label>
    ${WHASH?`<div class="chars" style="margin:-4px 0 4px">${hashtagPreview()}</div>`:''}

    <div class="rowb">
      <button class="act" onclick="closeSheet()">Cancel</button>
      <span class="spacer"></span>
      <button class="act go" onclick="${PUBLISHED?'closeSheet()':'publishPost()'}"
        >${PUBLISHED?'Post published (Close)':'Publish post'}</button>
    </div>
    <pre class="hprev" id="sheetout" hidden></pre>`;

  const ta=$('#ccopy');
  const cnt=()=>$('#ccnt').textContent=tabCount(ta.value);
  ta.oninput=()=>{
    ({copy:()=>CDRAFT=ta.value, x:()=>XDRAFT=ta.value,
      li:()=>LIDRAFT=ta.value})[CTAB]();
    cnt();
  };
  cnt();
  pickWhen(WHEN);
}

let CDRAFT='';
function rememberCopy(){
  const ta=$('#ccopy'); if(!ta) return;
  ({copy:()=>CDRAFT=ta.value, x:()=>XDRAFT=ta.value,
    li:()=>LIDRAFT=ta.value})[CTAB]();
}
function closeSheet(){ COMPOSE=false; $('#sheet').classList.remove('on'); }

/* Cuts the approved copy down to something X will take, link and voice kept.
   Asked for rather than run automatically: a 280 character post is a different
   post, and you should see what was dropped before it goes out. */
let FANOUT=false;
/* Only when it does not already fit. The shortener is a model call, and a
   headline plus a link is often inside 280 on its own. */
async function fitXCopy(){
  const lim=(CHAN['x']||{}).max_chars||280;
  if(!XDRAFT||xLen(XDRAFT)<=lim) return;
  const why=$('#cwhy'); if(why) why.textContent='Cutting for X…';
  const r=await api('/api/compose/shorten',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({copy:XDRAFT, profile:PROF, channel:'x'})});
  if(r&&r.copy){ XDRAFT=forX(r.copy); repaintCompose(); toast('X copy cut to fit'); }
  if(why) why.textContent='';
}

async function shortenForX(){
  rememberCopy();
  if(!(CDRAFT||'').trim()){toast('Write the copy first');return;}
  const why=$('#cwhy'); if(why) why.textContent='Cutting it down\u2026';
  const r=await api('/api/compose/shorten',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({copy:forX(CDRAFT), profile:PROF, channel:'x'})});
  if(r.error){ if(why) why.textContent=r.error; toast('Could not shorten it'); return; }
  XDRAFT=forX(r.copy); CTAB='x'; paintCompose();
  const w=$('#cwhy');
  if(w) w.textContent=(r.why||'')+(r.over?` Still ${r.over} over, so trim it.`:'');
  toast(r.over?`${r.counts} characters, still over`:`${r.counts} characters`);
}

/* Hands your description, the source pack and the voice rules to Claude and
   puts the result back in the box, still yours to edit. */
async function claudeCompose(){
  rememberCopy();
  if(!CDRAFT.trim()){toast('Describe it first, even roughly');return;}
  const first=[...WANT][0];
  const why=$('#cwhy'); why.textContent='Writing…';
  const r=await api('/api/compose/draft',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:SEL, profile:PROF, brief:CDRAFT,
      page:pageUrl(),
      channel:first?first.split('|')[0]:null})});
  if(r.error){why.textContent=r.error;toast('Could not write it');return;}
  CDRAFT=r.copy||CDRAFT;
  paintCompose();
  $('#cwhy').textContent=r.thin?('Thin: '+(r.why||'')):(r.why||'');
}

async function publishPost(){
  rememberCopy();
  const accounts=[...WANT].map(k=>({channel:k.split('|')[0],profile:k.split('|')[1]}));
  if(!accounts.length){toast('Pick at least one account');return;}
  if(!CDRAFT.trim()){toast('Write the copy first');return;}
  if(LIVE&&!confirm(`Publish to ${accounts.length} account${
    accounts.length===1?'':'s'}?`)) return;
  const out=$('#sheetout'); out.hidden=false; out.textContent='Publishing…';
  const r=await api('/api/compose',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({media:SEL, instrument:MKTSYM||null,
      article_id:(MKSCORE&&MKSCORE.article_id)||'',
      copy:CDRAFT, copy_x:XDRAFT,
      copy_linkedin:LIDRAFT, accounts, people:ROLES,
      profile:PROF, audio:TRACK, audio_start:TRACKSTART,
      rendered:PREVIEW?PREVIEW.id:null,
      keep_original:KEEPORIG, shape:SHAPE, length:LEN, ...renderOpts(),
      hashtags:WHASH,
      when:{mode:WHEN,
            /* Only when it is the instruction. The date box keeps whatever
               was last typed in it even on Auto, and sending that made the
               desk treat every post as hand-scheduled. */
            date:WHEN==='man'&&$('#dd')?$('#dd').value:null,
            time:WHEN==='man'&&$('#tt')?$('#tt').value:null}})});
  if(r.error){out.textContent=r.error;toast(r.error);return;}
  const lines=(r.created||[]).map(c=>
    `${c.channel} · ${c.date} ${c.time}`
    +(c.collaborators.length?` · collab ${c.collaborators.join(' ')}`:'')
    +(c.tags.length?` · tags ${c.tags.join(' ')}`:'')
    +(c.first_comment?` · link moved to the first comment`:''));
  (r.skipped||[]).forEach(x=>lines.push(`${x.channel}: ${x.why}`));
  (r.pushed||[]).forEach(p=>lines.push(`  ${p.channel}: ${p.out.split('\n').pop()}`));
  if(r.exported) lines.push('',`Saved for hand-uploading: ${r.exported}`);
  if(!r.live) lines.unshift('NOT LIVE. Scheduled here, nothing sent to Zernio.','');
  out.textContent=lines.join('\n');
  toast(`${(r.created||[]).length} scheduled`);
  /* It has gone. The button stops offering to send it again and becomes the
     way out, because the sheet has nothing left to do. */
  if((r.created||[]).length){ PUBLISHED=true; repaintCompose(); }
  SEL=[]; CDRAFT=''; TRACK=null; TRACKSTART=0; KEEPORIG=false; PREVIEW=null;
  LEN=null; CARDS=[]; CARDI=0; CAPFIT={}; XDRAFT=''; LIDRAFT=''; CTAB='copy';
  CREDIT='off'; SONG='';
  await load();
}

function open_(id){
  const seed=POSTS.find(p=>p.id===id); if(!seed)return;
  GROUP = seed.group_id
    ? POSTS.filter(p=>p.group_id===seed.group_id)
        .sort((a,b)=>a.date.localeCompare(b.date)||a.time.localeCompare(b.time))
    : [seed];
  TAB = Math.max(0, GROUP.findIndex(p=>p.id===id));
  paintSheet();
}
function setTab(i){ saveTabLocal(); TAB=i; paintSheet(); }

/* Keep edits to the tab you are leaving, without a round trip. */
function saveTabLocal(){
  const p=GROUP[TAB]; if(!p||!$('#cp'))return;
  p.copy=$('#cp').value; p.first_comment=$('#fc').value;
  if($('#ti')) p.title=$('#ti').value;
  p.date=$('#dd')?$('#dd').value:p.date; p.time=$('#tt')?$('#tt').value:p.time;
  const h=sheetHashtags(); if(h) p.hashtags=JSON.stringify(h);
}

function paintSheet(){
  CUR=GROUP[TAB];
  const p=CUR, lim=p.max_chars, m=MEDIA.find(x=>x.id===p.media_id);
  const plat=(CHAN[p.channel]||{}).platform;
  const col=handlesFor(p,'collab'), tg=handlesFor(p,'tag');
  const hs=((PROFS[p.profile]||{}).voice||{}).hashtags||[];
  const on=new Set(JSON.parse(p.hashtags||'[]'));
  const wantHash=plat==='instagram'||plat==='facebook';

  $('#card').innerHTML=`
    <h2>${esc(title(p.campaign))}</h2>
    ${GROUP.length>1?`<div class="tabs">${GROUP.map((g,i)=>
      `<button class="${i===TAB?'on':''}" onclick="setTab(${i})">
        <span style="color:${cink(g.channel)}">&#9632;</span>
        ${esc((CHAN[g.channel]||{}).label||g.channel)}</button>`).join('')}</div>`:''}
    <div class="meta">
      <b>${esc(p.channel_label)}</b> · ${esc(p.phase)} phase ·
      <span class="state">${NICE[p.state]||p.state}</span>
    </div>
    ${m?`<div class="prev">${m.kind==='video'
        ?vplayer(`/media/${m.id}/raw`)
        :`<img src="/media/${m.id}/raw" alt="${esc(m.original)}">`}</div>
       <div class="chars">${esc(m.original)}${m.note?` · ${esc(m.note)}`:''}</div>`
      :`<div class="chars">No asset attached. ${esc(p.asset_label||'')}</div>`}
    <label for="ml">Asset</label>
    <select id="ml">${mopts(p)}</select>

    ${plat==='youtube'?`<label for="ti">Title</label>
      <input type="text" id="ti" maxlength="100" value="${esc(p.title||'')}"
        placeholder="YouTube will not take a video without one">
      <div class="chars" id="tc"></div>`:''}

    <label for="cp">Copy</label>
    <textarea id="cp">${esc(p.copy||'')}</textarea>
    <div class="chars" id="cc"></div>
    ${p.why?`<div class="why">${esc(p.why)}</div>`:''}

    <label for="fc">First comment</label>
    <input type="text" id="fc" value="${esc(p.first_comment||'')}">

    ${wantHash?`<label>Hashtags</label>
      <div class="chipset" id="hset">${hs.map(h=>
        `<button type="button" class="chip2${on.has(h)?' on':''}"
          onclick="this.classList.toggle('on')" data-h="${esc(h)}">#${esc(h)}</button>`
        ).join('')}</div>
      <input type="text" id="hx" placeholder="more, space separated, no #"
        value="${esc(JSON.parse(p.hashtags||'[]').filter(h=>!hs.includes(h)).join(' '))}">`:''}

    ${col.length?`<label>Collaborators</label>
      <div class="chipset">${chips(p,'collaborators',col)}</div>`:''}
    ${tg.length?`<label>Tag</label>
      <div class="chipset">${chips(p,'tags',tg)}</div>`:''}

    <label>When${GROUP.length>1?', for all '+GROUP.length:''}</label>
    <label class="opt on" id="o-auto"><input type="radio" name="w" value="auto"
      checked onchange="pickWhen('auto')">
      Its next free slot on each account${GROUP.length>1?'':`, ${p.date} at ${p.time}`}</label>
    <label class="opt" id="o-man"><input type="radio" name="w" value="man"
      onchange="pickWhen('man')">Post at a time I choose</label>
    <div class="when" id="manbox" hidden>
      <input type="date" id="dd" value="${p.date}">
      <input type="time" id="tt" value="${p.time}">
      <span class="chars">America/Edmonton</span>
    </div>
    <label class="opt" id="o-soon"><input type="radio" name="w" value="soon"
      onchange="pickWhen('soon')">Two minutes from now</label>

    ${p.zernio_id?`<div class="warn">This is already at Zernio${
      p.state==='published'?' and has published':''}. Saving changes the desk
      only. ${p.state==='published'
        ?'Zernio can replace the text of a published post, but not the media or the time.'
        :'Changing the time cancels it there and re-creates it.'}
      <button class="act" onclick="republish()">Update at Zernio</button></div>`:''}
    <div class="rowb">
      <button class="act" onclick="claudeDraft()">${p.copy?'Rewrite':'Write it'}</button>
      <button class="act" onclick="save()">Save</button>
      <span class="spacer"></span>
      <button class="act no" onclick="decide('reject')">Reject</button>
      <button class="act go" onclick="postIt()">Approve${
        GROUP.length>1?' all '+GROUP.length:''} to post</button>
    </div>
    <pre class="hprev" id="sheetout" hidden></pre>`;

  const ti=$('#ti');
  if(ti){
    const tn=()=>$('#tc').textContent=`${ti.value.length} of 100 characters`
      +(ti.value.trim()?'':'. Left empty, the desk uses the first line of the copy.');
    ti.oninput=tn; tn();
  }
  const ta=$('#cp');
  const cnt=()=>{const n=ta.value.length,c=$('#cc');
    c.textContent=lim?`${n} of ${lim}`:`${n} characters${n>210?' · past the fold at 210':''}`;
    c.classList.toggle('over',!!lim&&n>lim);};
  ta.oninput=cnt; cnt();
  pickWhen(WHEN);
  $('#sheet').classList.add('on');
}

function sheetHashtags(){
  const set=$('#hset');
  if(!set) return null;
  const picked=[...set.querySelectorAll('.chip2.on')].map(b=>b.dataset.h);
  const extra=($('#hx').value||'').split(/[\s,]+/)
    .map(h=>h.replace(/^#/,'').trim()).filter(Boolean);
  return [...new Set([...picked,...extra])];
}

async function claudeDraft(){
  const out=$('#sheetout');
  out.textContent='Writing…'; out.hidden=false;
  const r=await api(`/api/posts/${CUR.id}/draft`,{method:'POST'});
  if(r.error){out.textContent=r.error; toast('Could not write it'); return;}
  await load();
  out.hidden=true;
  open_(CUR.id);
  toast(r.thin?'Written, but it says it is thin':'Written');
  if(r.thin) {const o=$('#sheetout'); o.textContent='Thin: '+r.why; o.hidden=false;}
}

let WHEN='auto';
function pickWhen(w){
  WHEN=w;
  ['auto','man','soon'].forEach(k=>
    $('#o-'+k).classList.toggle('on',k===w));
  $('#manbox').hidden = w!=='man';
}

async function republish(){
  const out=$('#sheetout'); out.hidden=false; out.textContent='Updating at Zernio…';
  await save(true);
  const r=await api(`/api/posts/${CUR.id}/republish`,{method:'POST'});
  out.textContent=r.error||[r.updated==='text'?'Text replaced at Zernio.'
    :'Cancelled and re-created at Zernio.', r.note||'', r.output||''].
    filter(Boolean).join('\n');
  toast(r.error?'Not changed':'Updated at Zernio');
  await load();
}

async function postIt(){
  saveTabLocal();
  const out=$('#sheetout'); out.hidden=false; out.textContent='';
  if(LIVE&&!confirm(WHEN==='soon'
      ? `Send ${GROUP.length} post${GROUP.length===1?'':'s'} in two minutes?`
      : `Approve and schedule ${GROUP.length} post${GROUP.length===1?'':'s'}?`)) return;

  const log=[];
  for(const p of GROUP){
    const body={copy:p.copy, first_comment:p.first_comment||'',
      title:p.title||'', media_id:p.media_id, tags:JSON.parse(p.tags||'[]'),
      collaborators:JSON.parse(p.collaborators||'[]'),
      hashtags:JSON.parse(p.hashtags||'[]')};
    if(WHEN==='man'){ body.date=$('#dd').value; body.time=$('#tt').value; }
    await api(`/api/posts/${p.id}`,{method:'PATCH',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const a=await api(`/api/posts/${p.id}/approve`,{method:'POST'});
    if(a.error){log.push(`${p.channel}: ${a.error}`);continue;}
    const r=await api('/api/push',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:true,id:p.id,soon:WHEN==='soon'})});
    log.push(`${p.channel}: ${(r.output||r.error||'').trim().split('\n').pop()}`);
    if(!r.live) log.push('  (not live, nothing was sent)');
  }
  out.textContent=log.join('\n');
  toast(LIVE?'Done':'Dry run only, the desk is not live');
  await load();
}

function mopts(p){
  const want=(p.asset_type||'').startsWith('video')?'video':
             p.asset_type==='image'?'image':null;
  const list=MEDIA.filter(m=>!want||m.kind===want);
  return `<option value="">Nothing chosen</option>`+list.map(m=>
    `<option value="${m.id}"${m.id===p.media_id?' selected':''}>${esc(m.original)}</option>`
  ).join('')+(list.length?'':`<option disabled>Nothing of that type in the library</option>`);
}

$('#sheet').onclick=e=>{if(e.target.id==='sheet')closeSheet()};
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSheet()});

async function save(quiet){
  await api(`/api/posts/${CUR.id}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({copy:$('#cp').value,first_comment:$('#fc').value,
      title:$('#ti')?$('#ti').value:(CUR.title||''),
      media_id:$('#ml').value?+$('#ml').value:null,
      date:$('#dd').value, time:$('#tt').value,
      hashtags:sheetHashtags()||JSON.parse(CUR.hashtags||'[]'),
      tags:hlist(CUR,'tags'), collaborators:hlist(CUR,'collaborators')})});
  if(!quiet){toast('Saved'); await load(); $('#sheet').classList.remove('on');}
}
async function decide(a){
  await save(true).catch(()=>{});
  const r=await api(`/api/posts/${CUR.id}/${a}`,{method:'POST'});
  if(r.error){toast(r.error);return;}
  toast(a==='approve'?'Approved':'Rejected'); await load();
  $('#sheet').classList.remove('on');
}

/* Approved but never sent: usually a push that failed, or a channel with no
   account. It asks before sending anything, and names every post first. */
$('#pushbtn').onclick=async()=>{
  const r=await api('/api/push',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});
  const list=r.posts||[];
  if(!list.length){toast('Nothing approved is waiting to be sent');return;}
  const lines=list.map(p=>`${p.date}  ${p.channel}`).join('\n');
  if(!confirm(`${list.length} approved post${list.length===1?'':'s'} have not `
    +`reached Zernio:\n\n${lines}\n\nSend ${list.length===1?'it':'them'} now, `
    +`at the time each one is scheduled for?`+(LIVE?'':'\n\nThe desk is NOT LIVE, '
    +'so this will be a dry run.')))return;
  const go=await api('/api/push',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})});
  toast(go.ok&&go.live?'Sent':go.ok?'Dry run only, the desk is not live'
        :(go.error||'Did not go through'));
  load();
};

boot();
</script></body></html>"""


def lan_ip():
    """Best guess at the address a phone on the same wifi should use."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packets sent, just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    MEDIA.mkdir(exist_ok=True)
    host = os.environ.get("HOST", "0.0.0.0")
    # 5001, so this can run beside the Memphis and The Grande desk on 5000.
    port = int(os.environ.get("PORT", "5001"))
    print(f"  Guavynator  http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  This wifi   http://{lan_ip()}:{port}   for uploading from a phone")
        print("  Reachable by anything on this network. HOST=127.0.0.1 to keep it local.")
    # The autoposter runs beside the desk. It does nothing until both the Live
    # switch and the autopost switch are on, so starting it here is safe.
    threading.Thread(target=auto_loop, daemon=True).start()
    threading.Thread(target=wire_loop, daemon=True).start()

    app.run(host=host, port=port, debug=False)
