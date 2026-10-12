"""Build the Tide wiki data bundle from the mod jar + instance config.

Usage: python tools/extract.py [jar] [config_dir] [extra.jar ...]

Outputs: data.js (window.TIDE = {...}) and assets/ (textures copied from the jar).
Extra jars fill in item textures Tide references but doesn't ship (vanilla, other mods).
Stdlib only. Re-run after changing the mod version.
"""

import json
import io
import math
import os
import re
import shutil
import struct
import sys
import time
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Usage: python tools\extract.py <tide.jar> [config_dir] [extra.jar ...]
JAR = sys.argv[1] if len(sys.argv) > 1 else ""
CONFIG_DIR = sys.argv[2] if len(sys.argv) > 2 else ""
EXTRA_JARS = sys.argv[3:]

MOD = "tide"
BIOME_TAG_PREFIX = "data/tide/tags/worldgen/biome/"

GROUPS = {
    "freshwater": "Freshwater",
    "saltwater": "Saltwater",
    "underground": "Underground",
    "lava": "Lava",
    "void": "Void",
}

RARITIES = ["common", "uncommon", "rare", "very_rare", "legendary"]

DIMENSIONS = {
    "minecraft:overworld": "Overworld",
    "minecraft:the_nether": "The Nether",
    "minecraft:the_end": "The End",
    "minecraft:the_void": "The Void",
}

FLUIDS = {
    "water": "Water",
    "lava": "Lava",
    "void": "the fishable Void surface",
}

WEATHER = {"clear": "Clear skies", "rain": "Rain", "storm": "Thunderstorm"}

MOON_PHASES = {
    0: "Full Moon", 1: "Waning Gibbous", 2: "Last Quarter", 3: "Waning Crescent",
    4: "New Moon", 5: "Waxing Crescent", 6: "First Quarter", 7: "Waxing Gibbous",
}

COMPAT = {
    "hybridaquatic": ("Hybrid Aquatic", "Hybrid Aquatic fish and crates are merged into Tide's journal."),
    "netherdepths": ("Nether Depths Upgrade", "Lava fish from Nether Depths Upgrade are registered."),
    "fishingreal": ("Fishing Real", "Fishing Real loot is converted into Tide loot."),
    "stardewfishing": ("Stardew Fishing", "Stardew Fishing minigames and fish data are used when installed."),
    "fishofthieves": ("Fish of Thieves", "Its grubs, earthworms and leeches work as bait."),
    "accessories": ("Trinkets / Curios", "Informational items get a dedicated accessory slot."),
    "seasons": ("Fabric Seasons / Serene Seasons", "Fish spawn conditions follow the current season."),
    "modmenu": ("Mod Menu", "Adds a Tide entry to the mod list screen."),
}

# Everything below is transcribed from the public source (github.com/Lightning-64/Tide-2, main = v2.1.1).
SRC = "https://github.com/Lightning-64/Tide-2/blob/main/src/main/java/com/li64/tide/"

# TideItems.java: register("...", properties -> new TideFishingRodItem(baitSlots, durability, properties))
RODS = [
    ("stone_fishing_rod", 1, 48), ("iron_fishing_rod", 1, 64), ("golden_fishing_rod", 2, 36),
    ("crystal_fishing_rod", 2, 80), ("diamond_fishing_rod", 2, 128), ("netherite_fishing_rod", 2, 512),
    ("echo_fishing_rod", 3, 256), ("prismarine_fishing_rod", 3, 256), ("midas_fishing_rod", 3, 256),
    ("village_fishing_rod", 4, 256), ("blazing_fishing_rod", 4, 256), ("honeycomb_fishing_rod", 5, 256),
    ("sunflower_fishing_rod", 6, 256),
]

# TideItems.java: FishingHookItem registrations. TideFishingHook.getContext() applies the temperature offsets,
# TimeOfDayCondition adds 12000 ticks when the Twilight hook is equipped.
HOOKS = [
    ("fishing_hook", "Default hook. No effect.", None),
    ("fiery_hook", "Raises water temperature by 0.25", {"temperature": 0.25}),
    ("permafrost_hook", "Lowers water temperature by 0.25", {"temperature": -0.25}),
    ("twilight_hook", "Shifts the time-of-day test by half a day (12000 ticks), flipping day and night windows",
     {"timeShift": 12000}),
    ("lavaproof_hook", "Lets the rod fish in lava", {"lava": True}),
    ("void_hook", "Lets the rod fish in the void", {"void": True}),
]

# FishCatchMinigame.java line modifiers
LINE_EFFECTS = {
    "fishing_line": {},
    "copper_line": {"speed": 0.9},
    "iron_line": {"strength": 0.86},
    "golden_line": {"speed": 0.95, "luck": 1},
    "diamond_line": {"strength": 0.75},
}

MECHANICS = {
    "weightFormula": "effective weight = selection_weight + selection_quality x fishing luck",
    "weightSource": SRC + "data/fishing/selector/FishingEntry.java (modifyWeight)",
    "weightNote": "Modifiers are applied afterwards, each multiplying the weight. A weight of 0 or less drops the fish from the roll.",
    "shareFormula": "chance = this fish's weight / sum of weights of every eligible fish",
    "fishEntryFormula": "The generic \"fish\" loot entry weighs 85 - luck, so it disappears entirely at luck 85.",
    "fishEntrySource": SRC + "data/fishing/selector/FishSelector.java",
    "modifierFormula": "temperature modifier: weight x max(0, 1 - ((T - preferred) / tolerance)^2)",
    "modifierSource": SRC + "data/fishing/modifiers/types/TemperatureModifier.java",
    "crateFormula": "crate weight = crateWeight x (magnetic bait ? 5 : 1) + crateQuality x luck",
    "crateSource": SRC + "data/fishing/selector/CrateSelector.java",
    "magneticBait": "Magnetic Bait multiplies the crate roll weight by 5 and shows +25% crate chance.",
    "minigameAreaFormula": "hit area = clamp(1 - strength, 0.05, 1.0)",
    "minigameSpeedFormula": "minigame speed = max(speed / 20 x minigameDifficultyMultiplier, 0.05) movements per second",
    "minigameSource": SRC + "data/minigame/FishCatchMinigame.java",
    "sizeFormula": "lengths follow a log-normal curve fitted so typical_low_cm is the 10th percentile and "
                   "typical_high_cm the 90th, then clamped to the record bounds",
    "sizeSource": SRC + "data/fishing/FishSizeModel.java",
    "sizeFloor": "absolute minimum = record_low_cm, or 0.6 x typical_low_cm when unset",
    "biteFormula": "ticks until a bite = rand(200, 600) x medium multiplier - (2000 / (1 + e^(-0.2 x lureSpeed)) - 1000), "
                   "floored at 10 ticks",
    "biteSource": SRC + "registries/entities/misc/fishing/TideFishingHook.java",
    "mediumMultipliers": {"water": 1.0, "lava": 1.6, "void": 1.8},
    "luckSources": [
        "Luck of the Seas enchantment (vanilla)",
        "Lucky Bait: +2 luck",
        "Golden Line: +1 luck",
        "Golden or Midas Fishing Rod: +1 luck",
        "Prismarine Fishing Rod: +1 luck and +1 speed while it is raining",
        "Suncatcher (sunflower rod): +1 luck in direct sunlight",
    ],
    "luckUses": [
        "Raises the weight of the 22 fish that carry selection_quality 0.5",
        "Raises crate chance through the crateQuality config value",
        "Adds vanilla fishing loot luck to the rolled loot tables",
        "Gates fish that require a minimum luck, such as Midas Fish at luck 5",
    ],
    "rarityNote": "journal_profile.rarity only colours the journal entry and sets its star count. It has no effect on spawning.",
    "sunflowerNote": "The Suncatcher bonus needs direct sky light, daytime and no rain.",
    "blazingNote": "The Blazing Fishing Rod treats any lava as Nether lava, so Nether fish can be caught outside the Nether.",
}

Z10 = -1.2815515655446004
Z90 = 1.2815515655446004
LUCK_SAMPLES = [0, 2, 5, 10, 20]


# ---------------------------------------------------------------- helpers

def titleize(path):
    path = path.split("/")[-1].split(":")[-1]
    return path.replace("_", " ").strip().title()


def biome_name(biome_id):
    if biome_id in DIMENSIONS:
        return DIMENSIONS[biome_id]
    if biome_id.startswith("tide:") or biome_id.startswith("minecraft:"):
        return titleize(biome_id)
    return titleize(biome_id)


def tag_name(tag_id):
    """Human name for an ingredient/biome tag id like #c:ingots/iron.

    Strip the is_ prefix but never pluralize it: is_badlands is Badlands, not
    Badlandss. A tag name is still a tag, so callers keep the raw id around.
    """
    path = tag_id.lstrip("#").split(":")[-1]
    if path.startswith("is_"):
        path = path[3:]
    return path.replace("/", " / ").replace("_", " ").title()


def clock(ticks):
    """Tide's own TideUtils.ticksToRealTime, not a raw tick-to-clock conversion.

    Minecraft's tick 0 is dawn (06:00), not midnight, so the raw scale is six hours
    low across the whole day. A plain ticks/24000*1440 conversion renders every
    window six hours early.
    """
    hours = ticks / 1000.0 + 6
    if hours >= 24:
        hours -= 24
    hour = int(hours)
    minute = int((hours - hour) * 60)
    return "{:02d}:{:02d}".format(hour, minute)


def plural(word):
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


# ---------------------------------------------------------------- load jar

class Wiki:
    def __init__(self, jar_path):
        self.jar = zipfile.ZipFile(jar_path)
        self.names = set(self.jar.namelist())
        self.lang = json.loads(self.jar.read("assets/%s/lang/en_us.json" % MOD))
        self.meta = json.loads(self.jar.read("fabric.mod.json"))
        self.tags = {}
        self.optional = set()
        self._load_tags()
        self.item_icons = {}
        self._load_models()

    def read_json(self, name):
        return json.loads(self.jar.read(name))

    def tr(self, key, default=None):
        value = self.lang.get(key)
        if value is None:
            return default
        return value if isinstance(value, str) else json.dumps(value)

    def desc_lines(self, prefix):
        """Collect item.tide.x.desc, .desc_0, .desc_1 ... in order."""
        base = prefix + ".desc"
        if base in self.lang:
            return [self.lang[base]]
        lines = []
        i = 0
        while "{}_{}".format(base, i) in self.lang:
            lines.append(self.lang["{}_{}".format(base, i)])
            i += 1
        return lines

    def display_name(self, item_id):
        ns, path = item_id.split(":", 1)
        for kind in ("item", "block", "entity"):
            name = self.tr("%s.%s.%s" % (kind, ns, path))
            if name:
                return name
        return titleize(item_id)

    # -- tags

    def _load_tags(self):
        for name in self.names:
            if name.startswith("data/tide/tags/") and name.endswith(".json"):
                key = name[len("data/tide/tags/"):-len(".json")]
                values = self.read_json(name).get("values", [])
                ids = [v["id"] if isinstance(v, dict) else v for v in values]
                self.tags[key] = ids
                # entries marked required:false only exist when that mod is installed
                for value in values:
                    if isinstance(value, dict) and not value.get("required", True):
                        self.optional.add(value["id"])

    def expand_biomes(self, values, depth=0):
        """Biome display names plus the raw tag ids that could not be expanded.

        Returns (names, tags, mod_names). tags are tags outside the tide namespace,
        which the jar cannot expand; mod_names are biomes that only exist if another
        mod is installed.
        """
        out, tags, mods = [], [], []
        for value in values:
            if value.startswith("#") and depth < 5:
                key = value[1:]
                if key.startswith("tide:"):
                    name = key.split(":", 1)[1]
                    tag = "worldgen/biome/" + name
                    if tag not in self.tags:
                        tag = name
                    if tag in self.tags:
                        sub = self.expand_biomes(self.tags[tag], depth + 1)
                        out += sub[0]
                        tags += sub[1]
                        mods += sub[2]
                    else:
                        out.append(tag_name(value))
                        tags.append(value)
                else:
                    # vanilla or common tag: not in the jar, so show the name and keep the id
                    out.append(tag_name(value))
                    tags.append(value)
            else:
                out.append(biome_name(value))
                if value in self.optional:
                    mods.append(biome_name(value))
        def uniq(seq):
            seen = set()
            return [v for v in seq if not (v in seen or seen.add(v))]
        return uniq(out), uniq(tags), uniq(mods)

    # -- item icons

    def _load_models(self):
        for name in self.names:
            if not name.startswith("assets/%s/models/" % MOD) or not name.endswith(".json"):
                continue
            try:
                model = self.read_json(name)
            except ValueError:
                continue
            textures = model.get("textures", {})
            tex = (textures.get("layer0") or textures.get("all") or textures.get("side")
                   or textures.get("north") or textures.get("front") or textures.get("up")
                   or textures.get("particle"))
            if not tex:
                tex = next((v for v in textures.values() if isinstance(v, str) and not v.startswith("#")), None)
            if not tex or tex.startswith("#"):
                continue
            rel = tex.split(":", 1)[1]
            png = "assets/%s/textures/%s.png" % (MOD, rel)
            if png not in self.names:
                continue
            if "/models/item/" in name:
                key = "item:" + name.split("/models/item/")[1][:-5]
            elif "/models/block/" in name:
                key = "block:" + name.split("/models/block/")[1][:-5]
            else:
                continue
            self.item_icons[key] = "assets/textures/%s.png" % rel

    def icon_for_item(self, item_id):
        if item_id.startswith("%s:" % MOD):
            path = item_id.split(":", 1)[1]
            jar_png = "assets/%s/textures/item/%s.png" % (MOD, path)
            if jar_png in self.names:
                return "assets/textures/item/%s.png" % path
            key = "item:" + path
            if key in self.item_icons:
                return self.item_icons[key]
        return None

    def icon_for_block(self, block_id):
        if block_id.startswith("%s:" % MOD):
            key = "block:" + block_id.split(":", 1)[1]
            if key in self.item_icons:
                return self.item_icons[key]
        return None


# ------------------------------------------------------- condition -> prose

def describe_conditions(wiki, conditions):
    """Turn a fish's condition list into prose plus the raw facts other pages need."""
    lines = []
    facts = {
        "biomes": [], "biomeTags": [], "modBiomes": [], "dimensions": [], "fluids": [], "timeRanges": [],
        "moonPhases": [], "weather": [], "seasons": [], "structures": [], "depthRange": None,
        "above": None, "below": None, "luckGate": None, "temperature": None, "openWater": False,
    }
    seen = {key: set() for key in ("dimensions", "fluids")}

    def add(key, value):
        if value not in facts[key]:
            facts[key].append(value)

    def walk(node):
        kind = node.get("type")
        if kind == "tide:found_in":
            names, tags, mods = wiki.expand_biomes(node.get("biomes", []))
            for name in names:
                add("biomes", name)
            for tag in tags:
                add("biomeTags", tag)
            for name in mods:
                add("modBiomes", name)
            if names:
                lines.append("Biomes: " + ", ".join(names))
        elif kind == "tide:freshwater":
            lines.append("Freshwater")
        elif kind == "tide:saltwater":
            lines.append("Saltwater")
        elif kind == "tide:fluid":
            fluid = node.get("fluid", "water")
            if fluid not in seen["fluids"]:
                seen["fluids"].add(fluid)
                facts["fluids"].append(fluid)
            lines.append("Fluid: " + FLUIDS.get(fluid, titleize(fluid)))
        elif kind == "tide:dimension":
            for dimension in node.get("dimensions", []):
                add("dimensions", dimension)
            names = [DIMENSIONS.get(d, biome_name(d)) for d in facts["dimensions"]]
            if names:
                lines.append("Dimension: " + ", ".join(names))
        elif kind == "tide:above":
            facts["above"] = node.get("y")
            lines.append("Above Y {}".format(node.get("y")))
        elif kind == "tide:below":
            facts["below"] = node.get("y")
            lines.append("Below Y {}".format(node.get("y")))
        elif kind == "tide:depth_range":
            facts["depthRange"] = {"min": node.get("min_y"), "max": node.get("max_y")}
            lines.append("Between Y {} and Y {}".format(node.get("min_y"), node.get("max_y")))
        elif kind == "tide:seasons":
            facts["seasons"] += [s for s in node.get("seasons", []) if s not in facts["seasons"]]
            lines.append("Season: " + ", ".join(titleize(s) for s in node.get("seasons", [])))
        elif kind == "tide:time_of_day":
            ranges = []
            for entry in node.get("ranges", []):
                ranges.append({"min": entry.get("min", 0), "max": entry.get("max", 0)})
            facts["timeRanges"] += [r for r in ranges if r not in facts["timeRanges"]]
            lines.append("Time of day: " + ", ".join(
                "{}-{}".format(clock(r["min"]), clock(r["max"])) for r in ranges))
        elif kind == "tide:weather":
            facts["weather"] += [t for t in node.get("types", []) if t not in facts["weather"]]
            lines.append("Weather: " + ", ".join(WEATHER.get(t, titleize(t)) for t in node.get("types", [])))
        elif kind == "tide:moon_phase":
            facts["moonPhases"] += [p for p in node.get("any_of", []) if p not in facts["moonPhases"]]
            lines.append("Moon phase: " + ", ".join(
                MOON_PHASES.get(p, str(p)) for p in node.get("any_of", [])))
        elif kind == "tide:luck":
            facts["luckGate"] = node.get("min")
            lines.append("Needs fishing luck of at least {}".format(node.get("min")))
        elif kind == "tide:found_in_structures":
            names = [titleize(s) for s in node.get("structures", [])]
            for name in names:
                add("structures", name)
            if names:
                lines.append("Inside structure: " + ", ".join(names))
        elif kind == "tide:open_water":
            facts["openWater"] = True
            lines.append("Needs open water")
        elif kind == "tide:either":
            a = describe_conditions(wiki, [node["a"]])[0] if node.get("a") else []
            b = describe_conditions(wiki, [node["b"]])[0] if node.get("b") else []
            lines.append("Either ({}) or ({})".format(
                "; ".join(a) or "anywhere", "; ".join(b) or "anywhere"))
        elif kind == "tide:not":
            sub = describe_conditions(wiki, [node["condition"]])[0] if node.get("condition") else []
            lines.append("Not ({})".format(sub or "anywhere"))
        else:
            lines.append(titleize(kind.split(":")[-1]) + " condition")

    for condition in conditions:
        walk(condition)

    medium = None
    if "void" in facts["fluids"]:
        medium = "void"
    elif "lava" in facts["fluids"]:
        medium = "lava"
    elif "Saltwater" in lines:
        medium = "saltwater"
    elif "Freshwater" in lines:
        medium = "freshwater"
    return lines, facts, medium


def size_stats(size):
    """Percentiles of the log-normal the game samples lengths from."""
    low, high, record = size.get("typical_low_cm"), size.get("typical_high_cm"), size.get("record_high_cm")
    if not low or not high:
        return None
    sigma = (math.log(high) - math.log(low)) / (Z90 - Z10)
    mu = math.log(low) - Z10 * sigma
    return {
        "p10": round(low, 1),
        "p50": round(math.exp(mu), 1),
        "p90": round(high, 1),
        "mean": round(math.exp(mu + sigma * sigma / 2), 1),
        "floor": round(0.6 * low, 1),
        "record": record,
    }


# ---------------------------------------------------------------- sections

def build_fish(wiki):
    fish = []
    for name in sorted(wiki.names):
        if not (name.startswith("data/%s/fishing/fish/" % MOD) and name.endswith(".json")):
            continue
        data = wiki.read_json(name)
        key = name.split("/")[-1][:-5]
        item_id = "tide:" + key
        profile = data.get("journal_profile", {})
        group = profile.get("group") or name.split("/")[-2]
        where, facts, medium = describe_conditions(wiki, data.get("conditions", []))
        size = data.get("size", {})
        location_key = profile.get("location", "")
        temperature = None
        for modifier in data.get("modifiers", []):
            if modifier.get("type") == "tide:temperature":
                temperature = {"preferred": modifier.get("preferred_temperature"),
                               "tolerance": modifier.get("temperature_tolerance")}
        cook_tier = cookable_tier(wiki, item_id)
        fish.append({
            "id": item_id,
            "name": wiki.display_name(item_id),
            "group": group,
            "groupName": GROUPS.get(group, titleize(group)),
            "medium": medium or group,
            "rarity": profile.get("rarity", "common"),
            "location": wiki.tr(location_key) or titleize(location_key or group),
            "description": wiki.tr(profile.get("description"), "") or wiki.tr("journal.description.missing", ""),
            "size": {
                "low": size.get("typical_low_cm"),
                "high": size.get("typical_high_cm"),
                "record": size.get("record_high_cm"),
            },
            "speed": data.get("speed"),
            "strength": data.get("strength"),
            "weight": data.get("selection_weight"),
            "quality": data.get("selection_quality", 0.0),
            "behavior": data.get("behavior") or "sine",
            "shiny": "shiny_data" in data,
            "bucket": data.get("bucket"),
            "entity": "tide:" + key,
            "icon": wiki.icon_for_item(item_id) or ("assets/textures/entity/fish/%s.png" % key
                                                    if "assets/%s/textures/entity/fish/%s.png" % (MOD, key) in wiki.names else None),
            "where": where,
            "biomes": facts["biomes"],
            "biomeTags": facts["biomeTags"],
            "modBiomes": facts["modBiomes"],
            "dimensions": facts["dimensions"],
            "fluids": facts["fluids"],
            "temperature": temperature,
            "cookTier": cook_tier,
            "timeRanges": facts["timeRanges"],
            "moonPhases": facts["moonPhases"],
            "weather": facts["weather"],
            "seasons": facts["seasons"],
            "structures": facts["structures"],
            "depthRange": facts["depthRange"],
            "above": facts["above"],
            "below": facts["below"],
            "luckGate": facts["luckGate"],
            "openWater": facts["openWater"],
            "sizeStats": size_stats(size),
            "tags": [tag.split(":")[-1] for tag in wiki.tags.get("item/fish", []) if not tag.startswith("#")],
        })
    add_pool_shares(fish)
    fish.sort(key=lambda f: (GROUPS.get(f["group"], f["group"]), f["name"]))
    return fish


def cookable_tier(wiki, item_id):
    for tier in ("small_cooked_fish", "large_cooked_fish", "cooked_fish", "grilled_tuna"):
        if item_id in wiki.tags.get("item/cookable/" + tier, []):
            return tier
    if item_id in wiki.tags.get("item/cookable_fish", []):
        return "cooked_fish_slice"
    return None


def pool_members(fish, target):
    """Fish that share a medium and dimension with the target, ignoring biome/depth/time filters."""
    def fluids(entry):
        return sorted(entry["fluids"] or ["water"])

    members = []
    for other in fish:
        if fluids(other) != fluids(target):
            continue
        if target["dimensions"] and other["dimensions"] and set(other["dimensions"]) != set(target["dimensions"]):
            continue
        members.append(other)
    return members


def add_pool_shares(fish):
    """Weight and pool share at a few luck values, per fish."""
    caches = {}
    for entry in fish:
        key = (tuple(sorted(entry["fluids"] or ["water"])), tuple(sorted(entry["dimensions"] or [])))
        if key not in caches:
            members = pool_members(fish, entry)
            caches[key] = (members, {luck: sum(m["weight"] + m["quality"] * luck for m in members)
                                     for luck in LUCK_SAMPLES})
        members, totals = caches[key]
        entry["pool"] = len(members)
        entry["weightsAtLuck"] = {str(luck): round(entry["weight"] + entry["quality"] * luck, 2)
                                  for luck in LUCK_SAMPLES}
        entry["shareAtLuck"] = {str(luck): (round((entry["weight"] + entry["quality"] * luck) / totals[luck] * 100, 3)
                                           if totals[luck] > 0 else 0.0)
                                for luck in LUCK_SAMPLES}


def ing_ref(ref):
    if isinstance(ref, str):
        return {"id": ref}
    if "item" in ref:
        return {"id": ref["item"]}
    if "tag" in ref:
        return {"tag": ref["tag"]}
    if ref.get("type") == "minecraft:tag":
        return {"tag": "c:" + ref["name"].split(":")[-1] if ":" not in ref["name"] else ref["name"]}
    return {}


def build_recipes(wiki):
    recipes = []
    for name in sorted(wiki.names):
        if not (name.startswith("data/%s/recipe/" % MOD) and name.endswith(".json")):
            continue
        data = wiki.read_json(name)
        kind = data.get("type", "")
        result = data.get("result", {})
        result_id = result.get("id") or result.get("item")
        recipe = {
            "file": name,
            "category": data.get("category", "misc"),
            "type": kind,
            "result": {"id": result_id, "count": result.get("count", 1)},
            "resultName": wiki.display_name(result_id) if result_id else "",
        }
        if kind == "minecraft:crafting_shaped":
            recipe["pattern"] = data.get("pattern", [])
            recipe["key"] = {sym: ing_ref(ref) for sym, ref in data.get("key", {}).items()}
        elif kind == "minecraft:crafting_shapeless":
            recipe["ingredients"] = [ing_ref(r) for r in data.get("ingredients", [])]
        elif kind == "minecraft:smithing_transform":
            base = data.get("base", [])
            recipe["base"] = [ing_ref(r) for r in (base if isinstance(base, list) else [base])]
            recipe["template"] = ing_ref(data.get("template", {}))
            recipe["addition"] = ing_ref(data.get("addition", {}))
        elif kind in ("minecraft:smelting", "minecraft:smoking", "minecraft:campfire_cooking"):
            recipe["ingredient"] = ing_ref(data.get("ingredient", {}))
            recipe["time"] = data.get("cookingtime")
            recipe["xp"] = data.get("experience")
        elif kind == "tide:fishy_note_recipe":
            recipe["special"] = "Any fish can be crafted into a Fishy Note."
        recipes.append(recipe)
    recipes.sort(key=lambda r: (r["category"], r["resultName"]))
    return recipes


def build_loot(wiki, prefix, kind):
    tables = []
    for name in sorted(wiki.names):
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        data = wiki.read_json(name)
        pools = []
        for pool in data.get("pools", []):
            entries = []
            for entry in pool.get("entries", []):
                chance = None
                for cond in entry.get("conditions", []):
                    if cond.get("condition") == "minecraft:random_chance":
                        chance = cond.get("chance")
                count = 1
                for func in entry.get("functions", []):
                    if func.get("function") == "minecraft:set_count":
                        count = func.get("count")
                entries.append({
                    "id": entry.get("name"),
                    "name": wiki.display_name(entry.get("name", "")),
                    "weight": entry.get("weight", 1),
                    "quality": entry.get("quality", 0),
                    "chance": chance,
                    "count": count,
                    "enchanted": any(f.get("function") == "minecraft:enchant_with_levels" for f in entry.get("functions", [])),
                })
            rolls = pool.get("rolls")
            pools.append({
                "rolls": rolls if not isinstance(rolls, dict) else "{} - {}".format(rolls.get("min"), rolls.get("max")),
                "entries": entries,
            })
        tables.append({"file": name, "name": titleize(name.split("/")[-1][:-5]), "kind": kind, "pools": pools})
    return tables


def build_items(wiki, fish, baits, lines):
    """Every item id Tide knows about, with name, tooltip lines, icon, group tags."""
    ids = set()

    def add(item_id):
        if item_id and ":" in item_id and not item_id.startswith("#"):
            ids.add(item_id)

    for f in fish:
        add(f["id"])
        add(f["bucket"])
    for tag, values in wiki.tags.items():
        if tag.startswith("item/"):
            for value in values:
                if not value.startswith("#"):
                    add(value)
    for bait in baits:
        add(bait["item"])
    for line in lines:
        add(line["item"])
    for recipe in build_recipes(wiki):
        add(recipe["result"]["id"])
        for ref in (recipe.get("ingredients") or []) + (recipe.get("base") or []):
            if "id" in ref:
                add(ref["id"])
        if recipe.get("ingredient", {}).get("id"):
            add(recipe["ingredient"]["id"])

    tag_map = {}
    for tag, values in wiki.tags.items():
        if not tag.startswith("item/"):
            continue
        group = tag[len("item/"):]
        for value in values:
            if not value.startswith("#"):
                tag_map.setdefault(value, []).append(group)

    block_ids = [wiki.tr("block.tide.%s" % key) and "tide:" + key for key in ("wooden_crate", "purpur_crate", "obsidian_crate")]

    def rod_bonus(item_id):
        if not item_id.startswith("tide:") or not item_id.endswith("_fishing_rod"):
            return None
        stem = item_id.split(":")[1][:-len("_fishing_rod")]
        stem = {"golden": "gold"}.get(stem, stem)
        return wiki.tr("text.tide.rod_tooltip.%s_bonus" % stem)

    items = []
    for item_id in sorted(ids):
        ns, path = item_id.split(":", 1)
        bonus = rod_bonus(item_id)
        desc = wiki.desc_lines("item.%s.%s" % (ns, path)) if ns == MOD else []
        if bonus:
            desc = desc + [bonus]
        items.append({
            "id": item_id,
            "name": wiki.display_name(item_id),
            "desc": desc,
            "icon": wiki.icon_for_item(item_id) or wiki.icon_for_block(item_id),
            "tags": sorted(tag_map.get(item_id, [])),
            "isBlock": item_id in block_ids,
            "isFish": item_id in {f["id"] for f in fish} or item_id in {f["bucket"] for f in fish if f["bucket"]},
        })
    return items


def build_vanilla_fish(wiki):
    """Vanilla fish that got journal entries, so the dex is complete."""
    out = []
    for key, description in sorted(wiki.lang.items()):
        if not key.startswith("journal.description.minecraft."):
            continue
        path = key.split(".")[-1]
        item_id = "minecraft:" + path
        out.append({"id": item_id, "name": wiki.display_name(item_id), "description": description})
    return out


def build_rod_bonuses(wiki):
    return [{"key": k.split(".")[-1].replace("_bonus", "").replace("_", " ").title(),
             "text": v}
            for k, v in sorted(wiki.lang.items())
            if k.startswith("text.tide.rod_tooltip.") and k.endswith("_bonus")]


def build_extras(wiki):
    """Small loose strings used by the minigame and journal pages."""
    return {
        "minigameFeedback": {k.split(".")[-1]: v for k, v in sorted(wiki.lang.items())
                             if k.startswith("minigame.")},
        "toastNewFish": wiki.tr("newfish.toast.title", "New Fish Discovered"),
        "toastNewPage": wiki.tr("newpage.toast.title", "New Journal Page"),
        "baitPrefix": wiki.tr("text.tide.bait_tooltip.prefix", "When used as bait:"),
        "luckLabel": wiki.tr("text.tide.bait_tooltip.lucky", "%s fishing luck"),
        "speedLabel": wiki.tr("text.tide.bait_tooltip.speed", "%s fishing speed"),
        "crateLabel": wiki.tr("text.tide.bait_tooltip.crate", "%s crate chance"),
        "accessoryPrefix": wiki.tr("text.tide.accessory_tooltip.prefix", "When applied:"),
        "rodAccessoryPrefix": wiki.tr("text.tide.rod_tooltip.accessories_prefix", "Accessories:"),
        "baitSlotDesc": wiki.tr("text.tide.rod_tooltip.bait_desc", "Bait slots (right click to fill)"),
        "bucketableLabel": wiki.tr("text.tide.fish.alive", "Bucketable"),
    }


def build_blocks(wiki):
    blocks = []
    for key, name in wiki.lang.items():
        if not key.startswith("block.tide."):
            continue
        path = key.split(".")[-1]
        blocks.append({
            "id": "tide:" + path,
            "name": name,
            "desc": wiki.desc_lines("block.tide.%s" % path) or wiki.desc_lines("item.tide.%s" % path),
            "icon": wiki.icon_for_block("tide:" + path),
        })
    blocks.sort(key=lambda b: b["name"])
    return blocks


def build_baits(wiki):
    baits = []
    for name in sorted(wiki.names):
        if not (name.startswith("data/%s/bait/" % MOD) and name.endswith(".json")):
            continue
        data = wiki.read_json(name)
        item_id = data.get("item")
        baits.append({
            "item": item_id,
            "name": wiki.display_name(item_id),
            "luck": data.get("luck_bonus", 0),
            "speed": data.get("speed_bonus", 0),
            "crateChance": 25 if item_id == "tide:magnetic_bait" else 0,
            "icon": wiki.icon_for_item(item_id),
            "source": name.split("/")[-2] if name.count("/") > 4 else "tide",
        })
    return baits


def build_lines(wiki):
    lines = []
    for name in sorted(wiki.names):
        if not (name.startswith("data/%s/rod_accessories/" % MOD) and name.endswith(".json")):
            continue
        data = wiki.read_json(name)
        item_id = data.get("item")
        path = item_id.split(":")[-1]
        lines.append({
            "item": item_id,
            "name": wiki.display_name(item_id),
            "desc": wiki.desc_lines("item.tide.%s" % path),
            "color": data.get("color"),
            "icon": wiki.icon_for_item(item_id),
            "effects": LINE_EFFECTS.get(path, {}),
        })
    return lines


def build_rods(wiki):
    lava_rods = set(wiki.tags.get("item/lava_fishing_rods", []))
    luck_rods = set(wiki.tags.get("item/luck_boosting_rods", []))
    rods = []
    for path, slots, durability in RODS:
        item_id = "tide:" + path
        stem = path.replace("_fishing_rod", "")
        bonus = wiki.tr("text.tide.rod_tooltip.%s_bonus" % {"golden": "gold"}.get(stem, stem))
        rods.append({
            "id": item_id,
            "name": wiki.display_name(item_id),
            "icon": wiki.icon_for_item(item_id),
            "baitSlots": slots,
            "durability": durability,
            "lavaFishing": item_id in lava_rods,
            "luckBoost": item_id in luck_rods,
            "netherOverride": path == "blazing_fishing_rod",
            "bonus": bonus,
            "desc": wiki.desc_lines("item.tide." + path),
        })
    rods.sort(key=lambda r: (r["durability"], r["name"]))
    return rods


def build_hooks(wiki):
    hooks = []
    for path, effect, flags in HOOKS:
        item_id = "tide:" + path
        hooks.append({
            "id": item_id,
            "name": wiki.display_name(item_id),
            "icon": wiki.icon_for_item(item_id),
            "effect": effect,
            "desc": wiki.desc_lines("item.tide." + path),
            "flags": flags or {},
        })
    return hooks


def build_config(wiki):
    option_texts = {k[len("text.autoconfig.tide.option."):]: v
                    for k, v in wiki.lang.items() if k.startswith("text.autoconfig.tide.option.")}

    def name_for(path):
        return option_texts.get(path, titleize(path.replace(".", " ")))

    def tooltip_for(path):
        return option_texts.get(path + ".@Tooltip")

    config = {}
    for side, filename in (("server", "tide_server.json"), ("client", "tide_client.json")):
        path = os.path.join(CONFIG_DIR, filename)
        if not os.path.exists(path):
            continue
        values = json.load(open(path, encoding="utf-8"))
        categories = []
        for cat_key, options in values.items():
            entries = []
            for opt_key, value in options.items():
                option_path = "{}.{}.{}".format(side, cat_key, opt_key)
                display = value
                if isinstance(value, list):
                    display = "; ".join(
                        "{} {} {}".format(v.get("dimension", ""), v.get("type", ""), v.get("height", "")).strip()
                        for v in value) or "empty"
                entries.append({
                    "key": opt_key,
                    "name": name_for(option_path),
                    "tooltip": tooltip_for(option_path),
                    "default": display,
                })
            categories.append({"key": cat_key, "name": name_for("{}.{}".format(side, cat_key)), "options": entries})
        config[side] = categories
    return config


def build_advancements(wiki):
    advancements = []
    for name in sorted(wiki.names):
        if not (name.startswith("data/%s/advancement/" % MOD) and name.endswith(".json")):
            continue
        rel = name[len("data/%s/advancement/" % MOD):-len(".json")]
        if "/" in rel:
            continue
        data = wiki.read_json(name)
        display = data.get("display", {})

        def text(field):
            value = display.get(field, {})
            return wiki.tr(value.get("translate", ""), "") or value.get("text", "")

        icon = display.get("icon", {}).get("id")
        advancements.append({
            "id": "tide:" + rel,
            "title": text("title"),
            "description": text("description"),
            "icon": wiki.icon_for_item(icon),
            "parent": (data.get("parent") or "").split(":")[-1] or None,
            "root": data.get("parent") is None,
        })
    advancements = [a for a in advancements if a["title"]]
    advancements.sort(key=lambda a: (not a["root"], a["title"]))
    return advancements


def build_compat(wiki):
    """Compat targets, from the compat package layout plus third-party data folders."""
    aliases = {
        "hybrid_aquatic": "hybridaquatic",
        "netherdepthsupgrade": "netherdepths",
        "stardew_fishing": "stardewfishing",
    }
    found = set()
    for name in wiki.names:
        match = re.match(r"com/li64/tide/compat/([^/]+)/", name)
        if match:
            found.add(match.group(1))
        if name.startswith("data/"):
            folder = name.split("/")[1]
            if folder in aliases:
                found.add(aliases[folder])
    compat, seen = [], set()
    for key in sorted(found):
        title, note = COMPAT.get(key, (titleize(key), "Supported."))
        if title in seen:
            continue
        seen.add(title)
        compat.append({"id": key, "name": title, "note": note})
    compat.sort(key=lambda c: c["name"])
    return compat


def build_commands(wiki):
    def strings(prefix):
        return [{"key": k, "text": wiki.lang[k]} for k in sorted(wiki.lang) if k.startswith(prefix)]

    return [
        {
            "usage": "/fishing <results|fish|fish_selector|crate|crate_selector|loot_table>",
            "name": "Fishing results",
            "desc": "Debug command. Run it while holding a fishing rod to print every fish, crate or loot table Tide would pick in your current context (biome, fluid, time, weather, depth).",
            "strings": strings("commands.fishing."),
        },
        {
            "usage": "/journal <unlock|unlock_all|lock>",
            "name": "Fishing journal",
            "desc": "Unlock or lock journal entries for a player. /journal unlock_all <player> completes the journal.",
            "strings": strings("commands.journal"),
        },
    ]


# ---------------------------------------------------------------- assets

def sprite_meta(wiki):
    """Item textures that are sprite strips: frame list + layout, read from the PNG header."""
    meta = {}
    for name in wiki.names:
        if not (name.startswith("assets/%s/textures/item/" % MOD) and name.endswith(".png")):
            continue
        head = wiki.jar.read(name)[:24]
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            continue
        width, height = struct.unpack(">II", head[16:24])
        if height > width and height % width == 0:
            frames, layout = height // width, "v"
        elif width > height and width % height == 0:
            frames, layout = width // height, "h"
        else:
            continue
        rel = "assets/" + name[len("assets/%s/" % MOD):]
        meta[rel] = {"frames": frames, "layout": layout, "size": min(width, height)}
    return meta


# --------------------------------------------------------------- PNG slicing

def png_decode(data):
    """Minimal PNG reader: 8-bit RGB/RGBA/palette, non-interlaced.

    Returns (w, h, channels, pixels); palette images are expanded to RGBA.
    """
    pos, idat = 8, b""
    width = height = depth = colour = interlace = None
    palette = trns = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IEND":
            break
    if depth != 8 or colour not in (2, 3, 6) or interlace != 0:
        return None
    if colour == 3 and palette is None:
        return None
    channels = 3 if colour == 2 else 4
    bpp = 1 if colour == 3 else channels
    raw = zlib.decompress(idat)
    stride = width * bpp
    out = bytearray(height * width * channels)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            upleft = prev[x - bpp] if x >= bpp else 0
            if filter_type == 1:
                line[x] = (line[x] + left) & 255
            elif filter_type == 2:
                line[x] = (line[x] + up) & 255
            elif filter_type == 3:
                line[x] = (line[x] + (left + up) // 2) & 255
            elif filter_type == 4:
                estimate = left + up - upleft
                da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - upleft)
                predictor = left if (da <= db and da <= dc) else (up if db <= dc else upleft)
                line[x] = (line[x] + predictor) & 255
        if colour == 3:
            for x in range(width):
                index = line[x]
                rgb = palette[index * 3:index * 3 + 3]
                alpha = trns[index] if trns and index < len(trns) else 255
                out[(y * width + x) * 4:(y * width + x) * 4 + 4] = rgb + bytes([alpha])
        else:
            out[y * width * channels:(y + 1) * width * channels] = line
        prev = line
    return width, height, channels, bytes(out)


def png_encode(width, height, channels, pixels):
    """Encode raw pixels as an 8-bit RGB/RGBA PNG."""
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw += pixels[y * stride:(y + 1) * stride]
    colour = 2 if channels == 3 else 6

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------- isometric block icons

ISO_SIZE = 32
ISO_YAW = math.radians(225.0)    # Minecraft's GUI view: north + east faces
ISO_PITCH = math.radians(30.0)
ISO_SCALE = 1.19
ISO_CX = ISO_CY = ISO_SIZE / 2.0
ISO_C, ISO_S = math.cos(ISO_YAW), math.sin(ISO_YAW)
ISO_C2, ISO_S2 = math.cos(ISO_PITCH), math.sin(ISO_PITCH)


# Vanilla cube templates: mod jars reference these parents but don't bundle them.
VANILLA_CUBE_MODELS = {
    "cube": {
        "elements": [{"from": [0, 0, 0], "to": [16, 16, 16],
                      "faces": {"down": {"texture": "#down"}, "up": {"texture": "#up"},
                                "north": {"texture": "#north"}, "south": {"texture": "#south"},
                                "east": {"texture": "#east"}, "west": {"texture": "#west"}}}],
    },
    "cube_all": {"parent": "block/cube",
                   "textures": {"down": "#all", "up": "#all", "north": "#all",
                                "south": "#all", "east": "#all", "west": "#all",
                                "particle": "#all"}},
    "cube_bottom_top": {"parent": "block/cube",
                          "textures": {"down": "#bottom", "up": "#top", "north": "#side",
                                       "south": "#side", "east": "#side", "west": "#side",
                                       "particle": "#side"}},
    "cube_column": {"parent": "block/cube",
                     "textures": {"down": "#end", "up": "#end", "north": "#side",
                                  "south": "#side", "east": "#side", "west": "#side",
                                  "particle": "#side"}},
    "cube_mirrored": "cube",
    "cube_mirrored_all": "cube_all",
}


def load_block_model(jars, ns, name):
    """Resolve a block model through its parent chain -> (elements, textures, ns)."""
    elements = None
    textures = {}
    seen = set()
    while name and (ns, name) not in seen:
        seen.add((ns, name))
        model = None
        if ns == "minecraft" and name.startswith("block/"):
            key = name[len("block/"):]
            if key in VANILLA_CUBE_MODELS:
                entry = VANILLA_CUBE_MODELS[key]
                model = VANILLA_CUBE_MODELS[entry] if isinstance(entry, str) else entry
        if model is None:
            model_file = name if "/" in name else "block/" + name
            for jar in jars:
                try:
                    model = json.loads(jar.read("assets/%s/models/%s.json" % (ns, model_file)))
                    break
                except (KeyError, ValueError):
                    continue
        if model is None:
            return None
        if elements is None and "elements" in model:
            elements = model["elements"]
        for key, value in (model.get("textures") or {}).items():
            textures[key] = value
        parent = model.get("parent")
        if not parent:
            break
        if ":" in parent:
            ns, name = parent.split(":", 1)
        else:
            name = parent
    return (elements, textures, ns) if elements is not None else None


def resolve_texture_ref(ref, textures):
    seen = set()
    while isinstance(ref, str) and ref.startswith("#"):
        if ref in seen:
            return None
        seen.add(ref)
        ref = textures.get(ref[1:])
    return ref if isinstance(ref, str) and not ref.startswith("#") else None


def load_texture(jars, ns, ref, cache):
    """Decode a texture PNG referenced by a model -> (w, h, channels, pixels)."""
    if ":" in ref:
        tex_ns, path = ref.split(":", 1)
    else:
        tex_ns, path = ns, ref
    entry = "assets/%s/textures/%s.png" % (tex_ns, path)
    if entry not in cache:
        cache[entry] = None
        for jar in jars:
            try:
                cache[entry] = png_decode(jar.read(entry))
                break
            except KeyError:
                continue
    return cache[entry]


def face_quad(direction, box, uv):
    """Quad corners in UV order: (x, y, z, u, v), u/v in 0..1, v=0 at texture bottom."""
    x0, y0, z0, x1, y1, z1 = box
    u0, v0, u1, v1 = uv
    corners = {
        "down": [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        "up": [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
        "north": [(x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0)],
        "south": [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        "west": [(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)],
        "east": [(x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1)],
    }.get(direction)
    if corners is None:
        return None
    uvs = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
    return [(p[0], p[1], p[2], u / 16.0, v / 16.0) for p, (u, v) in zip(corners, uvs)]


def rotate_vertex(vertex, rotation):
    if not rotation or not rotation.get("angle"):
        return vertex
    angle = math.radians(rotation["angle"])
    axis = rotation.get("axis", "y")
    origin = rotation.get("origin", [8, 8, 8])
    x, y, z = vertex[0] - origin[0], vertex[1] - origin[1], vertex[2] - origin[2]
    c, s = math.cos(angle), math.sin(angle)
    if axis == "x":
        y, z = y * c - z * s, y * s + z * c
    elif axis == "y":
        x, z = x * c + z * s, -x * s + z * c
    elif axis == "z":
        x, y = x * c - y * s, x * s + y * c
    return (x + origin[0], y + origin[1], z + origin[2])


def project(vertex):
    x, y, z = vertex[0] - 8.0, vertex[1] - 8.0, vertex[2] - 8.0
    x, z = x * ISO_C + z * ISO_S, -x * ISO_S + z * ISO_C
    y, z = y * ISO_C2 - z * ISO_S2, y * ISO_S2 + z * ISO_C2
    return (ISO_CX + x * ISO_SCALE, ISO_CY - y * ISO_SCALE, z)


def rasterize(quads, size=ISO_SIZE):
    """Draw textured quads with a z-buffer. Returns RGBA pixels."""
    zbuf = [float("-inf")] * (size * size)
    pixels = bytearray(size * size * 4)
    for quad, texture in quads:
        if texture is None:
            continue
        tw, th, tch, texels = texture
        verts = []
        for v in quad:
            sx, sy, depth = project(v[:3])
            verts.append((sx, sy, depth, v[3], v[4]))
        for tri in ((verts[0], verts[1], verts[2]), (verts[0], verts[2], verts[3])):
            (ax, ay, az, au, av) = tri[0]
            (bx, by, bz, bu, bv) = tri[1]
            (cx, cy, cz, cu, cv) = tri[2]
            x0 = max(0, int(math.ceil(min(ax, bx, cx))))
            x1 = min(size - 1, int(math.floor(max(ax, bx, cx))))
            y0 = max(0, int(math.ceil(min(ay, by, cy))))
            y1 = min(size - 1, int(math.floor(max(ay, by, cy))))
            denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if denom == 0:
                continue
            for py in range(y0, y1 + 1):
                for px in range(x0, x1 + 1):
                    sx, sy = px + 0.5, py + 0.5
                    w0 = ((by - cy) * (sx - cx) + (cx - bx) * (sy - cy)) / denom
                    w1 = ((cy - ay) * (sx - cx) + (ax - cx) * (sy - cy)) / denom
                    w2 = 1.0 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    depth = w0 * az + w1 * bz + w2 * cz
                    idx = py * size + px
                    if depth <= zbuf[idx]:
                        continue
                    u = w0 * au + w1 * bu + w2 * cu
                    v = w0 * av + w1 * bv + w2 * cv
                    tx = min(tw - 1, max(0, int(u * (tw - 1))))
                    ty = min(th - 1, max(0, int((1.0 - v) * (th - 1))))
                    o = (ty * tw + tx) * tch
                    if tch == 4 and texels[o + 3] < 128:
                        continue
                    zbuf[idx] = depth
                    pixels[idx * 4] = texels[o]
                    pixels[idx * 4 + 1] = texels[o + 1]
                    pixels[idx * 4 + 2] = texels[o + 2]
                    pixels[idx * 4 + 3] = 255 if tch == 3 else texels[o + 3]
    return pixels


def render_block_icon(jars, ns, name, assets_dir):
    """Render a block model as an isometric icon PNG. Returns the asset path or None."""
    model = load_block_model(jars, ns, name)
    if not model:
        return None
    elements, textures, model_ns = model
    texture_cache = {}
    quads = []
    for element in elements:
        box = (element.get("from", [0, 0, 0]) + element.get("to", [16, 16, 16]))
        box = (box[0], box[1], box[2], box[3], box[4], box[5])
        rotation = element.get("rotation")
        for direction, face in (element.get("faces") or {}).items():
            quad = face_quad(direction, box, face.get("uv") or default_face_uv(direction, box))
            if quad is None:
                continue
            ref = resolve_texture_ref(face.get("texture"), textures)
            texture = load_texture(jars, model_ns, ref, texture_cache) if ref else None
            rotated = []
            for v in quad:
                pos = rotate_vertex(v[:3], rotation)
                rotated.append((pos[0], pos[1], pos[2], v[3], v[4]))
            quads.append((rotated, texture))
    pixels = rasterize(quads)
    filled = sum(1 for i in range(3, len(pixels), 4) if pixels[i] >= 128)
    if filled < 20:  # even small blocks cover dozens of pixels
        return None
    out_rel = "assets/textures/block/%s_%s_icon.png" % (ns, name)
    out_path = os.path.join(assets_dir, out_rel[len("assets/"):].replace("/", os.sep))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as handle:
        handle.write(png_encode(ISO_SIZE, ISO_SIZE, 4, bytes(pixels)))
    return out_rel


def default_face_uv(direction, box):
    x0, y0, z0, x1, y1, z1 = box
    if direction in ("down", "up"):
        return (x0, z0, x1, z1)
    if direction in ("north", "south"):
        return (x0, y0, x1, y1)
    return (z0, y0, z1, y1)


def item_model_block(jar, ns, path):
    """Block model (ns, name) if the item renders a block model, else None."""
    try:
        model = json.loads(jar.read("assets/%s/models/item/%s.json" % (ns, path)))
    except (KeyError, ValueError):
        return None
    parent = model.get("parent") or ""
    if not parent:
        return None
    if ":" in parent:
        parent_ns, parent_name = parent.split(":", 1)
    else:
        parent_ns, parent_name = ns, parent
    if not parent_name.startswith("block/"):
        return None
    return parent_ns, parent_name[len("block/"):]


def render_block_icons(items, wiki, blocks, assets_dir):
    """Swap flat block textures for isometric renders wherever possible."""
    rendered = 0
    extra_jars = []
    for jar_path in EXTRA_JARS:
        if os.path.exists(jar_path):
            extra_jars.append(zipfile.ZipFile(jar_path))
    jars = [wiki.jar] + extra_jars
    try:
        for item in items:
            ns, path = item["id"].split(":", 1)
            for jar in jars:
                target = item_model_block(jar, ns, path)
                if not target:
                    continue
                icon = render_block_icon(jars, target[0], target[1], assets_dir)
                if icon:
                    item["icon"] = icon
                    wiki.item_icons["block:" + target[1]] = icon
                    rendered += 1
                break
        for b in blocks:
            if b.get("icon"):
                b["icon"] = wiki.icon_for_block(b["id"]) or b["icon"]
    finally:
        for jar in extra_jars:
            jar.close()
    if rendered:
        print("isometric block icons  %d" % rendered)
    return rendered


def _ground_truth(png_bytes, decoded):
    """Fail the build if our decoder disagrees with Pillow on real data."""
    try:
        from PIL import Image
    except ImportError:
        return
    width, height, channels, pixels = decoded
    mode = "RGBA" if channels == 4 else "RGB"
    im = Image.open(io.BytesIO(png_bytes)).convert(mode)
    if im.size != (width, height) or im.tobytes() != bytes(pixels):
        raise AssertionError("png_decode disagrees with Pillow on real data")


def slice_sprites(wiki, meta, assets_dir):
    """Cut each sprite strip into square frame PNGs under assets/. Returns path -> [frame paths]."""
    result = {}
    for rel, info in sorted(meta.items()):
        blob = wiki.jar.read("assets/%s/%s" % (MOD, rel[len("assets/"):]))
        decoded = png_decode(blob)
        if decoded is None:
            continue
        _ground_truth(blob, decoded)
        width, height, channels, pixels = decoded
        size, count = info["size"], info["frames"]
        stride = size * channels          # bytes per row of one frame
        frame_bytes = size * stride       # bytes per whole frame
        if channels * size * size != frame_bytes:
            continue
        paths = []
        for index in range(count):
            if info["layout"] == "v":
                frame = pixels[index * frame_bytes:(index + 1) * frame_bytes]
            else:
                frame = bytearray()
                for y in range(size):
                    start = (y * width + index * size) * channels
                    frame += pixels[start:start + stride]
                frame = bytes(frame)
            if len(frame) != frame_bytes:
                continue
            out_rel = rel.replace(".png", "_f%d.png" % index)
            out_path = os.path.join(assets_dir, out_rel[len("assets/"):].replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            blob = png_encode(size, size, channels, frame)
            with open(out_path, "wb") as handle:
                handle.write(blob)
            # self-check: the written frame must decode back to exactly these pixels
            verify = png_decode(blob)
            if verify is None or verify[3] != frame or verify[:3] != (size, size, channels):
                raise AssertionError("sliced frame %s did not round-trip" % out_rel)
            paths.append(out_rel)
        if len(paths) == count:
            result[rel] = paths
            print("  sliced %-42s %d frames of %dx%d" % (rel.split("/")[-1], count, size, size))
    return result


def copy_assets(wiki, skip=()):
    dest = os.path.join(ROOT, "assets")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    count = 0
    prefix = "assets/%s/textures/" % MOD
    for name in wiki.names:
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        if "assets/" + name[len("assets/%s/" % MOD):] in skip:
            continue
        rel = name[len("assets/%s/" % MOD):]
        out = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as handle:
            handle.write(wiki.jar.read(name))
        count += 1
    # mod logo lives outside the textures folder
    logo = "%s.png" % MOD
    if logo in wiki.names:
        with open(os.path.join(dest, logo), "wb") as handle:
            handle.write(wiki.jar.read(logo))
        count += 1
    return count


# ---------------------------------------------------------------- main

def write_placeholder(assets_dir):
    """Shared texture for items whose icon is not in the mod jar (vanilla, other mods)."""
    size, channels = 16, 4
    fill, edge = (26, 31, 38, 255), (58, 67, 80, 255)
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            pixels += bytes(edge if x in (0, size - 1) or y in (0, size - 1) else fill)
    rel = "assets/textures/item/_placeholder.png"
    out = os.path.join(assets_dir, rel[len("assets/"):].replace("/", os.sep))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as handle:
        handle.write(png_encode(size, size, channels, bytes(pixels)))
    return rel


def model_texture(jar, ns, model_path):
    """First texture a model references; block models prefer side/top over particle."""
    try:
        model = json.loads(jar.read("assets/%s/models/%s.json" % (ns, model_path)))
    except (KeyError, ValueError):
        return None
    textures = model.get("textures") or {}
    for key in ("layer0", "all", "side", "north", "front", "up", "top", "particle"):
        tex = textures.get(key)
        if isinstance(tex, str) and not tex.startswith("#"):
            return tex
    for tex in textures.values():
        if isinstance(tex, str) and not tex.startswith("#"):
            return tex
    return None


def extra_icon_entry(jar, names, ns, path):
    """Where an item's texture lives in a foreign jar, or None."""
    for entry in (
        "assets/%s/textures/item/%s.png" % (ns, path),
        "assets/%s/textures/item/%s_0.png" % (ns, path),
        "assets/%s/textures/item/%s_00.png" % (ns, path),
        "assets/%s/textures/block/%s.png" % (ns, path),
        "assets/%s/textures/entity/%s.png" % (ns, path),
    ):
        if entry in names:
            return entry
    for model_path in ("item/" + path, "block/" + path):
        tex = model_texture(jar, ns, model_path)
        if tex:
            tex_ns, tex_path = tex.split(":", 1) if ":" in tex else (ns, tex)
            entry = "assets/%s/textures/%s.png" % (tex_ns, tex_path)
            if entry in names:
                return entry
    prefix = "assets/%s/textures/entity/%s/" % (ns, path)
    hits = sorted(n for n in names if n.startswith(prefix) and n.endswith(".png"))
    return hits[0] if hits else None


EXTRA_MANIFEST = os.path.join(HERE, "extra-icons.json")
EXTRA_CACHE = os.path.join(HERE, "extra-cache")


def load_manifest():
    try:
        with open(EXTRA_MANIFEST, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_manifest(manifest):
    with open(EXTRA_MANIFEST, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)


def cache_externals(manifest, assets_dir):
    """Snapshot extracted external textures so jar-less runs keep them."""
    if not manifest:
        return 0
    os.makedirs(EXTRA_CACHE, exist_ok=True)
    saved = 0
    for entry in manifest.values():
        src = os.path.join(assets_dir, entry["out"][len("assets/"):].replace("/", os.sep))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(EXTRA_CACHE, os.path.basename(entry["out"])))
            saved += 1
    return saved


def restore_cached_externals(manifest, assets_dir):
    """Restore the snapshot after the assets directory was wiped."""
    restored = 0
    for entry in manifest.values():
        src = os.path.join(EXTRA_CACHE, os.path.basename(entry["out"]))
        if not os.path.exists(src):
            continue
        dst = os.path.join(assets_dir, entry["out"][len("assets/"):].replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1
    return restored


def resolve_extra_icons(items, jar_paths, assets_dir, manifest):
    """Fill icons for items Tide references but doesn't ship (vanilla, other mods)."""
    missing = [i for i in items if not i["icon"]]
    if not missing:
        return 0
    wanted = {i["id"]: i for i in missing}
    resolved = 0
    for jar_path in jar_paths:
        if not os.path.exists(jar_path):
            print("  extra jar not found, skipped: %s" % jar_path)
            continue
        with zipfile.ZipFile(jar_path) as jar:
            names = set(jar.namelist())
            for item_id, item in sorted(wanted.items()):
                if item["icon"]:
                    continue
                ns, path = item_id.split(":", 1)
                entry = extra_icon_entry(jar, names, ns, path)
                if not entry:
                    continue
                out_rel = "assets/textures/item/%s_%s.png" % (ns, path)
                out_path = os.path.join(assets_dir, out_rel[len("assets/"):].replace("/", os.sep))
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                with open(out_path, "wb") as handle:
                    handle.write(jar.read(entry))
                item["icon"] = out_rel
                manifest[item_id] = {"out": out_rel, "jar": os.path.basename(jar_path), "entry": entry}
                resolved += 1
        print("  extra jar: %s" % os.path.basename(jar_path))
    # jars absent or lacking a texture: fall back to the cached copy
    for item_id, item in wanted.items():
        if not item["icon"] and item_id in manifest:
            item["icon"] = manifest[item_id]["out"]
            resolved += 1
    if resolved:
        print("icons from extra jars  %d" % resolved)
    return resolved


def main():
    if not os.path.exists(JAR):
        sys.exit("Jar not found: %s" % JAR)
    wiki = Wiki(JAR)

    fish = build_fish(wiki)
    recipes = build_recipes(wiki)
    baits = build_baits(wiki)
    lines = build_lines(wiki)
    blocks = build_blocks(wiki)
    items = build_items(wiki, fish, baits, lines)
    crates = build_loot(wiki, "data/%s/loot_table/chests/crates/" % MOD, "crate")
    fishing_loot = build_loot(wiki, "data/%s/loot_table/gameplay/fishing/" % MOD, "fishing loot")
    advancements = build_advancements(wiki)
    compat = build_compat(wiki)
    commands = build_commands(wiki)
    config = build_config(wiki)
    vanilla_fish = build_vanilla_fish(wiki)
    rod_bonuses = build_rod_bonuses(wiki)
    extras = build_extras(wiki)
    rods = build_rods(wiki)
    hooks = build_hooks(wiki)
    sprites = sprite_meta(wiki)
    manifest = load_manifest()
    cache_externals(manifest, os.path.join(ROOT, "assets"))
    textures = copy_assets(wiki, skip=set(sprites))
    restore_cached_externals(manifest, os.path.join(ROOT, "assets"))
    sprite_frames = slice_sprites(wiki, sprites, os.path.join(ROOT, "assets"))
    sprites = sprite_frames
    placeholder = write_placeholder(os.path.join(ROOT, "assets"))
    resolve_extra_icons(items, EXTRA_JARS, os.path.join(ROOT, "assets"), manifest)
    save_manifest(manifest)
    render_block_icons(items, wiki, blocks, os.path.join(ROOT, "assets"))

    groups = {}
    for f in fish:
        groups[f["group"]] = groups.get(f["group"], 0) + 1
    rarities = {}
    for f in fish:
        rarities[f["rarity"]] = rarities.get(f["rarity"], 0) + 1

    data = {
        "meta": {
            "id": MOD,
            "name": wiki.meta.get("name"),
            "version": wiki.meta.get("version"),
            "author": ", ".join(wiki.meta.get("authors", [])),
            "license": wiki.meta.get("license"),
            "description": wiki.meta.get("description"),
            "homepage": wiki.meta.get("contact", {}).get("homepage"),
            "issues": wiki.meta.get("contact", {}).get("issues"),
            "minecraft": wiki.meta.get("depends", {}).get("minecraft"),
            "depends": sorted(wiki.meta.get("depends", {}).keys()),
            "icon": "assets/%s.png" % MOD,
        },
        "stats": {
            "fish": len(fish),
            "groups": groups,
            "rarities": rarities,
            "recipes": len(recipes),
            "items": len(items),
            "blocks": len(blocks),
            "baits": len(baits),
            "lines": len(lines),
            "rods": len(rods),
            "hooks": len(hooks),
            "crates": len(crates),
            "advancements": len(advancements),
            "textures": textures,
        },
        "fish": fish,
        "vanillaFish": vanilla_fish,
        "rodBonuses": rod_bonuses,
        "extras": extras,
        "rods": rods,
        "hooks": hooks,
        "mechanics": MECHANICS,
        "spriteMeta": sprites,
        "items": items,
        "blocks": blocks,
        "recipes": recipes,
        "baits": baits,
        "lines": lines,
        "crates": crates,
        "fishingLoot": fishing_loot,
        "advancements": advancements,
        "compat": compat,
        "commands": commands,
        "config": config,
    }

    out = os.path.join(ROOT, "data.js")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("// Generated by tools/extract.py from %s\n" % os.path.basename(JAR))
        handle.write("window.TIDE = ")
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write(";\n")

    # cache-bust: a fresh data.js URL so the browser never serves stale data
    index_path = os.path.join(ROOT, "index.html")
    with open(index_path, encoding="utf-8") as handle:
        html = handle.read()
    stamp = int(time.time())
    html = re.sub(r'<script src="data\.js(\?v=\d+)?"', '<script src="data.js?v=%d"' % stamp, html)
    with open(index_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    # sanity checks
    assert fish, "no fish parsed"
    assert recipes, "no recipes parsed"
    assert all(f["where"] or f["group"] for f in fish), "fish without conditions"

    print("fish         %3d  %s" % (len(fish), groups))
    print("rarity       %s" % rarities)
    print("recipes      %3d" % len(recipes))
    print("items        %3d" % len(items))
    print("blocks       %3d" % len(blocks))
    print("rods/hooks   %3d / %d" % (len(rods), len(hooks)))
    print("baits        %3d" % len(baits))
    print("lines        %3d" % len(lines))
    print("crates       %3d" % len(crates))
    print("advancements %3d" % len(advancements))
    print("textures     %3d copied" % textures)
    print("data.js      %.1f KB" % (os.path.getsize(out) / 1024))
    missing = [f["id"] for f in fish if not f["icon"]]
    print("fish without icon: %d %s" % (len(missing), missing[:5]))
    print("fish without size stats: %d" % len([f for f in fish if not f["sizeStats"]]))
    print("sprite strips   %3d  %s" % (len(sprites), sorted(sprites)))

if __name__ == "__main__":
    main()
