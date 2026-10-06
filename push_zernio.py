#!/usr/bin/env python3
"""
Hands approved posts to Zernio.

  python3 push_zernio.py accounts             list connected accounts and their ids
  python3 push_zernio.py push --dry-run       show exactly what would be sent
  python3 push_zernio.py push                 schedule every approved post
  python3 push_zernio.py push --id 29         just that one
  python3 push_zernio.py push --id 29 --soon  schedule it 2 minutes out

Nothing is sent that has not been approved in the desk. Channels marked
`delivery: email` in channels.yaml are skipped here: they go to a person, not
to Zernio.

Media is uploaded at push time via the presign endpoint. Those uploads live
for 7 days, which is why this runs near the scheduled date rather than as one
batch over the whole window.
"""

import argparse, json, mimetypes, os, sqlite3, sys, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import yaml

ROOT = Path(__file__).parent
DB = ROOT / "desk.db"
MEDIA = ROOT / "media"
BASE = "https://zernio.com/api/v1"

# "Send now" schedules this many minutes out rather than publishing immediately.
# Everything therefore goes through Zernio's normal scheduled path, which keeps
# the dashboard review that SKILL.md relies on, and leaves a window to cancel.
# publishNow is never set by this file.
SOON_MINUTES = 2


def load_env(path=ROOT / ".env"):
    """Read .env into os.environ without clobbering anything already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def cfg(name):
    try:
        return yaml.safe_load((ROOT / name).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def api(method, path, body=None, key=None, base=BASE):
    key = key or os.environ.get("ZERNIO_API_KEY")
    if not key:
        raise RuntimeError("ZERNIO_API_KEY is not set. Put it in .env.")
    req = urllib.request.Request(
        f"{base}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None


def api_all(path, key=None, limit=100, cap=50):
    """Every page of a paginated list endpoint, not just the first.

    Zernio pages at 50 by default and reports the real total in `pagination`.
    Reading page one only was silently halving every number in the desk.
    """
    out, page = [], 1
    while page <= cap:
        sep = "&" if "?" in path else "?"
        r = api("GET", f"{path}{sep}page={page}&limit={limit}", key=key)
        if isinstance(r, list):
            out.extend(r)
            break
        rows = r.get("posts") or r.get("data") or r.get("accounts") or []
        out.extend(rows)
        pg = r.get("pagination") or {}
        if not rows or page >= (pg.get("pages") or 1):
            break
        page += 1
    return out


# ------------------------------------------------------------------- media

def upload(path: Path, key=None):
    """Presign, PUT the bytes, return the public URL to reference in a post."""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    pre = api("POST", "/media/presign", key=key, body={
        "filename": path.name, "contentType": mime, "size": path.stat().st_size})
    up = pre.get("uploadUrl")
    if not up:
        raise RuntimeError(f"presign returned no uploadUrl: {pre}")
    req = urllib.request.Request(up, method="PUT", data=path.read_bytes(),
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=600):
        pass
    return pre["publicUrl"], mime


# ------------------------------------------------------------------ payload

def account_for(profiles, profile, channel):
    z = ((profiles.get(profile) or {}).get("zernio") or {})
    acct = (z.get("accounts") or {}).get(channel)
    return (None if acct in (None, "", "TODO") else acct,
            os.environ.get(z.get("api_key_env") or "ZERNIO_API_KEY"))


def soon_at(tz):
    """Local wall-clock time SOON_MINUTES from now, in the configured zone."""
    z = ZoneInfo(tz) if (ZoneInfo and tz) else None
    return (datetime.now(z) + timedelta(minutes=SOON_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")


YT_TITLE_MAX = 100          # YouTube's own ceiling


def title_from(text, limit=YT_TITLE_MAX):
    """A headline out of the body of a post.

    YouTube will not take a video without a title, and Zernio holds the dialog
    open until there is one. The first sentence of the copy is almost always
    the right thing, so the desk writes it rather than leaving a post sitting
    in the queue waiting to be typed at.
    """
    text = (text or "").strip()
    if not text:
        return ""
    first = text.splitlines()[0].strip()
    # Angle brackets are refused by YouTube outright.
    first = first.replace("<", "(").replace(">", ")")
    if len(first) <= limit:
        return first
    # One character of the budget belongs to the ellipsis, or the result comes
    # back one over the limit and YouTube refuses it.
    cut = first[:limit - 1]
    space = cut.rfind(" ")
    stem = cut[:space] if space > limit * 0.5 else cut
    return stem.rstrip(" ,;:-\u2014") + "\u2026"


TIKTOK_TITLE_MAX = 90


def tiktok_fields(row, body, items):
    """What TikTok needs on top of an ordinary post.

    TikTok refuses any post without tiktokSettings. Its privacy level must be
    one the account offers, and @guavysentiment3 offers public only. The two
    consent flags are TikTok's legal requirement that the post was seen and
    agreed to before it went: the desk sets them because posting from here,
    by hand or on Auto, is that agreement. Pictures go as a photo post, whose
    `content` is a 90-character title with the full caption in `description`.
    An ad promotes Guavy, which TikTok requires disclosed as the account's
    own brand; a Wire post is not commercial.
    """
    photo = bool(items) and all(i.get("type") == "image" for i in items)
    ad = (row["campaign"] or "") == "ads"
    ts = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True,
          "content_preview_confirmed": True, "express_consent_given": True,
          "commercialContentType": "brand_organic" if ad else "none"}
    out = {}
    if photo:
        ts.update(media_type="photo", photo_cover_index=0,
                  description=body[:4000])
        # The headline where the post has one: a Wire post's copy opens with
        # the article's first paragraph, and 90 characters of that is a
        # sentence cut off, not a title.
        head = (row["title"] or "").strip() if "title" in row.keys() else ""
        out["content"] = title_from(head or body, TIKTOK_TITLE_MAX)
    else:
        ts.update(allow_duet=False, allow_stitch=False)
    out["tiktokSettings"] = ts
    return out


def payload_for(row, platform, account_id, items, tz, soon=False):
    body = (row["copy"] or "").rstrip()
    tags = json.loads(row["tags"] or "[]")
    collabs = json.loads(row["collaborators"] or "[]")
    thashes = json.loads(row["hashtags"] or "[]")

    # Tags are mentions, so they belong in the text. Collaborators are a
    # platform feature and go in the payload, on platforms that have one.
    if tags:
        line = " ".join(t if t.startswith("@") else "@" + t for t in tags)
        if line not in body:
            body = f"{body}\n\n{line}"

    if thashes:
        line = " ".join(h if h.startswith("#") else "#" + h for h in thashes)
        if line not in body:
            body = f"{body}\n\n{line}"

    psd = {}
    if (row["first_comment"] or "").strip():
        psd["firstComment"] = row["first_comment"].strip()
    if collabs and platform == "instagram":
        psd["collaborators"] = [c.lstrip("@") for c in collabs]

    # A top-level field on the Zernio post, not something inside
    # platformSpecificData. Sent for everything, because everything there has
    # one, and required for YouTube.
    title = (row["title"] or "").strip() if "title" in row.keys() else ""
    if not title:
        title = title_from(body)
    if not title and platform == "youtube":
        # Nothing to draw on, and YouTube will not take an empty one.
        title = (row["campaign"] or row["channel_label"] or "New post").strip()
    title = title[:YT_TITLE_MAX]

    out = {"content": body, "title": title,
           "platforms": [{"platform": platform, "accountId": account_id,
                          **({"platformSpecificData": psd} if psd else {})}]}
    if items:
        out["mediaItems"] = list(items)
    if platform == "tiktok":
        out.update(tiktok_fields(row, body, items))
    out["scheduledFor"] = soon_at(tz) if soon else f"{row['date']}T{row['time']}:00"
    out["timezone"] = tz
    return out


# --------------------------------------------------------------------- run

def rows(con, only_id=None):
    q = "SELECT * FROM posts WHERE state='approved'"
    p = []
    if only_id:
        q += " AND id=?"
        p.append(only_id)
    return con.execute(q + " ORDER BY date, time", p).fetchall()


def push(only_id=None, soon=False, dry_run=False):
    load_env()
    chans = cfg("channels.yaml")
    profiles = cfg("profiles.yaml").get("profiles") or {}
    tz = chans.get("timezone", "UTC")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    todo = rows(con, only_id)
    if not todo:
        print("Nothing approved to push." if not only_id
              else f"Post {only_id} is not approved.")
        return 0

    sent = skipped = failed = 0
    for r in todo:
        ch = (chans.get("channels") or {}).get(r["channel"]) or {}
        tag = f"{r['id']:>4} {r['date']} {r['channel']:<14}"

        if (ch.get("delivery") or "zernio") != "zernio":
            print(f"{tag} skipped, this channel is a hand-off, not Zernio")
            skipped += 1
            continue

        account_id, key = account_for(profiles, r["profile"], r["channel"])
        if not account_id:
            print(f"{tag} skipped, no accountId for this channel in profiles.yaml")
            skipped += 1
            continue

        # A carousel is several files in the order they were picked.
        items = []
        ids = json.loads(r["media_ids"] or "[]") or ([r["media_id"]] if r["media_id"] else [])
        for mid in ids:
            m = con.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
            if not m:
                continue
            path = MEDIA / m["path"]
            if dry_run:
                items.append({"type": m["kind"],
                              "url": f"(would upload {path.name}, {m['bytes']} bytes)"})
            else:
                url, _ = upload(path, key)
                items.append({"type": m["kind"], "url": url})
        if not items and r["media_url"]:
            items = [{"type": "video" if (r["asset_type"] or "").startswith("video")
                      else "image", "url": r["media_url"]}]

        # The desk's platform name is not always Zernio's. X is still twitter.
        platform = (ch.get("zernio_platform") or ch.get("platform")
                    or r["channel"].split("_")[0])
        body = payload_for(r, platform, account_id, items, tz, soon)

        if dry_run:
            print(f"{tag} would schedule for {body['scheduledFor']}")
            print(json.dumps(body, indent=2)[:1200])
            continue

        try:
            res = api("POST", "/posts", body=body, key=key)
        except RuntimeError as e:
            print(f"{tag} FAILED  {e}")
            con.execute("UPDATE posts SET state='failed', note=? WHERE id=?",
                        (str(e)[:400], r["id"]))
            con.commit()
            failed += 1
            continue

        # The id has been seen at the top level and nested under post/data.
        node = res.get("post") or res.get("data") or res
        pid = (node.get("_id") or node.get("id")
               or res.get("_id") or res.get("id") or "")
        con.execute("UPDATE posts SET state='scheduled', zernio_id=?, updated=? WHERE id=?",
                    (pid, datetime.now().isoformat(timespec="seconds"), r["id"]))
        con.commit()
        print(f"{tag} scheduled for {body['scheduledFor']}  {pid}")
        sent += 1

    print(f"\n{sent} sent, {skipped} skipped, {failed} failed")
    return failed


def list_accounts():
    load_env()
    res = api("GET", "/accounts")
    items = res if isinstance(res, list) else (res.get("accounts") or res.get("data") or [])
    print(f"{'accountId':<28} {'platform':<12} name")
    for a in items:
        print(f"{a.get('_id',''):<28} {a.get('platform',''):<12} "
              f"{a.get('username') or a.get('name') or ''}")
    print(f"\n{len(items)} connected accounts")
    return items


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts")
    pp = sub.add_parser("push")
    pp.add_argument("--id", type=int)
    pp.add_argument("--soon", "--now", dest="soon", action="store_true",
                    help=f"schedule {SOON_MINUTES} minutes from now instead of "
                         "at the slot time")
    pp.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        sys.exit(0 if a.cmd == "accounts" and list_accounts() is not None
                 else push(a.id, a.soon, a.dry_run) if a.cmd == "push" else 0)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
