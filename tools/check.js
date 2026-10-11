/* Smoke check: render every route (and every fish + recipe card) with a stub DOM.
   Run: node tools/check.js   -- fails loudly if any page throws. */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..");

function element(tag) {
  return {
    tag: tag || "div",
    innerHTML: "",
    value: "",
    dataset: {},
    classList: { add() {}, remove() {} },
    focus() {},
    setSelectionRange() {},
    querySelector() { return null; },
    insertBefore(node) { if (app.children.indexOf(node) < 0) app.children.push(node); },
    appendChild(node) { if (app.children.indexOf(node) < 0) app.children.push(node); },
  };
}

/* tiny DOM: app > (nav, main) */
const app = element("div");
app.children = [];

let main = null;
const listeners = {};
const document = {
  activeElement: null,
  getElementById() { return app; },
  querySelector(sel) {
    const tag = sel.replace(/^\[?data-nav.*$/, "nav");
    if (tag === "main") return main;
    return app.children.find((child) => child.tag === tag) || null;
  },
  createElement() { return element(); },
  querySelectorAll() { return []; },
  addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
};

const window = {
  TIDE: null,
  location: { hash: "#/" },
  scrollTo() {},
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};

const context = vm.createContext({
  window, document, location: window.location, console,
  setInterval() { return 0; },
});
vm.runInContext(fs.readFileSync(path.join(root, "data.js"), "utf8"), context);

window.TIDE = context.window.TIDE;
const T = window.TIDE;

/* re-run app.js per route so the router renders with that hash */
function renderAt(hash) {
  window.location.hash = hash;
  app.children = [];
  main = element("main");
  vm.runInContext(fs.readFileSync(path.join(root, "app.js"), "utf8"), context);
  document.querySelector = ((real) => (sel) => {
    if (sel === "main") return main;
    return app.children.find((child) => child.tag === sel.replace(/^\./, "")) || null;
  })(document.querySelector);
  main = app.children.find((child) => child.tag === "main") || main;
  return main.innerHTML;
}

let failures = 0;
function check(label, html, needle) {
  const ok = html.length > 300 && (!needle || html.indexOf(needle) >= 0);
  if (!ok) { failures++; console.log("FAIL  " + label + "  (len " + html.length + ")"); }
  else console.log("ok    " + label + "  " + html.length + " chars");
}

/* No page may print a computed garbage value. */
try {
  const every = ["#/", "#/fish", "#/loadout", "#/items", "#/blocks", "#/recipes", "#/crates",
    "#/minigame", "#/stats", "#/config", "#/commands", "#/advancements", "#/compat"]
    .concat(T.fish.map((f) => "#/fish/" + f.id), T.fish.map((f) => "#/loadout/" + f.id));
  const junk = [];
  for (const route of every) {
    const html = renderAt(route);
    const hit = html.match(/NaN|undefined|\[object Object\]/);
    if (hit) junk.push(route + " -> " + hit[0]);
  }
  if (junk.length) { failures++; console.log("FAIL  NaN/undefined rendered on " + junk.slice(0, 6).join(", ")); }
  else console.log("ok    no NaN/undefined on any of " + every.length + " routes");

  // config defaults are values, not display names
  const stats = renderAt("#/stats");
  check("crate table uses real config values", stats, "crateWeight 4");
  if (/Crate weight[\s\S]{0,200}NaN/.test(stats)) { failures++; console.log("FAIL  crate weight table still NaN"); }
  else console.log("ok    crate weight table renders numbers, not display names");
} catch (error) {
  failures++;
  console.log("FAIL  junk-value checks threw: " + error.message);
}

/* Biome names must be real biome names, not pluralised tag leftovers. */
try {
  const names = [...new Set(T.fish.flatMap((f) => f.biomes || []))];
  const bad = names.filter((n) => /(ss|ys|hs)$/.test(n));
  if (bad.length) { failures++; console.log("FAIL  malformed biome names: " + bad.join(", ")); }
  else console.log("ok    " + names.length + " biome names, none malformed");

  const coelBiomes = T.fish.find((f) => f.name === "Coelacanth");
  if (!(coelBiomes.biomeTags || []).includes("#minecraft:is_deep_ocean")) {
    failures++; console.log("FAIL  coelacanth lost its biome tag");
  } else console.log("ok    unexpandable biome tags are recorded, not silently renamed");

  const opt = T.fish.filter((f) => (f.modBiomes || []).length);
  if (!opt.length) { failures++; console.log("FAIL  no fish flagged as needing another mod for its biomes"); }
  else console.log("ok    " + opt.length + " fish flag biomes that need another mod installed");
} catch (error) {
  failures++;
  console.log("FAIL  biome checks threw: " + error.message);
}

/* Every fish must resolve to a behaviour: absent means SINE, not "none". */
try {
  const missing = T.fish.filter((f) => !f.behavior).map((f) => f.id);
  if (missing.length) { failures++; console.log("FAIL  fish without a behaviour: " + missing.join(", ")); }
  else console.log("ok    all " + T.fish.length + " fish carry an explicit behaviour");

  const stats = renderAt("#/stats");
  check("behaviour table lists all six shapes", stats, "sine");
} catch (error) {
  failures++;
  console.log("FAIL  behaviour checks threw: " + error.message);
}

/* node tools/check.js --dump "#/loadout/tide:coelacanth"  -> page as plain text */
if (process.argv[2] === "--dump") {
  const html = renderAt(process.argv[3] || "#/");
  console.log(html.replace(/<[^>]+>/g, " ").replace(/&[a-z]+;|&#\d+;/g, " ").replace(/[ \t]+/g, " ").replace(/\n\s*\n/g, "\n"));
  process.exit(0);
}

const routes = ["#/", "#/fish", "#/loadout", "#/items", "#/blocks", "#/recipes", "#/crates",
  "#/minigame", "#/stats", "#/config", "#/commands", "#/advancements", "#/compat"];
for (const route of routes) {
  try {
    check(route, renderAt(route));
  } catch (error) {
    failures++;
    console.log("FAIL  " + route + "  threw: " + error.message);
  }
}

try {
  const html = renderAt("#/fish");
  check("fish page has cards", html, T.fish[0].name);
  const detail = renderAt("#/fish/tide:coelacanth");
  check("fish detail", detail, "Where to find it");
  for (const fish of T.fish) renderAt("#/fish/" + fish.id);
  console.log("ok    rendered " + T.fish.length + " fish detail pages");
} catch (error) {
  failures++;
  console.log("FAIL  fish pages threw: " + error.message);
}

try {
  const html = renderAt("#/recipes");
  if (html.indexOf("Magnetic Bait") < 0) { failures++; console.log("FAIL  recipes missing Magnetic Bait"); }
  else console.log("ok    recipes render ingredient grids");
} catch (error) {
  failures++;
  console.log("FAIL  recipes threw: " + error.message);
}

try {
  const html = renderAt("#/config");
  check("config shows values", html, "Rod Durability Multiplier");
} catch (error) {
  failures++;
  console.log("FAIL  config threw: " + error.message);
}

try {
  const html = renderAt("#/stats");
  check("stats formulas", html, "effective weight = selection_weight + selection_quality x fishing luck");
  check("stats 85-luck entry", html, "85 - luck");
  check("stats coelacanth example", html, "Coelacanth");
  check("stats crate formula", html, "crateWeight");
  for (const fish of T.fish) {
    const page = renderAt("#/stats");
    if (page.indexOf("undefined") >= 0) { failures++; console.log("FAIL  stats page prints undefined"); break; }
  }
} catch (error) {
  failures++;
  console.log("FAIL  stats threw: " + error.message);
}

try {
  const html = renderAt("#/loadout/tide:coelacanth");
  check("loadout coelacanth", html, "Recommended loadout");
  check("loadout lists hook", html, "Twilight Hook");
  check("loadout requirement", html, "Time window");
  for (const fish of T.fish) {
    const page = renderAt("#/loadout/" + fish.id);
    if (page.length < 400) { failures++; console.log("FAIL  loadout thin for " + fish.id); break; }
  }
  console.log("ok    rendered loadouts for " + T.fish.length + " fish");
  const lava = renderAt("#/loadout/tide:magma_mackerel");
  check("loadout lava fish", lava, "lava");
  const voided = renderAt("#/loadout/tide:voidseeker");
  check("loadout void fish", voided, "Hook of the Depths");
  const midas = renderAt("#/loadout/tide:midas_fish");
  check("loadout line icon", midas, "assets/textures/item/diamond_line.png");
  check("loadout bait icon", midas, "assets/textures/item/lucky_bait.png");
  const missing = [];
  for (const fish of T.fish) {
    const page = renderAt("#/loadout/" + fish.id);
    const plan = page.slice(page.indexOf("Recommended loadout"), page.indexOf("Alternative builds"));
    if (/<span class="icon( |")/.test(plan)) missing.push(fish.id);
  }
  if (missing.length) { failures++; console.log("FAIL  loadout icons missing for " + missing.join(", ")); }
  else console.log("ok    every loadout shows an icon for all four gear slots");

  // A temperature fish gated by a time window cannot use the one hook slot twice.
  const contested = T.fish.filter((f) => f.temperature && f.timeRanges.length);
  const badHook = [];
  for (const fish of contested) {
    const page = renderAt("#/loadout/" + fish.id);
    const plan = page.slice(page.indexOf("Recommended loadout"), page.indexOf("Alternative builds"));
    if (plan.indexOf("twilight_hook.png") >= 0) badHook.push(fish.id);
    else if (page.indexOf("does nothing for temperature") < 0) badHook.push(fish.id + " (no warning)");
  }
  if (badHook.length) { failures++; console.log("FAIL  hook-slot conflict not resolved for " + badHook.join(", ")); }
  else console.log("ok    " + contested.length + " temperature+time fish keep the hook slot free and warn about it");

  // Every temperature fish must offer both direction hooks, never guess one.
  const guessed = [];
  for (const fish of T.fish.filter((f) => f.temperature)) {
    const page = renderAt("#/loadout/" + fish.id);
    if (page.indexOf("fiery_hook.png") < 0 || page.indexOf("permafrost_hook.png") < 0) guessed.push(fish.id);
  }
  if (guessed.length) { failures++; console.log("FAIL  temperature fish missing a hook icon for " + guessed.join(", ")); }
  else console.log("ok    every temperature loadout shows both Fiery and Permafrost Hook icons");

  // A biome gate can pin a fish's own temperature to the edge of its range (all ocean
  // biomes read 0.5). The Coelacanth is the known case; the note must appear and must
  // point at the hook that moves the reading back toward preferred.
  const coel = renderAt("#/fish/tide:coelacanth");
  check("coelacanth warns about the biome temperature trap", coel, "Temperature trap.");
  if (coel.indexOf("Permafrost") < 0) { failures++; console.log("FAIL  coelacanth trap points the wrong way (deep ocean 30.2 C > preferred 24.0 C, needs Permafrost)"); }
  else console.log("ok    coelacanth trap points at Permafrost (cools 30.2 C toward 24.0 C)");
} catch (error) {
  failures++;
  console.log("FAIL  loadout threw: " + error.message);
}

try {
  const strips = T.spriteMeta || {};
  const keys = Object.keys(strips).sort();
  const expected = ["assets/textures/item/midas_fish.png",
    "assets/textures/item/midas_fish_shiny.png",
    "assets/textures/item/shooting_starfish.png"];
  if (JSON.stringify(keys) !== JSON.stringify(expected)) {
    failures++;
    console.log("FAIL  spriteMeta keys: " + keys.join(", "));
  } else if (!keys.every((k) => Array.isArray(strips[k]) && strips[k].length === 3)) {
    failures++;
    console.log("FAIL  spriteMeta frame lists wrong");
  } else {
    console.log("ok    spriteMeta has the 3 expected 3-frame strips");
  }

  const absent = keys.flatMap((k) => strips[k]).filter((p) => !fs.existsSync(path.join(root, p)));
  if (absent.length) { failures++; console.log("FAIL  frame files missing: " + absent.join(", ")); }
  else console.log("ok    all 9 sliced frame files exist on disk");

  const badDims = keys.flatMap((k) => strips[k]).filter((p) => {
    const head = fs.readFileSync(path.join(root, p)).subarray(0, 26);
    const width = head.readUInt32BE(16), height = head.readUInt32BE(20);
    return width !== 16 || height !== 16 || head.readUInt8(24) !== 8;
  });
  if (badDims.length) { failures++; console.log("FAIL  frame dimensions wrong: " + badDims.join(", ")); }
  else console.log("ok    every frame file is a square 8-bit PNG");

  const stripsOnDisk = keys.filter((k) => fs.existsSync(path.join(root, k)));
  if (stripsOnDisk.length) { failures++; console.log("FAIL  raw strips should not be copied: " + stripsOnDisk.join(", ")); }
  else console.log("ok    raw strip files are not copied");

  const midas = renderAt("#/fish/tide:midas_fish");
  check("midas fish flips frames", midas, 'data-sprite="assets/textures/item/midas_fish_f0.png|');
  if (midas.indexOf("animation-name") >= 0 || midas.indexOf("--frames") >= 0) {
    failures++; console.log("FAIL  markup still carries CSS animation leftovers");
  }
  const star = renderAt("#/fish/tide:shooting_starfish");
  check("shooting starfish flips frames", star, "shooting_starfish_f0.png");
  const css = fs.readFileSync(path.join(root, "style.css"), "utf8");
  if (/animation-name:\s*sprite|@keyframes\s+sprite/.test(css)) {
    failures++; console.log("FAIL  style.css still animates sprite strips with CSS");
  } else {
    console.log("ok    style.css has no CSS sprite animation left");
  }
  const leaking = T.fish.filter((f) => renderAt("#/fish/" + f.id).indexOf("data-sprite") >= 0)
    .filter((f) => f.id !== "tide:midas_fish" && f.id !== "tide:shooting_starfish");
  if (leaking.length) { failures++; console.log("FAIL  sprite wrapper leaked to " + leaking.map((f) => f.id).join(", ")); }
  else console.log("ok    only the two strip fish use the sprite wrapper");
} catch (error) {
  failures++;
  console.log("FAIL  sprite checks threw: " + error.message);
}

try {
  const placeholder = "assets/textures/item/_placeholder.png";
  if (!fs.existsSync(path.join(root, placeholder))) {
    failures++; console.log("FAIL  placeholder texture missing");
  } else console.log("ok    placeholder texture exists");

  const external = [
    ["minecraft:fishing_rod", "minecraft_fishing_rod.png"],
    ["minecraft:cod", "minecraft_cod.png"],
    ["minecraft:clock", "minecraft_clock.png"],
    ["minecraft:grass_block", "minecraft_grass_block.png"],
    ["minecraft:glow_lichen", "minecraft_glow_lichen.png"],
    ["minecraft:enchanted_golden_apple", "minecraft_enchanted_golden_apple.png"],
    ["hybrid_aquatic:magnetic_hook", "hybrid_aquatic_magnetic_hook.png"],
    ["hybrid_aquatic:ominous_hook", "hybrid_aquatic_ominous_hook.png"],
    ["naturalist:butterfly", "naturalist_butterfly.png"],
    ["naturalist:caterpillar", "naturalist_caterpillar.png"],
    ["fishofthieves:earthworms", "fishofthieves_earthworms.png"],
  ];
  let externalChecked = 0;
  for (const [id, icon] of external) {
    const item = T.items.find((i) => i.id === id);
    if (!item || !item.icon) continue;  // extra jar not passed this run
    externalChecked++;
    if (!item.icon.endsWith(icon)) {
      failures++; console.log("FAIL  %s icon should end in %s (got %s)", id, icon, item.icon);
    }
  }
  console.log("ok    external icons wired (%d checked, rest placeholder until jars passed)", externalChecked);

  const sprites = T.spriteMeta || {};
  const broken = T.items.filter((i) => i.icon && !sprites[i.icon] && !fs.existsSync(path.join(root, i.icon)));
  if (broken.length) { failures++; console.log("FAIL  icon files missing: " + broken.map((i) => i.id).join(", ")); }
  else console.log("ok    every non-null icon exists on disk (sprite strips animate from sliced frames)");

  const iconless = T.items.filter((i) => !i.icon);
  console.log("ok    %d item(s) without a texture (extra jars absent or no texture in jar)", iconless.length);

  const itemsPage = renderAt("#/items");
  check("placeholder still renders for chain", itemsPage, 'src="' + placeholder + '"');
  const emptyBoxes = (itemsPage.match(/<span class="icon(?! sprite)[^"]*"[^>]*><\/span>/g) || []).length;
  if (emptyBoxes) { failures++; console.log("FAIL  " + emptyBoxes + " empty icon boxes remain"); }
  else console.log("ok    no empty icon boxes on the items page");

  const planner = renderAt("#/loadout/tide:coelacanth");
  check("loadout still renders real rod icons", planner, "fishing_rod.png");
} catch (error) {
  failures++;
  console.log("FAIL  placeholder checks threw: " + error.message);
}

try {
  const isoBlocks = [
    "wooden_crate", "purpur_crate", "obsidian_crate", "angling_table",
    "fish_display", "lunar_calendar", "weather_radio",
  ];
  for (const name of isoBlocks) {
    const rel = "assets/textures/block/tide_%s_icon.png".replace("%s", name);
    const item = T.items.find((i) => i.id === "tide:" + name);
    if (!item || item.icon !== rel) {
      failures++; console.log("FAIL  tide:%s icon should be the isometric render (got %s)", name, item && item.icon);
    } else if (!fs.existsSync(path.join(root, rel))) {
      failures++; console.log("FAIL  isometric icon missing on disk: " + rel);
    } else {
      const head = fs.readFileSync(path.join(root, rel)).subarray(0, 24);
      if (head.readUInt32BE(16) !== 32 || head.readUInt32BE(20) !== 32) {
        failures++; console.log("FAIL  isometric icon is not 32x32: " + rel);
      }
    }
  }
  console.log("ok    all 7 tide block items use isometric 32x32 renders");

  const blockEntries = T.blocks || [];
  const blockBad = blockEntries.filter((b) => b.icon && b.icon.endsWith(".png") &&
    !fs.existsSync(path.join(root, b.icon)));
  if (blockBad.length) { failures++; console.log("FAIL  block icons missing: " + blockBad.map((b) => b.id).join(", ")); }
  else console.log("ok    every block icon exists on disk");

  const radio = renderAt("#/items");
  check("items page shows the 3D weather radio", radio, "tide_weather_radio_icon.png");
} catch (error) {
  failures++;
  console.log("FAIL  isometric icon checks threw: " + error.message);
}

console.log(failures ? "\n" + failures + " failure(s)" : "\nall checks passed");
process.exit(failures ? 1 : 0);
