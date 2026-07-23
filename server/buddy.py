"""
Buddy / Companion system for Whisper Studio.

A small creature lives in the chat UI corner. It is:
  - Deterministic per machine (same user@host always gets the same creature).
  - Purely cosmetic — it makes NO LLM calls and never touches a chat turn.
    (It used to inject a persona into every system prompt and fire a Bedrock
    reaction call ~40% of turns; both were removed.)
  - Rendered client-side as an animated SVG from the rolled "bones"; the
    backend only rolls traits, names it deterministically, and persists the
    hatched/muted state.
"""

import asyncio
import hashlib
import json
import logging
import os
import time

from fastapi import APIRouter, Request

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/buddy", tags=["buddy"])

DATA_DIR = data_root()
BUDDY_CONFIG_PATH = os.path.join(DATA_DIR, "buddy.json")

# ── Trait tables ──────────────────────────────────────────────────────────────

SPECIES = [
    "duck",
    "goose",
    "blob",
    "cat",
    "dragon",
    "octopus",
    "owl",
    "penguin",
    "turtle",
    "snail",
    "ghost",
    "axolotl",
    "capybara",
    "cactus",
    "robot",
    "rabbit",
    "mushroom",
    "chonk",
]

# Eye styles map to SVG eye shapes in the frontend (BuddyWidget).
EYES = ["dot", "star", "cross", "ring", "spiral", "sleepy"]

HATS = ["none", "crown", "tophat", "propeller", "halo", "wizard", "beanie", "tinyduck"]

STAT_NAMES = ["DEBUGGING", "PATIENCE", "CHAOS", "WISDOM", "SNARK"]

RARITIES = ["common", "uncommon", "rare", "epic", "legendary"]
RARITY_WEIGHTS = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1}
RARITY_FLOOR = {"common": 5, "uncommon": 15, "rare": 25, "epic": 35, "legendary": 50}
RARITY_STARS = {
    "common": "★",
    "uncommon": "★★",
    "rare": "★★★",
    "epic": "★★★★",
    "legendary": "★★★★★",
}
RARITY_COLORS = {
    "common": "#868e96",
    "uncommon": "#51cf66",
    "rare": "#74c0fc",
    "epic": "#da77f2",
    "legendary": "#ffa94d",
}

# Deterministic name pool + personality vocabulary (no LLM needed).
NAMES = [
    "Pip",
    "Sprout",
    "Mochi",
    "Tater",
    "Bingus",
    "Noodle",
    "Gizmo",
    "Biscuit",
    "Waffle",
    "Pixel",
    "Cricket",
    "Bean",
    "Pickle",
    "Boop",
    "Zonk",
    "Quill",
    "Fern",
    "Pebble",
    "Tofu",
    "Marble",
    "Simba",
    "Pesto",
    "Clover",
    "Nugget",
]
STAT_HIGH = {
    "DEBUGGING": "lives for a good stack trace",
    "PATIENCE": "will wait out the heat death of the universe",
    "CHAOS": "thrives on pure entropy",
    "WISDOM": "has seen some things",
    "SNARK": "has a comment for everything",
}
STAT_LOW = {
    "DEBUGGING": "treats bugs as features",
    "PATIENCE": "wants it done five minutes ago",
    "CHAOS": "colour-codes its spice rack",
    "WISDOM": "learns exclusively the hard way",
    "SNARK": "is unfailingly sincere",
}
# ── Deterministic PRNG (Mulberry32 port) ──────────────────────────────────────


def _mulberry32(seed: int):
    a = seed & 0xFFFFFFFF

    def rng():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = ((a ^ (a >> 15)) * (1 | a)) & 0xFFFFFFFF
        t = (t + ((t ^ (t >> 7)) * (61 | t))) & 0xFFFFFFFF
        t = (t ^ (t >> 14)) & 0xFFFFFFFF
        return t / 4294967296

    return rng


def _hash_string(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16) & 0xFFFFFFFF


def _pick(rng, arr):
    return arr[int(rng() * len(arr))]


def _roll_rarity(rng) -> str:
    total = sum(RARITY_WEIGHTS.values())
    roll = rng() * total
    for rarity in RARITIES:
        roll -= RARITY_WEIGHTS[rarity]
        if roll < 0:
            return rarity
    return "common"


def _roll_stats(rng, rarity: str) -> dict:
    floor = RARITY_FLOOR[rarity]
    peak = _pick(rng, STAT_NAMES)
    dump = peak
    while dump == peak:
        dump = _pick(rng, STAT_NAMES)
    stats = {}
    for name in STAT_NAMES:
        if name == peak:
            stats[name] = min(100, floor + 50 + int(rng() * 30))
        elif name == dump:
            stats[name] = max(1, floor - 10 + int(rng() * 15))
        else:
            stats[name] = floor + int(rng() * 40)
    return stats


SALT = "whisper-buddy-2026"


def _get_user_id() -> str:
    import getpass
    import socket

    return f"{getpass.getuser()}@{socket.gethostname()}"


def roll_companion(user_id: str = None) -> dict:
    """Generate deterministic companion bones from user_id."""
    uid = user_id or _get_user_id()
    rng = _mulberry32(_hash_string(uid + SALT))
    rarity = _roll_rarity(rng)
    species = _pick(rng, SPECIES)
    eye = _pick(rng, EYES)
    hat = "none" if rarity == "common" else _pick(rng, HATS)
    shiny = rng() < 0.01
    stats = _roll_stats(rng, rarity)
    return {
        "rarity": rarity,
        "species": species,
        "eye": eye,
        "hat": hat,
        "shiny": shiny,
        "stats": stats,
    }


def generate_soul_local(bones: dict, user_id: str = None) -> dict:
    """Deterministic name + personality from the rolled bones — no LLM call.

    Seeded separately from the bones roll so the name is stable for a given
    machine but independent of the trait sequence."""
    uid = user_id or _get_user_id()
    rng = _mulberry32(_hash_string(uid + SALT + "soul"))
    name = _pick(rng, NAMES)
    stats = bones.get("stats", {})
    if stats:
        peak = max(stats, key=stats.get)
        dump = min(stats, key=stats.get)
        personality = f"{STAT_HIGH[peak]}; {STAT_LOW[dump]}."
        personality = personality[0].upper() + personality[1:]
    else:
        personality = "A mysterious companion."
    return {"name": name, "personality": personality}


# ── Config persistence ────────────────────────────────────────────────────────


def load_buddy() -> dict | None:
    try:
        with open(BUDDY_CONFIG_PATH) as f:
            stored = json.load(f)
        bones = roll_companion()
        return {**stored, **bones}
    except Exception:
        return None


def save_buddy(soul: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BUDDY_CONFIG_PATH, "w") as f:
        json.dump(soul, f, indent=2)


def get_companion() -> dict | None:
    buddy = load_buddy()
    if not buddy or buddy.get("muted"):
        return None
    return buddy


def _bones_of(c: dict) -> dict:
    """The cosmetic traits the SVG renderer needs."""
    return {k: c[k] for k in ("rarity", "species", "eye", "hat", "shiny", "stats") if k in c}


# ── API routes ─────────────────────────────────────────────────────────────────


@router.get("")
async def get_buddy():
    companion = get_companion()
    if not companion:
        bones = roll_companion()
        return {
            "hatched": False,
            "bones": bones,
            "color": RARITY_COLORS[bones["rarity"]],
            "stars": RARITY_STARS[bones["rarity"]],
        }
    return {
        "hatched": True,
        "name": companion.get("name"),
        "personality": companion.get("personality", ""),
        "bones": _bones_of(companion),
        "color": RARITY_COLORS[companion["rarity"]],
        "stars": RARITY_STARS[companion["rarity"]],
    }


@router.post("/hatch")
async def hatch_buddy(request: Request):
    # Body is accepted for backward-compat but no longer used (no model call).
    try:
        await request.json()
    except Exception:
        pass
    bones = roll_companion()
    soul = generate_soul_local(bones)
    stored = {**soul, "hatchedAt": int(time.time()), "muted": False}
    save_buddy(stored)
    companion = {**bones, **stored}
    return {
        "name": companion["name"],
        "personality": companion["personality"],
        "bones": _bones_of(companion),
        "color": RARITY_COLORS[companion["rarity"]],
        "stars": RARITY_STARS[companion["rarity"]],
    }


# Topics for the OPT-IN "fresh facts (AI)" mode. Rotated so repeated calls
# don't all land on the same subject. Focused on broad, interesting general
# knowledge: science, biology, inventions, and geography (not AI).
_FACT_TOPICS = [
    "astronomy",
    "physics",
    "chemistry",
    "geology",
    "the human body",
    "animals",
    "the ocean",
    "plants and trees",
    "biology",
    "geography",
    "world landmarks",
    "famous inventions",
    "the history of technology",
    "space exploration",
]


@router.get("/fact")
async def buddy_fact():
    """OPT-IN: generate one short, true general-knowledge fact via Haiku.

    Facts span science, biology, inventions, and geography. Only called when the
    user enables "fresh facts (AI)" in the buddy bubble, a deliberate request on
    click, not a background timer. The default fact source is the curated
    client-side pack (no call). Falls back to a static fact if the model is
    unavailable so the bubble never empties.
    """
    import random

    import boto3

    from server.infrastructure.config import DEFAULTS, load_config

    fallback = "A day on Venus is longer than its entire year."
    try:
        config = load_config()
        chat_models = config.get("chat_models", DEFAULTS["chat_models"])
        model = (
            chat_models.get("haiku")
            or chat_models.get("sonnet")
            or next(iter(chat_models.values()))
        )
        region = config.get("bedrock_region", "us-east-1")
        bedrock = boto3.client("bedrock-runtime", region_name=region)
        topic = random.choice(_FACT_TOPICS)
        prompt = (
            f"Tell me one surprising, TRUE fact about {topic}. "
            "One sentence, under 25 words. No preamble, no quotes, just the fact. "
            "Do not use em dashes or en dashes in your reply."
        )
        system = (
            "You write one short, true fact for a UI bubble. "
            "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen."
        )
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 80,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        )

        def _invoke():
            resp = bedrock.invoke_model(modelId=model, body=body)
            return json.loads(resp["body"].read())["content"][0]["text"].strip()

        text = await asyncio.get_running_loop().run_in_executor(None, _invoke)
        return {"fact": text or fallback}
    except Exception as e:
        log.debug("AI fact generation failed, using fallback: %s", e)
        return {"fact": fallback}
