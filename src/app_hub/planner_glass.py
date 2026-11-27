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
"""
import os

from gi.repository import Gio, GLib

FUSION_SCHEMA = "org.gnome.shell.extensions.fusion-desk"
FUSION_UUID = "fusion-desk@hafij-deu.github.io"
STRENGTHS = ("style-strength", "shine-strength", "rim-strength", "glare-strength",
             "shadow-strength", "light-strength")
KEYS = ("effect", "material", "style", "opacity", "rim", "glare", "shadow",
        "light-follows") + STRENGTHS
EFFECTS = ("OFF", "SOFT", "STRONG")
MATERIALS = ("GLASS", "GLOSS")
STYLES = ("CLEAR", "FROSTED", "GEL", "TINTED", "SMOKE", "HALO", "GLOW", "PLANNER")
DEFAULT = {"effect": "STRONG", "material": "GLASS", "style": "FROSTED", "opacity": 75,
           "rim": True, "glare": True, "shadow": False, "light-follows": False,
           **{k: 100 for k in STRENGTHS}}
OFF = dict(DEFAULT, effect="OFF", opacity=100)
PLANNER_SPEC = dict(DEFAULT, style="PLANNER", opacity=100, glare=False, shadow=True)
CHOICES = (("follow", "Follow Harbor Settings"), ("planner", "Glass (Planner)"),
           ("normal", "Normal"))


# ---- shared glass files (same chain as harbor/common.py resolve_glass) ----

def config_path(name):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "harbor", name)


def _read(name):
    try:
        with open(config_path(name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write(name, text):
    path = config_path(name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def _set(spec, key, value):
    key = key.strip().removeprefix("glass-")
    value = str(value).strip().strip("'")
    if key == "effect" and value.upper() in EFFECTS:
        spec[key] = value.upper()
    elif key == "material" and value.upper() in MATERIALS:
        spec[key] = value.upper()
    elif key == "style" and value.upper() in STYLES:
        spec[key] = value.upper()
    elif key == "opacity" and value.isdigit():
        spec[key] = max(10, min(100, int(value)))
    elif key in STRENGTHS and value.isdigit():
        spec[key] = min(200, int(value))
    elif key in ("rim", "glare", "shadow", "light-follows"):
        spec[key] = value == "true"


def parse(text):
    text = (text or "").strip()
    if text == "off":
        return dict(OFF)
    spec = dict(DEFAULT)
    for pair in text.replace(",", "\n").splitlines():
        if "=" in pair:
            _set(spec, *pair.split("=", 1))
    return spec


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


def global_glass(fusion=None):
    fusion = fusion or fusion_settings()
    if fusion is not None:
        spec = dict(DEFAULT)
        keys = fusion.props.settings_schema.list_keys()
        for key in KEYS:
            if "glass-" + key in keys:
                _set(spec, key, fusion.get_value("glass-" + key).print_(False))
        return spec
    text = _read("glass-global")
    return parse(text) if text else dict(DEFAULT)


def scope_glass(scope):
    """A scope's own values ("harbor" or an app name), or None to follow."""
    text = _read("glass" if scope == "harbor" else "glass-" + scope)
    if text is None:
        return dict(OFF) if scope == "harbor" else None
    if text in ("", "global", "harbor"):
        return None
    return parse(text)


def resolve(app, fusion=None):
    if os.environ.get("PLANNER_GLASS"):      # screenshot runs: off|planner|key=value,...
        env = os.environ["PLANNER_GLASS"]
        return dict(PLANNER_SPEC) if env == "planner" else parse(env)
    spec = scope_glass(app)
    if spec is None:
        spec = scope_glass("harbor")
    if spec is None:
        spec = global_glass(fusion)
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

ACCENT = "linear-gradient(135deg, #6366f1, #8b5cf6)"
ACCENT_SHADOW = "0 10px 15px -3px rgba(99,102,241,0.30), 0 4px 6px -4px rgba(99,102,241,0.30)"
CARD_SHADOW = "0 1px 2px rgba(0,0,0,0.05)"
LG_SHADOW = "0 10px 15px -3px rgba(0,0,0,0.10), 0 4px 6px -4px rgba(0,0,0,0.10)"


def tokens(dark, see_through=False, opacity=100):
    a = max(0.35, opacity / 100) if see_through else 1.0
    if dark:
        stops = ((26, 26, 46), (22, 33, 62), (15, 52, 96))
        t = dict(card="rgba(24,24,27,0.80)", card_solid="#1e1e1e", bar="rgba(9,9,11,0.55)",
                 view="rgba(9,9,11,0.35)", sidebar="rgba(9,9,11,0.30)", field="rgba(39,39,42,0.50)",
                 popover="#1e1e1e", rim="rgba(255,255,255,0.10)", hair="#27272a",
                 text="#f4f4f5", soft="#9ca3af", accent_text="#a5b4fc", hover="rgba(255,255,255,0.06)")
    else:
        stops = ((224, 195, 252), (142, 197, 252))
        t = dict(card="rgba(255,255,255,0.80)", card_solid="#ffffff", bar="rgba(255,255,255,0.55)",
                 view="rgba(255,255,255,0.35)", sidebar="rgba(255,255,255,0.25)",
                 field="rgba(255,255,255,0.50)", popover="#ffffff", rim="rgba(255,255,255,0.20)",
                 hair="#f3f4f6", text="#111827", soft="#6b7280", accent_text="#4f46e5",
                 hover="rgba(255,255,255,0.45)")
    t["backdrop"] = "linear-gradient(135deg, %s)" % ", ".join(
        "rgba(%d,%d,%d,%.2f)" % (*c, a) for c in stops)
    return t


def css_gtk4(dark, see_through=False, opacity=100):
    """libadwaita: recolour through Adwaita's CSS variables, paint the planner
    gradient on the window, planner corners, gradient suggested buttons."""
    t = tokens(dark, see_through, opacity)
    return f"""
:root {{
  --accent-bg-color: #6366f1; --accent-fg-color: #ffffff; --accent-color: {t["accent_text"]};
  --window-bg-color: transparent; --window-fg-color: {t["text"]};
  --view-bg-color: {t["view"]}; --view-fg-color: {t["text"]};
  --headerbar-bg-color: {t["bar"]}; --headerbar-backdrop-color: {t["bar"]};
  --sidebar-bg-color: {t["sidebar"]}; --secondary-sidebar-bg-color: {t["sidebar"]};
  --card-bg-color: {t["card"]}; --dialog-bg-color: {t["card_solid"]};
  --popover-bg-color: {t["popover"]};
}}
window.background, window.background > contents {{ background-color: transparent; }}
window.background {{ background-image: {t["backdrop"]}; }}
window.dialog, window.messagedialog, dialog-host > dialog > sheet, popover > contents {{ background-image: none; }}
.card, .boxed-list, list.boxed-list, .app-card {{ border-radius: 16px; border: 1px solid {t["rim"]};
  box-shadow: inset 0 1px rgba(255,255,255,{0.08 if dark else 0.5}), {CARD_SHADOW}; }}
dialog-host > dialog > sheet, window.dialog {{ border-radius: 24px; }}
button {{ border-radius: 12px; }}
button.circular, button.pill {{ border-radius: 9999px; }}
button.suggested-action, button.default {{ background-image: {ACCENT}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {ACCENT_SHADOW}; }}
button.suggested-action:hover {{ opacity: 0.9; }}
button:active, button:checked:active {{ transform: scale(0.97); }}
entry, spinbutton, searchentry, dropdown > button, combobox > box > button {{ border-radius: 12px; }}
entry:focus-within, searchentry:focus-within {{ box-shadow: 0 0 0 2px #6366f1; }}
headerbar {{ box-shadow: inset 0 -1px {t["hair"]}; }}
row:selected, .navigation-sidebar > row:selected {{ background-image: linear-gradient(to right, rgba(99,102,241,0.16), rgba(168,85,247,0.16)); }}
progressbar > trough > progress, scale > trough > highlight {{ background-image: linear-gradient(to right, #6366f1, #9333ea); }}
switch:checked {{ background-image: {ACCENT}; }}
"""


def css_gtk3(dark, see_through=False, opacity=100):
    """GTK3 (no CSS variables): named colours and plain values."""
    t = tokens(dark, see_through, opacity)
    return f"""
@define-color theme_selected_bg_color #6366f1;
@define-color theme_selected_fg_color #ffffff;
@define-color accent_bg_color #6366f1;
window, .background {{ background-color: transparent; background-image: {t["backdrop"]}; color: {t["text"]}; }}
headerbar, .titlebar {{ background-color: {t["bar"]}; background-image: none; border: none;
  box-shadow: inset 0 -1px {t["hair"]}; color: {t["text"]}; }}
frame > border, .frame {{ border-radius: 16px; border: 1px solid {t["rim"]}; }}
box.card, .card, list, listbox {{ background-color: {t["card"]}; border-radius: 16px; }}
button {{ border-radius: 12px; }}
button.suggested-action, button.default {{ background-image: {ACCENT}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {ACCENT_SHADOW}; }}
button.destructive-action {{ border-radius: 12px; }}
entry, spinbutton, combobox button {{ border-radius: 12px; background-color: {t["field"]}; }}
entry:focus {{ box-shadow: 0 0 0 2px #6366f1; }}
progressbar progress, scale highlight, levelbar block.filled {{ background-color: #6366f1;
  background-image: linear-gradient(to right, #6366f1, #9333ea); }}
switch:checked {{ background-color: #6366f1; }}
menu, .menu, popover {{ background-color: {t["popover"]}; background-image: none; }}
label {{ color: {t["text"]}; }}
.dim-label {{ color: {t["soft"]}; }}
"""


# ---- per-app state, choice and live watching --------------------------------

class Look:
    """This app's PLANNER state. `on_change()` runs when the shared glass
    files, Fusion Desk's glass settings or this app's choice change."""

    def __init__(self, app, on_change=None):
        self.app = app
        self.on_change = on_change
        self._monitors = []
        self._fusion = None
        self._shell = None
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
            _write("glass-" + self.app, "harbor")
        elif choice == "planner":
            _write("glass-" + self.app, to_text(PLANNER_SPEC))
        elif choice == "normal":
            _write("glass-" + self.app, to_text(OFF))
        self._changed()

    def css_gtk4(self, dark):
        spec = self.spec
        return css_gtk4(dark, self.see_through, spec["opacity"]) if is_planner(spec) else ""

    def css_gtk3(self, dark):
        spec = self.spec
        return css_gtk3(dark, self.see_through, spec["opacity"]) if is_planner(spec) else ""

    def _changed(self, *_):
        if self.on_change:
            GLib.idle_add(lambda: (self.on_change(), False)[1])

    def watch(self):
        for name in ("glass", "glass-global", "glass-" + self.app):
            path = config_path(name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                mon = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
                mon.connect("changed", self._changed)
                self._monitors.append(mon)
            except (GLib.Error, OSError):
                pass
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
