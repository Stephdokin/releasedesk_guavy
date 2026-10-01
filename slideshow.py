#!/usr/bin/env python3
"""
Turns a set of photos and a piece of music into one video.

Platform music libraries are not reachable from any API, so the only way to
put a track under a post is to render it into the file before it goes out.
That is what this does: photos, a track, one MP4.

  python3 slideshow.py out.mp4 a.jpg b.jpg --audio track.mp3

ffmpeg comes from the imageio-ffmpeg wheel, so there is nothing to install
separately and nothing on PATH to depend on.
"""

import argparse, hashlib, os, re, shutil, subprocess, sys, tempfile
from fractions import Fraction
from pathlib import Path

SHAPES = {
    "portrait":  (1080, 1920),   # Reels, TikTok, Shorts
    "landscape": (1920, 1080),   # YouTube, Facebook video
    "square":    (1080, 1080),   # Instagram feed
}
SIZE = SHAPES["portrait"]
PER = 3.0                # one photo in a run of photos
SINGLE = 7.0             # one photo on its own
AUTO_MAX = 30.0          # longest the desk will choose by itself
MAX = 90.0               # longest it will render at all, asked for or not
FADE = 1.5               # audio fade at the end, scaled down on short clips
MIN_HOLD = 1.2           # faster than this and a photo is a flicker
MAX_FRAMES = 600         # a ceiling on the concat list, not a real limit


def exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path):
    """Seconds, read out of ffmpeg's own report. None if it cannot tell."""
    r = subprocess.run([exe(), "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


# Fonts come off the machine rather than being bundled, so the list is
# whatever is actually installed. Collections (.ttc) are skipped: they hold
# several faces behind one path and drawtext cannot be told which.
FONT_DIRS = ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts",
             "/System/Library/Fonts/Supplemental"]
# Faces that hold up at a glance on a phone, in the order they are worth
# trying. Condensed and heavy reads at distance; a text face does not.
FONT_PICKS = ["BebasNeue-Regular", "Bebas-Regular", "DIN Condensed Bold",
              "Oswald-Bold", "Anton-Regular", "Impact",
              "leaguegothic-condensed-regular-webfont",
              "RobotoCondensed-Bold", "Arial Black", "Arial Bold",
              "HelveticaNeue", "Arial"]


def fonts():
    """Every usable font file on this machine, by display name."""
    out = {}
    for d in FONT_DIRS:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for f in sorted(root.iterdir()):
            if f.suffix.lower() in (".ttf", ".otf") and f.is_file():
                out.setdefault(f.stem, str(f))
    return out


def default_font():
    """The best face present for a caption, or any face at all."""
    have = fonts()
    for name in FONT_PICKS:
        if name in have:
            return have[name]
    return next(iter(have.values()), None)


# Where a caption may sit. On vertical the platforms draw their own caption,
# username, audio ticker and buttons over the bottom of the frame, so the
# "lower third" of film convention is exactly the wrong place for it: anything
# below about 78% of the height gets covered on Reels and TikTok. Feed shapes
# are far less aggressive.
SAFE_BOTTOM = {True: 0.245, False: 0.10}      # keyed by "is it portrait"
# The top has its own furniture: the Reels header, TikTok's tabs, the status
# bar. Less of it than the bottom, but enough to stay out of.
# On a portrait, 0.15 also keeps the block inside the middle 4:5 that
# Instagram's feed shows of a 9:16 picture: (1 - 9/16 * 5/4) / 2 is 0.148.
SAFE_TOP = {True: 0.15, False: 0.06}
# Where the block sits inside what is left, 0 being as high as it may go and 1
# as low. Every one of these is clamped into the safe band, so "top" is below
# the platform's own header rather than against the edge of the frame.
POSITIONS = {"top": 0.0, "upper": 0.33, "middle": 0.5, "lower": 1.0}
ALIGNS = ("left", "center", "right")
CAP_SIDE = 0.07          # margin from the frame edge when not centred
CAP_STEP = 0.45          # seconds between one line appearing and the next
CAP_FADE = 0.35          # how long a line takes to come up
CAP_DELAY = 0.30         # let the picture land before any words arrive
CAP_LINE = 1.25          # line height, as a multiple of the font size
CAP_WIDTH = 0.86         # most of the frame a line may occupy
CAP_READ = 2.5           # words a second, for text nobody is concentrating on
CAP_MIN_READ = 1.5       # even two words need a beat to register


def caption_seconds(lines, words, fade=CAP_FADE, step=CAP_STEP,
                    delay=CAP_DELAY):
    """How long a post has to run for its caption to be readable.

    The last line lands at delay + the stagger + its own fade, and then it has
    to be read. Auto length uses this, so adding a line to a caption lengthens
    the post rather than leaving the words to be cut off.
    """
    if not lines:
        return 0.0
    return (delay + (lines - 1) * step + fade
            + max(words / CAP_READ, CAP_MIN_READ))


def ff_color(c, default="white"):
    """#RRGGBB to the 0xRRGGBB ffmpeg wants. Names and name@alpha pass through."""
    c = (c or "").strip() or default
    m = re.fullmatch(r"#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})(@[\d.]+)?", c)
    return f"0x{m.group(1)}{m.group(2) or ''}" if m else c


def text_width(text, font, size):
    """How wide a line actually is, in pixels.

    ffmpeg's expression evaluator has a print() that writes to the log, and
    drawtext exposes text_w, so asking it is exact. Guessing from character
    counts is not: the whole point of a condensed face is that it lies about
    how wide it will be.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
        tf = f.name
    try:
        vf = (f"drawtext=fontfile='{font}':textfile='{tf}':expansion=none:"
              f"fontsize={size}:fontcolor=white:y=0:x='print(text_w)'")
        r = subprocess.run([exe(), "-hide_banner", "-f", "lavfi", "-i",
                            "color=c=black:s=64x64:d=0.04", "-vf", vf,
                            "-frames:v", "1", "-f", "null", "-"],
                           capture_output=True, text=True)
        found = re.findall(r"^(\d+\.\d+)$", r.stderr, re.M)
        return float(found[-1]) if found else float(len(text) * size * 0.5)
    finally:
        Path(tf).unlink(missing_ok=True)


def wrap_line(line, font, size, limit, budget=None):
    """Break one long line on word boundaries so it fits the frame."""
    if text_width(line, font, size) <= limit:
        return [line]
    out, cur = [], ""
    for word in line.split():
        trial = f"{cur} {word}".strip()
        if budget is not None and budget[0] <= 0:
            cur = trial
            continue
        if budget is not None:
            budget[0] -= 1
        if cur and text_width(trial, font, size) > limit:
            out.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out


def caption_layout(text, font, size, w, max_lines=5):
    """The lines to draw and the size they fit at.

    A break you typed is kept: where a caption turns is an editorial choice,
    not something to re-flow behind your back. A line you did not break is
    wrapped rather than shrunk, because a headline squeezed down to fit on
    one line ends up too small to read, which defeats the point of putting it
    on the picture at all. Only when the wrapped block is still too tall does
    the size come down.
    """
    typed = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not typed:
        return [], size
    limit = w * CAP_WIDTH
    budget = [120]              # a ceiling on measurements, not a real limit
    lines = typed
    for _ in range(12):
        lines = [piece for l in typed
                 for piece in wrap_line(l, font, size, limit, budget)]
        if len(lines) <= max(max_lines, len(typed)) or size <= 24:
            break
        size = max(24, int(size * 0.88))
    # Whatever is left over after wrapping, shrink to fit.
    for _ in range(12):
        widest = max(text_width(l, font, size) for l in lines)
        if widest <= limit or size <= 16:
            break
        size = max(16, int(size * min(0.92, limit / widest)))
    return lines, size


def caption_box(n, size, w, h, position="lower"):
    """Where the block of lines starts, and how tall each line is.

    The band runs from under the platform's header to above its caption and
    buttons. The position picks a place inside that band, and the block is
    clamped into it, so a caption can sit high on the picture without ever
    landing under something the platform draws on top.
    """
    lh = size * CAP_LINE
    top_edge = h * SAFE_TOP[h > w]
    bottom_edge = h - h * SAFE_BOTTOM[h > w]
    room = (bottom_edge - top_edge) - lh * n
    f = POSITIONS.get(position, POSITIONS["lower"])
    return lh, max(top_edge, top_edge + max(room, 0) * f)


TRANSITIONS = ("cut", "fade", "cross")


def normalise_cards(cards, caption=None, position="lower", align="center"):
    """One list of cards, whatever it arrived as.

    A plain caption is card one, so a post written before cards existed still
    renders the same way.
    """
    out = []
    for c in (cards or []):
        text = (c.get("text") or "").strip() if isinstance(c, dict) else str(c).strip()
        if not text:
            continue
        d = c if isinstance(c, dict) else {}
        out.append(dict(text=text,
                        position=d.get("position") or position,
                        align=d.get("align") or align))
    if not out and (caption or "").strip():
        out = [dict(text=caption.strip(), position=position, align=align)]
    return out


def layout_cards(cards, font, size, w, fade=CAP_FADE):
    """Break every card into lines, at one size they all agree on.

    Sized card by card, a two word card would tower over a ten word one and
    the post would look like it changed its mind. The smallest size that any
    card needs is the size they all get.
    """
    if not cards:
        return [], size
    laid = [caption_layout(c["text"], font, size, w) for c in cards]
    common = min(sz for _, sz in laid)
    if any(sz != common for _, sz in laid):
        laid = [caption_layout(c["text"], font, common, w) for c in cards]
    out = []
    for c, (lines, _) in zip(cards, laid):
        out.append(dict(c, lines=lines,
                        need=caption_seconds(len(lines), len(c["text"].split()),
                                             fade=fade)))
    return out, common


def card_times(needs, total, hold, snap=True, least=1.2):
    """When each card starts and stops.

    Time is shared out by how long each card takes to read, not evenly: a card
    of ten words earns more of the post than a card of four. Boundaries are
    then pulled onto photo changes where that is close, because a caption that
    changes at the same instant as the picture reads as deliberate and one that
    changes halfway through a photo reads as a separate layer flickering over
    the top.
    """
    if not needs:
        return []
    needs = [max(float(n), 0.1) for n in needs]
    scale = total / sum(needs)
    ends, acc = [], 0.0
    for n in needs:
        acc += n * scale
        ends.append(acc)
    ends[-1] = total

    if snap and hold > 0.01:
        for i in range(len(ends) - 1):
            want = round(ends[i] / hold) * hold
            floor_ = (ends[i - 1] if i else 0.0) + least
            ceil_ = ends[i + 1] - least
            # Only if the photo change is somewhere sensible; otherwise the
            # card would be squeezed to nothing to reach it.
            if floor_ <= want <= ceil_:
                ends[i] = want

    out, start = [], 0.0
    for e in ends:
        out.append((round(start, 3), round(e, 3)))
        start = e
    return out


def card_alpha(start, end, fade, transition, last):
    """The alpha ramp for one line: in, hold, and out again.

    A single card holds to the end the way it always has. A card with another
    behind it has to leave, or they pile up on top of each other.
    """
    if fade <= 0 or transition == "cut":
        return (f"'gte(t,{start:.2f})'" if last
                else f"'gte(t,{start:.2f})*lt(t,{end:.2f})'")
    up = (f"if(lt(t,{start:.2f}),0,"
          f"if(lt(t,{start + fade:.2f}),(t-{start:.2f})/{fade:.2f},1))")
    if last:
        return f"'{up}'"
    out_at = max(end - fade, start + fade)
    return (f"'min({up},"
            f"if(lt(t,{out_at:.2f}),1,max(0,({end:.2f}-t)/{fade:.2f})))'")


def caption_filters(lines, size, w, h, color="white", shadow=4,
                    shadow_color="black@0.7", font=None, work=None,
                    step=CAP_STEP, fade=CAP_FADE, delay=CAP_DELAY,
                    position="lower", align="center", start=0.0, end=None,
                    transition="fade", last=True, tag=0):
    """One drawtext per line, each coming up a beat after the one above it.

    The text goes in a file rather than inline so a caption can hold colons,
    quotes, percent signs and apostrophes without being escaped into soup.
    """
    if not lines:
        return []
    lh, top = caption_box(len(lines), size, w, h, position)
    side = int(w * CAP_SIDE)
    x = {"left": f"{side}", "right": f"w-text_w-{side}"}.get(
        align, "(w-text_w)/2")
    if end is None:
        end = start + 1e6
    out = []
    for i, line in enumerate(lines):
        f = Path(work) / f"cap{tag:02d}_{i:02d}.txt"
        f.write_text(line, encoding="utf-8")
        at = start + delay + i * step
        alpha = card_alpha(at, end, fade, transition, last)
        bits = [f"fontfile='{font}'", f"textfile='{f}'", "expansion=none",
                f"fontsize={size}", f"fontcolor={ff_color(color)}",
                f"x={x}", f"y={int(top + i * lh)}", f"alpha={alpha}"]
        if shadow:
            bits += [f"shadowx={int(shadow)}", f"shadowy={int(shadow)}",
                     f"shadowcolor={ff_color(shadow_color, 'black@0.7')}"]
        out.append("drawtext=" + ":".join(bits))
    return out


def still(image, out, size=SIZE, caption=None, font=None, cap_size=None,
          cap_color="white", cap_shadow=4, cap_shadow_color="black@0.7",
          cap_position="lower", cap_align="center", guides=False,
          scale_to=None):
    """One frame with the caption already up, for checking where it lands.

    Rendering a whole video to find out the words are sitting under the Reels
    buttons is a slow way to learn it. This draws the same filters at full
    alpha, so what you see is where the text goes.

    With `guides`, the bands the platform draws its own furniture over are
    shaded, and the block is boxed. Those are drawn for looking at, never for
    posting: nothing calls this on the way out.
    """
    w, h = size
    work = Path(tempfile.mkdtemp(prefix="still-"))
    try:
        base = fit(image, work / "base.png", w, h)
        chain = []
        if guides:
            top = int(h * SAFE_TOP[h > w])
            bot = int(h * SAFE_BOTTOM[h > w])
            chain += [f"drawbox=x=0:y=0:w={w}:h={top}:color=red@0.30:t=fill",
                      f"drawbox=x=0:y={h - bot}:w={w}:h={bot}:"
                      f"color=red@0.30:t=fill"]
        lines, drawn = [], cap_size or max(28, int(h * 0.045))
        face = font or default_font()
        if (caption or "").strip() and face:
            shutil.copyfile(face, work / "face.ttf")
            face = work / "face.ttf"
            lines, drawn = caption_layout(caption, face, drawn, w)
            if guides and lines:
                lh, top_y = caption_box(len(lines), drawn, w, h, cap_position)
                chain.append(
                    f"drawbox=x={int(w * CAP_SIDE)}:y={int(top_y)}:"
                    f"w={int(w * (1 - 2 * CAP_SIDE))}:h={int(lh * len(lines))}:"
                    f"color=white@0.55:t=2")
            # No stagger and no fade: this is the state after everything is up.
            chain += caption_filters(lines, drawn, w, h, color=cap_color,
                                     shadow=cap_shadow, font=face, work=work,
                                     shadow_color=cap_shadow_color,
                                     step=0, fade=0, delay=0,
                                     position=cap_position, align=cap_align)
        if scale_to:
            # Scaled last, so the text is laid out against the real frame and
            # only the picture you are shown is smaller.
            chain.append(f"scale={int(scale_to)}:-2:flags=lanczos")
        cmd = [exe(), "-y", "-i", str(base)]
        if chain:
            cmd += ["-vf", ",".join(chain)]
        cmd += ["-frames:v", "1", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-400:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dict(path=str(out), width=w, height=h, lines=len(lines),
                size=drawn if lines else None)


# Words that end an audio filename without being part of the track's name.
# Only stripped from the end, where they actually live: a track may well be
# called "Demo" or "300".
TRACK_NOISE = {"final", "mix", "mixed", "master", "mastered", "remaster",
               "reference", "ref", "rough", "demo", "edit", "version", "ver",
               "bounce", "print", "wip", "draft", "clean", "explicit", "mp3",
               "wav", "instrumental", "inst"}
TRACK_KEY = re.compile(r"^[A-G](#|b)?(maj|min|m|M)\d*$")
CREDIT_NOTE = "\u266a"       # a quaver, not the emoji: see has_glyph()
CREDIT_SIDE = 0.05           # margin in from the frame edge
CREDIT_SIZE = 0.022          # of the frame height


def track_title(name):
    """A track title out of whatever the file happens to be called.

    Only a starting point; the desk lets you correct it and remembers what you
    typed. Trailing junk is stripped, not junk anywhere, so a track called
    "Demo" survives while "Track Name Final Mix 116 Fmaj" does not keep its tail.
    """
    t = Path(name or "").stem
    t = re.sub(r"^[0-9a-f]{8,16}-", "", t)          # the library's content hash
    t = t.replace("_", " ").replace("-", " ")
    t = re.sub(r"^\s*\d{1,3}\s*[.)]?\s+", "", t)    # a leading track number
    parts = t.split()
    while parts:
        last = parts[-1].strip(".,()[]")
        if (last.lower() in TRACK_NOISE or TRACK_KEY.match(last)
                or re.fullmatch(r"\d{2,3}", last)      # a tempo
                or re.fullmatch(r"v\d+", last.lower())):
            parts.pop()
            continue
        break
    return " ".join(parts).strip(" -_") or Path(name or "").stem


_GLYPH = {}


def has_glyph(ch, font):
    """Whether a font actually has a character, rather than a box in its place.

    Asked because the obvious choice here does not work: drawtext cannot load
    Apple Color Emoji at all, and a display face like Bebas Neue has no music
    symbol, so the note would come out as tofu with nothing to warn you.
    """
    key = (ch, str(font))
    if key in _GLYPH:
        return _GLYPH[key]

    def ink(text):
        work = Path(tempfile.mkdtemp(prefix="glyph-"))
        try:
            f = work / "g.txt"
            f.write_text(text, encoding="utf-8")
            vf = (f"drawtext=fontfile='{font}':textfile='{f}':expansion=none:"
                  f"fontsize=64:fontcolor=white:x=10:y=10,format=gray")
            r = subprocess.run([exe(), "-hide_banner", "-loglevel", "error",
                                "-f", "lavfi", "-i", "color=c=black:s=120x100:d=0.04",
                                "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-"],
                               capture_output=True)
            return hashlib.md5(r.stdout).hexdigest() if r.stdout else None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    got = ink(ch)
    # Compared against unassigned codepoints, never Private Use ones: a font
    # may carry real glyphs in the PUA, and DIN Condensed does, so its box for
    # a missing note looked like a hit and the credit drew tofu.
    blanks = {ink(c) for c in ("\u0888", "\u05ff", "\u2fe0")}
    _GLYPH[key] = bool(got) and got not in blanks and got != ink("")
    return _GLYPH[key]


# Faces to borrow a music note from, in order, when the caption's own face has
# none. The credit is one small line in a corner, so a different face there is
# not worth noticing; tofu would be.
NOTE_FONTS = ["Arial Bold", "Arial", "Impact", "Arial Unicode", "Verdana",
              "HelveticaNeue", "Georgia"]


def note_font(font):
    """A face that can actually draw a quaver, preferring the one in use."""
    if font and has_glyph(CREDIT_NOTE, font):
        return font
    have = fonts()
    for name in NOTE_FONTS:
        p = have.get(name)
        if p and has_glyph(CREDIT_NOTE, p):
            return p
    return None


def credit_text(title, style, font):
    """The line to draw, the face to draw it in, and whether the note was lost.

    The emoji is not an option: drawtext cannot load Apple Color Emoji at all.
    A quaver is a real character in a text face and draws in one colour, which
    is what a corner overlay wants anyway, but a display face often has no
    music symbol: Bebas Neue has none, and DIN Condensed draws a box. So the
    note is borrowed from a face that has one, and only if nothing on the
    machine doesdoes the credit fall back to the word "Music".
    """
    title = (title or "").strip()
    if not title:
        return "", font, False
    if style == "music":
        return f"Music: {title}", font, False
    face = note_font(font)
    if face:
        return f"{CREDIT_NOTE} {title}", face, False
    return f"Music: {title}", font, True


def credit_filter(text, w, h, font=None, work=None, size=None, color="white",
                  shadow=3, shadow_color="black@0.75"):
    """A small line in the top corner naming the track, out of the way."""
    if not text:
        return []
    size = size or max(20, int(h * CREDIT_SIZE))
    f = Path(work) / "credit.txt"
    f.write_text(text, encoding="utf-8")
    bits = [f"fontfile='{font}'", f"textfile='{f}'", "expansion=none",
            f"fontsize={size}", f"fontcolor={ff_color(color)}",
            f"x={int(w * CREDIT_SIDE)}",
            # Under the platform's own header, same as the caption.
            f"y={int(h * SAFE_TOP[h > w])}"]
    if shadow:
        bits += [f"shadowx={int(shadow)}", f"shadowy={int(shadow)}",
                 f"shadowcolor={ff_color(shadow_color, 'black@0.75')}"]
    return ["drawtext=" + ":".join(bits)]


def plan(n, target=None, per=None, max_len=MAX, floor=0):
    """How long each photo holds, and how long the whole post runs.

    The photos set the pace and the music is cut to fit, not the other way
    round. Stretching one still across a four minute track is dead air on
    every feed the desk posts to: the watch-through collapses and the ranking
    reads it as something nobody stayed for.

    One photo on its own gets SINGLE seconds, long enough to read the picture
    and land a hook of the track, short enough that it comes round two or
    three times in a scroll stop. Replays count, so a short clip that loops
    beats a long one that does not. A run of photos gets PER each, about as
    fast as an eye can take a photograph in and still see it.

    Two rules hold whatever is asked for. Every photo picked appears at least
    once, so a long set speeds up rather than losing its tail. And a length
    asked for is filled by a whole number of passes, nudging the hold by a
    fraction of a second, so the clip loops on the cut instead of stopping
    halfway through the set.

    `floor` is the least the post can run for and still be readable, which is
    what a caption sets. It lifts an automatic length and is ignored when a
    length was asked for.

    Returns (hold, total).
    """
    if n <= 0:
        raise ValueError("no images")
    hold = float(per) if per else (SINGLE if n == 1 else PER)
    hold = max(hold, MIN_HOLD)

    if target:
        total = min(max(float(target), MIN_HOLD), max_len)
    else:
        total = min(hold * n, AUTO_MAX)
        # A caption that needs longer than the pictures do wins. Asking for a
        # length explicitly does not get overridden: the note says it is tight
        # and leaves the choice alone.
        if floor:
            total = min(max(total, float(floor)), max_len)

    if n == 1:
        hold = total                      # one photo: a loop is just a hold
    elif hold * n > total:
        hold = total / n                  # speed up so the tail is not lost
    else:
        passes = max(1, round(total / (hold * n)))
        hold = total / (passes * n)       # whole passes, so it loops on the cut

    if hold < MIN_HOLD:                   # too many photos to flick through
        hold = MIN_HOLD
        total = min(hold * n, max_len)
    return round(hold, 3), round(total, 3)


# How much of a photo a fill would throw away before the desk stops calling it
# a reasonable crop. A 3:4 photo into 9:16 loses about a quarter of its width,
# which is fine. A 3:2 landscape loses nearly two thirds, which is not.
FILL_LIMIT = 0.35


def crop_loss(src, w, h):
    """The fraction of a photo a fill would cut off. 0 means it already fits."""
    sw, sh = photo_size(src)
    if not sw or not sh:
        return 0.0
    want, have = w / h, sw / sh
    if abs(want - have) < 1e-6:
        return 0.0
    # Filling scales until the short side covers, then cuts the long one.
    return 1 - (min(want, have) / max(want, have))


def photo_size(src):
    """Width and height of a still, out of ffmpeg's own report."""
    r = subprocess.run([exe(), "-hide_banner", "-i", str(src)],
                       capture_output=True, text=True)
    m = re.search(r"Video:.*?,\s(\d+)x(\d+)", r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def grade(brightness=0.0, contrast=1.0, saturation=1.0, warmth=0.0):
    """The colour adjustments, as one filter, or nothing when untouched."""
    bits = []
    if abs(brightness) > 1e-6 or abs(contrast - 1) > 1e-6 or abs(saturation - 1) > 1e-6:
        bits.append(f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}"
                    f":saturation={saturation:.3f}")
    if abs(warmth) > 1e-6:
        # Push red up and blue down, or the other way, without touching green.
        bits.append(f"colorbalance=rm={warmth:.3f}:bm={-warmth:.3f}")
    return bits


def mean_luma(src):
    """Average brightness of a still, 0 to 255. None if it cannot be read."""
    r = subprocess.run([exe(), "-hide_banner", "-loglevel", "error", "-i",
                        str(src), "-frames:v", "1",
                        "-vf", "scale=160:-2,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True)
    return sum(r.stdout) / len(r.stdout) if r.stdout else None


def resolve_mode(src, w, h, mode):
    """What "auto" actually decides for one photo."""
    if mode != "auto":
        return mode if mode in ("blur", "fill") else "blur"
    return "fill" if crop_loss(src, w, h) <= FILL_LIMIT else "blur"


def fit(src, dest, w, h, blur=30, mode="blur", grading=None):
    """One photo at exactly the output size.

    `mode` is how the shape difference is resolved. "blur" fits the whole photo
    over a blurred, cropped copy of itself, which keeps every pixel of the
    picture. "fill" crops to the frame, which is stronger but throws the edges
    away. "auto" fills when little would be lost and blurs when a lot would.

    Doing this once per photo rather than once per appearance is what makes a
    loop possible. Handed images of different shapes, the concat demuxer
    changes its stream parameters mid-render and ffmpeg rebuilds the filter
    graph each time; repeat the set a few times and enough rebuilds land that
    the video stream does not survive the mux, leaving a file with sound and
    no picture. Every frame the same size means no rebuild at all, and the
    blur gets computed once instead of once per pass.
    """
    mode = resolve_mode(src, w, h, mode)
    tone = ",".join(grade(**(grading or {})))
    tone = ("," + tone) if tone else ""

    if mode == "fill":
        vf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h}{tone},setsar=1[v]")
    else:
        vf = (f"[0:v]split=2[bg][fg];"
              f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},gblur=sigma={blur}[bgb];"
              f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
              f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2{tone},setsar=1[v]")
    # Pin the pixel format. A photo with an alpha channel fits to rgba and one
    # without fits to rgb24, and a sequence that changes format halfway stops
    # decoding there: the picture ends early while the track plays on.
    r = subprocess.run([exe(), "-y", "-i", str(src), "-filter_complex", vf,
                        "-map", "[v]", "-frames:v", "1", "-pix_fmt", "rgb24",
                        str(dest)], capture_output=True, text=True)
    if r.returncode != 0 or not Path(dest).exists():
        raise RuntimeError(f"could not fit {Path(src).name}: "
                           + r.stderr.strip()[-300:])
    return dest


def build(images, out, audio=None, size=SIZE, per=None, max_len=MAX,
          fade=FADE, start=0.0, target=None, caption=None, font=None,
          cap_size=None, cap_color="white", cap_shadow=4,
          cap_shadow_color="black@0.7", cap_fade=CAP_FADE,
          cap_position="lower", cap_align="center", credit=None,
          credit_style="note", cards=None, transition="fade", snap=True,
          mode="blur", grading=None):
    """Render the photos in the order given, with the music under them.

    Each photo is fitted whole, never cropped, over a blurred copy of itself so
    a landscape shot does not sit in black bars.
    """
    images = [Path(i) for i in images]
    if not images:
        raise ValueError("no images")

    w, h = size
    work = Path(tempfile.mkdtemp(prefix="slideshow-"))
    try:
        # The caption is laid out first, because how many lines it wraps to is
        # what decides how long the post has to run. Deciding the length before
        # reading the words is what left an automatic 7 seconds under a caption
        # that needed eight.
        deck = normalise_cards(cards, caption, cap_position, cap_align)
        drawn_at = cap_size or max(28, int(h * 0.045))
        face = None
        if deck:
            face = font or default_font()
            if face:
                shutil.copyfile(face, work / "face.ttf")   # no path to escape
                face = work / "face.ttf"
                deck, drawn_at = layout_cards(deck, face, drawn_at, w,
                                              fade=cap_fade)
            else:
                deck = []
        # Every card has to be read, so the length has to cover all of them.
        need_for_words = sum(c["need"] for c in deck)

        hold, total = plan(len(images), target=target, per=per,
                           max_len=max_len, floor=need_for_words)

        # The music is cut to the photos, so whatever is left of the track
        # after the cue is the one thing that can still shorten the post.
        short_by = 0.0
        if audio:
            a = duration(audio)
            if a:
                left = max(a - float(start or 0), 0.0)
                if 0 < left < total:
                    short_by = round(total - left, 1)
                    total = left

        # Fill the length by cycling the photos rather than holding each one
        # longer. A run that comes round again reads as a loop; one photo sat
        # on for the length of the post reads as a stall.
        #
        # Fit every photo to the output size first, so every frame is the same
        # shape. See fit().
        # Recorded per photo so the desk can say what auto decided, rather
        # than leaving you to work it out from the result.
        used = [resolve_mode(p, w, h, mode) for p in images]
        fitted = [fit(p, work / f"src{i:03d}.png", w, h, mode=used[i],
                      grading=grading)
                  for i, p in enumerate(images)]

        # Then lay the run out as a numbered sequence read at one frame per
        # hold. The concat demuxer's own `duration` directive is not reliable
        # here: it ends the picture after a single pass while the track plays
        # on, so the post looks frozen with the music still going. A fixed
        # input framerate leaves ffmpeg nothing to work out.
        count = max(1, int(-(-total // hold))) + 1       # ceil, plus a spare
        for i in range(min(count, MAX_FRAMES)):
            # Hard links, so repeating a photo costs an inode and not a copy.
            link = work / f"f{i:05d}.png"
            try:
                os.link(fitted[i % len(fitted)], link)
            except OSError:
                shutil.copyfile(fitted[i % len(fitted)], link)

        # Words on the picture, because these autoplay muted. A caption is the
        # only thing a sound-off viewer gets.
        chain = ["fps=30", "format=yuv420p", "setsar=1"]
        # The track credit needs a face whether or not there is a caption.
        credit_line, credit_fell_back = "", False
        if (credit or "").strip():
            if not face:
                picked = font or default_font()
                if picked:
                    shutil.copyfile(picked, work / "face.ttf")
                    face = work / "face.ttf"
            credit_line, cface, credit_fell_back = credit_text(
                credit, credit_style, face)
            if cface and Path(cface) != Path(work / "face.ttf"):
                shutil.copyfile(cface, work / "credit.ttf")
                cface = work / "credit.ttf"
            chain += credit_filter(credit_line, w, h, font=cface or face,
                                   work=work)
        spans = card_times([c["need"] for c in deck], total, hold,
                           snap=snap) if deck else []
        for i, (card, (a, b)) in enumerate(zip(deck, spans)):
            chain += caption_filters(
                card["lines"], drawn_at, w, h, color=cap_color,
                shadow=cap_shadow, font=face, work=work,
                shadow_color=cap_shadow_color, fade=cap_fade,
                position=card["position"], align=card["align"],
                # Only the first card waits for the picture to land.
                delay=CAP_DELAY if i == 0 else 0.0,
                start=a, end=b, transition=transition,
                last=(i == len(deck) - 1), tag=i)

        rate = Fraction(1 / hold).limit_denominator(100000)
        cmd = [exe(), "-y", "-framerate", f"{rate.numerator}/{rate.denominator}",
               "-i", str(work / "f%05d.png")]
        if audio:
            if start:
                cmd += ["-ss", f"{float(start):.3f}"]   # seek before the input
            cmd += ["-i", str(audio)]
        cmd += ["-vf", ",".join(chain), "-map", "0:v"]
        if audio:
            fade = min(fade, total * 0.15)   # 1.5s out of 7 is not a fade
            fade_at = max(total - fade, 0)
            cmd += ["-map", "1:a", "-af", f"afade=t=out:st={fade_at:.2f}:d={fade}",
                    "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-t", f"{total:.3f}", "-movflags",
                "+faststart", str(out)]

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-600:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return dict(path=str(out), seconds=round(total, 1), width=w, height=h,
                photos=len(images), hold=round(hold, 2),
                loops=round(total / (hold * len(images)), 2),
                short_by=short_by,
                caption_lines=sum(len(c["lines"]) for c in deck),
                caption_size=drawn_at if deck else None,
                caption_needs=round(need_for_words, 1),
                cards=[dict(text=c["text"], position=c["position"],
                            align=c["align"], lines=len(c["lines"]),
                            start=a, end=b)
                       for c, (a, b) in zip(deck, spans)],
                mode=mode, filled=used.count("fill"), blurred=used.count("blur"),
                credit=credit_line, credit_fell_back=credit_fell_back)


def dub(video, out, audio, fade=FADE, keep_original=False, start=0.0):
    """Put a track over a video, replacing what was there.

    The picture is copied through untouched, not re-encoded, so this costs no
    quality and takes about a second. Only the audio is rebuilt.
    """
    video, audio = Path(video), Path(audio)
    vlen = duration(video) or 0
    alen = duration(audio) or 0
    if not vlen:
        raise RuntimeError("could not read the video's length")

    fade_at = max(vlen - fade, 0)
    if keep_original:
        # Track under the original sound, the original held down a little.
        af = (f"[0:a]volume=0.25[a0];"
              f"[1:a]apad,atrim=0:{vlen:.3f},"
              f"afade=t=out:st={fade_at:.2f}:d={fade}[a1];"
              f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[a]")
        maps = ["-filter_complex", af, "-map", "0:v", "-map", "[a]"]
    else:
        af = f"apad,atrim=0:{vlen:.3f},afade=t=out:st={fade_at:.2f}:d={fade}"
        maps = ["-map", "0:v", "-map", "1:a", "-af", af]

    cmd = [exe(), "-y", "-i", str(video)]
    if start:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(audio)] + maps + [
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{vlen:.3f}", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-600:])
    left = max(alen - float(start or 0), 0)
    return dict(path=str(out), seconds=round(vlen, 1),
                track_seconds=round(alen, 1), start=round(float(start or 0), 1),
                short_by=round(max(vlen - left, 0), 1),
                replaced=not keep_original)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--audio")
    ap.add_argument("--shape", choices=sorted(SHAPES), default="portrait")
    ap.add_argument("--start", type=float, default=0.0,
                    help="seconds into the track to begin")
    ap.add_argument("--length", type=float,
                    help=f"how long the whole post runs, up to {MAX:.0f}s. "
                         f"Photos cycle to fill it. Default {SINGLE:.0f}s for "
                         f"one photo, {PER:.0f}s each for a run, capped at "
                         f"{AUTO_MAX:.0f}s.")
    ap.add_argument("--per", type=float,
                    help="override how long one photo holds")
    ap.add_argument("--dub", action="store_true",
                    help="the first input is a video; swap its audio")
    ap.add_argument("--keep-original", action="store_true",
                    help="mix the track under the original sound instead")
    a = ap.parse_args()
    try:
        if a.dub:
            print(dub(a.images[0], a.out, a.audio,
                      keep_original=a.keep_original, start=a.start))
        else:
            print(build(a.images, a.out, a.audio, size=SHAPES[a.shape],
                        start=a.start, target=a.length, per=a.per))
    except (RuntimeError, ValueError) as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
