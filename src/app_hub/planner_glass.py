"""PLANNER glass for GTK apps outside Harbor (App Hub, Draco, Astra, SysMic).

Vendored copy: the master lives in ~/Music/apps/glass-theme/planner_glass.py;
spec in PLANNER-SPEC.md beside it. Reads the shared glass files Harbor
Settings writes (~/.config/harbor/glass*, Fusion Desk's glass-* settings)
with Harbor's resolve chain, and builds GTK CSS for the Daily Routine Planner
look. Works with GTK4/libadwaita (css_gtk4) and GTK3 (css_gtk3).

    look = planner_glass.Look("draco", on_change)
    look.active      -> True when this app paints PLANNER
    look.choice()    -> "follow" | "planner" | "normal"
    look.set_choice("planner")
    look.css_gtk4(dark)

Layers (each one only uses the ones above it):
    1. files    read_file / write_file: the shared files in ~/.config/harbor/
                (a failed read or write is logged, writes are atomic)
    2. spec     set_value / parse / resolve: files + Fusion Desk -> one glass
                dict; resolve_explained also says which layer decided it
    3. tokens   colour helpers and tokens(): a spec -> named colours
    4. CSS      css_gtk4 / css_gtk3: tokens -> GTK CSS
    5. Look     one app's live state: watches the files, calls on_change

Finding problems:
    python3 planner_glass.py --check [app ...]   which layer decides each app's
                                                 look, the values, and every
                                                 ignored line in the files
    PLANNER_GLASS_DEBUG=1 <app>                  log each resolve that changes
"""
import colorsys
import os
import re
import sys

from gi.repository import Gio, GLib

FUSION_SCHEMA = "org.gnome.shell.extensions.fusion-desk"
FUSION_UUID = "fusion-desk@hafij-deu.github.io"
STRENGTHS = ("style-strength", "shine-strength", "rim-strength", "glare-strength",
             "shadow-strength", "light-strength")
LUMINA_KEYS = ("lumina", "lumina-colours", "lumina-colour", "lumina-intensity")
KEYS = ("effect", "material", "style", "opacity", "rim", "glare", "shadow",
        "light-follows") + STRENGTHS + LUMINA_KEYS
EFFECTS = ("OFF", "SOFT", "STRONG")
MATERIALS = ("GLASS", "GLOSS")
STYLES = ("CLEAR", "FROSTED", "GEL", "TINTED", "SMOKE", "HALO", "GLOW", "PLANNER")
# Lumina (style PLANNER) options, global or per app like every other glass key:
#   lumina=CALM (the v3 indigo look) | COLOURFUL (colour sets / accent / custom)
#   lumina-colours=AURORA|SUNSET|OCEAN|CANDY|FOREST|ACCENT|CUSTOM
#   lumina-colour=#rrggbb (for CUSTOM)   lumina-intensity=20..100
LUMINA_STYLES = ("CALM", "COLOURFUL")
LUMINA_SETS = {"AURORA": ("#ec4899", "#8b5cf6", "#38bdf8"), "SUNSET": ("#f97316", "#ec4899", "#8b5cf6"),
               "OCEAN": ("#06b6d4", "#3b82f6", "#6366f1"), "CANDY": ("#f472b6", "#a78bfa", "#34d399"),
               "FOREST": ("#22c55e", "#14b8a6", "#0ea5e9")}
LUMINA_COLOURS = tuple(LUMINA_SETS) + ("ACCENT", "CUSTOM")
# GNOME 47+ accent-color names (org.gnome.desktop.interface)
GNOME_ACCENTS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800",
                 "orange": "#ed5b00", "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac",
                 "slate": "#6f8396"}
DEFAULT = {"effect": "STRONG", "material": "GLASS", "style": "FROSTED", "opacity": 75,
           "rim": True, "glare": True, "shadow": False, "light-follows": False,
           **{k: 100 for k in STRENGTHS},
           "lumina": "CALM", "lumina-colours": "AURORA", "lumina-colour": "#8b5cf6", "lumina-intensity": 70}
OFF = dict(DEFAULT, effect="OFF", opacity=100)
PLANNER_SPEC = dict(DEFAULT, style="PLANNER", opacity=100, glare=False, shadow=True)
CHOICES = (("follow", "Follow Harbor Settings"), ("planner", "Lumina Glass"),
           ("normal", "Normal"))
# Old one-word file contents, still understood (any case).
LEGACY = {"off": OFF,
          "glossy": dict(DEFAULT, effect="SOFT", material="GLOSS", style="GEL", opacity=100),
          "liquid": dict(DEFAULT, style="CLEAR", opacity=80)}
FOLLOW_WORDS = ("", "global", "harbor")
DEBUG = bool(os.environ.get("PLANNER_GLASS_DEBUG"))
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_warned = set()


def log(msg, once_key=None):
    """One line on stderr; with once_key, only the first time per process."""
    if once_key is not None:
        if once_key in _warned:
            return
        _warned.add(once_key)
    print(f"planner_glass: {msg}", file=sys.stderr)


# ---- 1. files: ~/.config/harbor/glass* (same chain as Harbor Settings) -------

def config_path(name):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "harbor", name)


def read_file(name):
    """The file's text, stripped; None when it is missing or unreadable."""
    path = config_path(name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        log(f"cannot read {path}: {e}; ignoring it", once_key=("read", path))
        return None


def write_file(name, text):
    """Write atomically (temp file + rename), so a reader never sees a
    half-written file. Returns False (and logs) when it fails."""
    path = config_path(name)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"cannot write {path}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


# ---- 2. spec: parse values, resolve one app's glass --------------------------

def _number(value, low, high):
    try:
        return max(low, min(high, int(value, 10)))
    except ValueError:
        return None


def set_value(spec, key, value):
    """Set one glass value from text; returns False (spec unchanged) for an
    unknown key or a bad value."""
    key = key.strip().removeprefix("glass-")
    value = str(value).strip().strip("'")
    up = value.upper()
    if (key == "effect" and up in EFFECTS or key == "material" and up in MATERIALS
            or key == "style" and up in STYLES or key == "lumina" and up in LUMINA_STYLES
            or key == "lumina-colours" and up in LUMINA_COLOURS):
        spec[key] = up
    elif key in ("opacity", "lumina-intensity") or key in STRENGTHS:
        low, high = {"opacity": (10, 100), "lumina-intensity": (20, 100)}.get(key, (0, 200))
        n = _number(value, low, high) if value.isascii() else None
        if n is None:
            return False
        spec[key] = n
    elif key in ("rim", "glare", "shadow", "light-follows") and value.lower() in ("true", "false"):
        spec[key] = value.lower() == "true"
    elif key == "lumina-colour" and _HEX.fullmatch(value):
        spec[key] = value.lower()
    else:
        return False
    return True


def parse_checked(text):
    """(spec, accepted, ignored): the spec plus how many pairs were used and
    the lines that were not (for --check and the logs)."""
    text = (text or "").strip()
    if text.lower() in LEGACY:
        return dict(LEGACY[text.lower()]), 1, []
    spec, accepted, ignored = dict(DEFAULT), 0, []
    for pair in text.replace(",", "\n").splitlines():
        if not pair.strip():
            continue
        if "=" in pair and set_value(spec, *pair.split("=", 1)):
            accepted += 1
        else:
            ignored.append(pair.strip())
    return spec, accepted, ignored


def parse(text):
    return parse_checked(text)[0]


def to_text(spec):
    def show(v):
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    return "".join(f"{k}={show(spec[k])}\n" for k in KEYS)


def fusion_settings():
    for base in (os.path.join(GLib.get_user_data_dir(), "gnome-shell", "extensions"),
                 "/usr/share/gnome-shell/extensions", "/usr/local/share/gnome-shell/extensions"):
        folder = os.path.join(base, FUSION_UUID, "schemas")
        if not os.path.exists(os.path.join(folder, "gschemas.compiled")):
            continue
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(
                folder, Gio.SettingsSchemaSource.get_default(), False)
            schema = source.lookup(FUSION_SCHEMA, False)
        except GLib.Error:
            continue
        if schema is not None:
            return Gio.Settings.new_full(schema, None, None)
    return None


def global_glass(fusion=None, notes=None):
    """Global look: Fusion Desk's glass settings when its schema is installed
    (older builds without the Lumina keys take those from glass-global),
    else the glass-global file."""
    fusion = fusion or fusion_settings()
    text = read_file("glass-global")
    if fusion is not None:
        spec = dict(DEFAULT)
        keys = fusion.props.settings_schema.list_keys()
        missing = []
        for key in KEYS:
            if "glass-" + key in keys:
                set_value(spec, key, fusion.get_value("glass-" + key).print_(False))
            elif key in LUMINA_KEYS:
                missing.append(key)
        if missing and text:
            from_file = parse(text)
            spec.update({k: from_file[k] for k in missing})
            if notes is not None:
                notes.append(f"Fusion Desk has no {', '.join(missing)}: taken from glass-global")
        return spec
    if not text:
        return dict(DEFAULT)
    spec, _n, ignored = parse_checked(text)
    if notes is not None:
        notes.extend(f"glass-global: ignored line {line!r}" for line in ignored)
    return spec


def scope_glass(scope, notes=None):
    """A scope's own values ("harbor" or an app name), or None to follow.
    A file with no usable line also follows (logged), instead of silently
    giving the app the default frosted look."""
    name = "glass" if scope == "harbor" else "glass-" + scope
    text = read_file(name)
    if text is None:
        # missing: follow (the All Harbor apps scope is gone; it follows Global)
        if notes is not None and os.path.exists(config_path(name)):
            notes.append(f"{name} cannot be read: following the global look")
        return None
    if text.lower() in FOLLOW_WORDS:
        return None
    problems = []
    spec, accepted, ignored = parse_checked(text)
    problems += [f"{name}: ignored line {line!r}" for line in ignored]
    if not accepted:
        problems.append(f"{name} has no usable line: following the global look")
    for msg in problems:
        log(msg, once_key=msg)
    if notes is not None:
        notes.extend(problems)
    return spec if accepted else None


def resolve_explained(app, fusion=None):
    """(spec, source, notes): the app's glass, which layer decided it
    (PLANNER_GLASS / glass-<app> / glass / Fusion Desk / glass-global /
    default) and what was ignored on the way."""
    notes = []
    env = os.environ.get("PLANNER_GLASS")
    if env:      # screenshot runs: off|planner|key=value,...
        return (dict(PLANNER_SPEC) if env == "planner" else parse(env)), "PLANNER_GLASS", notes
    for scope, source in ((app, "glass-" + app), ("harbor", "glass")):
        spec = scope_glass(scope, notes)
        if spec is not None:
            return spec, source, notes
    fusion = fusion or fusion_settings()
    if fusion is not None:
        source = "Fusion Desk"
    else:
        source = "glass-global" if read_file("glass-global") else "default"
    return global_glass(fusion, notes), source, notes


_last_debug = {}


def resolve(app, fusion=None):
    spec, source, _notes = resolve_explained(app, fusion)
    if DEBUG and _last_debug.get(app) != (source, spec):
        _last_debug[app] = (source, spec)
        log(f"{app}: style={spec['style']} effect={spec['effect']} lumina={spec['lumina']} "
            f"colours={spec['lumina-colours']} (from {source})")
    return spec


def blurs_windows(fusion=None):
    """Whether Fusion Desk blurs behind the user's app windows (see-through ok)."""
    env = os.environ.get("HARBOR_GLASS_BLUR")
    if env is not None:
        return env != "0"
    fusion = fusion or fusion_settings()
    if fusion is None or "harbor-glass-blur" not in fusion.props.settings_schema.list_keys():
        return False
    if not fusion.get_boolean("harbor-glass-blur"):
        return False
    try:
        shell = Gio.Settings(schema_id="org.gnome.shell")
        return (FUSION_UUID in shell.get_strv("enabled-extensions")
                and FUSION_UUID not in shell.get_strv("disabled-extensions")
                and not shell.get_boolean("disable-user-extensions"))
    except Exception:
        return False


def is_planner(spec):
    return spec["effect"] != "OFF" and spec["style"] == "PLANNER"


# ---- PLANNER tokens and CSS -------------------------------------------------

# Palette v3 ("Planner Glass"): one violet-indigo hue family like the Daily
# Routine Planner app. A soft lavender -> periwinkle backdrop with enough
# colour for glass to read; cards are real glass on it: translucent white
# (dark: smoky violet) with a top-to-bottom sheen, a white rim, a bright top
# edge and a soft coloured shadow. Header = near-solid bar; footer = floating
# glass card.
# ---- 3. tokens: colour helpers and the named colours of a spec ---------------

ACCENT = "linear-gradient(135deg, #6366f1, #8b5cf6)"
ACCENT_SHADOW = "0 8px 18px -6px rgba(99,102,241,0.50)"


def rgb(hexcol):
    h = hexcol.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(hexcol, base, amount):
    c = rgb(hexcol)
    return tuple(round(base[i] + (c[i] - base[i]) * amount) for i in range(3))


def to_hex(rgb):
    return "#%02x%02x%02x" % rgb


def rgba(hexcol, alpha):
    return "rgba(%d,%d,%d,%.3f)" % (*rgb(hexcol), max(0.0, min(1.0, alpha)))


def triad(hexcol):
    """Three neighbouring hues around one colour (-32 deg, itself, +32 deg), kept
    bright enough to read as colour: one accent still gives a colour sweep."""
    r, g, b = (v / 255 for v in rgb(hexcol))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    light, sat = min(max(light, 0.45), 0.62), max(sat, 0.55)

    def at(dh):
        return to_hex(tuple(round(v * 255) for v in colorsys.hls_to_rgb((h + dh) % 1.0, light, sat)))
    return at(-32 / 360), at(0), at(32 / 360)


def system_accent():
    """GNOME's accent colour (Settings -> Appearance), blue when unknown."""
    try:
        iface = Gio.Settings(schema_id="org.gnome.desktop.interface")
        if "accent-color" in iface.props.settings_schema.list_keys():
            return GNOME_ACCENTS.get(iface.get_string("accent-color"), "#3584e4")
    except Exception as e:  # no schema in odd sessions
        log(f"no GNOME accent colour ({e}); using blue", once_key="accent")
    return "#3584e4"


def lumina_colours(spec):
    """(c1, c2, c3) for a COLOURFUL Lumina spec, None for CALM."""
    if not spec or spec.get("lumina") != "COLOURFUL":
        return None
    which = spec.get("lumina-colours", "AURORA")
    if which == "ACCENT":
        return triad(system_accent())
    if which == "CUSTOM":
        return triad(spec.get("lumina-colour", "#8b5cf6"))
    return LUMINA_SETS.get(which, LUMINA_SETS["AURORA"])
CARD_SHADOW = "0 10px 24px -12px rgba(76,61,180,0.30), 0 2px 6px -2px rgba(76,61,180,0.10)"


def soften(dark):
    """Glass effect Soft: flat frosted glass (no sheen, a faint rim, almost no
    shadow) instead of Strong's glossy glass; same colours and corners."""
    if dark:
        return dict(card_img="none", rim="rgba(255,255,255,0.06)", spec="inset 0 0 transparent",
                    shadow="0 2px 8px -6px rgba(0,0,0,0.35)")
    return dict(card="rgba(255,255,255,0.58)", card_img="none", rim="rgba(255,255,255,0.40)",
                spec="inset 0 0 transparent", shadow="0 2px 8px -6px rgba(76,61,180,0.12)")


def tokens(dark, see_through=False, opacity=100, spec=None):
    a = max(0.35, opacity / 100) if see_through else 1.0
    if dark:
        stops = ((26, 21, 51), (27, 31, 69), (20, 26, 58))
        t = dict(card="rgba(40,35,76,0.55)",
                 card_img="linear-gradient(to bottom, rgba(255,255,255,0.10), rgba(255,255,255,0.03))",
                 card_solid="#221e3d", bar="rgba(24,21,48,%.2f)" % (0.80 if see_through else 0.92),
                 view="rgba(20,17,40,0.30)", sidebar="rgba(20,17,40,0.40)",
                 field="rgba(255,255,255,0.06)", popover="#221e3d",
                 rim="rgba(255,255,255,0.13)", spec="inset 0 1px rgba(255,255,255,0.16)",
                 hair="rgba(255,255,255,0.07)", line="rgba(255,255,255,0.14)",
                 text="#eeedf9", soft="#aaa7c8", faint="#75729a", accent_text="#a5b4fc",
                 btn="rgba(255,255,255,0.08)", btn_hover="rgba(255,255,255,0.14)",
                 btn_text="#d4d0ff", btn_line="rgba(255,255,255,0.14)",
                 hover="rgba(255,255,255,0.06)", sel="rgba(129,140,248,0.24)",
                 shadow="0 12px 28px -12px rgba(0,0,0,0.55)")
    else:
        stops = ((232, 218, 255), (220, 224, 255), (203, 220, 255))
        t = dict(card="rgba(255,255,255,0.30)",
                 card_img="linear-gradient(to bottom, rgba(255,255,255,0.72), rgba(255,255,255,0.40))",
                 card_solid="#ffffff", bar="rgba(255,255,255,%.2f)" % (0.78 if see_through else 0.90),
                 view="rgba(255,255,255,0.25)", sidebar="rgba(255,255,255,0.35)",
                 field="rgba(255,255,255,0.55)", popover="#ffffff",
                 rim="rgba(255,255,255,0.85)", spec="inset 0 1px rgba(255,255,255,0.95)",
                 hair="rgba(99,102,241,0.10)", line="rgba(255,255,255,0.90)",
                 text="#1e1b3a", soft="#5b5778", faint="#8f8bab", accent_text="#4f46e5",
                 btn="rgba(255,255,255,0.50)", btn_hover="rgba(255,255,255,0.80)",
                 btn_text="#4338ca", btn_line="rgba(255,255,255,0.90)",
                 hover="rgba(255,255,255,0.45)", sel="rgba(99,102,241,0.14)",
                 shadow=CARD_SHADOW)
    if spec and spec.get("effect") == "SOFT":
        t.update(soften(dark))
    t.update(a1="#6366f1", a2="#8b5cf6", a3="#9333ea", accent=ACCENT, accent_shadow=ACCENT_SHADOW,
             shade="49,46,129")
    cols = lumina_colours(spec)
    if cols:  # COLOURFUL: backdrop, accents and selection from the chosen colours
        c1, c2, c3 = cols
        k = (spec.get("lumina-intensity", 70) or 70) / 100
        # colour goes into a few headings, icons and selections; the backdrop
        # keeps only a faint tint so window edges stay calm (no coloured glows)
        if dark:
            base = (20, 16, 40)
            stops = (mix(c1, base, 0.09 * k), mix(c2, base, 0.08 * k), mix(c3, base, 0.07 * k))
            on = (255, 255, 255)
            t.update(accent_text=to_hex(mix(c2, on, 0.60)), btn_text=to_hex(mix(c2, on, 0.55)),
                     title=to_hex(mix(c2, on, 0.50 + 0.3 * k)), chip_line=rgba(c3, 0.30),
                     sel=rgba(c2, 0.18))
        else:
            base = (250, 248, 255)
            stops = (mix(c1, base, 0.06 * k), mix(c2, base, 0.045 * k), mix(c3, base, 0.055 * k))
            ink = (20, 16, 40)
            t.update(accent_text=to_hex(mix(c2, ink, 0.80)), btn_text=to_hex(mix(c2, ink, 0.78)),
                     title=to_hex(mix(c2, ink, 0.60 + 0.25 * k)),
                     chip_line="rgba(255,255,255,0.85)", sel=rgba(c2, 0.14),
                     hair=rgba(c2, 0.12),
                     # white buttons vanish on white glass cards: a faint wash of
                     # the hue and a hairline keep them visible but quiet
                     btn=rgba(c2, 0.08), btn_hover=rgba(c2, 0.14), btn_line=rgba(c2, 0.22))
        # ONE hue (the middle colour) for heading/icon ink and its chip
        on_ = (255, 255, 255) if dark else (20, 16, 40)
        tone = 0.62 + 0.2 * k if dark else 0.78 + 0.12 * k
        t["i2"] = to_hex(mix(c2, on_, tone))
        t["chip2"] = rgba(c2, (0.20 if dark else 0.14) * (0.6 + 0.6 * k))
        t.update(a1=c1, a2=c2, a3=c3, accent="linear-gradient(135deg, %s, %s)" % (c1, c2),
                 accent_shadow="0 4px 10px -6px rgba(0,0,0,0.35)", shade="%d,%d,%d" % rgb(c2),
                 colourful=True)
    t["backdrop"] = "linear-gradient(150deg, %s)" % ", ".join(
        "rgba(%d,%d,%d,%.2f)" % (*c, a) for c in stops)
    t["glass"] = "background-color: %s; background-image: %s; border: 1px solid %s; box-shadow: %s, %s;" % (
        t["card"], t["card_img"], t["rim"], t["spec"], t["shadow"])
    return t


# ---- 4. CSS: tokens -> GTK4/libadwaita and GTK3 ------------------------------

def css_gtk4(dark, see_through=False, opacity=100, spec=None):
    """libadwaita: recolour through Adwaita's CSS variables, paint the planner
    gradient on the window, planner corners, gradient suggested buttons."""
    t = tokens(dark, see_through, opacity, spec)
    return f"""
:root {{
  --accent-bg-color: {t["a1"]}; --accent-fg-color: #ffffff; --accent-color: {t["accent_text"]};
  --window-bg-color: transparent; --window-fg-color: {t["text"]};
  --view-bg-color: {t["view"]}; --view-fg-color: {t["text"]};
  --headerbar-bg-color: {t["bar"]}; --headerbar-backdrop-color: {t["bar"]};
  --sidebar-bg-color: {t["sidebar"]}; --secondary-sidebar-bg-color: {t["sidebar"]};
  --card-bg-color: {t["card"]}; --dialog-bg-color: {t["card_solid"]}; --sheet-bg-color: {t["card_solid"]};
  --popover-bg-color: {t["popover"]};
}}
window.background, window.background > contents {{ background-color: transparent; }}
window.background {{ background-image: {t["backdrop"]}; }}
window.dialog, window.messagedialog, dialog-host > dialog > sheet, popover > contents {{ background-image: none; }}
/* dialogs and sheets are opaque: the window rule above leaves them transparent (video showed through) */
window.dialog, window.messagedialog, window.dialog > contents, window.messagedialog > contents,
dialog-host > dialog > sheet, floating-sheet > sheet, bottom-sheet > sheet {{ background-color: {t["card_solid"]}; }}
.card, .boxed-list, list.boxed-list, .app-card {{ border-radius: 16px; {t["glass"]} }}
list.boxed-list > row, .boxed-list > row {{ background-color: transparent; }}
button:not(.flat):not(.suggested-action):not(.destructive-action):not(.osd):not(.image-button):not(.toggle) {{
  background-color: {t["btn"]}; color: {t["btn_text"]}; box-shadow: inset 0 0 0 1px {t["btn_line"]}, {t["spec"]}; }}
button:not(.flat):not(.suggested-action):not(.destructive-action):not(.osd):not(.image-button):not(.toggle):hover {{
  background-color: {t["btn_hover"]}; }}
.dim-label, .caption, .subtitle {{ color: {t["soft"]}; }}
button.toggle:checked, viewswitcher button:checked, stackswitcher button:checked,
.linked > button:checked {{ background-color: {t["sel"]}; color: {t["accent_text"]}; box-shadow: none; }}
dialog-host > dialog > sheet, window.dialog {{ border-radius: 24px; }}
button {{ border-radius: 12px; }}
button.circular, button.pill {{ border-radius: 9999px; }}
button.suggested-action, button.default {{ background-image: {t["accent"]}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {t["accent_shadow"]}; }}
button.suggested-action:hover {{ opacity: 0.9; }}
button:active, button:checked:active {{ transform: scale(0.97); }}
entry, spinbutton, searchentry, dropdown > button, combobox > box > button {{ border-radius: 12px; }}
entry:focus-within, searchentry:focus-within {{ box-shadow: 0 0 0 2px {t["a1"]}; }}
/* header: solid surface anchored to the top; footer: floating rounded card */
headerbar {{ background-color: {t["bar"]}; box-shadow: 0 1px {t["hair"]}, 0 6px 14px -10px rgba({t["shade"]},{0.5 if dark else 0.25}); }}
toolbarview > .bottom-bar, actionbar > revealer > box {{ margin: 0 12px 12px; border-radius: 16px; {t["glass"]} }}
actionbar {{ background-color: transparent; border: none; box-shadow: none; }}
row:selected, .navigation-sidebar > row:selected {{ background-color: {t["sel"]}; background-image: none; }}
progressbar > trough > progress, scale > trough > highlight {{ background-image: linear-gradient(to right, {t["a1"]}, {t["a2"]}, {t["a3"] if t.get("colourful") else t["a2"]}); }}
progressbar > trough, scale > trough {{ background-color: {t["btn"]}; }}
switch:checked {{ background-image: {t["accent"]}; }}
""" + _colour_text(t, adwaita=True)


def _colour_text(t, adwaita=False):
    """COLOURFUL, used with care: text stays neutral; headings, row/sidebar icons
    (on a small glossy chip) and selections take ONE hue (the set's middle
    colour); the other two colours only appear inside accent gradients."""
    if not t.get("colourful"):
        return ""
    gloss = "linear-gradient(to bottom, rgba(255,255,255,0.30), rgba(255,255,255,0.0) 60%)"
    css = f"""
.title-1, .title-2, .app-title {{ color: {t["title"]}; }}
{"row box.prefixes > image, " if adwaita else ""}row > box > image.hb-row-icon {{ background-image: {gloss};
  border-radius: 10px; padding: 6px; box-shadow: inset 0 0 0 1px {t["chip_line"]}; }}
row:selected, .navigation-sidebar > row:selected {{ background-color: {t["sel"]}; background-image: {gloss}; }}
row:selected label.title, .navigation-sidebar > row:selected label {{ color: {t["title"]}; }}
link, label link, .link {{ color: {t["accent_text"]}; }}
button.suggested-action, button.default {{ background-image: {gloss}, {t["accent"]}; }}
"""
    # one hue (the middle colour) for every heading/icon/chip: calm, not a rainbow
    ink, chip = t["i2"], t["chip2"]
    css += f""".hb-section-group .heading {{ color: {ink}; }}
row > box > image.hb-row-icon {{ color: {ink}; background-color: {chip}; }}
.navigation-sidebar > row image, list.hb-nav > row image {{ color: {ink}; }}
"""
    if adwaita:   # libadwaita-only widgets (GTK3 has no preferencesgroup / prefixes box)
        css += f"""preferencesgroup .heading {{ color: {ink}; }}
row box.prefixes > image {{ color: {ink}; background-color: {chip}; }}
"""
    return css


def css_gtk3(dark, see_through=False, opacity=100, spec=None):
    """GTK3 (no CSS variables): named colours and plain values."""
    t = tokens(dark, see_through, opacity, spec)
    return f"""
@define-color theme_selected_bg_color {t["a1"]};
@define-color theme_selected_fg_color #ffffff;
@define-color accent_bg_color {t["a1"]};
window, .background {{ background-color: transparent; background-image: {t["backdrop"]}; color: {t["text"]}; }}
headerbar, .titlebar {{ background-color: {t["card_solid"]}; border: none; background-image: none;
  box-shadow: inset 0 -1px {t["hair"]}; color: {t["text"]}; }}
headerbar label, .titlebar label {{ color: {t["text"]}; }}
frame > border, .frame {{ border-radius: 16px; border: 1px solid {t["rim"]}; }}
box.card, .card, list, listbox, frame > border {{ border-radius: 16px; {t["glass"]} }}
/* GTK3 has no light/dark switch for us: paint every control so any base theme works */
button {{ border-radius: 12px; background-color: {t["btn"]}; background-image: none;
  border: 1px solid {t["btn_line"]}; color: {t["btn_text"]}; box-shadow: {t["spec"]}; text-shadow: none;
  -gtk-icon-shadow: none; }}
button label, button image {{ color: {t["btn_text"]}; }}
button:hover {{ background-color: {t["btn_hover"]}; }}
button:active, button:checked {{ background-color: {t["sel"]}; }}
button:disabled, button:disabled label {{ color: {t["faint"]}; }}
combobox button, combobox button label {{ background-color: {t["field"]}; color: {t["text"]};
  border: 1px solid {t["line"]}; }}
headerbar button, .titlebar button {{ background-color: transparent; border-color: transparent; }}
actionbar > revealer > box {{ margin: 0 12px 12px; border-radius: 16px; {t["glass"]} }}
headerbar button:hover, .titlebar button:hover {{ background-color: {t["hover"]}; }}
button.suggested-action, button.default {{ background-image: {t["accent"]}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {t["accent_shadow"]}; }}
button.suggested-action label, button.default label {{ color: #ffffff; }}
button.destructive-action {{ border-radius: 12px; }}
entry, spinbutton, combobox button {{ border-radius: 12px; background-color: {t["field"]};
  background-image: none; color: {t["text"]}; border: 1px solid {t["line"]}; }}
spinbutton entry, spinbutton button {{ background-color: transparent; border-color: transparent;
  box-shadow: none; }}
entry:focus, spinbutton:focus {{ box-shadow: 0 0 0 2px {t["a1"]}; }}
check, radio {{ background-color: {t["field"]}; background-image: none; border: 1px solid {t["faint"]};
  color: #ffffff; }}
check:checked, radio:checked, check:indeterminate {{ background-color: {t["a1"]};
  background-image: {t["accent"]}; border-color: {t["a1"]}; }}
check {{ border-radius: 6px; }}
scale trough {{ background-color: {t["btn"]}; background-image: none; border-color: transparent; }}
scale slider {{ background-color: #ffffff; background-image: none; border: 1px solid {t["a1"]}; }}
progressbar progress, scale highlight, levelbar block.filled {{ background-color: {t["a1"]};
  background-image: linear-gradient(to right, {t["a1"]}, {t["a3"]}); }}
switch:checked {{ background-color: {t["a1"]}; }}
menu, .menu, popover {{ background-color: {t["popover"]}; background-image: none; }}
label {{ color: {t["text"]}; }}
.dim-label {{ color: {t["soft"]}; }}
""" + _colour_text(t)


# ---- per-app state, choice and live watching --------------------------------

# ---- 5. Look: per-app state, choice and live watching ------------------------

class Look:
    """This app's PLANNER state. `on_change()` runs when the shared glass
    files, Fusion Desk's glass settings or this app's choice change."""

    def __init__(self, app, on_change=None):
        self.app = app
        self.on_change = on_change
        self._monitors = []
        self._fusion = None
        self._shell = None
        self._pending = False
        self.watch()

    @property
    def spec(self):
        return resolve(self.app, self._fusion)

    @property
    def active(self):
        return is_planner(self.spec)

    @property
    def see_through(self):
        spec = self.spec
        return spec["opacity"] < 100 and blurs_windows(self._fusion)

    def choice(self):
        own = scope_glass(self.app)
        if own is None:
            return "follow"
        return "planner" if is_planner(own) else "normal"

    def set_choice(self, choice):
        if choice == "follow":
            write_file("glass-" + self.app, "harbor")
        elif choice == "planner":
            # keep the Lumina options (Calm/Colourful, colours) the app had
            spec = dict(PLANNER_SPEC, **{k: self.spec[k] for k in LUMINA_KEYS})
            write_file("glass-" + self.app, to_text(spec))
        elif choice == "normal":
            write_file("glass-" + self.app, to_text(OFF))
        self._changed()

    def set_own(self, key, value):
        """Give this app its own Lumina values with one key changed (what
        Harbor Settings' per-app scope writes too)."""
        own = scope_glass(self.app) or dict(self.spec)
        own["effect"] = own["effect"] if own["effect"] != "OFF" else "STRONG"
        own["style"] = "PLANNER"
        if not set_value(own, key, value):
            log(f"{self.app}: ignored {key}={value!r}")
        write_file("glass-" + self.app, to_text(own))
        self._changed()

    def css_gtk4(self, dark):
        spec = self.spec
        return css_gtk4(dark, self.see_through, spec["opacity"], spec) if is_planner(spec) else ""

    def css_gtk3(self, dark):
        # never see-through: these GTK3 apps have no RGBA visual, so a
        # translucent window would paint black
        spec = self.spec
        return css_gtk3(dark, False, spec["opacity"], spec) if is_planner(spec) else ""

    def _changed(self, *_):
        # one restyle per burst: a single save fires several monitor events
        if self.on_change and not self._pending:
            self._pending = True
            GLib.idle_add(self._run_change)

    def _run_change(self):
        self._pending = False
        self.on_change()
        return False

    def watch(self):
        for name in ("glass", "glass-global", "glass-" + self.app):
            path = config_path(name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                mon = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
                mon.connect("changed", self._changed)
                self._monitors.append(mon)
            except (GLib.Error, OSError) as e:
                log(f"{self.app}: live updates of {path} are off: {e}")
        try:  # COLOURFUL + ACCENT follows the GNOME accent live
            self._iface = Gio.Settings(schema_id="org.gnome.desktop.interface")
            self._iface.connect("changed::accent-color", lambda *_: self.spec.get(
                "lumina-colours") == "ACCENT" and self._changed())
        except Exception:
            self._iface = None
        self._fusion = fusion_settings()
        if self._fusion is not None:
            self._fusion.connect(
                "changed", lambda _s, key: (key.startswith("glass-") or key == "harbor-glass-blur")
                and self._changed())
            try:
                self._shell = Gio.Settings(schema_id="org.gnome.shell")
                self._shell.connect("changed::enabled-extensions", self._changed)
            except Exception:
                self._shell = None


# ---- diagnostics: python3 planner_glass.py --check [app ...] -----------------

def check(apps):
    """Print which layer decides each app's look, its values and the problems."""
    fusion = fusion_settings()
    print(f"config folder: {os.path.dirname(config_path('x'))}")
    print(f"Fusion Desk schema: {'found' if fusion is not None else 'not installed'}")
    print(f"GNOME accent: {system_accent()}")
    problems = 0
    for app in apps:
        spec, source, notes = resolve_explained(app, fusion)
        if spec["effect"] == "OFF":
            look = "off"
        elif is_planner(spec):
            look = "Lumina " + spec["lumina"].title()
        else:
            look = spec["style"].title()
        print(f"\n{app}: {look}, effect {spec['effect']}, opacity {spec['opacity']} (from {source})")
        if is_planner(spec) and spec["lumina"] == "COLOURFUL":
            print(f"  colours {spec['lumina-colours']} -> {', '.join(lumina_colours(spec))}, "
                  f"intensity {spec['lumina-intensity']}")
        for n in notes:
            print(f"  ! {n}")
        problems += len(notes)
        for dark in (False, True):   # the CSS must build in both modes
            try:
                css_gtk4(dark, False, spec["opacity"], spec)
                css_gtk3(dark, False, spec["opacity"], spec)
            except Exception as e:  # report it, never hide it
                problems += 1
                print(f"  ! CSS fails ({'dark' if dark else 'light'}): {e!r}")
    print(f"\n{problems} problem(s)")
    return problems


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        known = ("files", "taskmgr", "settings", "apphub", "astra", "sysmic", "extendo")
        sys.exit(1 if check(sys.argv[2:] or known) else 0)
    print(__doc__)
