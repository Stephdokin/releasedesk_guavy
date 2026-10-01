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

`voice.claims` in `profiles.yaml` is written and binding: read it before any
copy. The writer's voice comes from that block plus the `## Voice` section of
`.claude/skills/guavy-post/SKILL.md`, never the band's `social-schedule` skill.

Open: who is credited on posts (`source-pack.md`), a `release` date if the
campaign is ever more than always-on, and connecting Facebook, YouTube, TikTok
and X, which comes last. See README "What is not done yet".

## Working here

```bash
pip install -r requirements.txt
python3 app.py            # http://127.0.0.1:5001
```

Keep the desk honest: it must never claim something went out when it did not,
and never invent a fact that is not in the source pack.
