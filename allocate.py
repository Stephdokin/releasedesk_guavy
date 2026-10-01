#!/usr/bin/env python3
"""
Slot allocator for overlapping release campaigns.

Channels have fixed weekly capacity. Campaigns compete for it, weighted by how
far each is from its release date. Output is a schedule of empty slots, each
tagged with the campaign and asset that should fill it. Copy is written later.

  python3 allocate.py --from 2026-09-04 --to 2027-06-30
"""

import argparse, json, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load():
    cfg = yaml.safe_load((ROOT / "channels.yaml").read_text())
    camps = []
    for f in sorted(ROOT.glob("campaigns/*/campaign.yaml")):
        c = yaml.safe_load(f.read_text())
        c["_dir"] = f.parent
        camps.append(c)
    return cfg, camps


def phase_of(cfg, campaign, week_start):
    """Which phase a campaign is in for a given week, and its weight."""
    rel = campaign["release"]
    rel_week = rel - timedelta(days=rel.weekday())
    offset = (week_start - rel_week).days // 7
    for name, p in cfg["phases"].items():
        if p["from"] <= offset <= p["to"]:
            return name, p["weight"], offset
    return None, 0, offset


def largest_remainder(demands, capacity):
    """Distribute integer capacity proportional to weights, no float drift."""
    total = sum(demands.values())
    if total == 0 or capacity == 0:
        return {k: 0 for k in demands}
    exact = {k: capacity * w / total for k, w in demands.items()}
    out = {k: int(math.floor(v)) for k, v in exact.items()}
    left = capacity - sum(out.values())
    order = sorted(exact, key=lambda k: (-(exact[k] - out[k]), k))
    for k in order[:left]:
        out[k] += 1
    return out


class Pool:
    """Tracks asset consumption so nothing gets posted twice on one channel."""

    def __init__(self, campaigns):
        self.spec = {}
        # counted per channel: the same clip can run on TikTok and Instagram
        self.count = defaultdict(int)  # (campaign, asset_id, channel) -> uses
        for c in campaigns:
            for a in c["assets"]:
                self.spec[(c["name"], a["id"])] = a

    def pick(self, campaign, channel, chan_spec, phase, day):
        order = ["trailer", "lyric_video", "music_video", "clips_lyric",
                 "clips_clean", "bts_photos", "live_video", "qa_shorts", "archive"]
        cands = sorted(
            campaign["assets"],
            key=lambda a: order.index(a["id"]) if a["id"] in order else 99,
        )
        for a in cands:
            key = (campaign["name"], a["id"], channel)
            if a["type"] not in chan_spec["accepts"]:
                continue
            if "channels" in a and channel not in a["channels"]:
                continue
            if a.get("confirmed") is False:
                continue
            if a.get("hold_until") and day < a["hold_until"]:
                continue
            if a.get("expires") and day >= a["expires"]:
                continue
            if a.get("phase_min") and phase not in ("taper",):
                continue
            if self.count[key] >= a["count"]:
                continue
            self.count[key] += 1
            return a
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--out", default="schedule.json")
    args = ap.parse_args()

    cfg, campaigns = load()
    blackouts = {b.isoformat() if hasattr(b, "isoformat") else str(b)
             for b in (cfg.get("blackouts") or [])}
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    pool = Pool(campaigns)

    week = start - timedelta(days=start.weekday())
    slots, unfilled = [], 0

    while week <= end:
        active = {}
        for c in campaigns:
            name, weight, off = phase_of(cfg, c, week)
            if name:
                active[c["name"]] = (name, weight, off)

        if active:
            demands = {n: w for n, (_, w, _) in active.items()}
            fill = min(1.0, sum(demands.values()) / cfg.get("full_load", 5))
            for ch, spec in cfg["channels"].items():
                capacity = int(round(spec["per_week"] * fill))
                alloc = largest_remainder(demands, capacity)
                days = []
                for dn in spec["days"]:
                    d = week + timedelta(days=DAYS.index(dn))
                    if start <= d <= end and d.isoformat() not in blackouts:
                        days.append(d)
                # release day beats the channel's usual days
                for c in campaigns:
                    r = c["release"]
                    if week <= r < week + timedelta(days=7) and r not in days \
                       and start <= r <= end and r.isoformat() not in blackouts:
                        days.insert(0, r)
                days.sort()
                di = 0
                for cname, n in sorted(alloc.items(), key=lambda kv: -kv[1]):
                    camp = next(c for c in campaigns if c["name"] == cname)
                    phase = active[cname][0]
                    for _ in range(n):
                        if di >= len(days):
                            break
                        day = days[di]
                        di += 1
                        asset = pool.pick(camp, ch, spec, phase, day)
                        if asset is None:
                            unfilled += 1
                            continue
                        slots.append({
                            "date": day.isoformat(),
                            "time": spec["time"],
                            "channel": ch,
                            "channel_label": spec["label"],
                            "campaign": cname,
                            "phase": phase,
                            "week_offset": active[cname][2],
                            "asset": asset["id"],
                            "asset_label": asset["label"],
                            "asset_type": asset["type"],
                            "strict_duplicates": spec.get("strict_duplicates", False),
                            "first_comment_link": spec.get("links_in_first_comment", False),
                            "requires_signoff": spec.get("requires_signoff", False),
                            "max_chars": spec.get("max_chars"),
                            "angle_hint": spec.get("angle"),
                            "copy": None,
                        })
        week += timedelta(days=7)

    slots.sort(key=lambda s: (s["date"], s["time"], s["channel"]))
    (ROOT / args.out).write_text(json.dumps(slots, indent=2))

    by_camp = defaultdict(int)
    by_chan = defaultdict(int)
    for s in slots:
        by_camp[s["campaign"]] += 1
        by_chan[s["channel"]] += 1

    print(f"{len(slots)} slots, {args.start} to {args.end}")
    print(f"{unfilled} slots left empty for want of assets\n")
    print("by campaign:")
    for k, v in sorted(by_camp.items()):
        print(f"  {k:28} {v:4}")
    print("\nby channel:")
    for k, v in sorted(by_chan.items(), key=lambda kv: -kv[1]):
        print(f"  {k:28} {v:4}")


if __name__ == "__main__":
    main()
