# Guavynator Release Desk

A scheduling and approval desk for **Guavy**, and only Guavy. Lifted from the
Memphis and The Grande release desk at
`~/Documents/agents/poster/release-desk`, which is the older and more exercised
copy. When something here looks odd, that is where to compare against.

## Before touching anything

Read `README.md`. It carries the traps, and several of them cost real debugging
to find. The ones that bite hardest:

- A channel key names an account, not a platform.
- Zernio says `twitter`, the desk says `x`. Go through `platform_alias()`.
- Zernio pages at 50. Use `api_all()`, never `api()` on a list endpoint.
- `platformSpecificData` is not validated. A field only works if you have
  watched it work.
- One exception blanks a whole tab. Each tab is drawn by one function.
- Never set `display` on a tab panel: it beats `[hidden]` and pins the tab open.

## What is different here

- **One profile.** `profiles.yaml` holds Guavy alone. No borrowed accounts, no
  signoff gate, no second profile to attribute anything to.
- **One account per platform**, so a channel key is just the platform name.
- **Port 5001**, so this and the band's desk can run at once.
- `desk.db` and `media/` start empty. Nothing of the band's came across.

## What is not done

Nearly all of the configuration. `profiles.yaml` and
`campaigns/guavy/source-pack.md` are full of TODOs, and they are load-bearing:
the writer is handed the voice block and the source pack and nothing else, so
an empty pack means it has nothing true to say.

**If Guavy is the market analysis product**, performance figures, backtests and
anything that reads as a recommendation are regulated speech. Decide what may
be said and write it into `voice.claims` before the first post. Until then the
honest answer to "can we post this" is no.

Zernio account ids are `TODO`. Run `python3 push_zernio.py accounts` to list
them, and paste the right ones in. Nothing will push until that is done.

## Working here

```bash
pip install -r requirements.txt
python3 app.py            # http://127.0.0.1:5001
```

Keep the desk honest: it must never claim something went out when it did not,
and never invent a fact that is not in the source pack.
