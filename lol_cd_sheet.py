#!/usr/bin/env python3
"""
LoL Cooldown Sheet
==================
Waits for a League of Legends game to start, then shows every enemy champion's
Q/W/E/R cooldowns and cast ranges (per rank) and their summoner spell cooldowns, so you
know roughly when things are back up for trading / cooldown punishing.

This is a static reference sheet, NOT an active cooldown timer.

Data sources (both official Riot):
  * Live Client Data API  - https://127.0.0.1:2999  (served by the game client while you're in a match)
  * Data Dragon           - https://ddragon.leagueoflegends.com  (champion + summoner spell data, icons)

Usage:
  python lol_cd_sheet.py                      # GUI, waits for a game automatically
  python lol_cd_sheet.py --console            # print to terminal instead of a window
  python lol_cd_sheet.py --demo               # preview with sample enemies (no game needed)
  python lol_cd_sheet.py --demo Ahri Zed "Lee Sin" Jinx Thresh
  python lol_cd_sheet.py --columns 5 --no-names   # wide, compact layout
  python lol_cd_sheet.py --no-ranges          # hide ability ranges
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIVE_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
DDRAGON = "https://ddragon.leagueoflegends.com"
# Raw game data; Data Dragon has no recharge times for charge-based abilities (Corki R, Zyra W, ...).
CDRAGON = "https://raw.communitydragon.org"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
POLL_SECONDS = 2

POS_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}
# Data Dragon values that are misleading for "when is it back up" purposes.
SUMMONER_CD_OVERRIDES = {
    "SummonerSmite": "90",  # ddragon lists 15 (gap between charges); each charge recharges in 90s
}
# Data values that hold the real cooldown when a spell's cooldown field is 0 (Veigar W, Kalista E).
BIN_COOLDOWN_VALUES = ("BaseCooldown", "FakedCooldown", "Cooldown")
# Data Dragon ranges below this are self-casts / placeholders (Darius Q = 1, Teemo W = 20); at or above
# RANGE_UNLIMITED they're global or placeholders (Ezreal R = 25000, Janna W = 4294967295). Every ability
# like that (as of patch 16.23) has a hand-checked entry in RANGE_OVERRIDES; the fallback for anything new
# is "self" / "?".
RANGE_SELF, RANGE_UNLIMITED = 100, 10000
# Data Dragon spell id -> range text. Sources: the in-game targeting indicators in the game files
# (CommunityDragon) and the LoL wiki (via Meraki Analytics). Notation: "AoE N" = radius around the
# champion, "AA" = empowers their basic attack (their attack range), "A–B" = grows while charging /
# with level, "x·y·z" = Hwei's three sub-casts, "/" = per rank.
RANGE_OVERRIDES = {
    # Data Dragon has a placeholder (0 / 1 / 20 / 25000 / ...) for these.
    "AatroxQ": "850·550·300", "AatroxE": "300", "AatroxR": "AoE 600",
    "AkshanW": "self",
    "FerociousHowl": "self",  # Alistar R
    "AsheSpiritOfTheHawk": "global", "EnchantedCrystalArrow": "global",
    "BardQ": "850",
    "Overdrive": "self",  # Blitzcrank W
    "BraumE": "self",
    "BriarR": "global",
    "VorpalSpikes": "AA",  # Cho'Gath E
    "DariusCleave": "AoE 425",
    "DrMundoE": "AA", "DrMundoR": "self",
    "DravenRCast": "global",
    "EliseR": "self",
    "EvelynnR": "500",
    "EzrealR": "global",
    "GangplankR": "global",
    "GarenW": "self",
    "GnarW": "550",  # Mega Gnar stun; mini Gnar W is passive
    "GragasW": "AA",
    "GwenW": "AoE 480",
    "HecarimUlt": "1000",
    "HeimerdingerR": "self",
    "HweiQ": "800·1900·1200", "HweiW": "1200·650·self", "HweiE": "1100·900·800",
    "SowTheWind": "550",  # Janna W
    "JhinR": "3500",
    "JinxR": "global",
    "KaisaE": "self",
    "KarthusFallenOne": "global",
    "NetherBlade": "AA",  # Kassadin W
    "KatarinaW": "self",
    "KhazixR": "self",
    "KledW": "AA",
    "LeblancR": "varies",  # copies her last ability
    "LeonaShieldOfDaybreak": "AA",
    "Meditate": "self", "WujuStyle": "self", "Highlander": "self",  # Master Yi W/E/R
    "MelR": "global",
    "MissFortuneBulletTime": "1400",
    "MordekaiserW": "self",
    "AspectOfTheCougar": "self",  # Nidalee R
    "NocturneShroudofDarkness": "self",
    "NunuW": "750–1750",
    "OrnnW": "500",
    "Tremors2": "800+",  # Rammus R, longer with movement speed
    "RenektonReignOfTheTyrant": "AoE 375",
    "RumbleShield": "self",
    "SennaR": "global",
    "SeraphineR": "1200",
    "SettQ": "AA", "SettW": "805",
    "ShenR": "global",
    "ShyvanaQ": "500",
    "PoisonTrail": "self", "InsanityPotion": "self",  # Singed Q/R
    "SionQ": "750",
    "SivirW": "AA", "SivirE": "self",
    "SorakaR": "global",
    "TahmKenchRWrapper": "250",
    "TeemoW": "self",
    "TristanaQ": "self",
    "WildCards": "1450", "CardmasterStack": "AA",  # Twisted Fate Q/E
    "TwitchHideInShadows": "self",
    "UdyrW": "self",
    "VayneInquisition": "self",
    "WarwickR": "2.5×MS",  # leap distance = 250% of his movement speed
    "YoneE": "300",
    "YorickQ": "AA",
    "YunaraQ": "AA", "YunaraE": "self", "YunaraR": "self",
    "YuumiQ": "1150", "YuumiW": "700",
    "ZaahenQ": "AA", "ZaahenE": "350",
    "ZeriE": "300",
    "ZoeW": "varies",  # casts whatever summoner spell / item she picked up
    # Data Dragon only lists the starting range of these charged / scaling abilities.
    "GalioW": "AoE 275–450",
    "PoppyR": "500–1200",
    "PykeQ": "400–1100",
    "TristanaE": "550–700", "TristanaR": "550–700",  # grows with her attack range by level
    "VarusQ": "925–1600",
    "ViQ": "250–715",
    "ViegoW": "500–900",
    "XerathArcanopulseChargeUp": "750–1450",
    "ZacE": "1200/1350/1500/1650/1800",
    "ZacR": "AoE 300",
    # Data Dragon's number is just wrong for these.
    "FeralScream": "650",  # Cho'Gath W (Data Dragon: 300)
    "MilioW": "650",  # (Data Dragon: 350, the heal radius)
}
POS_LABEL = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "Bot", "UTILITY": "Support"}

# The game client serves the live API with a self-signed Riot cert on localhost.
_LOCAL_SSL = ssl.create_default_context()
_LOCAL_SSL.check_hostname = False
_LOCAL_SSL.verify_mode = ssl.CERT_NONE


def http_get(url: str, timeout: float = 15, ctx: ssl.SSLContext | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lol-cd-sheet/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def write_atomic(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def version_key(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (-1,)


def fmt_cd(values) -> str:
    vals = [v for v in (values or []) if isinstance(v, (int, float))]
    if not vals or all(v == 0 for v in vals):
        return "—"
    if len(set(vals)) == 1:
        return f"{vals[0]:g}"
    return "/".join(f"{v:g}" for v in vals)


def fmt_range(values) -> str:
    vals = [v for v in (values or []) if isinstance(v, (int, float))]
    if not vals:
        return "—"
    if max(vals) < RANGE_SELF:
        return "self"
    if max(vals) >= RANGE_UNLIMITED:
        return "?"  # placeholder for an ability not in RANGE_OVERRIDES yet (new champion?)
    if len(set(vals)) == 1:
        return f"{vals[0]:g}"
    return "/".join(f"{v:g}" for v in vals)


# --------------------------------------------------------------------------- #
# Data Dragon
# --------------------------------------------------------------------------- #
class DataDragon:
    def __init__(self):
        self.version: str | None = None
        self.champ_lookup: dict[str, str] = {}  # normalized name/id -> ddragon id
        self.champ_ids: set[str] = set()
        self.summoners: dict[str, dict] = {}  # ddragon id -> data
        self.summ_lookup: dict[str, str] = {}  # normalized name/id -> ddragon id
        self.champ_by_key: dict[str, str] = {}  # numeric champion id (League client) -> ddragon id
        self.champ_names: dict[str, str] = {}
        self.summ_by_key: dict[str, str] = {}  # numeric summoner spell id (League client) -> ddragon id
        self._champ_cache: dict[str, dict] = {}
        self._ammo_cache: dict[str, dict] = {}
        self._full_cache: dict | None = None

    def load(self):
        try:
            versions = json.loads(http_get(f"{DDRAGON}/api/versions.json"))
            self.version = versions[0]
        except Exception:
            # Offline? Fall back to the newest version we have cached.
            cached = [p.name for p in CACHE_DIR.glob("*") if p.is_dir()] if CACHE_DIR.exists() else []
            if not cached:
                raise
            self.version = max(cached, key=version_key)

        champs = self.json("data/en_US/champion.json")["data"]
        for cid, c in champs.items():
            self.champ_ids.add(cid)
            self.champ_lookup[norm(cid)] = cid
            self.champ_lookup.setdefault(norm(c["name"]), cid)
            self.champ_by_key[c["key"]] = cid
            self.champ_names[cid] = c["name"]

        summs = self.json("data/en_US/summoner.json")["data"]
        self.summoners = summs
        # Shorter ids first so e.g. "SummonerSnowball" wins over mode-specific variants.
        for sid in sorted(summs, key=len):
            self.summ_lookup.setdefault(norm(sid), sid)
            self.summ_lookup.setdefault(norm(summs[sid]["name"]), sid)
            self.summ_by_key.setdefault(summs[sid]["key"], sid)

    def fetch(self, rel: str) -> Path:
        """Download a file from the ddragon CDN for the current version (cached on disk)."""
        path = CACHE_DIR / self.version / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, http_get(f"{DDRAGON}/cdn/{self.version}/{rel}"))
        return path

    def fetch_optional(self, rel: str) -> Path | None:
        try:
            return self.fetch(rel)
        except Exception:
            return None

    def cached(self, rel: str) -> Path | None:
        """The file if it's already on disk, without touching the network."""
        path = CACHE_DIR / self.version / rel
        return path if path.exists() else None

    def prefetch_all(self):
        """Download every champion's data for this patch in the background (once per patch), so nothing has to
        be fetched while a game is starting. ~15 MB; already-cached champions are skipped."""
        try:
            self._full()
        except Exception:
            pass
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda cid: self.bin_spells(cid), sorted(self.champ_ids)))

    def _full(self) -> dict:
        """championFull.json: every champion's spell data in one file (instead of one request per champion)."""
        if self._full_cache is None:
            self._full_cache = self.json("data/en_US/championFull.json")["data"]
        return self._full_cache

    def json(self, rel: str) -> dict:
        return json.loads(self.fetch(rel).read_text(encoding="utf-8"))

    def champion(self, cid: str) -> dict:
        if cid not in self._champ_cache:
            if self._full_cache is None and self.cached("data/en_US/championFull.json"):
                try:
                    self._full()
                except ValueError:
                    pass
            if self._full_cache and cid in self._full_cache:
                self._champ_cache[cid] = self._full_cache[cid]
            else:
                self._champ_cache[cid] = self.json(f"data/en_US/champion/{cid}.json")["data"][cid]
        return self._champ_cache[cid]

    def bin_spells(self, cid: str, fetch: bool = True) -> dict | None:
        """Data Data Dragon lacks, from CommunityDragon's game files, keyed by spell script name:
        {"MissileBarrage": {"charges": [4, 4, 4], "recharge": [20, 20, 20]},
         "VeigarDarkMatter": {"cd": [8, 8, 8, 8, 8, 8]}}.
        "cd" is only set for spells whose regular cooldown field is 0 because the real one lives in a
        data value (Veigar W "BaseCooldown", Kalista E "FakedCooldown").
        None if it isn't cached and fetch is False, or the download failed (retried on the next call)."""
        if cid in self._ammo_cache:
            return self._ammo_cache[cid]
        path = CACHE_DIR / self.version / "cdragon" / f"{cid.lower()}.spells.json"
        result = None
        if path.exists():
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if result is None:
            if not fetch:
                return None
            result = {}
            patch = ".".join(self.version.split(".")[:2])
            raw = None
            for ver in (patch, "latest"):  # CDragon can lag a few hours behind on patch day
                try:
                    raw = json.loads(http_get(f"{CDRAGON}/{ver}/game/data/characters/{cid.lower()}/{cid.lower()}.bin.json"))
                    break
                except Exception:
                    continue
            if raw is None:
                return None  # don't cache the failure, try again next time
            for key, obj in raw.items():
                sp = obj.get("mSpell") if isinstance(obj, dict) else None
                name = obj.get("mScriptName") if sp else None
                if not name:
                    continue
                info = {}
                if sp.get("mMaxAmmo"):
                    info["charges"] = sp["mMaxAmmo"]
                    info["recharge"] = sp.get("mAmmoRechargeTime") or []
                if not any(sp.get("cooldownTime") or []):
                    for dv in sp.get("DataValues") or sp.get("mDataValues") or []:
                        if dv.get("name") in BIN_COOLDOWN_VALUES and any(dv.get("values") or []):
                            info["cd"] = dv["values"]
                            break
                if not info:
                    continue
                if name in result and not key.endswith("/" + name):
                    continue  # prefer the canonical Characters/X/Spells/YAbility/Y entry
                result[name] = info
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, json.dumps(result).encode("utf-8"))
        self._ammo_cache[cid] = result
        return result

    def resolve_champion(self, player: dict) -> str | None:
        raw = player.get("rawChampionName") or ""
        m = re.search(r"displayname_(\w+)$", raw)
        if m and m.group(1) in self.champ_ids:
            return m.group(1)
        return self.champ_lookup.get(norm(player.get("championName", "")))

    def resolve_summoner(self, spell: dict) -> str | None:
        raw = spell.get("rawDisplayName") or ""
        m = re.search(r"(Summoner[A-Za-z0-9]+)_DisplayName", raw)
        if m:
            sid = m.group(1)
            if sid in self.summoners:
                return sid
            # e.g. upgraded/variant spells like "SummonerSmiteDuel" -> "SummonerSmite"
            for known in sorted(self.summoners, key=len, reverse=True):
                if sid.startswith(known):
                    return known
        name = norm(spell.get("displayName", ""))
        if name in self.summ_lookup:
            return self.summ_lookup[name]
        # e.g. "Unleashed Teleport" -> Teleport, "Challenging Smite" -> Smite
        for key in sorted(self.summ_lookup, key=len, reverse=True):
            if key and key in name:
                return self.summ_lookup[key]
        return None


# --------------------------------------------------------------------------- #
# Live game -> view model
# --------------------------------------------------------------------------- #
def fetch_live_game() -> dict | None:
    try:
        data = json.loads(http_get(LIVE_URL, timeout=2, ctx=_LOCAL_SSL))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("allPlayers"):
        return None  # loading screen / not ready yet
    return data


class LeagueClient:
    """Read-only access to the League client's local API (LCU), which knows the champions long before the
    in-game API (port 2999) comes up after the loading screen: enemy picks as they lock in during champ
    select, and all 10 champions + summoner spells from the start of the loading screen.

    Converts what it finds into the same shape as the live API's allgamedata, so build_view() can use it."""

    LOADING_PHASES = ("GameStart", "InProgress", "Reconnect")

    def __init__(self):
        self._me: tuple[str, str] | None = None  # (port, puuid) of the logged-in summoner

    @staticmethod
    def _lockfiles() -> list[Path]:
        dirs = [Path("C:/Riot Games/League of Legends")]
        installs = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Riot Games" / "RiotClientInstalls.json"
        try:
            dirs = [Path(d) for d in json.loads(installs.read_text(encoding="utf-8")).get("associated_client", {})] + dirs
        except (OSError, ValueError, AttributeError):
            pass
        return [d / "lockfile" for d in dirs]

    def _creds(self) -> tuple[str, str] | None:
        for lf in self._lockfiles():
            try:
                _name, _pid, port, password, _proto = lf.read_text(encoding="utf-8").strip().split(":")
            except (OSError, ValueError):
                continue
            return port, "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()
        return None  # client not running (the lockfile only exists while it is)

    def _get(self, creds, path: str):
        port, auth = creds
        try:
            return json.loads(http_get(f"https://127.0.0.1:{port}{path}", timeout=2, ctx=_LOCAL_SSL,
                                       headers={"Authorization": auth}) or b"null")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def snapshot(self, dd: "DataDragon") -> tuple[str | None, dict | None]:
        """(gameflow phase, allgamedata-like dict or None if there's nothing to show yet)."""
        creds = self._creds()
        if not creds:
            return None, None
        phase = self._get(creds, "/lol-gameflow/v1/gameflow-phase")
        if phase == "ChampSelect":
            return phase, self._from_champ_select(dd, creds)
        if phase in self.LOADING_PHASES:
            return phase, self._from_gameflow(dd, creds)
        return phase, None

    def _my_puuid(self, creds) -> str:
        if not self._me or self._me[0] != creds[0]:
            me = self._get(creds, "/lol-summoner/v1/current-summoner") or {}
            if not me.get("puuid"):
                return ""
            self._me = (creds[0], me["puuid"])
        return self._me[1]

    @staticmethod
    def _player(dd, team, uid, champ_id, spell_ids, position, name) -> dict:
        cid = dd.champ_by_key.get(str(champ_id))
        p = {"championName": dd.champ_names.get(cid, "?"), "rawChampionName": f"game_character_displayname_{cid}",
             "team": team, "position": (position or "").upper(), "riotId": uid, "riotIdGameName": name or "", "fromClient": True,
             "summonerSpells": {}}
        for slot, key in zip(("summonerSpellOne", "summonerSpellTwo"), spell_ids):
            sid = dd.summ_by_key.get(str(key))
            if sid:
                p["summonerSpells"][slot] = {"displayName": dd.summoners[sid]["name"],
                                             "rawDisplayName": f"GeneratedTip_SummonerSpell_{sid}_DisplayName"}
        return p

    def _from_champ_select(self, dd, creds) -> dict | None:
        s = self._get(creds, "/lol-champ-select/v1/session")
        if not isinstance(s, dict):
            return None
        me = s.get("localPlayerCellId")
        players, my_uid = [], None
        for team, members in (("ORDER", s.get("myTeam") or []), ("CHAOS", s.get("theirTeam") or [])):
            for m in members:
                uid = f"cell{m.get('cellId')}"
                if team == "ORDER" and m.get("cellId") == me:
                    my_uid = uid
                elif not m.get("championId"):  # not locked in yet (hovers are ignored)
                    continue
                players.append(self._player(dd, team, uid, m.get("championId"), (m.get("spell1Id"), m.get("spell2Id")),
                                            m.get("assignedPosition"), m.get("gameName") or m.get("summonerName")))
        if not any(p["team"] == "CHAOS" for p in players):
            return None  # no enemy picks yet (or blind pick, where they stay hidden until loading)
        return {"activePlayer": {"riotId": my_uid}, "allPlayers": players, "gameData": {"gameMode": ""}}

    def _from_gameflow(self, dd, creds) -> dict | None:
        s = self._get(creds, "/lol-gameflow/v1/session")
        gd = (s or {}).get("gameData") or {}
        sels = gd.get("playerChampionSelections") or []
        by_puuid = {x.get("puuid"): x for x in sels if x.get("puuid")}
        by_name = {x.get("summonerInternalName"): x for x in sels if x.get("summonerInternalName")}
        by_champ = {x.get("championId"): x for x in sels if x.get("championId")}
        my_puuid = self._my_puuid(creds)
        players = []
        for team, members in (("ORDER", gd.get("teamOne") or []), ("CHAOS", gd.get("teamTwo") or [])):
            for i, m in enumerate(members):
                sel = by_puuid.get(m.get("puuid")) or by_name.get(m.get("summonerInternalName")) \
                    or by_champ.get(m.get("championId")) or {}
                champ = m.get("championId") or sel.get("championId")
                if not champ:
                    continue
                uid = m.get("puuid") or f"{team}{i}"
                players.append(self._player(dd, team, uid, champ, (sel.get("spell1Id"), sel.get("spell2Id")),
                                            m.get("selectedPosition"), m.get("gameName") or m.get("summonerName")))
        mine = next((p["team"] for p in players if my_puuid and p["riotId"] == my_puuid), None)
        if not any(p["team"] != mine for p in players):
            return None  # no enemies listed (practice tool; bots in some modes only appear in game)
        mode = ((gd.get("queue") or {}).get("gameMode")) or ""
        return {"activePlayer": {"riotId": my_puuid}, "allPlayers": players, "gameData": {"gameMode": mode}}


STATE_LABEL = {"live": "In game", "loading": "Loading screen", "champselect": "Champ select", "idle": "Waiting"}


class GameWatcher:
    """Polls the in-game API, falling back to the League client during champ select / loading screen."""
    STATES = {"ChampSelect": "champselect", **{p: "loading" for p in LeagueClient.LOADING_PHASES}}

    def __init__(self, dd: "DataDragon"):
        self.dd, self.lcu = dd, LeagueClient()
        self.last_sig = None
        self.live_seen = False
        self._last_log = None

    def _log(self, phase, lcu_data, live: bool):
        """Append to cache/watcher.log whenever what the client reports changes (no player names), to see
        what happened if a sheet didn't show up when expected."""
        players = (lcu_data or {}).get("allPlayers") or []
        line = (f"client phase={phase} | client players: "
                f"{', '.join(sorted(p['team'] + ':' + p['championName'] for p in players)) or 'none'}"
                f" | in-game API: {'up' if live else 'down'}")
        if line == self._last_log:
            return
        self._last_log = line
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            log = CACHE_DIR / "watcher.log"
            if log.exists() and log.stat().st_size > 200_000:
                log.unlink()
            with log.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass

    def poll(self) -> tuple[str, dict | None]:
        """Returns (state, data). state: "live" / "loading" / "champselect" / "idle". data is only set when
        the players changed since the last mark_built(), i.e. the sheet needs (re)building."""
        phase, lcu_data = self.lcu.snapshot(self.dd)
        live_up = False
        # Connecting to the closed port 2999 takes ~2 s on Windows, so skip it unless a game is running
        # (or the client isn't reachable, e.g. it was closed mid-game).
        data = fetch_live_game() if phase is None or phase in LeagueClient.LOADING_PHASES else None
        if data:
            state, self.live_seen, live_up = "live", True, True
        else:
            data, state = lcu_data, self.STATES.get(phase, "idle")
            if state == "loading" and self.live_seen:
                state, data = "live", None  # in-game API hiccup mid-game: keep the sheet built from it
            elif state != "loading":
                self.live_seen = False
        self._log(phase, lcu_data, live_up)
        if data and game_signature(data) == self.last_sig:
            data = None
        return state, data

    def mark_built(self, data: dict):
        self.last_sig = game_signature(data)


def player_id(p: dict) -> str:
    return p.get("riotId") or p.get("summonerName") or p.get("riotIdGameName") or ""


def game_signature(data: dict):
    def spells(p):
        return tuple((s or {}).get("displayName", "") for s in (p.get("summonerSpells") or {}).values())
    return tuple(sorted((p.get("team", ""), p.get("championName", ""), player_id(p), spells(p))
                        for p in data["allPlayers"]))


def split_teams(data: dict) -> list[tuple[str, list[dict]]]:
    players = data["allPlayers"]
    ap = data.get("activePlayer") or {}
    me = {ap.get(k) for k in ("riotId", "summonerName", "riotIdGameName")} - {None, ""}
    my_team = None
    for p in players:
        if {p.get(k) for k in ("riotId", "summonerName", "riotIdGameName")} & me:
            my_team = p.get("team")
            break

    def ordered(ps):
        return sorted(ps, key=lambda p: POS_ORDER.get(p.get("position", ""), 9))

    if my_team:
        return [("Enemy team", ordered([p for p in players if p.get("team") != my_team]))]
    # Spectating / couldn't find ourselves: show both teams.
    teams: dict[str, list] = {}
    for p in players:
        teams.setdefault(p.get("team", "?"), []).append(p)
    names = {"ORDER": "Blue team", "CHAOS": "Red team"}
    return [(names.get(t, t), ordered(ps)) for t, ps in teams.items()]


def ability_cds(spell: dict, extra: dict | None) -> tuple[str, str | None]:
    """Returns (cooldown text, recharge text or None) for a Data Dragon spell.

    Charge-based abilities have a short cast cooldown (what Data Dragon lists, e.g. Corki R = 2s)
    plus a per-charge recharge time (Corki R = 20s) that Data Dragon omits entirely.
    `extra` is the spell's DataDragon.bin_spells() entry, if any.
    """
    extra = extra or {}
    ranks = range(1, spell.get("maxrank", 5) + 1)  # CDragon arrays: index 0 = rank 0

    def per_rank(arr):
        return [arr[i] if i < len(arr) else arr[-1] for i in ranks] if arr else []

    # Data Dragon's cooldown, not the bin's raw cooldownTime (that gets overridden, e.g. Darius Q).
    base = spell.get("cooldown") or []
    if not any(base) and extra.get("cd"):  # Data Dragon says 0 (Veigar W, Kalista E)
        base = per_rank(extra["cd"])
    cd = fmt_cd(base)
    charges, recharge = per_rank(extra.get("charges")), per_rank(extra.get("recharge"))
    base = base or [0] * len(recharge)
    # Ignore internal "ammo" whose recharge is shorter than the real cooldown (LeBlanc R, Warwick W).
    if not recharge or not charges or not any(r > b for r, b in zip(recharge, base)):
        return cd, None
    if max(charges) <= 1:
        return fmt_cd(recharge), None  # single charge (Rengar): the recharge IS the cooldown
    return cd, f"{fmt_cd(recharge)} recharge ×{fmt_cd(charges)}"


def build_entry(dd: DataDragon, p: dict, want_icons: bool, quick: bool = False) -> dict:
    cid = dd.resolve_champion(p)
    entry = {
        "champion": p.get("championName", "?"),
        "position": POS_LABEL.get(p.get("position", ""), ""),
        "player": (p.get("riotIdGameName") or ("" if p.get("fromClient") else player_id(p))).split("#")[0],
        "icon": None,
        "abilities": [],
        "summoners": [],
        "error": None,
        "incomplete": False,  # quick pass skipped CommunityDragon data that isn't cached yet
    }
    if not cid:
        entry["error"] = "Champion not found in Data Dragon"
    else:
        try:
            c = dd.champion(cid)
            entry["champion"] = c["name"]
            if want_icons:
                entry["icon"] = f"img/champion/{c['image']['full']}"
            try:
                extras = dd.bin_spells(cid, fetch=not quick)
            except Exception:
                extras = None
            entry["incomplete"] = extras is None and quick
            extras = extras or {}
            for key, s in zip("QWER", c["spells"]):
                cd, recharge = ability_cds(s, extras.get(s["id"]))
                entry["abilities"].append({
                    "key": key,
                    "name": s["name"],
                    "cd": cd,
                    "recharge": recharge,
                    "range": RANGE_OVERRIDES.get(s["id"]) or fmt_range(s.get("range")),
                    "icon": f"img/spell/{s['image']['full']}" if want_icons else None,
                })
        except Exception as e:  # network hiccup etc.
            entry["error"] = f"Couldn't load champion data: {e}"

    spells = p.get("summonerSpells") or {}
    for slot in ("summonerSpellOne", "summonerSpellTwo"):
        sp = spells.get(slot)
        if not sp:
            continue
        sid = dd.resolve_summoner(sp)
        if sid:
            s = dd.summoners[sid]
            entry["summoners"].append({
                "name": s["name"],
                "cd": SUMMONER_CD_OVERRIDES.get(sid) or fmt_cd(s.get("cooldown")),
                "icon": f"img/spell/{s['image']['full']}" if want_icons else None,
            })
        else:
            entry["summoners"].append({"name": sp.get("displayName", "?"), "cd": "?", "icon": None})
    return entry


def build_view(dd: DataDragon, data: dict, want_icons: bool = True, quick: bool = False) -> dict:
    """quick=True only uses what's already on disk (plus Data Dragon champion data if missing), so the sheet
    can be shown right away; view["complete"] says whether a normal pass would add anything."""
    teams = split_teams(data)
    with ThreadPoolExecutor(max_workers=10) as pool:
        # Champion JSONs in parallel, one per player.
        groups = [(title, list(pool.map(lambda p: build_entry(dd, p, want_icons, quick), ps))) for title, ps in teams]
        complete = not any(e["incomplete"] for _, es in groups for e in es)
        if want_icons:
            # Every icon in parallel; build_entry left them as CDN-relative paths.
            holders = [x for _, es in groups for e in es for x in (e, *e["abilities"], *e["summoners"]) if x["icon"]]
            paths = list(pool.map(lambda h: (dd.cached if quick else dd.fetch_optional)(h["icon"]), holders))
            complete = complete and all(paths)
            for h, path in zip(holders, paths):
                h["icon"] = path
    return {"groups": groups, "mode": (data.get("gameData") or {}).get("gameMode", ""), "version": dd.version,
            "complete": complete}


def demo_game(champs: list[str]) -> dict:
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    second = ["Teleport", "Smite", "Ignite", "Heal", "Exhaust"]

    def spells(a, b):
        return {"summonerSpellOne": {"displayName": a}, "summonerSpellTwo": {"displayName": b}}

    players = [{"championName": "Garen", "riotId": "You#DEMO", "team": "ORDER", "position": "TOP",
                "summonerSpells": spells("Flash", "Teleport")}]
    for i, c in enumerate(champs):
        players.append({"championName": c, "riotId": f"Enemy{i + 1}#DEMO", "team": "CHAOS",
                        "position": positions[i % 5], "summonerSpells": spells("Flash", second[i % 5])})
    return {"activePlayer": {"riotId": "You#DEMO"}, "allPlayers": players, "gameData": {"gameMode": "DEMO"}}


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #
def print_view(view: dict, show_ranges: bool = True):
    out = []
    for title, entries in view["groups"]:
        out.append(f"\n===== {title.upper()} =====  (patch data {view['version']})")
        for e in entries:
            head = e["champion"] + (f" [{e['position']}]" if e["position"] else "")
            if e["player"]:
                head += f"  - {e['player']}"
            out.append("\n" + head)
            if e["summoners"]:
                out.append("   " + "   ".join(f"{s['name']} {s['cd']}s" for s in e["summoners"]))
            if e["error"]:
                out.append(f"   ! {e['error']}")
            for a in e["abilities"]:
                cd = " · ".join(x for x in (a["cd"] if a["cd"] != "—" or not a["recharge"] else "", a["recharge"]) if x)
                rng = f"   range {a['range']}" if show_ranges and a["range"] != "—" else ""
                out.append(f"   {a['key']}  {a['name'][:24]:<24} {cd}{rng}")
    # Plain ASCII so it reads right in any console code page.
    text = ("\n".join(out) + "\n").translate(str.maketrans({"—": "-", "–": "-", "·": "|", "×": "x"}))
    sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8", errors="replace"))
    sys.stdout.flush()


def run_console(args):
    dd = DataDragon()
    dd.load()
    if args.demo is not None:
        print_view(build_view(dd, demo_game(args.demo or DEFAULT_DEMO), want_icons=False), not args.no_ranges)
        return
    print(f"Data Dragon {dd.version} loaded. Waiting for champ select / a game... (Ctrl+C to quit)")
    threading.Thread(target=dd.prefetch_all, daemon=True).start()
    watcher = GameWatcher(dd)
    while True:
        state, data = watcher.poll()
        if data:
            view = build_view(dd, data, want_icons=False, quick=True)
            print(f"\n[{STATE_LABEL[state]}]")
            print_view(view, not args.no_ranges)
            if not view["complete"]:
                print(f"\n[{STATE_LABEL[state]} · with charge recharge times]")
                print_view(build_view(dd, data, want_icons=False), not args.no_ranges)
            watcher.mark_built(data)
        time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
DEFAULT_DEMO = ["Darius", "Lee Sin", "Ahri", "Jinx", "Thresh"]

BG, CARD, BORDER = "#0e141b", "#18222d", "#2a3a4a"
FG, MUTED, CD_FG, RECHARGE_FG, RANGE_FG = "#e6e1d6", "#8a96a3", "#f2c66d", "#e0a35a", "#7cc7c0"
KEY_COLORS = {"Q": "#5fb3ff", "W": "#6fdc8c", "E": "#c792ea", "R": "#ff6b6b"}
UI_FONT, MONO_FONT = "Segoe UI", "Consolas"


def run_gui(args):
    import tkinter as tk

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.q: queue.Queue = queue.Queue()
            self.photo_cache: dict = {}
            self.view = None

            root.title("LoL Cooldown Sheet")
            root.configure(bg=BG)
            self.topmost = tk.BooleanVar(value=not args.no_topmost)
            self.show_names = tk.BooleanVar(value=not args.no_names)
            self.show_ranges = tk.BooleanVar(value=not args.no_ranges)
            self.columns = tk.IntVar(value=max(1, min(5, args.columns)))
            root.attributes("-topmost", self.topmost.get())
            root.geometry("+20+20")

            bar = tk.Frame(root, bg=BG, padx=8, pady=6)
            bar.pack(fill="x")
            self.status = tk.Label(bar, text="Loading Data Dragon…", fg=MUTED, bg=BG, font=(UI_FONT, 9), anchor="w")
            self.status.pack(side="left", fill="x", expand=True)
            opts = dict(bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=FG,
                        font=(UI_FONT, 9), highlightthickness=0, bd=0)
            tk.Checkbutton(bar, text="On top", variable=self.topmost, command=self.apply_topmost, **opts).pack(side="right")
            tk.Checkbutton(bar, text="Ranges", variable=self.show_ranges, command=self.rerender, **opts).pack(side="right")
            tk.Checkbutton(bar, text="Names", variable=self.show_names, command=self.rerender, **opts).pack(side="right")
            tk.Label(bar, text="Cols", fg=MUTED, bg=BG, font=(UI_FONT, 9)).pack(side="right", padx=(8, 2))
            tk.Spinbox(bar, from_=1, to=5, width=2, textvariable=self.columns, command=self.rerender,
                       bg=CARD, fg=FG, buttonbackground=CARD, relief="flat", font=(UI_FONT, 9)).pack(side="right")

            # Scrollable body
            wrap = tk.Frame(root, bg=BG)
            wrap.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, width=460, height=120)
            sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.body = tk.Frame(self.canvas, bg=BG, padx=8, pady=4)
            self.canvas.create_window((0, 0), window=self.body, anchor="nw")
            self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

            tk.Label(self.body, text="Waiting for a game to start…\n\nThe sheet fills in automatically once you're "
                                     "loaded into the game.", fg=MUTED, bg=BG, font=(UI_FONT, 10),
                     justify="left").pack(anchor="w", pady=20)

            threading.Thread(target=self.worker, daemon=True).start()
            root.after(150, self.drain)

        # ---- background thread -------------------------------------------------
        def worker(self):
            def status(msg):
                if msg != getattr(self, "_last_status", None):
                    self._last_status = msg
                    self.q.put(("status", msg))

            try:
                dd = DataDragon()
                dd.load()
            except Exception as e:
                status(f"Couldn't load Data Dragon (internet?): {e}")
                return

            if args.demo is not None:
                self.q.put(("view", build_view(dd, demo_game(args.demo or DEFAULT_DEMO))))
                status(f"Demo mode · patch data {dd.version}")
                return

            status(f"Patch data {dd.version} · waiting for champ select / a game…")
            threading.Thread(target=dd.prefetch_all, daemon=True).start()
            watcher = GameWatcher(dd)
            shown = None  # state the sheet on screen was built in
            while True:
                state, data = watcher.poll()
                if data:
                    status(f"{STATE_LABEL[state]} · loading champion data…")
                    try:
                        # Show what's on disk right away, then fill in icons / recharge times still downloading.
                        view = build_view(dd, data, quick=True)
                        self.q.put(("view", view))
                        if not view["complete"]:
                            self.q.put(("view", build_view(dd, data)))
                    except Exception as e:
                        status(f"Error building sheet: {e}")
                        time.sleep(POLL_SECONDS)
                        continue
                    watcher.mark_built(data)
                    shown = state
                if state == "idle":
                    status("Game ended · showing last game · waiting for next game…" if shown in ("live", "loading")
                           else f"Patch data {dd.version} · waiting for champ select / a game…")
                elif state == "champselect" and shown != "champselect":
                    status("Champ select · enemy picks show up here as they lock in")
                else:
                    status(f"{STATE_LABEL[state]} · patch data {dd.version}")
                time.sleep(POLL_SECONDS)

        # ---- main thread ----------------------------------------------------------
        def drain(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "status":
                        self.status.config(text=payload)
                    elif kind == "view":
                        self.view = payload
                        self.rerender()
            except queue.Empty:
                pass
            self.root.after(150, self.drain)

        def apply_topmost(self):
            self.root.attributes("-topmost", self.topmost.get())

        def photo(self, path, zoom, sub):
            if not path:
                return None
            key = (str(path), zoom, sub)
            if key not in self.photo_cache:
                try:
                    img = tk.PhotoImage(file=str(path))
                    if zoom > 1:
                        img = img.zoom(zoom)
                    if sub > 1:
                        img = img.subsample(sub)
                    self.photo_cache[key] = img
                except tk.TclError:
                    self.photo_cache[key] = None
            return self.photo_cache[key]

        def rerender(self):
            if not self.view:
                return
            for w in self.body.winfo_children():
                w.destroy()
            try:
                cols = max(1, min(5, int(self.columns.get())))
            except (tk.TclError, ValueError):
                cols = 1
            for title, entries in self.view["groups"]:
                tk.Label(self.body, text=title.upper(), fg=MUTED, bg=BG,
                         font=(UI_FONT, 9, "bold")).pack(anchor="w", pady=(6, 2))
                grid = tk.Frame(self.body, bg=BG)
                grid.pack(anchor="w")
                for i, e in enumerate(entries):
                    self.card(grid, e).grid(row=i // cols, column=i % cols, sticky="nsew", padx=3, pady=3)
            note = ("Base cooldowns before ability haste (Data Dragon; charge recharge times from CommunityDragon). "
                    "\"recharge ×N\" = time per charge, N max charges.")
            if self.show_ranges.get():
                note += (" Ranges in game units: \"AoE N\" = radius around them, \"AA\" = empowered basic "
                         "attack, \"A–B\" = grows while charging / with level, \"/\" = per rank.")
            tk.Label(self.body, text=note,
                     wraplength=440, justify="left",
                     fg=MUTED, bg=BG, font=(UI_FONT, 8)).pack(anchor="w", pady=(4, 2))
            self.fit_window()

        def fit_window(self):
            self.body.update_idletasks()
            w = self.body.winfo_reqwidth()
            h = min(self.body.winfo_reqheight(), self.root.winfo_screenheight() - 140)
            self.canvas.configure(width=w, height=h)

        def card(self, parent, e):
            card = tk.Frame(parent, bg=CARD, padx=8, pady=4, highlightthickness=1, highlightbackground=BORDER)

            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x")
            img = self.photo(e["icon"], 1, 3)  # 120px -> 40px
            if img:
                tk.Label(head, image=img, bg=CARD).pack(side="left")
            names = tk.Frame(head, bg=CARD)
            names.pack(side="left", padx=(6, 10))
            tk.Label(names, text=e["champion"], fg=FG, bg=CARD, font=(UI_FONT, 12, "bold")).pack(anchor="w")
            sub = " · ".join(x for x in (e["position"], e["player"][:16]) if x)
            if sub:
                tk.Label(names, text=sub, fg=MUTED, bg=CARD, font=(UI_FONT, 8)).pack(anchor="w")

            summs = tk.Frame(head, bg=CARD)
            summs.pack(side="right")
            for s in e["summoners"]:
                f = tk.Frame(summs, bg=CARD)
                f.pack(side="left", padx=(6, 0))
                simg = self.photo(s["icon"], 5, 16)  # 64px -> 20px
                if simg:
                    tk.Label(f, image=simg, bg=CARD).pack(side="left")
                else:
                    tk.Label(f, text=s["name"], fg=MUTED, bg=CARD, font=(UI_FONT, 8)).pack(side="left")
                tk.Label(f, text=f"{s['cd']}s", fg=CD_FG, bg=CARD, font=(MONO_FONT, 9)).pack(side="left", padx=(2, 0))

            if e["error"]:
                tk.Label(card, text=e["error"], fg=KEY_COLORS["R"], bg=CARD, font=(UI_FONT, 9)).pack(anchor="w")

            rows = tk.Frame(card, bg=CARD)
            rows.pack(fill="x", pady=(3, 0))
            for r, a in enumerate(e["abilities"]):
                col = 0
                aimg = self.photo(a["icon"], 5, 16)  # 64px -> 20px
                if aimg:
                    tk.Label(rows, image=aimg, bg=CARD).grid(row=r, column=col, padx=(0, 4), pady=0)
                col += 1
                tk.Label(rows, text=a["key"], fg=KEY_COLORS[a["key"]], bg=CARD,
                         font=(MONO_FONT, 10, "bold")).grid(row=r, column=col, padx=(0, 6))
                col += 1
                if self.show_names.get():
                    name = a["name"] if len(a["name"]) <= 22 else a["name"][:21] + "…"
                    tk.Label(rows, text=name, fg=FG, bg=CARD, font=(UI_FONT, 9), anchor="w",
                             width=20).grid(row=r, column=col, sticky="w")
                    col += 1
                cds = tk.Frame(rows, bg=CARD)
                cds.grid(row=r, column=col, sticky="w")
                if a["cd"] != "—" or not a["recharge"]:  # Zyra W etc. have no cast cooldown at all
                    tk.Label(cds, text=a["cd"], fg=CD_FG, bg=CARD, font=(MONO_FONT, 10)).pack(side="left")
                if a["recharge"]:
                    sep = " · " if a["cd"] != "—" else ""
                    tk.Label(cds, text=sep + a["recharge"], fg=RECHARGE_FG, bg=CARD,
                             font=(MONO_FONT, 10)).pack(side="left")
                col += 1
                if self.show_ranges.get() and a["range"] != "—":
                    rng = tk.Frame(rows, bg=CARD)
                    rng.grid(row=r, column=col, sticky="w", padx=(12, 0))
                    tk.Label(rng, text="↔", fg=MUTED, bg=CARD, font=(UI_FONT, 9)).pack(side="left")
                    tk.Label(rng, text=a["range"], fg=RANGE_FG, bg=CARD,
                             font=(MONO_FONT, 10)).pack(side="left", padx=(2, 0))
            return card

    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Show enemy champion cooldowns at the start of a LoL game.")
    ap.add_argument("--console", action="store_true", help="print to the terminal instead of opening a window")
    ap.add_argument("--demo", nargs="*", metavar="CHAMPION",
                    help="preview with sample enemies instead of a live game (default: %s)" % ", ".join(DEFAULT_DEMO))
    ap.add_argument("--columns", type=int, default=1, help="cards per row in the window (1-5, default 1)")
    ap.add_argument("--no-names", action="store_true", help="hide ability names (more compact)")
    ap.add_argument("--no-ranges", action="store_true", help="hide ability cast ranges")
    ap.add_argument("--no-topmost", action="store_true", help="don't keep the window above other windows")
    args = ap.parse_args()
    try:
        (run_console if args.console else run_gui)(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
