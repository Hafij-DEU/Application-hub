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

# Palette v2 ("Planner Mist"): one violet-indigo hue family. The backdrop is a
# low-chroma mist so white cards read clean; neutral buttons are indigo-tinted
# instead of grey; text is indigo-tinted ink. Dark = deep ink-violet.
ACCENT = "linear-gradient(135deg, #6366f1, #8b5cf6)"
ACCENT_SHADOW = "0 8px 16px -6px rgba(99,102,241,0.45)"
CARD_SHADOW = "0 1px 2px rgba(49,46,129,0.06), 0 6px 16px -8px rgba(49,46,129,0.12)"
LG_SHADOW = "0 12px 24px -10px rgba(49,46,129,0.22)"


def tokens(dark, see_through=False, opacity=100):
    a = max(0.35, opacity / 100) if see_through else 1.0
    if dark:
        stops = ((19, 17, 31), (23, 20, 48), (29, 25, 58))
        t = dict(card="rgba(31,28,51,%.2f)" % (0.86 if see_through else 1),
                 card_solid="#221f38", bar="rgba(28,25,48,%.2f)" % (0.92 if see_through else 1),
                 view="rgba(19,17,31,0.40)", sidebar="rgba(19,17,31,0.45)",
                 field="#29263f", popover="#24213a", rim="rgba(196,181,253,0.12)",
                 hair="#2c2945", line="rgba(196,181,253,0.18)",
                 text="#ecebf7", soft="#a9a6c4", faint="#6f6c8c", accent_text="#a5b4fc",
                 btn="rgba(139,128,249,0.16)", btn_hover="rgba(139,128,249,0.26)",
                 btn_text="#d4d0ff", btn_line="rgba(167,139,250,0.22)", hover="rgba(196,181,253,0.08)",
                 sel="rgba(129,140,248,0.22)")
    else:
        stops = ((246, 244, 255), (238, 240, 255), (232, 236, 254))
        t = dict(card="rgba(255,255,255,%.2f)" % (0.90 if see_through else 1),
                 card_solid="#ffffff", bar="rgba(255,255,255,%.2f)" % (0.92 if see_through else 1),
                 view="rgba(255,255,255,0.45)", sidebar="rgba(255,255,255,0.55)",
                 field="#f7f6fd", popover="#ffffff", rim="#e6e3f6",
                 hair="#ece9f8", line="#dcd8f0",
                 text="#1e1b3a", soft="#5f5b7a", faint="#9a97b3", accent_text="#4f46e5",
                 btn="#e9e6ff", btn_hover="#dedaff", btn_text="#4338ca", btn_line="#d9d4fb",
                 hover="rgba(99,102,241,0.07)", sel="rgba(99,102,241,0.12)")
    t["backdrop"] = "linear-gradient(160deg, %s)" % ", ".join(
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
  box-shadow: {CARD_SHADOW}; }}
button:not(.flat):not(.suggested-action):not(.destructive-action):not(.osd):not(.image-button):not(.toggle) {{
  background-color: {t["btn"]}; color: {t["btn_text"]}; box-shadow: inset 0 0 0 1px {t["btn_line"]}; }}
button:not(.flat):not(.suggested-action):not(.destructive-action):not(.osd):not(.image-button):not(.toggle):hover {{
  background-color: {t["btn_hover"]}; }}
.dim-label, .caption, .subtitle {{ color: {t["soft"]}; }}
button.toggle:checked, viewswitcher button:checked, stackswitcher button:checked,
.linked > button:checked {{ background-color: {t["sel"]}; color: {t["accent_text"]}; box-shadow: none; }}
dialog-host > dialog > sheet, window.dialog {{ border-radius: 24px; }}
button {{ border-radius: 12px; }}
button.circular, button.pill {{ border-radius: 9999px; }}
button.suggested-action, button.default {{ background-image: {ACCENT}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {ACCENT_SHADOW}; }}
button.suggested-action:hover {{ opacity: 0.9; }}
button:active, button:checked:active {{ transform: scale(0.97); }}
entry, spinbutton, searchentry, dropdown > button, combobox > box > button {{ border-radius: 12px; }}
entry:focus-within, searchentry:focus-within {{ box-shadow: 0 0 0 2px #6366f1; }}
/* header: solid surface anchored to the top; footer: floating rounded card */
headerbar {{ background-color: {t["bar"]}; box-shadow: 0 1px {t["hair"]}, 0 6px 14px -10px rgba(49,46,129,{0.5 if dark else 0.25}); }}
toolbarview > .bottom-bar, actionbar > revealer > box {{ background-color: {t["card"]};
  margin: 0 12px 12px; border-radius: 16px; border: 1px solid {t["rim"]}; box-shadow: {LG_SHADOW}; }}
toolbarview > .bottom-bar.raised, toolbarview > .bottom-bar.border {{ box-shadow: {LG_SHADOW}; }}
actionbar {{ background-color: transparent; border: none; box-shadow: none; }}
row:selected, .navigation-sidebar > row:selected {{ background-color: {t["sel"]}; background-image: none; }}
progressbar > trough > progress, scale > trough > highlight {{ background-image: linear-gradient(to right, #6366f1, #8b5cf6); }}
progressbar > trough, scale > trough {{ background-color: {t["btn"]}; }}
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
headerbar, .titlebar {{ background-color: {t["card_solid"]}; border: none; background-image: none;
  box-shadow: inset 0 -1px {t["hair"]}; color: {t["text"]}; }}
headerbar label, .titlebar label {{ color: {t["text"]}; }}
frame > border, .frame {{ border-radius: 16px; border: 1px solid {t["rim"]}; }}
box.card, .card, list, listbox {{ background-color: {t["card"]}; border-radius: 16px; }}
/* GTK3 has no light/dark switch for us: paint every control so any base theme works */
button {{ border-radius: 12px; background-color: {t["btn"]}; background-image: none;
  border: 1px solid {t["btn_line"]}; color: {t["btn_text"]}; box-shadow: none; text-shadow: none;
  -gtk-icon-shadow: none; }}
button label, button image {{ color: {t["btn_text"]}; }}
button:hover {{ background-color: {t["btn_hover"]}; }}
button:active, button:checked {{ background-color: {t["sel"]}; }}
button:disabled, button:disabled label {{ color: {t["faint"]}; }}
combobox button, combobox button label {{ background-color: {t["field"]}; color: {t["text"]};
  border: 1px solid {t["line"]}; }}
headerbar button, .titlebar button {{ background-color: transparent; border-color: transparent; }}
actionbar > revealer > box {{ background-color: {t["card"]}; margin: 0 12px 12px; border-radius: 16px;
  border: 1px solid {t["rim"]}; }}
headerbar button:hover, .titlebar button:hover {{ background-color: {t["hover"]}; }}
button.suggested-action, button.default {{ background-image: {ACCENT}; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.20); box-shadow: {ACCENT_SHADOW}; }}
button.suggested-action label, button.default label {{ color: #ffffff; }}
button.destructive-action {{ border-radius: 12px; }}
entry, spinbutton, combobox button {{ border-radius: 12px; background-color: {t["field"]};
  background-image: none; color: {t["text"]}; border: 1px solid {t["line"]}; }}
spinbutton entry, spinbutton button {{ background-color: transparent; border-color: transparent;
  box-shadow: none; }}
entry:focus, spinbutton:focus {{ box-shadow: 0 0 0 2px #6366f1; }}
check, radio {{ background-color: {t["field"]}; background-image: none; border: 1px solid {t["faint"]};
  color: #ffffff; }}
check:checked, radio:checked, check:indeterminate {{ background-color: #6366f1;
  background-image: linear-gradient(135deg, #6366f1, #8b5cf6); border-color: #6366f1; }}
check {{ border-radius: 6px; }}
scale trough {{ background-color: {t["btn"]}; background-image: none; border-color: transparent; }}
scale slider {{ background-color: #ffffff; background-image: none; border: 1px solid #6366f1; }}
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
