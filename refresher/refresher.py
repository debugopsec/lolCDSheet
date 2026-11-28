#!/usr/bin/env python3
"""
Refresher for the LoL Cooldown Sheet
====================================
Updates the three static tables baked into lol_cd_sheet.py:

  * ability ranges        - scraped from the League of Legends wiki ability data templates
  * skill order + role    - scraped from u.gg champion pages

Run it when a patch changes ability ranges, when the popular leveling order shifts, or when a new
champion comes out. It also tells you which champions are missing from the tables.

COOLDOWNS ARE NOT REFRESHED HERE. They come from Data Dragon / CommunityDragon and update
automatically the first time the app starts after a patch.

Takes a few minutes (about 1,000 wiki templates + 170 u.gg pages). Needs internet.

Usage:
    python refresher.py            # fetch, report, and rewrite lol_cd_sheet.py
    python refresher.py --check    # only report what is missing, don't rewrite anything
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import lol_cd_sheet as app  # noqa: E402

UA_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
# The wiki rejects browser user agents (403) and asks for a descriptive one.
UA_WIKI = {"User-Agent": "lol-cd-sheet/1.0 (data refresher)"}
UGG = "https://u.gg/lol/champions/{slug}/build"
WIKI_API = "https://wiki.leagueoflegends.com/en-us/api.php"
SIMPLE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
PROGRESSION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:to|–|-)\s*(\d+(?:\.\d+)?)\s*$")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def get_text(url: str, tries: int = 3) -> str | None:
    headers = UA_WIKI if url.startswith(WIKI_API) else UA_BROWSER
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        except Exception:
            time.sleep(1 + attempt * 2)
    return None


def wiki(**params) -> dict | None:
    params.setdefault("format", "json")
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    body = get_text(url, tries=2)
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# u.gg: skill order + primary role
# --------------------------------------------------------------------------- #
def slug(name: str) -> str:
    s = name.lower().replace("'", "").replace(".", "").replace("&", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def parse_ugg(html: str) -> tuple[str | None, str | None]:
    """('QWEWQEQ...' one letter per level 1-18, role)."""
    i = html.find("Skill Path")
    chunk = html[i:i + 40000] if i >= 0 else ""
    levels: dict[int, str] = {}
    for row in chunk.split("skill-order-row")[1:5]:
        m = re.search(r'skill-label bottom-right">([QWER])<', row)
        if not m:
            continue
        for lvl in re.findall(r'class="skill-up[^"]*"><div>(\d+)</div>', row):
            n = int(lvl)
            if 1 <= n <= 18 and n not in levels:
                levels[n] = m.group(1)
    path = "".join(levels.get(i, "?") for i in range(1, 19)) if len(levels) >= 15 else None
    m = re.search(r"Build for (Top|Jungle|Mid|Middle|Bot|Bottom|ADC|Support)", html)
    role = m.group(1) if m else None
    role = {"Bottom": "Bot", "ADC": "Bot", "Middle": "Mid"}.get(role, role)
    return path, role


def fetch_ugg(name: str) -> tuple[str, str | None, str | None]:
    html = get_text(UGG.format(slug=slug(name)))
    if not html:
        return name, None, None
    path, role = parse_ugg(html)
    return name, path, role


# --------------------------------------------------------------------------- #
# LoL wiki: ability ranges
# --------------------------------------------------------------------------- #
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
    """Evaluate a wiki range expression into plain text, or None if it uses anything unsupported."""
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
            if name == "tip":
                out.append("")
            elif name in ("tt", "fd", "pp"):
                val = render(parts[1]) if len(parts) > 1 else None
                if val is None:
                    return None
                out.append(val)
            elif name == "dv":
                vals = [render(p) for p in parts[1:]]
                vals = [v for v in vals if v is not None]
                out.append(vals[0] if vals else "")
            else:
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
        return f"{value:g}" if 100 <= value <= 20000 else None
    m = PROGRESSION.match(plain)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if 100 <= a < b <= 20000:
            return f"{a:g}–{b:g}"
        if 100 <= b <= 20000 and a < 100:
            return f"{b:g}"
    return None


def wiki_param(text: str, name: str) -> str | None:
    m = re.search(rf"^\|\s*{name}\s*=\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def wiki_templates() -> list[str]:
    titles, cont = [], {}
    while True:
        data = wiki(action="query", list="categorymembers", cmtitle="Category:Ability data templates",
                    cmlimit="500", cmnamespace="10", **cont)
        if not data:
            return titles
        titles += [m["title"] for m in data["query"]["categorymembers"]]
        if "continue" not in data:
            return titles
        cont = data["continue"]


def wiki_texts(titles: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        data = wiki(action="query", prop="revisions", rvprop="content", rvslots="main",
                    titles="|".join(titles[i:i + 40]))
        for page in (data or {}).get("query", {}).get("pages", {}).values():
            rev = page.get("revisions")
            if rev:
                out[page["title"]] = rev[0]["slots"]["main"]["*"]
    return out


def build_ranges(dd) -> dict[str, str]:
    name_to_id = {}
    for cid in dd.champ_ids:
        name_to_id[app.norm(dd.champ_names[cid])] = cid
        name_to_id[app.norm(cid)] = cid

    texts = wiki_texts(wiki_templates())
    candidates: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for title, text in texts.items():
        m = re.match(r"Template:Data (.+)/(.+)$", title)
        if not m:
            continue
        cid = name_to_id.get(app.norm(m.group(1)))
        skill = wiki_param(text, "skill")
        if not cid or not skill:
            continue
        slot = "QWER".find(skill[0].upper())
        if slot < 0:
            continue
        value = parse_range(wiki_param(text, "range"))
        if value is None:
            value = parse_range(wiki_param(text, "target range"))
        if value is None:
            spells = dd.champion(cid)["spells"]
            raw = [v for v in (spells[slot].get("range") or []) if isinstance(v, (int, float))] \
                if slot < len(spells) else []
            if not raw or max(raw) < app.RANGE_SELF or max(raw) >= app.RANGE_UNLIMITED:
                value = parse_range(wiki_param(text, "effect radius"))
                if value and value[0].isdigit():
                    value = f"AoE {value}"
        if value is not None:
            candidates.setdefault((cid, slot), []).append((m.group(2), value))

    table: dict[str, str] = {}
    for (cid, slot), options in candidates.items():
        spells = dd.champion(cid)["spells"]
        if slot >= len(spells):
            continue
        spell = spells[slot]
        sname = app.norm(spell["name"])
        exact = [o for o in options if app.norm(o[0]) == sname]
        loose = [o for o in options if sname.startswith(app.norm(o[0])) or app.norm(o[0]).startswith(sname)]
        pick = exact or loose or (options if len(options) == 1 else [])
        if len({p[1] for p in pick}) != 1:
            continue
        value = pick[0][1]
        raw = [v for v in (spell.get("range") or []) if isinstance(v, (int, float))]
        placeholder = not raw or max(raw) < app.RANGE_SELF or max(raw) >= app.RANGE_UNLIMITED
        nums = re.findall(r"\d+(?:\.\d+)?", value)
        if not placeholder and nums and raw:
            a, b = float(nums[-1]), max(raw)
            if b and not (0.5 <= a / b <= 2.0):
                continue  # wiki measures something else (dash / recast / bounce) - keep Data Dragon
        table[spell["id"]] = value
    return table


# --------------------------------------------------------------------------- #
# Rewriting lol_cd_sheet.py
# --------------------------------------------------------------------------- #
def dict_block(header: str, items, per_line: int = 1) -> str:
    lines = [header]
    for i in range(0, len(items), per_line):
        chunk = " ".join(f"{k!r}: {v!r}," for k, v in items[i:i + per_line])
        lines.append(f"    {chunk}")
    lines.append("}\n")
    return "\n".join(lines)


def replace_block(src: str, header: str, block: str) -> str:
    i = src.find(header)
    if i < 0:
        raise SystemExit(f"Could not find the table starting with {header!r} in lol_cd_sheet.py")
    start = src.rfind("\n", 0, i) + 1
    end = src.find("\n}\n", i)
    if end < 0:
        raise SystemExit(f"Could not find the end of {header!r}")
    return src[:start] + block + src[end + 3:]


def main():
    ap = argparse.ArgumentParser(description="Refresh ranges, skill orders and roles in lol_cd_sheet.py.")
    ap.add_argument("--check", action="store_true", help="only report missing champions, don't rewrite the file")
    ap.add_argument("--ranges-only", action="store_true", help="only refresh ability ranges (skips u.gg)")
    args = ap.parse_args()

    dd = app.DataDragon()
    dd.load()
    print(f"Patch data: {dd.version}")

    names = sorted(dd.champ_names.values())
    skill_path: dict[str, str] = {}
    roles: dict[str, str] = {}
    if not args.ranges_only:
        print("Fetching skill orders and roles from u.gg ...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, (name, path, role) in enumerate(pool.map(fetch_ugg, names), 1):
                if path:
                    skill_path[name] = path
                if role:
                    roles[name] = role.lower()
                print(f"  {i}/{len(names)} {name}", end="\r", flush=True)
        print()

    print("Fetching ability ranges from the LoL wiki ...")
    ranges = build_ranges(dd)

    have = set(skill_path) & set(roles)
    missing = [n for n in names if n not in have] if not args.ranges_only else []
    print(f"\nSkill order/role: {len(skill_path)} paths, {len(roles)} roles, {len(ranges)} ranges")
    if args.ranges_only:
        print("(--ranges-only: skill orders and roles left as they are)")
    elif missing:
        print("New / unrecognised champions (they use fallback values until added):")
        print("  " + ", ".join(missing))
    else:
        print("Every champion has a skill order and role.")

    if args.check:
        print("\n--check: nothing written.")
        return

    complete = len(ranges) >= 300
    if not args.ranges_only:
        complete = complete and len(skill_path) >= 120 and len(roles) >= 120
    if not complete:
        print("\nRefusing to write: the fetched tables look incomplete (a site is down or changed layout).")
        print("Nothing was modified.")
        return

    target = ROOT / "lol_cd_sheet.py"
    src = target.read_text(encoding="utf-8")
    src = replace_block(src, "WIKI_RANGES = {",
                        dict_block("WIKI_RANGES = {", sorted(ranges.items()), 1))
    if not args.ranges_only:
        src = replace_block(src, "SKILL_PATH: dict[str, str] = {",
                            dict_block("SKILL_PATH: dict[str, str] = {", sorted(skill_path.items()), 1))
        src = replace_block(src, "CHAMP_ROLE: dict[str, str] = {",
                            dict_block("CHAMP_ROLE: dict[str, str] = {", sorted(roles.items()), 4))
    target.write_text(src, encoding="utf-8", newline="\n")
    print(f"Updated {target.name}. Cooldowns were not touched (those update automatically).")


if __name__ == "__main__":
    main()
