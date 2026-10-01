# Guavynator Release Desk

Scheduling and approval for Guavy. Lifted from the Memphis and The Grande
release desk, which is the older copy and still the one to compare against when
something here looks wrong.

Drop media on the Media tab, describe it,
select what you want, press Create post. You write the copy, approve it here,
and only then does anything reach Zernio.

## Setup

```bash
pip install -r requirements.txt
python3 app.py
```

Then open http://127.0.0.1:5001

Port 5001 rather than 5000, so this and the Memphis and The Grande desk can
run side by side.

Put the Zernio key in `.env` before pushing anything:

```bash
python3 push_zernio.py accounts      # ids to paste into profiles.yaml
python3 push_zernio.py push --dry-run
```

## What is not done yet

The machinery works and the configuration is mostly in.

- `profiles.yaml` carries Guavy's voice and a binding `voice.claims` block,
  dated September 2026. The writer is handed that block, the `## Voice`
  section of `.claude/skills/guavy-post/SKILL.md`, and the source pack, and
  nothing else. It no longer falls back to the band's `social-schedule` skill.
- `campaigns/guavy/source-pack.md` still has one open question: who, if anyone,
  is credited on posts.
- Zernio ids are in for LinkedIn and Instagram. Facebook, YouTube, TikTok and X
  are not connected, deliberately: they come last, once the post flow is proven.
- `campaign.yaml` has no `release` date, so the campaign reads as always-on.
- `channels.yaml` posting days and times are inherited, not chosen. Set them
  deliberately.

## What is guarded

A file in the library cannot be deleted while it is attached to a post that has
moved past drafting. Detach it there first.

Nothing reaches Zernio until it is approved. You approve posts by hand; the
one exception is the Wire autoposter, which approves its own posts while Auto
is switched on. `publishNow` is never set, so everything goes to Zernio as a
scheduled post, though a post timed "soon" fires two minutes later.

## Tuning

`channels.yaml` is the control panel.

- `days` and `time` per channel are when the desk places a post when you leave
  the timing on Auto
- `blackouts` are dates nothing schedules on
- `phases` labels a post by how far it sits from the campaign's `release`
  week. Guavy has no release date, so every post reads as taper

## Where things stand

The desk posts for one profile, Guavy, on two connected Zernio accounts:
LinkedIn and Instagram.

Seven tabs: **Suggested, Media, Posts, Calendar, Queued, Published, Numbers.**

The flow is media first. Drop photos, video or music
anywhere on the Media tab, press Upload files, or paste a URL. One box takes
both kinds: a page gives up its feature image and opening paragraph, a link
straight to a picture just fetches the picture. They are never the same URL, so
there is nothing to choose between, and the button only says which it has got. Describe each file, select what you want, press Create post. The
dialog carries the copy, the people to tag or collaborate with, music, the
accounts, the timing and the hashtags. Publish writes it to the schedule and
sends it. The desk makes the slot itself, on the channel's next posting day.

**A photo can appear more than once.** Right-click a shot in the compose strip,
or press the copy button, and it lands again next to itself ready to be moved
somewhere else in the run. The selection is a sequence, not a set: `moveSel`,
`dropSel` and `dupSel` work on where a photo sits rather than which photo it
is, because "which" stopped being unique. Clicking a tile off in the grid takes
every copy, which is what not-this-one means.

### Folders

The Media tab has folders: a bar of All, Loose, then whatever you have made.
Click one to filter the grid, drag files onto it to file them, drag them onto
Loose to take them out again. Dragging one file of a selection moves the whole
selection, because moving forty photos one at a time is the thing folders are
here to fix.

They are a shelf, not a filing system. One level, no nesting, per profile like
the media itself. A folder is a label: deleting one leaves everything that was
in it in the library, loose. Naming a folder that already exists selects the
existing one rather than making a second.

Folders are not campaigns. `campaign` ties a file to a release with a source
pack behind it; a folder is just where you put things so you can find them.

### Things that are easy to get wrong

- **A channel key names an account, not a platform.** Here there is one
  account per platform, so the key is the platform name, but code that assumes
  so will break the day a second account arrives.
- **Every video on screen carries the desk's own controls**, not the browser's:
  play, back to the start, and a line to drag. `vplayer()` draws them and the
  handlers walk up from whatever was clicked, so a repaint cannot leave a stale
  wire behind. Thumbnails in the pickers stay bare on purpose; a control bar
  there would fight click-to-select.
- **Zernio says `twitter`, the desk says `x`.** Folded in via `aliases` in
  `channels.yaml`. Anything reading Zernio goes through `platform_alias()` or
  posts vanish silently.
- **Not every platform reports every metric.** TikTok and YouTube give views
  and never impressions or reach; X gives no reach; Facebook reports views on
  about a quarter of posts. Drawn as 0 that reads as "nobody saw it" when it
  means "nobody counted it", and you go to the platform, find numbers, and
  conclude the desk is broken. `/api/zernio/published` returns a `reports` map
  built from the data itself, and a metric a platform never reports is drawn
  as an em dash with a note rather than a zero.
- **Zernio's lists are account-wide, not per profile.** The desk fetches
  everything and filters afterwards, so Published, Queued and a sync all pull
  the same two lists, and switching profile pulls them again to filter them
  differently. `zernio_all()` holds each list for 90 seconds, which turns
  opening every tab and switching profile into one fetch instead of eight.
  Pressing Sync, or Refresh on the Published tab, passes `force` and ignores
  the held copy: those are asked for on purpose. The Published bar says how
  old the answer is.
- **Zernio pages at 50.** Use `push_zernio.api_all()`, never `api()` on a list
  endpoint, or every number is quietly halved.
- **The compose box and the desk have to agree what a mode is called.** The
  box sent `man` for a chosen date; `slot_for` tested for `date`, fell through
  to the next free slot, and said nothing. Ten posts landed anywhere from the
  next day to three weeks out. `WHEN_MODES` lists every spelling, and a mode
  not on the list is refused where the request arrives rather than quietly
  becoming auto. The box only sends a date when a date is the instruction: it
  keeps whatever was last typed even on Auto.
- **Rescheduling means Zernio, not just desk.db.** Once a post is `scheduled`
  it lives in Zernio's queue, and moving it here only makes the calendar lie.
  `PUT /posts/<id>` with `scheduledFor` and `timezone` works, and updates the
  per-platform entry too. `PATCH` is refused with a 405.
- **A YouTube post needs a title, and it is not in `platformSpecificData`.**
  `title` is a top-level field on the Zernio post. Sent empty, Zernio holds its
  dialog open until somebody types one, which is a post stuck in the queue
  waiting to be noticed. `push_zernio.title_from()` takes the first line of the
  copy, swaps angle brackets that YouTube refuses, and trims to 100 counting
  the ellipsis. Type one in the post sheet and that wins.
- **`platformSpecificData` is not validated.** Zernio stores any key you send,
  including misspelled ones. A field only works if you have watched it work.
- **Attribution is by origin.** A post belongs to the profile that composed it
  where the desk knows, and to the account owner otherwise.
- **A slideshow goes out instead of the photos in it.** The post carries the
  rendered video, so the photos it was made from are named nowhere on it and
  the Media tab called them unused while they were out being watched. The
  render records its ingredients in `made_from`, and the media
  list walks back through that. They cannot go in `media_ids`: push_zernio
  uploads every id in there, so the source photos would be posted alongside
  the video.
- **Media is per profile, copied not referenced.** `media/` holds the desk's own
  copy. Uniqueness is `(profile, sha256)`, so the same file can live on two profiles.
- **Phone video hides its orientation** in the track matrix; `probe.py` swaps
  the dimensions on a quarter turn.
- **The box and the renderer have to agree about length.** `index()` serves
  slideshow.py's timings into the page, and the compose box asks
  `/api/caption/fit` for the real line breaks a moment after you stop typing,
  rather than guessing from character counts. Guessing was wrong in both
  directions: counting typed lines missed wrapping, and counting characters
  over-counted, because a condensed face fits far more across than the count
  suggests. Either way Auto showed a length the render would not pick.
- **The music emoji cannot be drawn.** drawtext will not load Apple Color
  Emoji at all, so 🎵 is not available. The credit uses a quaver, which is an
  ordinary character in a text face, and even that is missing from most display
  faces: Bebas Neue has no music symbol and DIN Condensed draws a box for it.
  `note_font()` borrows a face that has one for that line only.
- **Checking a font for a glyph needs an unassigned codepoint**, never a
  Private Use one. A font may carry real glyphs in the PUA and DIN Condensed
  does, so comparing a missing note against U+E000 said the note was there and
  the credit rendered tofu. `has_glyph()` compares against U+0888 and friends.
- **A caption is measured, not guessed.** `text_width()` asks ffmpeg by
  printing `text_w` out of the expression evaluator. Estimating from character
  counts does not work: the whole point of a condensed face is that it lies
  about how wide it will be.
- **A slideshow can come out with sound and no picture.** Feed ffmpeg stills of
  different sizes or different pixel formats and it stops decoding at the first
  change, so the picture ends early while the track plays on and the container
  still reports the full length. `slideshow.fit()` renders every photo to the
  output size and pins `rgb24` first, which is what makes looping safe. Check a
  render by decoding the video stream alone, not by reading the duration.
- **`FLOOR` follows the data.** Zernio backfilled about twelve weeks to 14 June
  and holds 88 of the 299 posts the accounts claim.
- **Dates are local, never UTC.** `new Date().toISOString()` rolls over at
  midnight in London, so from 6pm Mountain the desk called it tomorrow: the
  calendar boxed the wrong day and the Posts count dropped anything scheduled
  for today. Use `iso(d)`, which reads the local parts. The published and
  follower ranges are the exception and are UTC on both sides on purpose.
- **Grey on the calendar means gone by, not disabled.** Published posts and
  anything dated before today are drawn in the dim ink so a glance at the month
  reads as the work ahead. They are still there and still clickable, and the
  Published box in the calendar bar hides them outright if you want only what
  is coming.
- **A month cell holds three chips**, then an "and N more" link that opens the
  day. The link sits outside the chips, because inside it scrolled away with
  the posts it was there to announce. The grid is no longer squeezed into
  `calc(100vh - 268px)` either: on a short window that left a cell one chip
  tall and hid the rest behind a scrollbar macOS does not draw, so a day with
  five posts looked like a day with one. Rows have a real minimum now and the
  page scrolls instead.
- **The page is served `no-store`.** It is rebuilt on every request and
  changes whenever app.py does, so a browser holding yesterday's JavaScript
  looks like a bug in the desk. Nothing about the page is worth caching.
- **Do not repaint under a drag.** Redrawing the folder bar on `dragover` to
  show the highlight replaces the very element the pointer is over, and a
  browser cancels the drag when that happens. The class goes on the node.
- **A string key in an inline handler needs single quotes.** The attribute is
  delimited by double ones, so `JSON.stringify('loose')` closes it early and
  the rest of the handler becomes stray markup. Only the Loose chip was
  affected, and only because every other key is a number or null.
- **A dragged tile is not an upload.** The drop zone checks for `Files` in
  `dataTransfer.types`, or moving a file within the library lights up the
  uploader and hands it an empty list.
- **Never set `display` on a tab panel.** Tabs are switched with the `hidden`
  attribute, and any author rule beats the browser's `[hidden]{display:none}`
  whatever its specificity. `#up{display:block}` pinned the Media tab open:
  every other tab still drew, underneath it, below 60vh of photos, which reads
  as tabs that do nothing at all. Size a panel behind `:not([hidden])`.
- **Check tab visibility through the computed style**, not the attribute. The
  attribute was correct the whole time it was broken.
- **Fetch both, then assign both.** `drawPublished` set `PUB`, awaited the
  followers, then read `PUB` again. Changing profile during that second await
  set it back to null and the tab threw. Anything that awaits twice has to
  hold its results locally until it is done, and check the profile has not
  moved on underneath it.
- **`/api/accounts` with no profile answers with every account there is.**
  `boot()` used to call it before reading the remembered profile, so a fresh
  load offered the other profile's accounts until you touched the picker.
  Resolve `PROF` first.
- **One exception blanks a whole tab.** Each tab is drawn by one function. Test
  UI changes by executing the page JS against real API output.

### Music

`slideshow.py` renders with ffmpeg from the `imageio-ffmpeg` wheel, so there is
nothing to install. Photos plus a track become one video, portrait, landscape or
square, each photo fitted whole over a blurred copy of itself. A single video
plus a track keeps its picture stream untouched and gets new sound. A cue point
sets where in the track to begin. **Preview builds the file and plays it; publish
then sends that same file rather than rendering twice.**

No API can reach a platform's music library. The track has to be in the file.

**How long it runs.** Auto is 7 seconds for one photo and 3 seconds each for a
run, up to 30 **or as long as the caption needs, whichever is longer**. The
caption is laid out before the length is decided, so adding a line lengthens
the post rather than leaving the words to be cut off, and the Auto button shows
the number it will actually use. Ask for a length explicitly and that is
respected instead, with the note saying if it is too short to read. Ask for
longer and the photos cycle rather than each being held longer, and the hold is nudged by a fraction of a second so the length is
filled by whole passes and the clip loops on the cut. Every photo picked always
appears at least once: a long set speeds up instead of losing its tail. Set it
in the compose box, or `--length` from the command line; press the chosen
length again for Auto. The music is cut to the photos, so the only thing that
can still shorten a post is running out of track after the cue.

The old behaviour was to stretch the photos to fill the track, capped at a
minute. A still held for sixty seconds is dead air: nobody watches it, and a
feed that measures watch-through reads it as a post nobody stayed for. Short
and looping wins, because a replay counts.

**How the photos fill the frame.** Whole photo over a blurred copy of itself,
cropped to fill, or filled where little would be lost and blurred where a lot
would. A 3:4 photo into 9:16 loses about a quarter of its width, which is fine;
a 3:2 landscape loses nearly two thirds, which is not, and that is the line
`FILL_LIMIT` draws. The render reports how many went each way. There are four
colour sliders as well, and **Read the photos** measures how dark the selection
actually is and lifts it if it needs it. That is arithmetic on the luma, not a
model: it costs nothing and says the same thing twice.

**Words on the picture.** The compose box has a caption field. Leave it empty
and nothing is drawn, which is the default. Fill it and the lines come up one
at a time over the music, then hold. These autoplay muted on every feed the
desk posts to, so for a sound-off viewer the caption is the whole message.

**Where** puts the block at the top, upper third, middle or lower third, and
**Justify** sets left, centred or right. Every position is clamped into the
band between the platform's own furniture: its caption, username, audio ticker
and buttons across the bottom, its header and tabs across the top. So Top means
under the header rather than against the edge of the frame, and the lower third
of film convention never slides down into the buttons.

**Show me where** draws a single frame with the caption already up and those
bands shaded red, so you can see where the words land without rendering a
video. It takes about a second. The guides exist for looking at: `still()` is
never called on the way out, and nothing posts a frame with them on it.

A break you type is kept, because where a caption turns is an editorial choice.
A line you do not break is wrapped rather than shrunk: a headline squeezed onto
one line ends up too small to read, which defeats the point. Typeface, size,
colour, shadow size and shadow colour are all in the row under the field. The
font list is read off this machine, so it is different on a different computer
and a caption rendered here will not render the same elsewhere.

A caption lives on the file, not the post, so a photo carries its words into
every post it appears in. Captions go on photo posts; a video keeps its own
picture and is not re-encoded, so it does not take one.

**Captions are a sequence.** Each card has its own words and its own place on
the frame; typeface, size, colour, shadow and how they arrive stay one set for
the whole post, because a card that brings its own font is how a video ends up
looking like a ransom note. Time is shared out by how long each card takes to
read, not evenly, and boundaries are pulled onto photo changes where that is
close: a caption changing at the same instant as the picture reads as
deliberate, one changing halfway through a photo reads as a layer flickering
over the top. Between cards: cut, fade or cross-fade, with a default that
outlives the post. **Write them from the copy** drafts a set from the approved
copy, adding no fact the copy does not already carry.

Lines arrive one at a time. **Arrive** sets how: pop in, quick fade or slow
fade. The note under the box counts the lines it will really draw, wrapping
included, and says whether the length you picked leaves time to read them.

**Naming the track.** A radio in the Music section puts the track name in the top
left corner in small text, either `♪ Title` or `Music: Title`. The title starts
from the filename with its tail of mixes, tempos and keys taken off, so
`0_Opening Bell_Reference Mix_116_Fmaj.wav` becomes `Opening Bell`. Correct it
once and it is saved on the track, so the next post starts right. It sits under
the platform's header like the caption, and works with or without one.

**Publishing a video drops a copy in `~/Downloads`**, named
`<profile>-<date>-<file>.mp4`. Personal Facebook profiles cannot be posted to
by any API, so that upload is by hand and the file has to be findable. A `.MOV`
off a phone is remuxed rather than re-encoded, so it costs a second and no
quality, and the rotation matrix survives. Set `DOWNLOAD_DIR` to put it
somewhere else.

### The two AI features

"Write this up for me" in the compose box, "Summarise for X" on the X tab, and
the Suggested tab. All three call the
Anthropic API with `ANTHROPIC_API_KEY`, read `SKILL.md`, the profile's voice
block and the campaign source pack, and will say a thing is thin rather than
invent a fact that is not in the pack.

## One post, three pieces of writing

The copy box has tabs: **Copy, X, LinkedIn.**

- **Copy** goes to Instagram, Facebook, TikTok and YouTube as written. All four
  take well over a thousand characters, so a full post reaches them intact.
- **X** is its own short version, because 280 characters is a different post
  rather than a trimmed one. **Summarise for X** hands the approved copy to
  Claude and puts the result in the box: the link kept verbatim, the voice
  kept, nothing added that the copy did not already say, and a line telling you
  what it dropped. **Copy the copy across** brings it over untouched instead.
- **LinkedIn** starts as a copy of the main copy, so you put the preface on the
  front and edit the rest from there rather than writing it twice. Opening the
  tab seeds it once; after that your edits stand, and **Copy the copy across
  again** re-seeds on purpose.

Either variant left empty falls back to the main copy. Copy over a platform's
limit is refused for that channel with a reason, and the other channels go.

**X counts every link as 23 characters**, however long it really is. This
post's URL is 105, so counting raw length would refuse a post X would take
happily. `body_length()` applies the t.co rule for X and plain length
everywhere else, and the X counter shows both numbers.

Each channel already owns its own post row, so the variant is chosen once at
publish and written into that row. Nothing is resolved again at push time.

**Links come out of the body on LinkedIn.** The three LinkedIn channels are
marked `links_in_first_comment` because LinkedIn buries a post that carries a
link in its body. At publish the URLs are lifted out and become the first
comment, which `push_zernio.py` sends as `firstComment`. The writer's
punctuation stays behind, the same link twice is still one comment, and copy
that is nothing but a link is left alone rather than posted empty. Every other
channel keeps its copy exactly as written.

Worth knowing: `firstComment` goes out inside `platformSpecificData`, which
Zernio does not validate. Watch the first LinkedIn post land before trusting
it. And copy that says "read more here:" now says that with no link under it
on LinkedIn, so the preface is the place to say the link is in the comments.

`max_chars` lives on the platform in `channels.yaml`, and a channel may
override it to hold one account to something tighter. Copy over the limit is
refused for that channel with a reason, and the other channels still go.

| Platform | Body limit |
|---|---|
| X | 280 |
| Instagram | 2,200 |
| TikTok | 2,200 |
| LinkedIn | 3,000 |
| YouTube | 5,000 |
| Facebook | 63,206 |

## Known gaps

- LinkedIn native posts can never sync. Zernio cannot discover posts it did not
  create there, so anything posted to LinkedIn by hand is invisible to the desk.
  Posting from here is the only way LinkedIn work gets measured.
- Facebook collaborators are unproven. Instagram collabs are known to work.
- Personal Facebook profiles cannot be posted to by any API. When Facebook is
  connected it has to be a Page.
- The `handles` list in `profiles.yaml` is empty, so tagging offers
  nobody on those platforms. Handles added from the compose box go to the
  `handles` table instead, and are merged on read.
- Every press of Preview writes a new file and a new row in the library, so
  iterating on one post leaves a trail of rendered videos behind. Two renders
  that come out byte for byte identical are one row by `(profile, sha256)` but
  still two files, so the second is orphaned on disk. Nothing clears either up.
