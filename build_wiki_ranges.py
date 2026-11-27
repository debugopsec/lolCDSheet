"""Regenerate wiki_ranges.py: ability ranges from the League of Legends wiki (ability data templates).

Run from this folder with Python 3:  python build_wiki_ranges.py
Takes a minute or two (about 1,000 wiki pages, 40 per request). Network required.

The wiki stores ranges as template expressions ({{tt|900|note}}, {{dv|centered|edge}}, {{pp|a to b}}).
Only expressions that resolve to a single clean number (or a "a to b" progression) are used; anything
ambiguous (dashes, offsets, alternate values) is skipped and the sheet falls back to Data Dragon.
"""
from __future__ import annotations

import collections
import json
import re
import sys
import urllib.parse
import urllib.request

API = "https://wiki.leagueoflegends.com/en-us/api.php"
UA = {"User-Agent": "lol-cd-sheet/1.0 (range table generator)"}
SIMPLE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
PROGRESSION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:to|–|-)\s*(\d+(?:\.\d+)?)\s*$")


def api(**kw):
    q = urllib.parse.urlencode({"format": "json", **kw})
    req = urllib.request.Request(f"{API}?{q}", headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def all_templates() -> list[str]:
    titles, cont = [], {}
    while True:
        r = api(action="query", list="categorymembers", cmtitle="Category:Ability data templates",
                cmlimit="500", cmnamespace="10", **cont)
        titles += [m["title"] for m in r["query"]["categorymembers"]]
        if "continue" not in r:
            return titles
        cont = r["continue"]


def fetch_texts(titles: list[str]) -> dict[str, str]:
    out = {}
    for i in range(0, len(titles), 40):
        r = api(action="query", prop="revisions", rvprop="content", rvslots="main",
                titles="|".join(titles[i:i + 40]))
        for p in r["query"]["pages"].values():
            rev = p.get("revisions")
            if rev:
                out[p["title"]] = rev[0]["slots"]["main"]["*"]
    return out


def param(text: str, name: str) -> str | None:
    m = re.search(rf"^\|\s*{name}\s*=\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def split_top(body: str, sep: str = "|") -> list[str]:
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(body):
        if body.startswith("{{", i):
            depth += 1
            cur.append("{{")
            i += 2
            continue
        if body.startswith("}}", i):
            depth -= 1
            cur.append("}}")
            i += 2
            continue
        if body[i] == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(body[i])
        i += 1
    parts.append("".join(cur))
    return parts


def render(s: str) -> str | None:
    """Evaluate the range expression into plain text, or None if it uses anything unsupported."""
    out, i = [], 0
    while i < len(s):
        if s.startswith("{{", i):
            depth, j = 0, i
            while j < len(s):
                if s.startswith("{{", j):
                    depth += 1
                    j += 2
                elif s.startswith("}}", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            if depth != 0:
                return None
            parts = split_top(s[i + 2:j - 2])
            name = parts[0].strip()
            if name in ("tip",):           # icon only, no text
                out.append("")
            elif name in ("tt", "fd", "pp"):  # {{tt|value|note}}, {{fd|0.75}}, {{pp|a to b|...}}
                val = render(parts[1]) if len(parts) > 1 else None
                if val is None:
                    return None
                out.append(val)
            elif name == "dv":             # {{dv|centered|edge}}: first usable branch
                vals = [render(p) for p in parts[1:]]
                vals = [v for v in vals if v is not None]
                out.append(vals[0] if vals else "")
                if len(vals) > 1 and vals[0] == vals[1]:
                    pass
            else:                          # {{ap|...}}, {{plus}}, {{times}}, ...: don't guess
                return None
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def parse_range(line: str | None) -> str | None:
    if not line:
        return None
    text = re.sub(r"<!--.*?-->", "", line, flags=re.S).strip()
    low = text.lower()
    if low.startswith(("self", "n/a", "none")):
        return "self"
    if low.startswith("global"):
        return "global"
    plain = render(text)
    if plain is None:
        return None
    plain = plain.strip()
    m = SIMPLE.match(plain)
    if m:
        value = float(m.group(1))
        if 100 <= value <= 20000:
            return f"{value:g}"
        return None
    m = PROGRESSION.match(plain)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if 100 <= a < b <= 20000:
            return f"{a:g}–{b:g}"
        if 100 <= b <= 20000 and a < 100:
            return f"{b:g}"
    return None


def main():
    sys.path.insert(0, ".")
    import lol_cd_sheet as app

    dd = app.DataDragon()
    dd.load()
    name_to_id = {}
    for cid in dd.champ_ids:
        name_to_id[app.norm(dd.champ_names[cid])] = cid
        name_to_id[app.norm(cid)] = cid

    texts = fetch_texts(all_templates())
    print(f"{len(texts)} ability templates fetched")

    cands = collections.defaultdict(list)
    for title, text in texts.items():
        m = re.match(r"Template:Data (.+)/(.+)$", title)
        if not m:
            continue
        cid = name_to_id.get(app.norm(m.group(1)))
        skill = param(text, "skill")
        if not cid or not skill:
            continue
        slot = "QWER".find(skill[0].upper())
        if slot < 0:
            continue
        value = parse_range(param(text, "range"))
        if value is None:
            value = parse_range(param(text, "target range"))
        if value is None:
            spells = dd.champion(cid)["spells"]
            raw = [v for v in (spells[slot].get("range") or []) if isinstance(v, (int, float))] \
                if slot < len(spells) else []
            if not raw or max(raw) < app.RANGE_SELF or max(raw) >= app.RANGE_UNLIMITED:
                value = parse_range(param(text, "effect radius"))
                if value and value[0].isdigit():
                    value = f"AoE {value}"
        if value is not None:
            cands[(cid, slot)].append((m.group(2), value))

    table, skipped = {}, []
    for (cid, slot), options in cands.items():
        spells = dd.champion(cid)["spells"]
        if slot >= len(spells):
            continue
        spell = spells[slot]
        sname = app.norm(spell["name"])
        exact = [o for o in options if app.norm(o[0]) == sname]
        loose = [o for o in options if sname.startswith(app.norm(o[0])) or app.norm(o[0]).startswith(sname)]
        pick = exact or loose or (options if len(options) == 1 else [])
        if len({p[1] for p in pick}) != 1:
            skipped.append(f"{cid} {slot} {spell['name']}: {options}")
            continue
        table[spell["id"]] = pick[0][1]

    # Sanity guard: the wiki's "Range" is sometimes a different measurement (a dash distance, a recast
    # range, a bounce-adjusted maximum). Where it disagrees with Data Dragon by more than 2x, keep Data
    # Dragon's figure instead of introducing a wild outlier.
    def numeric(text: str) -> float | None:
        nums = re.findall(r"\d+(?:\.\d+)?", str(text))
        return float(nums[-1]) if nums else None

    guarded, outliers = {}, []
    for sid, value in table.items():
        champ = next(c for c in dd.champ_ids
                     for s in dd.champion(c)["spells"] if s["id"] == sid)
        spell = next(s for s in dd.champion(champ)["spells"] if s["id"] == sid)
        raw = [v for v in (spell.get("range") or []) if isinstance(v, (int, float))]
        placeholder = not raw or max(raw) < app.RANGE_SELF or max(raw) >= app.RANGE_UNLIMITED
        a, b = numeric(value), (max(raw) if raw else None)
        if not placeholder and a and b and not (0.5 <= a / b <= 2.0):
            outliers.append(f"{sid}: wiki {value} vs ddragon {app.fmt_range(raw)}")
            continue
        guarded[sid] = value
    table = guarded

    lines = [
        '"""Ability ranges from the League of Legends wiki (ability data templates).',
        "Generated by build_wiki_ranges.py -- re-run that script to refresh. Values use the sheet's",
        'notation: "AoE N" = radius around the caster, "A-B" = grows while charging / with level.',
        '"""',
        "",
        "RANGES = {",
    ]
    for sid in sorted(table):
        lines.append(f"    {sid!r}: {table[sid]!r},")
    lines += ["}", ""]
    with open("wiki_ranges.py", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    print(f"wrote wiki_ranges.py: {len(table)} spells, {len(skipped)} ambiguous/skipped, "
          f"{len(outliers)} outliers kept on Data Dragon")
    for s in outliers[:10]:
        print("  outlier:", s)
    for s in skipped[:10]:
        print("  skipped:", s)


if __name__ == "__main__":
    main()
