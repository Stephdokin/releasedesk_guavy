#!/usr/bin/env python3
"""
Reads the Guavy Wire.

The desk is a separate process from anything else that talks to Guavy, so it
authenticates for itself with GUAVY_API_KEY.

Two things about the API shape drive everything here:

  - Routes are /api/v2/{market}/..., with the market as a path segment. There
    is a /api/v1 but it is crypto-only, and it answers a wrong path with a
    hint saying so rather than a 404 you have to guess at.
  - Briefs are per symbol. There is no "everything happening in this market"
    endpoint, so finding the most important news means asking each symbol and
    sorting what comes back. Markets are small, 7 to 10 symbols each, so that
    is a handful of calls rather than a crawl.

  python3 guavy.py symbols crypto
  python3 guavy.py top crypto            the best brief right now
"""

import json, os, sys, time
import urllib.error, urllib.parse, urllib.request

BASE = os.environ.get("GUAVY_API_BASE", "https://guavy.com/api/v2")
MARKETS = ("crypto", "stocks", "forex", "commodities")

# Calls are token-metered, and a poll asks every symbol in a market. Holding
# the answers briefly turns four sub-tabs redrawing into one round of calls.
_CACHE = {}
CACHE_SECONDS = 180


class GuavyError(RuntimeError):
    pass


def key():
    k = os.environ.get("GUAVY_API_KEY")
    if not k:
        raise GuavyError("GUAVY_API_KEY is not set. Put it in .env.")
    return k


def api(path, timeout=30, cache=True):
    """One GET against the Wire."""
    url = f"{BASE}{path}"
    hit = _CACHE.get(url)
    if cache and hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key()}",
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read(400).decode("utf-8", "replace")
        try:
            got = json.loads(body)
            msg = got.get("error") or body
            if got.get("hint"):
                msg = f"{msg} ({got['hint']})"
        except ValueError:
            msg = body
        raise GuavyError(f"Guavy said {e.code}: {msg}"[:400])
    except Exception as e:
        raise GuavyError(f"Could not reach Guavy: {e}"[:300])
    if cache:
        _CACHE[url] = (time.time(), out)
    return out


SYMBOL_PAGE = 500


def symbols(market, cap=2000):
    """Every symbol in a market, paged.

    The endpoint takes `limit` and `skip`, and its documented default is 100.
    It actually returns 10, which is how the desk spent a while believing
    crypto had ten coins in it and no Bitcoin. Ask explicitly, and keep asking
    until a page comes back short.
    """
    out, skip = [], 0
    while len(out) < cap:
        got = api(f"/{market}/instruments/list-symbols"
                  f"?limit={SYMBOL_PAGE}&skip={skip}")
        page = (got.get("symbols") or got.get("data")
                or got.get("instruments") or [])
        page = [s if isinstance(s, str) else (s.get("symbol") or s.get("ticker"))
                for s in page if s]
        out += page
        if len(page) < SYMBOL_PAGE:
            break
        skip += SYMBOL_PAGE
    # The Wire repeats a symbol across pages now and then; order is kept.
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def briefs(market, symbol, limit=10):
    out = api(f"/{market}/newsroom/get-recent-briefs/"
              f"{urllib.parse.quote(str(symbol))}?limit={int(limit)}")
    return out.get("briefs") or out.get("data") or []


# The Wire's public URL carries the article id as base62 of the uuid, with the
# uppercase alphabet first, appended to a slug of the headline. The slug is
# decoration: /wire/{market}/{id} 301s to the canonical address, so the link is
# built from the id and then resolved rather than reproducing the slug rule.
B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SITE = os.environ.get("GUAVY_SITE", "https://guavy.com")


def short_id(article_id):
    """base62 of the article uuid, 22 characters."""
    import uuid as _uuid
    n = _uuid.UUID(str(article_id)).int
    s = ""
    while n:
        n, r = divmod(n, 62)
        s = B62[r] + s
    return s.rjust(22, B62[0])


def wire_url(market, article_id, resolve=True, timeout=15):
    """Where this article lives on guavy.com/wire.

    Returns the canonical address when the site will tell us, and the short
    form otherwise. The short form works either way: it redirects.
    """
    try:
        short = f"{SITE}/wire/{market}/{short_id(article_id)}"
    except (ValueError, AttributeError):
        return ""
    if not resolve:
        return short

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise _Moved(newurl)

    class _Moved(Exception):
        def __init__(self, url):
            self.url = url

    op = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(short, method="HEAD",
                                 headers={"User-Agent": "Guavynator"})
    try:
        op.open(req, timeout=timeout)
    except _Moved as m:
        return urllib.parse.urljoin(short, m.url)
    except Exception:
        pass
    return short


def article(market, article_id):
    return api(f"/{market}/newsroom/get-article/"
               f"{urllib.parse.quote(str(article_id))}")


# Markets that are not instruments. They appear in a brief's `symbols` and
# would otherwise be chosen as the thing to draw.
NOT_AN_INSTRUMENT = {"crypto", "stocks", "forex", "commodities", "equities"}


def primary_symbol(brief, found_under=None):
    """Which instrument a brief is actually about.

    Not the feed it was found in. One story reaches several symbols, so a
    Bitcoin story turns up in XRP's briefs too, and branding it from the feed
    would draw a Bitcoin headline in XRP's colours. The API already answers
    this: impacted_coins carries a confidence per asset, so the most confident
    one wins, then the first named symbol, then the feed as a last resort.
    """
    # impacted_coins is a list of objects in crypto and a list of bare strings
    # in some of the other markets, so both shapes are read.
    hits = []
    for c in (brief.get("impacted_coins") or []):
        if isinstance(c, str):
            asset, conf = c, 0.0
        elif isinstance(c, dict):
            asset, conf = c.get("asset"), float(c.get("confidence") or 0)
        else:
            continue
        if asset and str(asset).lower() not in NOT_AN_INSTRUMENT:
            hits.append((conf, asset))
    if hits:
        return max(hits, key=lambda h: h[0])[1]
    for s in (brief.get("symbols") or []):
        if s and str(s).lower() not in NOT_AN_INSTRUMENT:
            return s
    return found_under


def when(brief):
    """Milliseconds since the epoch, however the brief carries it."""
    ts = brief.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    return 0.0


SENTIMENT_MAX = 5.0          # the scale the API scores on, give or take


def weight(brief, w_sentiment=0.6, w_clout=0.4):
    """How much a brief is worth posting, 0 to 1.

    Clout alone is a poor sort. Across a full sweep of all four markets it runs
    20 to 100 with a median of 82, so nearly everything is loud and the ranking
    barely moves. Sentiment is what separates: it runs about -4 to +4, and what
    matters is distance from zero in either direction, because a strongly
    negative story is as worth posting as a strongly positive one. Neutral is
    the thing to skip.
    """
    s = abs(float(brief.get("sentiment") or 0)) / SENTIMENT_MAX
    c = float(brief.get("clout") or 0) / 100.0
    return w_sentiment * min(s, 1.0) + w_clout * min(c, 1.0)


def scan(market, since_ms=0.0, min_clout=0.0, per_symbol=5,
         min_sentiment=0.0, w_sentiment=0.6, w_clout=0.4):
    """Every brief in a market newer than `since_ms`, best first.

    Ranked by `weight`, which is mostly how far the sentiment is from neutral
    and partly how loud the story is. Deduplicated on article_id: one story
    reaches several symbols, and posting it once per affected coin would be
    the same post four times.
    """
    seen, out = set(), []
    for sym in symbols(market):
        try:
            got = briefs(market, sym, per_symbol)
        except GuavyError:
            continue          # one quiet symbol should not stop the scan
        for b in got:
            aid = b.get("article_id")
            if not aid or aid in seen:
                continue
            if when(b) <= since_ms:
                continue
            if float(b.get("clout") or 0) < min_clout:
                continue
            if abs(float(b.get("sentiment") or 0)) < min_sentiment:
                continue
            seen.add(aid)
            out.append(dict(b, market=market, symbol=primary_symbol(b, sym),
                            found_under=sym,
                            weight=round(weight(b, w_sentiment, w_clout), 4)))
    out.sort(key=lambda b: (b["weight"], when(b)), reverse=True)
    return out


def top(market, since_ms=0.0, min_clout=0.0, min_sentiment=0.0,
        w_sentiment=0.6, w_clout=0.4):
    got = scan(market, since_ms, min_clout, 5, min_sentiment,
               w_sentiment, w_clout)
    return got[0] if got else None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    mkt = sys.argv[2] if len(sys.argv) > 2 else "crypto"
    try:
        if cmd == "symbols":
            print(json.dumps(symbols(mkt), indent=2))
        elif cmd == "scan":
            for b in scan(mkt)[:10]:
                print(f"{b['clout']:>6}  {b['date']}  {b['symbol']:<6} "
                      f"{b['title'][:70]}")
        else:
            print(json.dumps(top(mkt), indent=2)[:1500])
    except GuavyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
