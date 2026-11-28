#!/usr/bin/env python3
"""
LoL Cooldown Sheet
==================
Waits for a League of Legends game to start, then shows every enemy champion's
Q/W/E/R cooldowns and cast ranges (per rank) and their summoner spell cooldowns, so you
know roughly when things are back up for trading / cooldown punishing. During the game it
tracks each enemy's level and shows the cooldown they are actually on right now, based on
the most popular leveling order for that champion.

The "CD Quiz" tab is a small training game: guess an ability's cooldown, earn 100 / 50 / 25
points depending on how close you are, filtered by lane.

This is a static reference sheet, NOT an active cooldown timer.

Data sources:
  * Live Client Data API  - https://127.0.0.1:2999  (served by the game client while you're in a match)
  * Data Dragon           - https://ddragon.leagueoflegends.com  (champion + summoner spell data, icons)
  * CommunityDragon       - https://raw.communitydragon.org      (charge-based cooldowns)
  * LoL Wiki              - ability cast ranges (see WIKI_RANGES below)
  * u.gg                  - most popular skill order + primary role (see SKILL_PATH / CHAMP_ROLE)

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
import random
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
# Where the downloaded patch data lives. When the script is bundled into an .exe, keep it next to the exe
# instead of inside the temporary unpack folder.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "cache"
# Window position/size, remembered across runs. Kept out of the repo folder so it isn't committed.
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "LoLCooldownSheet"
CONFIG_FILE = CONFIG_DIR / "window.json"
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
# Ability ranges from the League of Legends wiki (its ability data templates, fetchable through the
# MediaWiki API), so cast ranges match what the wiki and the client show rather than Data Dragon's
# figures. Only ranges that resolve to one clean number (or "a–b" growth) are included.
WIKI_RANGES = {
    'AatroxE': '300',
    'AatroxQ': '650',
    'AatroxR': 'AoE 600',
    'AatroxW': '825',
    'AhriE': '1000',
    'AhriQ': '900',
    'AhriR': '500',
    'AkaliE': '825',
    'AkaliQ': '500',
    'AkaliR': '675',
    'AkaliW': '250',
    'AkshanE': '800',
    'AkshanR': '2500',
    'AlphaStrike': '600',
    'AmbessaQ': '650',
    'AmbessaR': '1250',
    'AnnieE': '800',
    'AnnieQ': '625',
    'AnnieR': '600',
    'ApheliosR': '1300',
    'AsheSpiritOfTheHawk': 'global',
    'AurelionSolQ': '750–920',
    'AurelionSolR': '1250',
    'AuroraE': '825',
    'AuroraQ': '900',
    'AuroraR': '250',
    'AzirEWrapper': '1100',
    'AzirQWrapper': '740',
    'AzirR': '400',
    'AzirW': '525',
    'BandageToss': '1100',
    'BardE': '900',
    'BardQ': '850',
    'BardR': '3400',
    'BardW': '800',
    'BelvethQ': '400',
    'BelvethR': '450',
    'BelvethW': '660',
    'BrandE': '675',
    'BrandQ': '1100',
    'BrandR': '750',
    'BrandW': '900',
    'BraumQ': '1050',
    'BraumRWrapper': '1200',
    'BraumW': '650',
    'BriarQ': '475',
    'BriarR': '12000',
    'BriarW': '300',
    'Bushwhack': '900',
    'CaitlynE': '800',
    'CaitlynQ': '1300',
    'CaitlynR': '3500',
    'CaitlynW': '800',
    'CamilleE': '800',
    'CamilleR': '475',
    'CarpetBomb': '300–600',
    'CassiopeiaE': '700',
    'CassiopeiaQ': '850',
    'CassiopeiaW': '700',
    'ChronoShift': '900',
    'Crystallize': '1000',
    'DariusCleave': 'AoE 460',
    'DariusExecute': '475',
    'Deceive': '400',
    'Destiny': '5500',
    'DianaQ': '900',
    'DianaTeleport': '825',
    'DrMundoE': '650',
    'DrMundoQ': '1050',
    'DravenDoubleShot': '1100',
    'DravenRCast': 'global',
    'EkkoE': '325',
    'EkkoQ': '1100',
    'EkkoW': '1600',
    'EliseHumanE': '1100',
    'EliseHumanQ': '575',
    'EliseHumanW': '950',
    'EnchantedCrystalArrow': 'global',
    'EvelynnE': '210',
    'EvelynnQ': '800',
    'EvelynnR': 'AoE 500',
    'EyeOfTheStorm': '800',
    'EzrealE': '475',
    'EzrealQ': '1200',
    'EzrealR': 'global',
    'EzrealW': '1200',
    'Feast': '175',
    'FiddleSticksE': '850',
    'FiddleSticksQ': '575',
    'FiddleSticksR': '800',
    'FiddleSticksW': '575',
    'FioraQ': '420',
    'FioraR': '500',
    'FioraW': '900',
    'FizzE': '400',
    'FizzQ': '550',
    'FizzR': '1300',
    'FlashFrost': '1100',
    'Fling': '125',
    'Frostbite': '600',
    'GalioE': '250–650',
    'GalioQ': '825',
    'GangplankE': '1000',
    'GangplankQWrapper': '625',
    'GangplankR': 'global',
    'GarenR': '400',
    'GlacialStorm': '750',
    'GnarE': '475',
    'GnarQ': '1125',
    'GnarW': '550',
    'GragasE': '600',
    'GragasQ': '850',
    'GragasR': '1000',
    'GragasW': 'AoE 250',
    'GravesChargeShot': '1100',
    'GravesMove': '275–375',
    'GravesQLineSpell': '800',
    'GravesSmokeGrenade': '950',
    'GwenE': '350',
    'GwenW': 'AoE 480',
    'Headbutt': '650',
    'HecarimUlt': '1510',
    'HeimerdingerE': '925',
    'HeimerdingerQ': '350',
    'HeimerdingerW': '1150',
    'HweiR': '1340',
    'IllaoiE': '950',
    'IreliaE': '775',
    'IreliaQ': '600',
    'IreliaR': '1000',
    'IreliaW': '775',
    'IvernE': '750',
    'IvernQ': '1150',
    'IvernW': '1000',
    'JackInTheBox': '500',
    'JarvanIVCataclysm': '650',
    'JarvanIVDemacianStandard': '860',
    'JarvanIVDragonStrike': '785',
    'JavelinToss': '1500',
    'JaxQ': '700',
    'JayceThunderingBlow': '240',
    'JayceToTheSkies': '600',
    'JhinE': '750',
    'JhinQ': '550',
    'JhinR': '3500',
    'JhinW': '2520',
    'JinxE': '925',
    'JinxR': 'global',
    'JinxW': '1500',
    'KSanteQ': '465',
    'KSanteR': '300',
    'KSanteW': '450',
    'KaisaQ': '600',
    'KaisaW': '3000',
    'KalistaMysticShot': '1200',
    'KalistaRx': '1200',
    'KalistaW': '1400–5000',
    'KarmaSolKimShield': '800',
    'KarmaSpiritBind': '675',
    'KarthusFallenOne': 'global',
    'KarthusLayWasteA1': '875',
    'KarthusWallOfPain': '1000',
    'KatarinaEWrapper': '725',
    'KatarinaQ': '625',
    'KayleQ': '900',
    'KayleR': '900',
    'KayleW': '900',
    'KaynQ': '350',
    'KaynR': '550',
    'KaynW': '700',
    'KennenShurikenHurlMissile1': '1050',
    'KhazixE': '700',
    'KhazixQ': '325',
    'KhazixW': '1025',
    'KindredQ': '300',
    'KindredW': '500',
    'KledE': '550',
    'KledQ': '800',
    'KogMawQ': '1200',
    'KogMawVoidOoze': '1360',
    'LeblancE': '950',
    'LeblancQ': '700',
    'LeblancR': 'global',
    'LeblancW': '600',
    'LeeSinQOne': '1200',
    'LeeSinR': '375',
    'LeeSinWOne': '700',
    'LeonaSolarFlare': '1200',
    'LeonaZenithBlade': '900',
    'LilliaE': '700',
    'LilliaW': '500',
    'LissandraE': '1025',
    'LissandraQ': '825',
    'LissandraR': '550',
    'LockeE': '375',
    'LockeQ': '950',
    'LockeR': '950',
    'LucianE': '200–425',
    'LucianQ': '1000',
    'LucianR': '1200',
    'LucianW': '900',
    'LuluE': '650',
    'LuluQ': '950',
    'LuluR': '900',
    'LuluW': '650',
    'LuxLightBinding': '1300',
    'LuxLightStrikeKugel': '1100',
    'LuxPrismaticWave': '1175',
    'LuxR': '3400',
    'MalzaharE': '650',
    'MalzaharQ': '900',
    'MalzaharR': '700',
    'MalzaharW': '150',
    'MaokaiE': '1100',
    'MaokaiQ': '600',
    'MaokaiR': '3000',
    'MaokaiW': '525',
    'MegaAdhesive': '1000',
    'MelE': '1000',
    'MelQ': '950',
    'MelR': 'global',
    'MilioE': '650',
    'MilioQ': '1200',
    'MilioW': '650',
    'MissFortuneBulletTime': 'AoE 1450',
    'MissFortuneScattershot': '1000',
    'MissileBarrage': '1300',
    'MonkeyKingDecoy': '100–300',
    'MonkeyKingNimbus': '650',
    'MordekaiserE': '700',
    'MordekaiserQ': '625',
    'MordekaiserR': '650',
    'MordekaiserW': '1000',
    'MorganaE': '800',
    'MorganaQ': '1300',
    'MorganaW': '900',
    'NaafiriE': '250–450',
    'NaafiriQ': '900',
    'NaafiriW': '900',
    'NamiE': '800',
    'NamiQ': '850',
    'NamiR': '2750',
    'NamiW': '725',
    'NasusE': '650',
    'NasusW': '700',
    'NautilusAnchorDrag': '1122',
    'NautilusGrandLine': '825',
    'NeekoE': '1000',
    'NeekoQ': '800',
    'NeekoW': '900',
    'NilahE': '450',
    'NilahQ': '600',
    'NocturneDuskbringer': '1200',
    'NocturneUnspeakableHorror': '425',
    'NullLance': '650',
    'NunuE': '700',
    'NunuQ': '125',
    'OlafRecklessStrike': '325',
    'OrianaIzunaCommand': '825',
    'OrianaRedactCommand': '1120',
    'OrnnE': '800',
    'OrnnQ': '750',
    'OrnnW': '500',
    'PantheonQ': '600',
    'PantheonR': '5500',
    'PantheonW': '600',
    'PhosphorusBomb': '825',
    'PoisonTrail': 'AoE 180',
    'PoppyE': '475',
    'PoppyQ': '460',
    'PoppyR': '500',
    'PrimalSurge': '900',
    'PuncturingTaunt': '325',
    'PykeE': '550',
    'PykeR': '750',
    'QiyanaE': '650',
    'QiyanaQ': '865',
    'QiyanaR': '875',
    'QiyanaW': '1100',
    'QuinnE': '600',
    'QuinnQ': '1050',
    'RakanE': '700',
    'RakanQ': '900',
    'RakanW': '650',
    'RekSaiE': '225',
    'RekSaiR': '1500',
    'RellE': '1200',
    'RellQ': '520',
    'RellW_Dismount': '100–400',
    'RenataE': '800',
    'RenataQ': '900',
    'RenataR': '2000',
    'RenataW': '800',
    'RenektonReignOfTheTyrant': 'AoE 375',
    'RenektonSliceAndDice': '450',
    'RengarE': '1000',
    'RiftWalk': '500',
    'RivenFeint': '250',
    'RocketGrab': '1115',
    'RumbleCarpetBomb': '1700',
    'RumbleFlameThrower': '600',
    'RumbleGrenade': '950',
    'Rupture': '950',
    'RyzeE': '550',
    'RyzeQWrapper': '1000',
    'RyzeW': '550',
    'SamiraE': '650',
    'SamiraQ': '950',
    'SeismicShard': '625',
    'SejuaniE': '600',
    'SejuaniQ': '650',
    'SejuaniR': '1300',
    'SejuaniW': '600',
    'SennaR': 'global',
    'SennaW': '1300',
    'SeraphineE': '1300',
    'SeraphineQ': '900',
    'SeraphineR': '1300',
    'SettE': '450',
    'SettR': '400',
    'ShenE': '300–600',
    'ShenR': 'global',
    'ShyvanaE': '800',
    'ShyvanaQ': 'AoE 275',
    'ShyvanaR': '400–1050',
    'SionE': '800',
    'SivirQ': '1250',
    'SivirW': 'AoE 500',
    'SkarnerE': '1000',
    'SmolderE': '700',
    'SmolderQ': '500',
    'SmolderR': '4250',
    'SmolderW': '1500',
    'SonaR': '1000',
    'SorakaE': '925',
    'SorakaQ': '800',
    'SorakaR': 'global',
    'SorakaW': '550',
    'SowTheWind': '550',
    'SwainE': '850',
    'SwainQ': '700',
    'SylasE': '400',
    'SylasR': '950',
    'SylasW': '400',
    'SyndraE': '700',
    'SyndraQ': '800',
    'SyndraR': '675',
    'SyndraW': '925',
    'TahmKenchQ': '900',
    'TahmKenchRWrapper': '250',
    'TaliyahQ': '1000',
    'TaliyahWVC': '900',
    'TalonE': '725',
    'TalonQ': '575',
    'TalonW': '900',
    'TaricE': '575',
    'TaricW': '800',
    'TeemoQ': '680',
    'ThreshQ': '1100',
    'ThreshW': '950',
    'TimeWarp': '550',
    'Tremors2': 'AoE 400',
    'TristanaE': '550–700',
    'TristanaR': '550–700',
    'TristanaW': '900',
    'TrundleCircle': '1000',
    'TrundlePain': '650',
    'TryndamereE': '660',
    'TwitchVenomCask': '950',
    'TwoShivPoison': '625',
    'UFSlash': '1000',
    'UrgotE': '475',
    'UrgotQ': '800',
    'UrgotR': '2500',
    'VarusE': '925',
    'VarusR': '1370',
    'VayneCondemn': '550',
    'VayneTumble': '300',
    'VeigarBalefulStrike': '1050',
    'VeigarDarkMatter': '950',
    'VeigarEventHorizon': '725',
    'VeigarR': '650',
    'VelkozQ': '1100',
    'VelkozR': '1555',
    'VelkozW': '1105',
    'VexE': '800',
    'VexQ': '1200',
    'ViR': '800',
    'ViegoE': '775',
    'ViegoR': '500',
    'ViktorE': '500',
    'ViktorQ': '600',
    'ViktorR': '700',
    'ViktorW': '800',
    'VladimirHemoplague': '625',
    'VladimirQ': '600',
    'VolibearE': '1200',
    'VolibearR': '700',
    'VolibearW': '325',
    'VorpalSpikes': '650',
    'WarwickQ': '365',
    'WildCards': '1450',
    'XerathArcaneBarrage2': '1000',
    'XerathLocusOfPower2': '5000',
    'XerathMageSpear': '1125',
    'XinZhaoE': '650',
    'XinZhaoW': '1000',
    'YasuoE': '475',
    'YasuoQ1Wrapper': '450',
    'YasuoR': '1400',
    'YasuoW': '350',
    'YoneE': '300',
    'YoneQ': '450',
    'YoneR': '1000',
    'YorickE': '700',
    'YorickQ': '1000',
    'YorickR': '600',
    'YorickW': '600',
    'YunaraE': 'AoE 2000',
    'YunaraQ': 'AoE 300',
    'YunaraW': '1150',
    'YuumiQ': '850',
    'YuumiR': '1100',
    'YuumiW': '700',
    'ZaahenE': '350',
    'ZaahenR': '600',
    'ZaahenW': '850',
    'ZacQ': '800',
    'ZacW': '350',
    'ZedQ': '925',
    'ZedR': '625',
    'ZedW': '325–650',
    'ZeriE': '300',
    'ZeriW': '1200',
    'ZiggsE': '900',
    'ZiggsQ': '1400',
    'ZiggsR': '5000',
    'ZiggsW': '1000',
    'ZileanQ': '900',
    'ZoeQ': '800',
    'ZoeR': '575',
    'ZyraE': '1100',
    'ZyraQ': '800',
    'ZyraR': '700',
    'ZyraW': '850',
    'trundledesecrate': '750',
}
POS_LABEL = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "Bot", "UTILITY": "Support"}

# Most popular ability leveling order (18 levels, e.g. "WQEQEQQRQWQWRWWEE") and primary role, from u.gg.
# Filled in below the range table; used to work out how many points an enemy has in each ability at their
# current level, so the sheet can show the cooldown they are actually on right now.
SKILL_PATH: dict[str, str] = {
    'Aatrox': 'QEWQQRQEQEREEWWRWW',
    'Ahri': 'WQEQQRQWQWRWWEEREE',
    'Akali': 'QWEQQRQEQEREEWWRWW',
    'Akshan': 'EQQWQRQEQEREEWWRWW',
    'Alistar': 'WQEQQRQWQWRWWEEREE',
    'Ambessa': 'QWEQQRQEQEREEWWRWW',
    'Amumu': 'WEQEEREQEQRQQWWRWW',
    'Anivia': 'QEEWEREQEQRQQWWRWW',
    'Annie': 'QWEQQRQWQWRWWEEREE',
    'Aphelios': 'WQQQQREQEQEREEEWWR',
    'Ashe': 'WQEWWRWQWQRQQEEREE',
    'Aurelion Sol': 'QEWQQRQWQWRWWEEREE',
    'Aurora': 'QEWQQRQEQEREEWWRWW',
    'Azir': 'WQEWWRWQWQRQQEEREE',
    'Bard': 'QWEQQRQWQWRWWEEREE',
    "Bel'Veth": 'QEWQQRQEQEEREWWWRW',
    'Blitzcrank': 'QEWQQRQEQEREEWWRWW',
    'Brand': 'WQEWWRWQWQRQQEEREE',
    'Braum': 'QEWQQRQEQEREEWWRWW',
    'Briar': 'WEQWWRWQWQRQQEEREE',
    'Caitlyn': 'QWEQQRQWQWRWWEEREE',
    'Camille': 'WEQQQRQEQEREEWWRWW',
    'Cassiopeia': 'EQEWEREQEQRQQWWRWW',
    "Cho'Gath": 'EQEWEREQEQRQQWWRWW',
    'Corki': 'EQWQQRQEQEEREWWWRW',
    'Darius': 'QWEQQRQEQEREEWWRWW',
    'Diana': 'QEWQQRQWQWRWWEEREE',
    'Dr. Mundo': 'QEWQQREEEERQQWWRWW',
    'Draven': 'QWEQQRQWQWRWWEEREE',
    'Ekko': 'QEWQQRQEQEREEWWRWW',
    'Elise': 'WQEQQRQWQWRWWEEREE',
    'Evelynn': 'QEQWQRQEQEREEWWRWW',
    'Ezreal': 'QEWQQRQEQEREEWWRWW',
    'Fiddlesticks': 'WEWQWRWQWQRQQEEREE',
    'Fiora': 'QWEQQRQEQEREEWWRWW',
    'Fizz': 'EWQEEREWEWRWWQQRQQ',
    'Galio': 'QWEQQRQWQWRWWEEREE',
    'Gangplank': 'EQQWQRQEQEREEWWRWW',
    'Garen': 'EQWEEREQEQRQQWWRWW',
    'Gnar': 'QEWQQRQWQWRWWEEREE',
    'Gragas': 'EWQQQRQEQEREEWWRWW',
    'Graves': 'EQQWQRQEQEREEWWRWW',
    'Gwen': 'QEWQQRQEQEREEWWRWW',
    'Hecarim': 'QWQEQRQWQWRWWEEREE',
    'Heimerdinger': 'QEWQQRQWQWWRWEEERE',
    'Hwei': 'EQWQQRQEQEREEWWRWW',
    'Illaoi': 'WEQEEREQEQRQQWWRWW',
    'Irelia': 'QEWQQRQWQWWRWEEERE',
    'Ivern': 'QEWEEREQEQRQQWWRWW',
    'Janna': 'WQEWWRWEWEREEQQRQQ',
    'Jarvan IV': 'EQWQQRQEQEREEWWRWW',
    'Jax': 'EQWWWRWEWEREEQQRQQ',
    'Jayce': 'QEWQQWQWQWQWWEEEEE',
    'Jhin': 'QWQEQRQWQWRWWEEREE',
    'Jinx': 'QWEQQRQWQWRWWEEREE',
    "K'Sante": 'EQWQQRQEQEREEWWRWW',
    "Kai'Sa": 'QWEQQRQEQEREEWWRWW',
    'Kalista': 'EQWEEREQEQRQQWWRWW',
    'Karma': 'QEWQQRQEQEREEWWRWW',
    'Karthus': 'QEQWQRQEQEREEWWRWW',
    'Kassadin': 'QWEQQRQEQEREEWWRWW',
    'Katarina': 'QEWQQRQEQEREEWWRWW',
    'Kayle': 'EQWQQRQEQEREEWWRWW',
    'Kayn': 'QEWQQRQWQWRWWEEREE',
    'Kennen': 'EWQQQRQWQWRWWEEREE',
    "Kha'Zix": 'QWEQQRQWQWWRWEEERE',
    'Kindred': 'WQEQQRQWQWRWWEEREE',
    'Kled': 'QWEQQRQWQWRWWEEREE',
    "Kog'Maw": 'WQEWWRWQWQRQQEEREE',
    'LeBlanc': 'WQEWWRWQWQRQQEEREE',
    'Lee Sin': 'EQWQQRQWQWRWWEEREE',
    'Leona': 'QEWWWRWEWEREEQQRQQ',
    'Lillia': 'QWEQQRQWQWRWWEEREE',
    'Lissandra': 'QWEQQRQWQWRWWEEREE',
    'Locke': 'QEWQQRQWQWRWWEEREE',
    'Lucian': 'QEWQQRQEQEREEWWRWW',
    'Lulu': 'EQWEEREWEWRWWQQRQQ',
    'Lux': 'EQWEEREQEQRQQWWRWW',
    'Malphite': 'QWEQQRQEQEREEWWRWW',
    'Malzahar': 'EWQEEREQEQRQQWWRWW',
    'Maokai': 'WQEQQRQWQWRWWEEREE',
    'Master Yi': 'QEWQQRQEQEREEWWRWW',
    'Mel': 'QEWQQRQEQEREEWWRWW',
    'Milio': 'EWQEEREWEWRWWQQRQQ',
    'Miss Fortune': 'QWEQQRQWQWRWWEEREE',
    'Mordekaiser': 'QEWQQRQEQEREEWWRWW',
    'Morgana': 'QWEQQRQWQWRWWEEREE',
    'Naafiri': 'QEWQQRQEQEREEWWRWW',
    'Nami': 'WEQWWRWEWEREEQQRQQ',
    'Nasus': 'QEWQQRQWQWRWWEEREE',
    'Nautilus': 'QWEQQRQWQWRWWEEREE',
    'Neeko': 'QEWQQRQEQEREEWWRWW',
    'Nidalee': 'EQWQQRQEQEREEWWRWW',
    'Nilah': 'QEWEEREQEQRQQWWRWW',
    'Nocturne': 'QWQEQRQEQEREEWWRWW',
    'Olaf': 'QEWQQRQEQEREEWWRWW',
    'Orianna': 'QWWEQRQQQWRWWEEREE',
    'Ornn': 'QWEWWRWEWEREEQQRQQ',
    'Pantheon': 'WQEQQRQWQWRWWEEREE',
    'Poppy': 'EQWQQRQEQEREEWWRWW',
    'Pyke': 'QEWQQRQEQEREEWWRWW',
    'Qiyana': 'QWQEQRQWQWWRWEEERE',
    'Quinn': 'EQWWWRWQWQRQQEEREE',
    'Rakan': 'WQEWWRWEWEREEQQRQQ',
    'Rammus': 'WQEQQRQEQEREEWWRWW',
    "Rek'Sai": 'QWEQQRQEQEREEWWRWW',
    'Rell': 'WQEWWRWEWEREEQQRQQ',
    'Renekton': 'QWEQQRQEQEREEWWRWW',
    'Rengar': 'QWEQQRQEQEREEWWRWW',
    'Riven': 'QWEQQRQEQEREEWWRWW',
    'Rumble': 'EQWQQRQEQEREEWWRWW',
    'Ryze': 'EQWQQRQEQEREEWWRWW',
    'Samira': 'QEWQQRQEQEREEWWRWW',
    'Sejuani': 'EWQWWRWQWQRQQEEREE',
    'Senna': 'QWEQQRQWQWRWWEEREE',
    'Seraphine': 'QEWQQRQWQWRWWEEREE',
    'Sett': 'WEQQQRQWQWRWWEEREE',
    'Shaco': 'WQEWWRWEWEREEQQRQQ',
    'Shen': 'QEWQQRQEQEREEWWRWW',
    'Shyvana': 'QEWQQRQWQWRWWEEREE',
    'Singed': 'QEQWQRQEQEREEWWRWW',
    'Sion': 'WQEQQRQWQWRWWEEREE',
    'Sivir': 'WQEQQRQWQWWRWEEERE',
    'Skarner': 'QWEQQRQWQWRWWEEREE',
    'Smolder': 'QWEQQRQWQWRWWEEREE',
    'Sona': 'QWEWWRWQWQRQQEEREE',
    'Soraka': 'QWEWWRWQWQRQQEEREE',
    'Swain': 'EWQQQRQWQWRWWEEREE',
    'Sylas': 'EWQWWRWEWEREEQQRQQ',
    'Syndra': 'QEWQQRQWQWRWWEEREE',
    'Tahm Kench': 'QWEQQRQWQWRWWEEREE',
    'Taliyah': 'QEWQQRQEQEREEWWRWW',
    'Talon': 'WQEWWRWQWQRQQEEREE',
    'Taric': 'EQWQEREEEQRQQWWRWW',
    'Teemo': 'EQWEEREQEQRQQWWRWW',
    'Thresh': 'QEWQQRQEQEREEWWRWW',
    'Tristana': 'EWQEEREQEQQRQWWWRW',
    'Trundle': 'QWEQQRQWQWRWWEEREE',
    'Tryndamere': 'EQQWQRQEQEEEWWWWRR',
    'Twisted Fate': 'WEQQQRQWQWRWWEEREE',
    'Twitch': 'QEWEEREQEQRQQWWRWW',
    'Udyr': 'QRWEQQQEQEQEEEWWWW',
    'Urgot': 'EWQQWRWWWQRQQEEREE',
    'Varus': 'EWQQQRQWQWRWWEEREE',
    'Vayne': 'QWEQQRQWQWRWWEEREE',
    'Veigar': 'QWEQQRQWQWRWWEEREE',
    "Vel'Koz": 'QEWQQRQWQWRWWEEREE',
    'Vex': 'EQWQQRQWQWRWWEEREE',
    'Vi': 'WEQQQRQEQEREEWWRWW',
    'Viego': 'QWQEQRQEQEREEWWRWW',
    'Viktor': 'QEEWEREQEQRQQWWRWW',
    'Vladimir': 'QWEQQRQEQEREEWWRWW',
    'Volibear': 'EQWWWRWQWQRQQEEREE',
    'Warwick': 'QWEWWRWQWQRQQEEREE',
    'Wukong': 'EQWQQRQEQEREEWWRWW',
    'Xayah': 'QEWEEREWEWRWWQQRQQ',
    'Xerath': 'QWEQQRQWQWRWWEEREE',
    'Xin Zhao': 'EWQWWRWEWEREEQQRQQ',
    'Yasuo': 'QEWQQRQEQEREEWWRWW',
    'Yone': 'QWEQQRQEQEREEWWRWW',
    'Yorick': 'QEWQQRQEQEREEWWRWW',
    'Yunara': 'QWEQQRQWQWRWWEEREE',
    'Yuumi': 'QEQEQRQEQERQEWWRWW',
    'Zaahen': 'QEWQQRQEQEREEWWRWW',
    'Zac': 'WQEEEREWEWRWWQQRQQ',
    'Zed': 'QWEQQRQEQEEREWWWRW',
    'Zeri': 'QEWQQRQEQEREEWWRWW',
    'Ziggs': 'QEWQQRQEQEREEWWRWW',
    'Zilean': 'QWEQQRQEQEREEWWRWW',
    'Zoe': 'QEWQQRQEQEREEWWRWW',
    'Zyra': 'EWQEEREQEQRQQWWRWW',
}
CHAMP_ROLE: dict[str, str] = {
    'Aatrox': 'top', 'Ahri': 'mid', 'Akali': 'mid', 'Akshan': 'mid',
    'Alistar': 'support', 'Ambessa': 'top', 'Amumu': 'jungle', 'Anivia': 'mid',
    'Annie': 'mid', 'Aphelios': 'bot', 'Ashe': 'bot', 'Aurelion Sol': 'mid',
    'Aurora': 'mid', 'Azir': 'mid', 'Bard': 'support', "Bel'Veth": 'jungle',
    'Blitzcrank': 'support', 'Brand': 'support', 'Braum': 'support', 'Briar': 'jungle',
    'Caitlyn': 'bot', 'Camille': 'top', 'Cassiopeia': 'mid', "Cho'Gath": 'jungle',
    'Corki': 'bot', 'Darius': 'top', 'Diana': 'mid', 'Dr. Mundo': 'top',
    'Draven': 'bot', 'Ekko': 'mid', 'Elise': 'jungle', 'Evelynn': 'jungle',
    'Ezreal': 'bot', 'Fiddlesticks': 'jungle', 'Fiora': 'top', 'Fizz': 'mid',
    'Galio': 'mid', 'Gangplank': 'top', 'Garen': 'top', 'Gnar': 'top',
    'Gragas': 'top', 'Graves': 'jungle', 'Gwen': 'top', 'Hecarim': 'jungle',
    'Heimerdinger': 'top', 'Hwei': 'mid', 'Illaoi': 'top', 'Irelia': 'top',
    'Ivern': 'jungle', 'Janna': 'support', 'Jarvan IV': 'jungle', 'Jax': 'top',
    'Jayce': 'top', 'Jhin': 'bot', 'Jinx': 'bot', "K'Sante": 'top',
    "Kai'Sa": 'bot', 'Kalista': 'bot', 'Karma': 'support', 'Karthus': 'jungle',
    'Kassadin': 'mid', 'Katarina': 'mid', 'Kayle': 'top', 'Kayn': 'jungle',
    'Kennen': 'top', "Kha'Zix": 'jungle', 'Kindred': 'jungle', 'Kled': 'top',
    "Kog'Maw": 'bot', 'LeBlanc': 'mid', 'Lee Sin': 'jungle', 'Leona': 'support',
    'Lillia': 'jungle', 'Lissandra': 'mid', 'Locke': 'mid', 'Lucian': 'bot',
    'Lulu': 'support', 'Lux': 'support', 'Malphite': 'top', 'Malzahar': 'mid',
    'Maokai': 'support', 'Master Yi': 'jungle', 'Mel': 'mid', 'Milio': 'support',
    'Miss Fortune': 'bot', 'Mordekaiser': 'top', 'Morgana': 'support', 'Naafiri': 'jungle',
    'Nami': 'support', 'Nasus': 'top', 'Nautilus': 'support', 'Neeko': 'support',
    'Nidalee': 'jungle', 'Nilah': 'bot', 'Nocturne': 'jungle', 'Olaf': 'top',
    'Orianna': 'mid', 'Ornn': 'top', 'Pantheon': 'support', 'Poppy': 'support',
    'Pyke': 'support', 'Qiyana': 'jungle', 'Quinn': 'top', 'Rakan': 'support',
    'Rammus': 'jungle', "Rek'Sai": 'jungle', 'Rell': 'support', 'Renekton': 'top',
    'Rengar': 'jungle', 'Riven': 'top', 'Rumble': 'top', 'Ryze': 'mid',
    'Samira': 'bot', 'Sejuani': 'jungle', 'Senna': 'support', 'Seraphine': 'support',
    'Sett': 'top', 'Shaco': 'jungle', 'Shen': 'top', 'Shyvana': 'jungle',
    'Singed': 'top', 'Sion': 'top', 'Sivir': 'bot', 'Skarner': 'jungle',
    'Smolder': 'bot', 'Sona': 'support', 'Soraka': 'support', 'Swain': 'support',
    'Sylas': 'mid', 'Syndra': 'mid', 'Tahm Kench': 'support', 'Taliyah': 'jungle',
    'Talon': 'jungle', 'Taric': 'support', 'Teemo': 'top', 'Thresh': 'support',
    'Tristana': 'bot', 'Trundle': 'top', 'Tryndamere': 'top', 'Twisted Fate': 'mid',
    'Twitch': 'bot', 'Udyr': 'jungle', 'Urgot': 'top', 'Varus': 'bot',
    'Vayne': 'bot', 'Veigar': 'mid', "Vel'Koz": 'support', 'Vex': 'mid',
    'Vi': 'jungle', 'Viego': 'jungle', 'Viktor': 'mid', 'Vladimir': 'mid',
    'Volibear': 'top', 'Warwick': 'jungle', 'Wukong': 'jungle', 'Xayah': 'bot',
    'Xerath': 'mid', 'Xin Zhao': 'jungle', 'Yasuo': 'mid', 'Yone': 'mid',
    'Yorick': 'top', 'Yunara': 'bot', 'Yuumi': 'support', 'Zaahen': 'top',
    'Zac': 'jungle', 'Zed': 'mid', 'Zeri': 'bot', 'Ziggs': 'bot',
    'Zilean': 'support', 'Zoe': 'mid', 'Zyra': 'support',
}


def skill_rank(champion: str, key: str, level: int | None) -> int:
    """Points in `key` at `level`, following the popular leveling order (falls back to R at 6/11/16)."""
    if not level or level < 1:
        return 1
    level = min(level, 18)
    path = SKILL_PATH.get(champion)
    if path and len(path) == 18:
        return max(1, path[:level].count(key))
    order = []
    for lv in range(1, 19):
        if lv in (6, 11, 16) and order.count("R") < 3:
            order.append("R")
            continue
        for k in "QWE":
            if order.count(k) < 5:
                order.append(k)
                break
    return max(1, order[:level].count(key))

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


def load_window_state() -> dict | None:
    """Window geometry saved by the last run, or None if there is nothing usable."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    geo = data.get("geometry") if isinstance(data, dict) else None
    if not (isinstance(geo, list) and len(geo) == 4 and all(isinstance(v, int) for v in geo)):
        return None
    return data


def save_window_state(state: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError:
        pass


def monitor_bounds(root) -> tuple[int, int, int, int] | None:
    """Work area of the monitor the window is currently on, in the same coordinates Tk uses.
    None when this isn't Windows / the query fails (the caller then falls back to the whole desktop)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        hwnd = int(root.wm_frame(), 16)
        mon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        # Win32 reports physical pixels; Tk geometry is in DPI-scaled units.
        scale = (user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96) / 96 if hasattr(user32, "GetDpiForWindow") else 1
        r = info.rcWork
        return (round(r.left / scale), round(r.top / scale),
                round((r.right - r.left) / scale), round((r.bottom - r.top) / scale))
    except Exception:
        return None


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
    return tuple(sorted((p.get("team", ""), p.get("championName", ""), player_id(p), spells(p), p.get("level"))
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
        "level": p.get("level"),
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
                rank = skill_rank(c["name"], key, entry["level"])
                # "now" uses the same corrected numbers the sheet shows, not Data Dragon's raw field
                # (Rengar's raw cooldowns are all 0.25s placeholders).
                vals = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", cd)]
                now = vals[min(rank - 1, len(vals) - 1)] if vals else None
                entry["abilities"].append({
                    "key": key,
                    "name": s["name"],
                    "cd": cd,
                    "recharge": recharge,
                    "rank": rank,
                    "maxrank": s.get("maxrank") or 5,
                    "now": now,
                    "range": WIKI_RANGES.get(s["id"]) or RANGE_OVERRIDES.get(s["id"]) or fmt_range(s.get("range")),
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
                "level": 11, "summonerSpells": spells("Flash", "Teleport")}]
    for i, c in enumerate(champs):
        players.append({"championName": c, "riotId": f"Enemy{i + 1}#DEMO", "team": "CHAOS",
                        "position": positions[i % 5], "level": 11,
                        "summonerSpells": spells("Flash", second[i % 5])})
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
NOW_FG, MAX_FG = "#ffd166", "#7ee787"
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
            self._render_key = None
            self._render_geo = None
            self._card_refs = []
            self._grids = []
            self._render_cols = 0
            self._card_w = 0
            self._reflow_job = None
            self._resize_job = None
            self._pending_width = 0
            self._note = None
            self._wheel_bound = False

            root.title("LoL Cooldown Sheet")
            root.configure(bg=BG)
            self.topmost = tk.BooleanVar(value=not args.no_topmost)
            self.show_names = tk.BooleanVar(value=not args.no_names)
            self.show_ranges = tk.BooleanVar(value=not args.no_ranges)
            cols_arg = str(args.columns).strip()
            self.columns = tk.StringVar(value=cols_arg if cols_arg in ("1", "2", "3", "4", "5") else "Auto")
            root.attributes("-topmost", self.topmost.get())
            # Remember where the window was last time (and its size). If the saved spot is no longer on
            # any monitor (unplugged display, resolution change), fall back to the top-left corner.
            saved = load_window_state() or {}
            self._restored = bool(saved)
            geo_x, geo_y = 20, 20
            geo_w, geo_h = min(560, root.winfo_screenwidth() - 40), 420
            if saved:
                geo_w, geo_h, geo_x, geo_y = saved["geometry"]
                vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
                vw, vh = root.winfo_vrootwidth(), root.winfo_vrootheight()
                if not (vx <= geo_x < vx + vw and vy <= geo_y < vy + vh):
                    geo_x, geo_y = 20, 20
            self._saved_geo = (geo_w, geo_h, geo_x, geo_y)
            root.geometry(f"{geo_w}x{geo_h}+{geo_x}+{geo_y}")
            self._save_job = None
            root.bind("<Configure>", self._remember_geometry, add="+")
            root.protocol("WM_DELETE_WINDOW", self.on_close)

            bar = tk.Frame(root, bg=BG, padx=8, pady=6)
            bar.pack(fill="x")
            # Tabs: the enemy sheet, and a cooldown guessing game.
            self.mode = "sheet"
            self.tab_sheet = tk.Label(bar, text="Sheet", fg=FG, bg=CARD, padx=9, pady=1,
                                      font=(UI_FONT, 9, "bold"), cursor="hand2")
            self.tab_quiz = tk.Label(bar, text="CD Quiz", fg=MUTED, bg=BG, padx=9, pady=1,
                                     font=(UI_FONT, 9), cursor="hand2")
            self.tab_sheet.pack(side="left", padx=(0, 4))
            self.tab_quiz.pack(side="left", padx=(0, 10))
            self.tab_sheet.bind("<Button-1>", lambda _e: self.show_mode("sheet"))
            self.tab_quiz.bind("<Button-1>", lambda _e: self.show_mode("quiz"))
            self.status = tk.Label(bar, text="Loading Data Dragon…", fg=MUTED, bg=BG, font=(UI_FONT, 9), anchor="w")
            self.status.pack(side="left", fill="x", expand=True)
            opts = dict(bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=FG,
                        font=(UI_FONT, 9), highlightthickness=0, bd=0)
            tk.Checkbutton(bar, text="On top", variable=self.topmost, command=self.apply_topmost, **opts).pack(side="right")
            # Sheet-only options (hidden on the quiz tab, which doesn't use them).
            self.sheet_opts = tk.Frame(bar, bg=BG)
            self.sheet_opts.pack(side="right")
            tk.Checkbutton(self.sheet_opts, text="Ranges", variable=self.show_ranges,
                           command=lambda: self.rerender(refit=True), **opts).pack(side="right")
            tk.Checkbutton(self.sheet_opts, text="Names", variable=self.show_names,
                           command=lambda: self.rerender(refit=True), **opts).pack(side="right")
            tk.Label(self.sheet_opts, text="Cols", fg=MUTED, bg=BG, font=(UI_FONT, 9)).pack(side="right", padx=(8, 2))
            tk.Spinbox(self.sheet_opts, values=("Auto", "1", "2", "3", "4", "5"), width=4,
                       textvariable=self.columns, command=lambda: self.rerender(refit=True),
                       bg=CARD, fg=FG, buttonbackground=CARD, relief="flat",
                       font=(UI_FONT, 9)).pack(side="right")

            # Scrollable body
            wrap = tk.Frame(root, bg=BG)
            wrap.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
            self.sb = sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.body = tk.Frame(self.canvas, bg=BG, padx=8, pady=4)
            self._body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
            # Body inside the canvas: keep it exactly as wide as the canvas so it reflows with the window.
            self.canvas.bind("<Configure>", self._on_canvas_resize)
            self.body.bind("<Configure>", self._on_body_resize)
            # Scroll the sheet only while the pointer is over it (not over another app).
            root.bind("<Enter>", self._bind_wheel)
            root.bind("<Leave>", self._unbind_wheel)

            tk.Label(self.body, text="Waiting for a game to start…\n\nThe sheet fills in automatically once you're "
                                     "loaded into the game.", fg=MUTED, bg=BG, font=(UI_FONT, 10),
                     justify="left").pack(anchor="w", pady=20)

            # ---- CD quiz tab ----------------------------------------------------
            self.quiz = tk.Frame(wrap, bg=BG, padx=12, pady=10)
            self.quiz_score = 0
            self.quiz_count = 0
            self.quiz_current = None
            self.quiz_done = False
            self.quiz_pool: list = []
            self._sheet_geo = None
            self.lanes = {k: tk.BooleanVar(value=True)
                          for k in ("top", "jungle", "mid", "bot", "support")}
            self._build_quiz_ui()

            threading.Thread(target=self.worker, daemon=True).start()
            root.after(150, self.drain)

        # ---- tabs ---------------------------------------------------------------
        def show_mode(self, mode: str):
            if mode == self.mode:
                return
            self.mode = mode
            if mode == "quiz":
                self.quiz.place(x=0, y=0, relwidth=1, relheight=1)
                self.quiz.lift()
                self.sheet_opts.pack_forget()
                self.tab_quiz.config(fg=FG, bg=CARD, font=(UI_FONT, 9, "bold"))
                self.tab_sheet.config(fg=MUTED, bg=BG, font=(UI_FONT, 9))
                # Remember the sheet's size, then shrink to what the quiz actually needs.
                self._sheet_geo = (self.root.winfo_width(), self.root.winfo_height())
                self.build_quiz_pool()
                self.new_question()
                self.root.after_idle(lambda: self.fit_window(force=True, content=self.quiz))
            else:
                self.quiz.place_forget()
                self.sheet_opts.pack(side="right")
                self.tab_sheet.config(fg=FG, bg=CARD, font=(UI_FONT, 9, "bold"))
                self.tab_quiz.config(fg=MUTED, bg=BG, font=(UI_FONT, 9))
                self._restore_sheet_geo()

        def _restore_sheet_geo(self):
            """Put the window back to the size the sheet had before the quiz tab shrank it."""
            if not self._sheet_geo:
                return
            w, h = self._sheet_geo
            x, y = self.root.winfo_x(), self.root.winfo_y()
            mon = monitor_bounds(self.root)
            if mon:
                mx, my, mw, mh = mon
                w, h = min(w, mw), min(h, mh)
                x = max(mx, min(x, mx + mw - w))
                y = max(my, min(y, my + mh - h))
            self._render_geo = (w, h)
            self.root.geometry(f"{w}x{h}+{x}+{y}")

        # ---- quiz ---------------------------------------------------------------
        def _build_quiz_ui(self):
            tk.Label(self.quiz, text="Guess the cooldown", fg=FG, bg=BG,
                     font=(UI_FONT, 13, "bold")).pack(anchor="w")
            self.quiz_score_label = tk.Label(self.quiz, text="Score 0  ·  0 answered", fg=MUTED, bg=BG,
                                             font=(UI_FONT, 9))
            self.quiz_score_label.pack(anchor="w", pady=(2, 8))

            lanes = tk.Frame(self.quiz, bg=BG)
            lanes.pack(anchor="w", pady=(0, 10))
            tk.Label(lanes, text="Lanes:", fg=MUTED, bg=BG, font=(UI_FONT, 9)).pack(side="left", padx=(0, 6))
            opts = dict(bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=FG,
                        font=(UI_FONT, 9), highlightthickness=0, bd=0)
            for key, label in (("top", "Top"), ("jungle", "Jungle"), ("mid", "Mid"), ("bot", "Bot"),
                               ("support", "Support")):
                tk.Checkbutton(lanes, text=label, variable=self.lanes[key],
                               command=self._lanes_changed, **opts).pack(side="left")

            self.quiz_card = tk.Frame(self.quiz, bg=CARD, padx=12, pady=10,
                                      highlightthickness=1, highlightbackground=BORDER)
            self.quiz_card.pack(anchor="w", fill="x")
            head = tk.Frame(self.quiz_card, bg=CARD)
            head.pack(anchor="w")
            self.quiz_champ_img = tk.Label(head, bg=CARD)
            self.quiz_champ_img.pack(side="left")
            self.quiz_champ_name = tk.Label(head, text="", fg=FG, bg=CARD, font=(UI_FONT, 13, "bold"))
            self.quiz_champ_name.pack(side="left", padx=(8, 0))
            row = tk.Frame(self.quiz_card, bg=CARD)
            row.pack(anchor="w", pady=(8, 0))
            self.quiz_ability_img = tk.Label(row, bg=CARD)
            self.quiz_ability_img.pack(side="left")
            self.quiz_ability = tk.Label(row, text="", fg=FG, bg=CARD, font=(UI_FONT, 11))
            self.quiz_ability.pack(side="left", padx=(8, 0))

            ask = tk.Frame(self.quiz, bg=BG)
            ask.pack(anchor="w", pady=(12, 4))
            self.quiz_ask = tk.Label(ask, text="Cooldown at rank 1 (seconds):", fg=MUTED, bg=BG,
                                     font=(UI_FONT, 9))
            self.quiz_ask.pack(side="left", padx=(0, 8))
            self.quiz_entry = tk.Entry(ask, width=7, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                       font=(MONO_FONT, 11), justify="center")
            self.quiz_entry.pack(side="left")
            self.quiz_entry.bind("<Return>", self.check_answer)
            self.quiz_button = tk.Button(ask, text="Check", command=self.check_answer, bg=CARD, fg=FG,
                                         activebackground=BORDER, activeforeground=FG, relief="flat",
                                         font=(UI_FONT, 9, "bold"), padx=10, cursor="hand2")
            self.quiz_button.pack(side="left", padx=(8, 0))

            self.quiz_result = tk.Label(self.quiz, text="", fg=MUTED, bg=BG, font=(UI_FONT, 10),
                                        justify="left", wraplength=360, height=2)
            self.quiz_result.pack(anchor="w", pady=(10, 0))
            self.quiz_next = tk.Button(self.quiz, text="Next champion", command=self.new_question, bg=CARD,
                                       fg=FG, activebackground=BORDER, activeforeground=FG, relief="flat",
                                       font=(UI_FONT, 9), padx=10, cursor="hand2", state="disabled")
            self.quiz_next.pack(anchor="w", pady=(10, 0))

        def _retry_pool(self):
            if self.mode != "quiz":
                return
            self.build_quiz_pool()
            if self.quiz_pool and not self.quiz_current:
                self.new_question()

        def _lanes_changed(self):
            self.build_quiz_pool()
            self.new_question()

        def build_quiz_pool(self):
            dd = getattr(self, "dd", None)
            if dd is None:
                self.quiz_pool = []
                return
            lanes_key = tuple(k for k, v in self.lanes.items() if v.get())
            if lanes_key == getattr(self, "_pool_key", None) and self.quiz_pool:
                return  # same lane filter as last time
            self._pool_key = lanes_key
            if dd._full_cache is None and dd.cached("data/en_US/championFull.json") is None:
                # Champion data still downloading on first launch: don't block the UI on it.
                self.quiz_pool = []
                if self.mode == "quiz":
                    self.root.after(2000, self._retry_pool)
                return
            picked = {k for k, v in self.lanes.items() if v.get()}
            pool = []
            for cid in dd.champ_ids:
                try:
                    c = dd.champion(cid)
                except Exception:
                    continue
                role = CHAMP_ROLE.get(c["name"], "").lower()
                if role:
                    if role not in picked:
                        continue
                elif len(picked) < 5:
                    continue  # unknown role: only when every lane is allowed
                for key, s in zip("QWER", c["spells"]):
                    # Same cooldown text the sheet shows, so charge-based / placeholder spells match
                    # (Rengar's Data Dragon cooldowns are all 0.25; CommunityDragon has the real ones).
                    try:
                        extras = dd.bin_spells(cid, fetch=False) or {}
                    except Exception:
                        extras = {}
                    cd_text, recharge = ability_cds(s, extras.get(s["id"]))
                    nums = re.findall(r"\d+(?:\.\d+)?", cd_text)
                    unit = "at rank 1"
                    if recharge:  # charge-based: the meaningful number is the time per charge
                        nums = re.findall(r"\d+(?:\.\d+)?", recharge) or nums
                        unit = "per charge"
                    if not nums:
                        continue
                    value = float(nums[0])
                    if value <= 0 or value == 0.25:
                        continue  # 0.25 is Data Dragon's "no cast cooldown" placeholder (Zoe W)
                    pool.append({"champ": c["name"], "champ_icon": f"img/champion/{c['image']['full']}",
                                 "key": key, "name": s["name"], "cd": value, "unit": unit,
                                 "icon": f"img/spell/{s['image']['full']}"})
            self.quiz_pool = pool

        def new_question(self):
            self.quiz_done = False
            self.quiz_next.configure(state="disabled")
            self.quiz_entry.delete(0, "end")
            self.quiz_entry.configure(state="normal")
            self.quiz_entry.focus_set()
            if not self.quiz_pool:
                self.quiz_current = None
                self.quiz_champ_img.config(image="")
                self.quiz_ability_img.config(image="")
                self.quiz_champ_name.config(text="No champions for those lanes")
                self.quiz_ability.config(text="")
                self.quiz_result.config(text="Tick another lane to get questions from it.", fg=MUTED)
                self.quiz_entry.configure(state="disabled")
                self.quiz_button.configure(state="disabled")
                return
            self.quiz_entry.configure(state="normal")
            self.quiz_button.configure(state="normal")
            q = random.choice(self.quiz_pool)
            for _ in range(6):
                if q != self.quiz_current or len(self.quiz_pool) == 1:
                    break
                q = random.choice(self.quiz_pool)
            self.quiz_current = q
            # Icons may still need downloading; do that off the UI thread so the next question is instant.
            self.quiz_champ_img.config(image="")
            self.quiz_ability_img.config(image="")
            threading.Thread(target=self._load_quiz_icons, args=(q,), daemon=True).start()
            self.quiz_champ_name.config(text=q["champ"])
            self.quiz_ability.config(text=f"{q['key']}  {q['name']}")
            self.quiz_ask.config(text=f"Cooldown {q.get('unit', 'at rank 1')} (seconds):")
            self.quiz_result.config(text="Type the cooldown in seconds and press Enter.", fg=MUTED)

        def _load_quiz_icons(self, q):
            dd = getattr(self, "dd", None)
            champ_path = spell_path = None
            if dd:
                champ_path = dd.fetch_optional(q["champ_icon"]) or None
                spell_path = dd.fetch_optional(q["icon"]) or None
            self.root.after(0, lambda: self._apply_quiz_icons(q, champ_path, spell_path))

        def _apply_quiz_icons(self, q, champ_path, spell_path):
            if q is not self.quiz_current:
                return  # moved on already
            img = self.photo(champ_path, 1, 5)  # 120px -> 24px
            self.quiz_champ_img.config(image=img or "")
            self.quiz_champ_img.image = img
            aimg = self.photo(spell_path, 5, 16)  # 64px -> 20px
            self.quiz_ability_img.config(image=aimg or "")
            self.quiz_ability_img.image = aimg

        def check_answer(self, _event=None):
            if self.quiz_done:
                self.new_question()
                return "break"
            if not self.quiz_current:
                return "break"
            try:
                guess = float(self.quiz_entry.get().replace(",", "."))
            except ValueError:
                self.quiz_result.config(text="Type a number, e.g. 7 or 6.5", fg=MUTED)
                return "break"
            true = self.quiz_current["cd"]
            diff = abs(guess - true)
            signed = guess - true
            points = 100 if diff < 0.01 else 50 if diff <= 2 else 25 if diff <= 4 else 0
            self.quiz_score += points
            self.quiz_count += 1
            verdict = "Perfect" if points == 100 else f"{signed:+g}s off"
            self.quiz_result.config(
                text=f"{verdict} — +{points} points\n{self.quiz_current['champ']} {self.quiz_current['key']} "
                     f"is {true:g}s {self.quiz_current.get('unit', 'at rank 1')} (you said {guess:g}s).",
                fg=MAX_FG if points == 100 else NOW_FG if points else KEY_COLORS["R"])
            self.quiz_score_label.config(text=f"Score {self.quiz_score}  ·  {self.quiz_count} answered")
            self.quiz_done = True
            self.quiz_entry.configure(state="disabled")
            self.quiz_next.configure(state="normal")
            return "break"

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
            self.q.put(("dd", dd))

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
                    elif kind == "dd":
                        self.dd = payload
                    elif kind == "view":
                        self.view = payload
                        self.rerender()
            except queue.Empty:
                pass
            self.root.after(150, self.drain)

        def apply_topmost(self):
            self.root.attributes("-topmost", self.topmost.get())

        def _remember_geometry(self, event=None):
            """Debounced save of where the user put the window / how big they made it."""
            if event is not None and event.widget is not self.root:
                return
            if self.root.state() != "normal":
                return  # minimised / maximised: keep the last real position
            if self._save_job is not None:
                self.root.after_cancel(self._save_job)
            self._save_job = self.root.after(800, self._save_geometry)

        def _save_geometry(self):
            self._save_job = None
            geo = (self.root.winfo_width(), self.root.winfo_height(),
                   self.root.winfo_x(), self.root.winfo_y())
            self._saved_geo = geo
            save_window_state({"geometry": list(geo)})

        def on_close(self):
            if self._save_job is not None:
                self.root.after_cancel(self._save_job)
                self._save_job = None
            self._save_geometry()
            self._unbind_wheel()
            self.root.destroy()

        def _bind_wheel(self, _e=None):
            if not self._wheel_bound:
                self.root.bind_all("<MouseWheel>", self._on_wheel)
                self._wheel_bound = True

        def _unbind_wheel(self, _e=None):
            if self._wheel_bound:
                self.root.unbind_all("<MouseWheel>")
                self._wheel_bound = False

        def _on_wheel(self, e):
            # Only scroll when there is something to scroll, so it doesn't fight other windows' scrolling.
            if self.canvas.bbox("all") and self.canvas.bbox("all")[3] > self.canvas.winfo_height():
                self.canvas.yview_scroll(int(-e.delta / 120), "units")

        def cols_count(self) -> int:
            """Cards per row: the fixed choice from the Cols box, or as many as fit the window width."""
            try:
                value = str(self.columns.get()).strip()
            except tk.TclError:
                value = "Auto"
            if value in ("1", "2", "3", "4", "5"):
                return int(value)
            width = self.canvas.winfo_width()
            card = max(420, getattr(self, "_card_w", 0) or 470)
            return max(1, min(5, int(max(width - 20, 1) // (card + 6))))

        def _auto_reflow(self):
            """After the user resizes the window: pick a new column count in Auto mode."""
            self._reflow_job = None
            if str(self.columns.get()).strip() in ("1", "2", "3", "4", "5"):
                return
            if self.cols_count() != self._render_cols:
                self.rerender()

        def _on_canvas_resize(self, e):
            # Repainting ~250 widgets on every drag step is what made resizing lag (about 120 ms per
            # step). Take the sheet off the canvas while the size keeps changing and put it back, once,
            # after the drag settles.
            self._pending_width = e.width
            if self._resize_job is None and self.view:
                self.canvas.itemconfigure(self._body_id, state="hidden")
            if self._resize_job is not None:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(180, self._apply_pending_width)

        def _apply_pending_width(self):
            self._resize_job = None
            width = getattr(self, "_pending_width", 0)
            if width:
                self.canvas.itemconfigure(self._body_id, width=max(1, width))
                if self._note is not None:
                    self._note.configure(wraplength=max(240, width - 24))
            self.canvas.itemconfigure(self._body_id, state="normal")
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            if self.view and self._reflow_job is None:
                self._reflow_job = self.root.after(60, self._auto_reflow)

        def _on_body_resize(self, _e=None):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

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

        def render_key(self):
            """Structure of what is on screen (champions, cooldowns, ranges, toggles). Levels, the
            per-level "now" values and the column count are deliberately NOT in here: they are updated
            in place instead of rebuilding the cards."""
            groups = tuple((title, tuple(
                (e["champion"], e["position"], e["player"], e["error"], str(e["icon"]),
                 tuple((a["key"], a["name"], a["cd"], a["recharge"], a["range"], str(a["icon"]))
                       for a in e["abilities"]),
                 tuple((s["name"], s["cd"], str(s["icon"])) for s in e["summoners"]))
                for e in entries))
                for title, entries in (self.view or {}).get("groups", []))
            return (groups, self.view.get("mode"), self.view.get("version"),
                    self.show_names.get(), self.show_ranges.get())

        def _apply_layout(self, cols: int):
            """Re-grid the existing cards. Cheap enough to run while the window is being dragged."""
            for grid, cards in self._grids:
                for i, card in enumerate(cards):
                    card.grid_configure(row=i // cols, column=i % cols)
            self._render_cols = cols

        def now_text(self, entry, ability):
            """Label text/colour for the current cooldown, or blank when there is no level yet."""
            if ability.get("now") is None or not entry.get("level"):
                return "", NOW_FG
            maxed = ability["rank"] >= ability["maxrank"]
            return f"  {ability['key']} {ability['now']:g}s", (MAX_FG if maxed else NOW_FG)

        def _flat_entries(self):
            return [e for _, entries in (self.view or {}).get("groups", []) for e in entries]

        def _update_live_fields(self):
            """Refresh only the level line and 'now' values, without touching the rest of the card."""
            entries = self._flat_entries()
            if len(entries) != len(self._card_refs):
                return
            for e, ref in zip(entries, self._card_refs):
                sub = " · ".join(x for x in (e["position"], e["player"][:16],
                                             f"Lv {e['level']}" if e.get("level") else "") if x)
                ref["sub"].config(text=sub)
                for label, a in zip(ref["now"], e["abilities"]):
                    text, colour = self.now_text(e, a)
                    label.config(text=text, fg=colour)

        def rerender(self, refit: bool = False):
            if not self.view:
                return
            cols = self.cols_count()
            key = self.render_key()
            if key == self._render_key:
                if cols != self._render_cols and self._grids:
                    self._apply_layout(cols)  # resized: just re-grid, never rebuild
                self._update_live_fields()
                return
            self._render_key = key
            # Hide the body while it is rebuilt, then show it again: one paint instead of dozens.
            self.canvas.itemconfigure(self._body_id, state="hidden")
            for w in self.body.winfo_children():
                w.destroy()
            self._card_refs = []
            self._grids = []
            self._note = None
            for title, entries in self.view["groups"]:
                tk.Label(self.body, text=title.upper(), fg=MUTED, bg=BG,
                         font=(UI_FONT, 9, "bold")).pack(anchor="w", pady=(6, 2))
                grid = tk.Frame(self.body, bg=BG)
                grid.pack(anchor="w")
                cards = []
                for i, e in enumerate(entries):
                    card, ref = self.card(grid, e)
                    card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=3, pady=3)
                    cards.append(card)
                    self._card_refs.append(ref)
                    if cols == 1:  # only a single column gives the card its natural width
                        self._card_w = max(getattr(self, "_card_w", 0), card.winfo_reqwidth())
                self._grids.append((grid, cards))
            self._render_cols = cols
            self._update_live_fields()
            note = ("Base cooldowns before ability haste (Data Dragon; charge recharge times from CommunityDragon). "
                    "\"recharge ×N\" = time per charge, N max charges. \"Q 10s\" = that ability's cooldown at "
                    "the enemy's current level (most popular leveling order), green once it is maxed.")
            if self.show_ranges.get():
                note += (" Ranges from the League of Legends wiki, in game units: \"AoE N\" = radius around them, "
                         "\"AA\" = empowered basic attack, \"A–B\" = grows while charging / with level, "
                         "\"/\" = per rank.")
            self._note = tk.Label(self.body, text=note,
                                  wraplength=max(240, self.canvas.winfo_width() - 24), justify="left",
                                  fg=MUTED, bg=BG, font=(UI_FONT, 8))
            self._note.pack(anchor="w", pady=(4, 2))
            self.fit_window(force=refit or (self._render_geo is None and not self._restored))
            self.canvas.itemconfigure(self._body_id, state="normal")
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(0)

        def fit_window(self, force: bool = False, content=None):
            """Size the window to the sheet, capped to the monitor (the rest scrolls). Never moves the
            window: the top-left corner stays exactly where the user put it. Normal updates only grow
            the window when the sheet needs more room; it never shrinks itself."""
            content = content or self.body
            self.root.update_idletasks()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            mon = monitor_bounds(self.root)
            if mon:
                mx, my, mw, mh = mon
            else:  # unknown desktop: use the whole virtual desktop
                mx, my = self.root.winfo_vrootx(), self.root.winfo_vrooty()
                mw, mh = self.root.winfo_vrootwidth(), self.root.winfo_vrootheight()
            if content is self.body:
                chrome_w = self.root.winfo_width() - self.canvas.winfo_width()
                chrome_h = self.root.winfo_height() - self.canvas.winfo_height()
            else:  # quiz tab: size to the panel that is packed instead of the canvas
                chrome_w = max(24, self.root.winfo_width() - content.winfo_width())
                chrome_h = max(90, self.root.winfo_height() - content.winfo_height())
            # Grow right/down only as far as this monitor allows from the current corner.
            max_w = max(360, mx + mw - x - 16)
            max_h = max(160, my + mh - y - 16)
            # Cards can be wider than the (canvas-width) body, so measure the children too.
            need_w = max([content.winfo_reqwidth()] + [c.winfo_reqwidth() for c in content.winfo_children()])
            fixed_cols = str(self.columns.get()).strip() in ("1", "2", "3", "4", "5")
            if content is self.body and not fixed_cols and self._render_geo is not None:
                # Auto columns: the window width is the user's business, cards reflow to it.
                w = self.root.winfo_width()
            else:
                w = max(420 if content is not self.body else 360, min(need_w + chrome_w + 12, max_w))
            h = max(160, min(content.winfo_reqheight() + chrome_h + 8, max_h))
            if not force and self.root.winfo_width() <= max_w and self.root.winfo_height() <= max_h:
                w = max(w, self.root.winfo_width())
                h = max(h, self.root.winfo_height())
            geo = (w, h)
            if force or geo != self._render_geo:
                self._render_geo = geo
                self.root.geometry(f"{w}x{h}+{x}+{y}")

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
            sub = " · ".join(x for x in (e["position"], e["player"][:16],
                                         f"Lv {e['level']}" if e.get("level") else "") if x)
            sub_label = tk.Label(names, text=sub, fg=MUTED, bg=CARD, font=(UI_FONT, 8))
            sub_label.pack(anchor="w")

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
            now_labels = []
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
                # Always created, so a later level-up can fill it in without rebuilding the card.
                now_label = tk.Label(cds, text="", fg=NOW_FG, bg=CARD, font=(MONO_FONT, 10, "bold"))
                now_label.pack(side="left")
                now_labels.append(now_label)
                col += 1
                if self.show_ranges.get() and a["range"] != "—":
                    rng = tk.Frame(rows, bg=CARD)
                    rng.grid(row=r, column=col, sticky="w", padx=(12, 0))
                    tk.Label(rng, text="↔", fg=MUTED, bg=CARD, font=(UI_FONT, 9)).pack(side="left")
                    tk.Label(rng, text=a["range"], fg=RANGE_FG, bg=CARD,
                             font=(MONO_FONT, 10)).pack(side="left", padx=(2, 0))
            return card, {"sub": sub_label, "now": now_labels}

    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Show enemy champion cooldowns at the start of a LoL game.")
    ap.add_argument("--console", action="store_true", help="print to the terminal instead of opening a window")
    ap.add_argument("--demo", nargs="*", metavar="CHAMPION",
                    help="preview with sample enemies instead of a live game (default: %s)" % ", ".join(DEFAULT_DEMO))
    ap.add_argument("--columns", default="auto", help="cards per row: auto (default) or 1-5")
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
