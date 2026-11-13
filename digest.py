#!/usr/bin/env python3
"""Regional India news digest.

Pulls the last ~day of headlines for every state/UT from Google News RSS,
groups duplicate coverage of the same event, caps each region so no region
crowds out the rest, and writes one HTML page with bias measurements.

Standard library only:  py digest.py  [--open] [--config config.json]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (personal regional news digest)"

# ---------------------------------------------------------------- text utils

STOP = set("""
a an the and or of to in on at for from by with as is are was were be been being
it its this that these those after before over under into amid about against
during new says say said will would can could may might has have had not no more
than up out off his her their them they he she we you who what when where why how
all also least people india indian india's state govt government two one first
day today news live updates update latest top check here amp via per
""".split())

# Words/phrases that signal editorialised or sensational framing in a headline.
LOADED = [
    "slams", "slammed", "blasts", "lashes out", "lashes", "rips into", "tears into",
    "hits out", "destroys", "demolishes", "shocking", "shocker", "shock", "outrage",
    "outraged", "fury", "furious", "brutal", "horrific", "exposed", "exposes",
    "massive", "huge", "epic", "stunning", "bombshell", "chaos", "mayhem",
    "war of words", "mocks", "jibe", "jibes", "taunts", "savage", "explosive",
    "sensational", "viral", "big blow", "humiliat", "shameful", "disgrace",
    "u-turn", "backfires", "meltdown", "panic", "nightmare", "must watch",
    "you won't believe", "befitting reply", "fumes", "slap", "thrash",
]
LOADED_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in LOADED) + r")", re.I)


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def tokens(text: str, drop: set[str]) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'s", ""))
    return frozenset(stem(w) for w in words if len(w) > 1 and w not in STOP and w not in drop)


def tone(title: str) -> dict:
    """Headline framing score: loaded words + shouting caps + exclamation marks."""
    hits = sorted({m.group(1).lower() for m in LOADED_RE.finditer(title)})
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", title) if w not in {"COVID", "ISRO", "NITI", "AIIMS", "NEET", "SEBI", "UPSC", "CBSE", "ULFA", "NSCN", "BSNL", "IPL", "BCCI"}]
    excl = title.count("!")
    score = len(hits) + len(caps) + excl
    reasons = hits + [f"CAPS:{c}" for c in caps] + (["!"] if excl else [])
    return {"score": score, "reasons": reasons}


def similar(a: frozenset, b: frozenset) -> bool:
    shared = len(a & b)
    if shared < 2:
        return False
    ov = shared / min(len(a), len(b))
    return (shared >= 3 and ov >= 0.4) or ov >= 0.6


# ---------------------------------------------------------------- fetching

def feed_url(query: str, cfg: dict) -> str:
    q = urllib.parse.quote_plus(f"{query} when:1d")
    lang, cc = cfg["language"], cfg["country"]
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={cc}&ceid={cc}:{lang.split('-')[0]}"


def fetch(url: str) -> str:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  ! failed: {url[:90]}... ({err})", file=sys.stderr)
    return ""


def parse(xml_text: str) -> list[dict]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        while source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        # Google bundles related coverage inside <description>; keep those headlines too.
        related = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                   for t in re.findall(r"<a [^>]*>(.*?)</a>", html.unescape(it.findtext("description") or ""))]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        out.append({
            "title": html.unescape(title),
            "link": it.findtext("link") or "",
            "source": source or "Unknown",
            "published": pub,
            "related": related,
        })
    return out


def state_terms(cfg: dict) -> dict[str, list[str]]:
    """Words that identify each state in a headline: its name plus the place names in its query."""
    terms = {}
    for states in cfg["regions"].values():
        for s in states:
            q = s.get("query", s["state"])
            words = re.findall(r'"([^"]+)"|(?<![-\w])([A-Z][\w]+)', q)
            t = {s["state"]} | {a or b for a, b in words if (a or b) not in {"OR"}}
            if s["state"] == "Jammu & Kashmir":
                t |= {"Kashmir", "J&K"}
            terms[s["state"]] = sorted(t)
    return terms


def national_attention(cfg: dict) -> dict:
    """What a typical national feed shows you: which states its headlines are about."""
    urls = [
        f"https://news.google.com/rss?hl={cfg['language']}&gl={cfg['country']}&ceid={cfg['country']}:{cfg['language'].split('-')[0]}",
        f"https://news.google.com/rss/headlines/section/topic/NATION?hl={cfg['language']}&gl={cfg['country']}&ceid={cfg['country']}:{cfg['language'].split('-')[0]}",
    ]
    heads = set()
    for u in urls:
        for a in parse(fetch(u)):
            heads.add(a["title"])
            heads.update(r for r in a["related"] if len(r) > 20)
    region_of = {s["state"]: r for r, ss in cfg["regions"].items() for s in ss}
    pats = {st: re.compile(r"\b(" + "|".join(re.escape(t) for t in ts) + r")\b", re.I)
            for st, ts in state_terms(cfg).items()}
    per_state, per_region, untagged = Counter(), Counter(), 0
    for h in heads:
        hit = {st for st, p in pats.items() if p.search(h)}
        if not hit:
            untagged += 1
        per_state.update(hit)
        per_region.update({region_of[s] for s in hit})
    return {"headlines": len(heads), "untagged": untagged,
            "per_state": per_state, "per_region": per_region}


# ---------------------------------------------------------------- pipeline

def collect(cfg: dict) -> list[dict]:
    jobs = []
    for region, states in cfg["regions"].items():
        for s in states:
            jobs.append((region, s["state"], s.get("query", s["state"])))

    print(f"Fetching {len(jobs)} state feeds...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        raws = list(ex.map(lambda j: fetch(feed_url(j[2], cfg)), jobs))

    cfg["_feed_sizes"] = {st: len(parse(raw)) for (_, st, _), raw in zip(jobs, raws)}
    now = datetime.now(timezone.utc)
    all_state_words = {w for _, st, _ in jobs for w in re.findall(r"[a-z]+", st.lower())}
    seen: dict[str, dict] = {}
    for (region, state, _), raw in zip(jobs, raws):
        for a in parse(raw):
            age_h = (now - a["published"]).total_seconds() / 3600
            if age_h > cfg["window_hours"] or age_h < -1:
                continue
            key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:90]
            named = state.lower() in a["title"].lower()
            if key in seen:
                # Same article found via two state queries: keep the state the headline names.
                if named and not seen[key]["named"]:
                    seen[key].update(region=region, state=state, named=True)
                continue
            a.update(region=region, state=state, named=named, age_h=age_h,
                     tokens=tokens(a["title"], all_state_words), tone=tone(a["title"]))
            seen[key] = a
    arts = list(seen.values())
    print(f"  {len(arts)} unique articles in the last {cfg['window_hours']}h")
    return arts


def _core(counter: Counter, size: int) -> frozenset:
    """Words shared by roughly a third of a cluster's headlines: what the story is about."""
    if size <= 2:
        return frozenset(counter)
    k = max(2, round(size * 0.34))
    c = frozenset(t for t, n in counter.items() if n >= k)
    return c or frozenset(t for t, _ in counter.most_common(6))


def cluster(arts: list[dict], window: float) -> list[dict]:
    """Group headlines about the same event, matching against each group's core words
    (not any single member) to avoid chaining unrelated stories together."""
    groups: list[dict] = []
    for a in sorted(arts, key=lambda x: x["age_h"]):
        best, best_shared = None, 0
        for g in groups:
            if similar(a["tokens"], g["core"]):
                shared = len(a["tokens"] & g["core"])
                if shared > best_shared:
                    best, best_shared = g, shared
        if best is None:
            best = {"items": [], "words": Counter()}
            groups.append(best)
        best["items"].append(a)
        best["words"].update(a["tokens"])
        best["core"] = _core(best["words"], len(best["items"]))

    # Merge pass: two groups describing the same event with different wording.
    merged = True
    while merged:
        merged = False
        groups.sort(key=lambda g: -len(g["items"]))
        for i in range(len(groups)):
            for j in range(len(groups) - 1, i, -1):
                if similar(groups[i]["core"], groups[j]["core"]):
                    g = groups.pop(j)
                    groups[i]["items"] += g["items"]
                    groups[i]["words"].update(g["words"])
                    groups[i]["core"] = _core(groups[i]["words"], len(groups[i]["items"]))
                    merged = True

    out = []
    for items in (g["items"] for g in groups):
        outlets = sorted({m["source"] for m in items})
        # Lead headline = the most neutrally worded one, then the freshest.
        lead = min(items, key=lambda m: (m["tone"]["score"], m["age_h"]))
        state = Counter(m["state"] for m in items).most_common(1)[0][0]
        freshest = min(m["age_h"] for m in items)
        charged = sum(1 for m in items if m["tone"]["score"] > 0)
        out.append({
            "lead": lead, "items": items, "outlets": outlets, "state": state,
            "region": lead["region"], "age_h": freshest,
            "charged_share": charged / len(items),
            "score": len(outlets) + max(0.0, 1 - freshest / window),
        })
    return out


def select(clusters: list[dict], states: list[str], cap: int) -> list[dict]:
    """Round-robin across states so small states still get a slot."""
    by_state = defaultdict(list)
    for c in clusters:
        by_state[c["state"]].append(c)
    for s in by_state:
        by_state[s].sort(key=lambda c: -c["score"])
    order = sorted(by_state, key=lambda s: -by_state[s][0]["score"])
    picked, rnd = [], 0
    while len(picked) < cap and any(len(by_state[s]) > rnd for s in order):
        for s in order:
            if rnd < len(by_state[s]) and len(picked) < cap:
                picked.append(by_state[s][rnd])
        rnd += 1
    return sorted(picked, key=lambda c: -c["score"])


# ---------------------------------------------------------------- bias stats

def accumulate(nat: dict, cfg: dict, path: Path, keep_days: int = 30) -> dict:
    """One day of national headlines is noisy; store each day and report the running total."""
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = {}
    hist[datetime.now().strftime("%Y-%m-%d")] = {
        "headlines": nat["headlines"], "untagged": nat["untagged"], "per_state": dict(nat["per_state"])}
    hist = dict(sorted(hist.items())[-keep_days:])
    path.write_text(json.dumps(hist, indent=1), encoding="utf-8")

    region_of = {s["state"]: r for r, ss in cfg["regions"].items() for s in ss}
    per_state, per_region = Counter(), Counter()
    for day in hist.values():
        per_state.update(day["per_state"])
        for st, n in day["per_state"].items():
            if st in region_of:
                per_region[region_of[st]] += n
    return {"headlines": sum(d["headlines"] for d in hist.values()),
            "untagged": sum(d["untagged"] for d in hist.values()),
            "per_state": per_state, "per_region": per_region, "days": len(hist)}

def bias_stats(arts: list[dict], cfg: dict, nat: dict) -> dict:
    pop = cfg["population_millions"]
    total_pop = sum(pop.values())
    per_region = Counter(a["region"] for a in arts)
    nat_total = sum(nat["per_state"].values()) or 1
    nat_region_total = sum(nat["per_region"].values()) or 1

    coverage = []
    for region, states in cfg["regions"].items():
        for s in states:
            st = s["state"]
            share = nat["per_state"][st] / nat_total
            pshare = pop.get(st, 0) / total_pop
            coverage.append({"state": st, "region": region, "national": nat["per_state"][st],
                             "local": cfg["_feed_sizes"].get(st, 0),
                             "share": share, "ratio": (share / pshare) if pshare else None})

    region_pop = defaultdict(float)
    for region, states in cfg["regions"].items():
        for s in states:
            region_pop[region] += pop.get(s["state"], 0)
    regions = []
    for region in cfg["regions"]:
        ra = [a for a in arts if a["region"] == region]
        src = Counter(a["source"] for a in ra)
        top_src, top_n = (src.most_common(1)[0] if src else ("-", 0))
        regions.append({
            "region": region, "articles": per_region[region],
            "national": nat["per_region"][region],
            "share": nat["per_region"][region] / nat_region_total,
            "pop_share": region_pop[region] / total_pop,
            "outlets": len(src), "top_source": top_src,
            "top_source_share": top_n / (len(ra) or 1),
            "charged": sum(1 for a in ra if a["tone"]["score"] > 0) / (len(ra) or 1),
        })

    outlets = []
    by_src = defaultdict(list)
    for a in arts:
        by_src[a["source"]].append(a)
    for src, la in by_src.items():
        rc = Counter(a["region"] for a in la)
        outlets.append({
            "source": src, "articles": len(la),
            "charged": sum(1 for a in la if a["tone"]["score"] > 0) / len(la),
            "home_region": rc.most_common(1)[0][0],
            "home_share": rc.most_common(1)[0][1] / len(la),
            "lean": cfg["source_lean"].get(src, ""),
        })
    outlets.sort(key=lambda o: -o["articles"])
    return {"coverage": coverage, "regions": regions, "outlets": outlets[:20], "total": len(arts),
            "nat_headlines": nat["headlines"], "nat_untagged": nat["untagged"], "nat_days": nat.get("days", 1)}


# ---------------------------------------------------------------- rendering

def ago(h: float) -> str:
    if h < 1:
        return f"{max(1, int(h * 60))}m ago"
    return f"{int(h)}h ago"


def e(s) -> str:
    return html.escape(str(s), quote=True)


CSS = """
:root{--bg:#f7f6f3;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e2dc;--accent:#2f5d8a;
--warn:#b3541e;--warn-bg:#fbeee5;--ok:#2e7d4f;--bar:#c9d6e3;--bar2:#2f5d8a;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--card:#1d1d21;--ink:#ececef;
--muted:#9a9aa3;--line:#2c2c33;--accent:#8cb4dc;--warn:#f0a070;--warn-bg:#3a2619;--ok:#7cc79a;--bar:#2c3a48;--bar2:#8cb4dc;--chip:#26272d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:0 16px 64px}
header{padding:28px 0 8px}h1{font-size:26px;margin:0}h2{font-size:20px;margin:36px 0 4px}
.sub{color:var(--muted);font-size:13px}
nav{position:sticky;top:0;background:var(--bg);padding:10px 0;display:flex;gap:6px;flex-wrap:wrap;
border-bottom:1px solid var(--line);z-index:5}
nav a{background:var(--chip);color:var(--ink);text-decoration:none;padding:4px 10px;border-radius:99px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.card a.h{color:var(--ink);text-decoration:none;font-weight:600;font-size:16px}
.card a.h:hover{color:var(--accent)}
.meta{color:var(--muted);font-size:12.5px;margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.st{color:var(--accent);font-weight:600}
.tag{font-size:11.5px;padding:1px 7px;border-radius:99px;background:var(--chip)}
.tag.warn{background:var(--warn-bg);color:var(--warn)}
.tag.ok{color:var(--ok)}
details{margin-top:6px;font-size:13px}summary{cursor:pointer;color:var(--muted)}
details li{margin:3px 0}details a{color:var(--accent)}
.silent{color:var(--muted);font-size:13px;margin:6px 0 0}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:8px}
th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:500;font-size:12px}td.n{text-align:right;font-variant-numeric:tabular-nums}
.barw{background:var(--bar);height:8px;border-radius:4px;position:relative;min-width:80px}
.barw i{position:absolute;left:0;top:0;bottom:0;background:var(--bar2);border-radius:4px}
.barw b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--warn)}
.hi{color:var(--warn);font-weight:600}.lo{color:var(--accent);font-weight:600}
.note{color:var(--muted);font-size:13px;max-width:70ch}
.tw{overflow-x:auto}
"""


def render(cfg: dict, picks: dict, stats: dict, clusters_by_region: dict) -> str:
    now = datetime.now().strftime("%a %d %b %Y, %H:%M")
    regions = list(cfg["regions"])
    nav = "".join(f'<a href="#{e(r)}">{e(r)}</a>' for r in regions) + '<a href="#bias">Bias</a>'
    body = []
    for r in regions:
        stories = picks[r]
        covered = {c["state"] for c in clusters_by_region[r]}
        silent = [s["state"] for s in cfg["regions"][r] if s["state"] not in covered]
        body.append(f'<h2 id="{e(r)}">{e(r)}</h2><div class="sub">{len(clusters_by_region[r])} distinct stories found, showing {len(stories)}</div>')
        for c in stories:
            L = c["lead"]
            n = len(c["outlets"])
            tags = []
            if n == 1:
                tags.append('<span class="tag" title="Only one outlet reported this">single source</span>')
            else:
                tags.append(f'<span class="tag ok">{n} outlets</span>')
            if c["charged_share"] > 0:
                why = sorted({w for m in c["items"] for w in m["tone"]["reasons"]})
                tags.append(f'<span class="tag warn" title="{e(", ".join(why))}">charged wording {int(c["charged_share"]*100)}%</span>')
            leans = sorted({cfg["source_lean"][o] for o in c["outlets"] if o in cfg["source_lean"]})
            if leans:
                tags.append(f'<span class="tag">lean: {e(" / ".join(leans))}</span>')
            others = ""
            if len(c["items"]) > 1:
                lis = "".join(
                    f'<li><a href="{e(m["link"])}" target="_blank" rel="noopener">{e(m["source"])}</a>: {e(m["title"])}'
                    + (f' <span class="tag warn">{e(", ".join(m["tone"]["reasons"]))}</span>' if m["tone"]["score"] else "")
                    + "</li>"
                    for m in sorted(c["items"], key=lambda m: m["source"]))
                others = f"<details><summary>Compare how {len(c['items'])} headlines framed it</summary><ul>{lis}</ul></details>"
            body.append(
                f'<div class="card"><a class="h" href="{e(L["link"])}" target="_blank" rel="noopener">{e(L["title"])}</a>'
                f'<div class="meta"><span class="st">{e(c["state"])}</span><span>{e(L["source"])}</span>'
                f'<span>{ago(c["age_h"])}</span>{"".join(tags)}</div>{others}</div>')
        if silent:
            body.append(f'<p class="silent">No coverage found in the window for: {e(", ".join(silent))}</p>')

    # ---- bias section
    rows = []
    for g in stats["regions"]:
        ratio = g["share"] / g["pop_share"] if g["pop_share"] else 0
        cls = "hi" if ratio > 1.5 else "lo" if ratio < 0.67 else ""
        rows.append(
            f'<tr><td>{e(g["region"])}</td><td class="n">{g["national"]}</td>'
            f'<td><div class="barw"><i style="width:{min(100, g["share"]*100):.1f}%"></i><b style="left:{min(100, g["pop_share"]*100):.1f}%"></b></div></td>'
            f'<td class="n">{g["share"]*100:.0f}% / {g["pop_share"]*100:.0f}%</td>'
            f'<td class="n {cls}">{ratio:.2f}×</td></tr>')
    region_table = ('<div class="tw"><table><tr><th>Region</th><th>National headlines</th><th>Share of national headlines (bar) vs population (line)</th>'
                    '<th>Headlines / pop.</th><th>Ratio</th></tr>' + "".join(rows) + "</table></div>")

    rows2 = "".join(
        f'<tr><td>{e(g["region"])}</td><td class="n">{g["articles"]}</td><td class="n">{g["outlets"]}</td>'
        f'<td>{e(g["top_source"])} <span class="sub">({g["top_source_share"]*100:.0f}%)</span></td>'
        f'<td class="n">{g["charged"]*100:.0f}%</td></tr>' for g in stats["regions"])
    pool_table = ('<div class="tw"><table><tr><th>Region</th><th>Articles pulled</th><th>Outlets</th><th>Biggest outlet</th><th>Charged</th></tr>'
                  + rows2 + "</table></div>")

    cov = sorted([c for c in stats["coverage"] if c["ratio"] is not None], key=lambda c: c["ratio"])
    def cov_row(c):
        cls = "hi" if c["ratio"] > 2 else "lo" if c["ratio"] < 0.5 else ""
        local = f'{c["local"]}+' if c["local"] >= 95 else str(c["local"])
        return (f'<tr><td>{e(c["state"])}</td><td class="sub">{e(c["region"])}</td><td class="n">{c["national"]}</td>'
                f'<td class="n {cls}">{c["ratio"]:.2f}×</td><td class="n">{local}</td></tr>')
    state_table = ('<div class="tw"><table><tr><th>State / UT</th><th>Region</th><th>National headlines</th><th>vs population</th><th>Local stories available</th></tr>'
                   + "".join(cov_row(c) for c in cov) + "</table></div>")

    orow = "".join(
        f'<tr><td>{e(o["source"])}</td><td class="n">{o["articles"]}</td><td class="n">{o["charged"]*100:.0f}%</td>'
        f'<td>{e(o["home_region"])} <span class="sub">({o["home_share"]*100:.0f}%)</span></td><td>{e(o["lean"]) or "<span class=sub>not set</span>"}</td></tr>'
        for o in stats["outlets"])
    outlet_table = ('<div class="tw"><table><tr><th>Outlet</th><th>Articles</th><th>Charged headlines</th><th>Mostly covers</th><th>Your lean label</th></tr>'
                    + orow + "</table></div>")

    bias = f"""
<h2 id="bias">Bias check</h2>
<p class="note">These numbers measure <b>what gets covered</b> and <b>how headlines are worded</b>.
They don't measure whether a story is true.</p>
<h3>1 · What a normal national feed shows you</h3>
<p class="note">From {stats['nat_headlines']} Google News India top and national headlines
({"today only, which is noisy, so run it daily and this averages over up to 30 days" if stats['nat_days'] == 1 else f"last {stats['nat_days']} days"}): each region's share of
the headlines that name a state ({stats['nat_untagged']} named no state), compared with its share of India's population (Census 2011).
1.0× means attention matches population. <span class="hi">Above 1.5×</span> means over-covered, <span class="lo">below 0.67×</span> means under-covered.
The digest above corrects for this by giving every region the same number of slots and every state a turn.</p>
{region_table}
<details><summary>Per state, most under-covered first</summary>
<p class="note">"Local stories available" counts what the state's own feed returned. 95+ means Google's cap was hit, so there were more.
States with lots of local stories but almost no national headlines are the blind spots.</p>{state_table}</details>
<h3>2 · Where this digest's stories come from</h3>
{pool_table}
<h3>3 · Outlets and framing</h3>
<p class="note">Charged = the headline uses loaded words ("slams", "shocking", "big blow"...), ALL-CAPS or "!".
For stories covered by several outlets, the digest shows the most neutrally worded headline and lets you compare the rest.
To add political-lean labels, fill in <code>source_lean</code> in config.json.</p>
{outlet_table}
"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>India Daily Digest</title>
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="India Digest"><meta name="theme-color" content="#2f5d8a"><style>{CSS}</style></head>
<body><div class="wrap"><header><h1>India, region by region</h1>
<div class="sub">Generated {e(now)} · last {cfg['window_hours']}h · up to {cfg['max_stories_per_region']} stories per region</div></header>
<nav>{nav}</nav>{''.join(body)}{bias}</div></body></html>"""


# ---------------------------------------------------------------- main

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=str(ROOT / "output" / "digest.html"))
    ap.add_argument("--open", action="store_true", help="open the digest in the browser")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    arts = collect(cfg)

    picks, clusters_by_region = {}, {}
    for region, states in cfg["regions"].items():
        cl = cluster([a for a in arts if a["region"] == region], cfg["window_hours"])
        clusters_by_region[region] = cl
        picks[region] = select(cl, [s["state"] for s in states], cfg["max_stories_per_region"])
        print(f"  {region:<10} {len(cl):>4} stories -> {len(picks[region])} shown")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    nat = accumulate(national_attention(cfg), cfg, out.parent / "history.json")
    stats = bias_stats(arts, cfg, nat)
    out.write_text(render(cfg, picks, stats, clusters_by_region), encoding="utf-8")
    print(f"Wrote {out}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
