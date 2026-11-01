"""Turn shorthand clip filenames like "mirage1v3clutchmp9" into YouTube titles.

Two layers:
  * rule_title(): offline. Splits the run-together name using a CS2 vocabulary (maps,
    weapons, callouts, 3k/4k, 1v3) plus wordninja's English word frequencies, then formats it.
  * ai_title(): asks Claude for a punchier title when an Anthropic API key is configured,
    and falls back to rule_title() on any error.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

import wordninja

log = logging.getLogger(__name__)

# Lowercased token -> how it should be written in a title.
MAPS = {
    "mirage": "Mirage", "inferno": "Inferno", "dust2": "Dust 2", "nuke": "Nuke", "ancient": "Ancient",
    "anubis": "Anubis", "vertigo": "Vertigo", "overpass": "Overpass", "train": "Train", "cache": "Cache",
    "office": "Office", "italy": "Italy", "agency": "Agency", "cobblestone": "Cobblestone",
}
WEAPONS = {
    "ak": "AK", "ak47": "AK-47", "m4": "M4", "m4a4": "M4A4", "m4a1": "M4A1-S", "m4a1s": "M4A1-S",
    "awp": "AWP", "scout": "Scout", "ssg": "SSG", "deagle": "Deagle", "deag": "Deagle", "usp": "USP", "usps": "USP-S",
    "glock": "Glock", "glocks": "Glocks", "p250": "P250", "tec9": "Tec-9", "fiveseven": "Five-SeveN",
    "cz": "CZ", "r8": "R8", "mp9": "MP9", "mac10": "MAC-10", "mac": "MAC", "mp7": "MP7", "mp5": "MP5",
    "ump": "UMP", "p90": "P90", "bizon": "Bizon", "famas": "FAMAS", "galil": "Galil", "aug": "AUG",
    "sg": "SG", "nova": "Nova", "xm": "XM", "mag7": "MAG-7", "negev": "Negev", "m249": "M249",
    "zeus": "Zeus", "knife": "Knife", "he": "HE", "molly": "Molly", "nade": "Nade", "nades": "Nades",
}
TERMS = {
    "ace": "ACE", "clutch": "Clutch", "headshot": "Headshot", "headshots": "Headshots", "hs": "HS",
    "eco": "Eco", "ecofrags": "Eco Frags", "frags": "Frags", "flick": "Flick", "flicks": "Flicks",
    "lurk": "Lurk", "lurks": "Lurks", "smokes": "Smokes", "smoke": "Smoke", "flash": "Flash",
    "retake": "Retake", "rush": "Rush", "entry": "Entry", "spray": "Spray", "wallbang": "Wallbang",
    "noscope": "No-Scope", "oneshot": "One Shot", "onetap": "One Tap", "jumpshot": "Jumpshot",
    "pistolround": "Pistol Round", "pistol": "Pistol", "scrim": "Scrim", "faceit": "FACEIT",
    "premier": "Premier", "unbox": "Unbox", "gloves": "Gloves", "kp": "KP",
    # callouts
    "asite": "A Site", "bsite": "B Site", "site": "Site", "mid": "Mid", "pit": "Pit", "apps": "Apps",
    "sec": "Sec", "cave": "Cave", "lane": "Lane", "tspawn": "T Spawn", "ctspawn": "CT Spawn",
    "spawn": "Spawn", "connector": "Connector", "palace": "Palace", "ramp": "Ramp", "jungle": "Jungle",
    "banana": "Banana", "long": "Long", "short": "Short", "catwalk": "Catwalk", "heaven": "Heaven",
    "hell": "Hell", "bhold": "B Hold", "ahold": "A Hold", "hold": "Hold", "boost": "Boost",
    "idk": "IDK", "sadge": "Sadge", "lol": "LOL", "wtf": "WTF", "op": "OP", "ez": "EZ", "gg": "GG",
}
VOCAB = {**TERMS, **WEAPONS, **MAPS}

_KILLS = re.compile(r"[2-5]k")      # 3k, 4k ...
_CLUTCH = re.compile(r"1v[1-5]")    # 1v3 ...
_NUM = re.compile(r"\d+")

_LM = wordninja.DEFAULT_LANGUAGE_MODEL
_MAXLEN = max(_LM._maxword, max(len(k) for k in VOCAB))
_VOCAB_COST = 1.0           # cheaper than any English word, so CS2 terms win ties
_UNKNOWN_CHAR_COST = 30.0   # per character for something we can't recognise at all


def _cost(piece: str) -> float:
    if piece in VOCAB or _KILLS.fullmatch(piece) or _CLUTCH.fullmatch(piece):
        return _VOCAB_COST
    c = _LM._wordcost.get(piece)
    if c is not None:
        return c
    if _NUM.fullmatch(piece):
        return 5.0
    return _UNKNOWN_CHAR_COST * len(piece) + 50


def split_words(text: str) -> list[str]:
    """Split a run-together lowercase string into the cheapest sequence of known pieces."""
    words: list[str] = []
    for chunk in re.split(r"[^a-z0-9]+", text.lower()):
        if not chunk:
            continue
        n = len(chunk)
        best = [0.0] + [math.inf] * n
        back = [0] * (n + 1)
        for i in range(1, n + 1):
            for j in range(max(0, i - _MAXLEN), i):
                c = best[j] + _cost(chunk[j:i])
                if c < best[i]:
                    best[i], back[i] = c, j
        out = []
        i = n
        while i > 0:
            out.append(chunk[back[i]:i])
            i = back[i]
        words.extend(reversed(out))
    return _merge_singles(words)


def _merge_singles(words: list[str]) -> list[str]:
    # "a"/"b" right before "site"/"hold" are callouts, not articles.
    out: list[str] = []
    for w in words:
        if out and out[-1] in ("a", "b") and w in ("site", "hold"):
            out[-1] = out[-1] + w
        else:
            out.append(w)
    return out


def _fmt(word: str) -> str:
    if word in VOCAB:
        return VOCAB[word]
    if _KILLS.fullmatch(word):
        return word.upper()
    if _CLUTCH.fullmatch(word):
        return word
    if word == "i":
        return "I"
    return word.capitalize()


def rule_title(filename: str, max_len: int = 90) -> str:
    stem = Path(filename).stem
    words = split_words(stem)
    title = " ".join(_fmt(w) for w in words) or stem
    return title[:max_len].rstrip()


SYSTEM_PROMPT = """You write titles for YouTube Shorts of Counter-Strike 2 gameplay clips.
The only context is the clip's filename, which the player typed as quick lowercase shorthand with no spaces.
It usually names a map (mirage, inferno, dust2, ancient, anubis, nuke...), a callout (a site, pit, apps, cave, mid...),
a weapon (usp, mp9, mac10, ak, awp...), and what happened (3k / 4k = kills in a round, ace, 1v3 clutch, eco frags,
headshots, flick, lurk). Sometimes it's just a joke or reaction phrase.

Write one title that a CS2 player would click: keep every fact from the filename, don't invent events that aren't
in it, keep the player's own words and tone when it's a phrase, and stay under 70 characters.
Weapons in their usual form (USP-S, MAC-10, AK-47), maps capitalised. No hashtags, no quotes, no emoji.
Reply with the title only."""


def ai_title(filename: str, api_key: str, model: str, timeout: float = 30.0) -> str:
    """Title from Claude. Raises on any API problem; the caller falls back to rule_title()."""
    import anthropic

    stem = Path(filename).stem
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
    response = client.beta.messages.create(
        model=model,
        max_tokens=2000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Filename: {stem}\nRough word split: {rule_title(stem)}",
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined")
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    text = text.strip("\"'").splitlines()[0].strip() if text else ""
    if not text:
        raise RuntimeError("empty title from model")
    return text[:95]


def make_title(filename: str, api_key: str = "", model: str = "claude-opus-5-5") -> str:
    if api_key:
        try:
            return ai_title(filename, api_key, model)
        except Exception as exc:
            log.warning("AI title failed for %s, using rule-based title: %s", filename, exc)
    return rule_title(filename)
