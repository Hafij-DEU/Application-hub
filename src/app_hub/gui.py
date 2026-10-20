"""GTK 4 / libadwaita window: My Apps, Updates, Library, All Apps, AI Install and Cleaner tabs."""

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import APP_ID, __version__, ai, cleaner, core, favorites, history, inventory, library, updates  # noqa: E402

ICON_DIR = os.path.join(core.LIB_DIR, "icons")
CSS = """
/* Theme colors only, so App Hub follows the system accent and light/dark style. */
.badge { font-size: 0.75em; font-weight: bold; padding: 2px 8px; border-radius: 999px;
         background: alpha(@accent_color, 0.15); color: @accent_color; }
.log-view { font-family: monospace; font-size: 0.9em; }
.disk-bar { padding: 6px 12px; }
.disk-level trough { min-height: 8px; }
.disk-level.warning progress { background-color: @warning_color; }
.disk-level.error progress { background-color: @error_color; }
.release-notes { font-size: 0.92em; }
"""


def _idle(fn, *args):
    GLib.idle_add(lambda: fn(*args) and False)


def _thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def make_icon(icon, size=40, fallback="application-x-executable"):
    img = Gtk.Image(pixel_size=size)
    gicon = None
    if icon:
        try:
            gicon = Gio.Icon.new_for_string(icon)
        except GLib.Error:
            gicon = None
    if isinstance(gicon, Gio.ThemedIcon):
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if not theme.has_gicon(gicon):
            gicon = None
    if isinstance(gicon, Gio.FileIcon) and not os.path.exists(gicon.get_file().get_path() or ""):
        gicon = None
    if gicon:
        img.set_from_gicon(gicon)
    else:
        img.set_from_icon_name(fallback)
    return img


def icon_file(icon):
    """Resolve an icon string to a file (to keep it after the app is gone)."""
    if not icon:
        return ""
    if os.path.isabs(icon):
        return icon if os.path.exists(icon) else ""
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        paintable = theme.lookup_by_gicon(Gio.Icon.new_for_string(icon), 96, 1, Gtk.TextDirection.NONE, 0)
        f = paintable.get_file()
        return f.get_path() if f else ""
    except (GLib.Error, TypeError):
        return ""


def badge(channel, label):
    lbl = Gtk.Label(label=label, valign=Gtk.Align.CENTER)
    lbl.add_css_class("badge")
    lbl.add_css_class(f"ch-{channel}")
    return lbl


def flat_button(icon, tooltip, cb):
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    btn.add_css_class("flat")
    btn.connect("clicked", lambda *_: cb())
    return btn


def text_button(label, cb, style=None):
    btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if style:
        btn.add_css_class(style)
    btn.connect("clicked", lambda *_: cb())
    return btn


def scrolled_page(*children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                  margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
    for c in children:
        box.append(c)
    clamp = Adw.Clamp(maximum_size=860, child=box)
    return Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), box


def launch_desktop(path, fallback_app=None):
    info = None
    try:
        info = Gio.DesktopAppInfo.new_from_filename(path) if path else None
    except TypeError:
        info = None
    if info:
        try:
            if info.launch([], None):
                return True
        except GLib.Error:
            pass
    if fallback_app:
        core.launch(fallback_app)
        return True
    return False


def set_hub_icon(img, app):
    """Icon of a GitHub-release app: its installed launcher, Library icon or bundled icon."""
    info = None
    if app.installed and app.desktop and not app.renamed:
        try:
            info = Gio.DesktopAppInfo.new(app.desktop)
        except TypeError:
            info = None
    bundled = os.path.join(ICON_DIR, f"{app.id}.png")
    if info and info.get_icon():
        img.set_from_gicon(info.get_icon())
    elif app.icon and os.path.exists(app.icon):
        img.set_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(app.icon)))
    elif os.path.exists(bundled):
        img.set_from_file(bundled)
    else:
        img.set_from_icon_name("application-x-executable")


def disk_usage(path):
    """(free, total) bytes of the filesystem holding path."""
    try:
        st = os.statvfs(path)
    except OSError:
        return 0, 0
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


# --------------------------------------------------------------------------
# My Apps: App Hub catalog + Favorites
# --------------------------------------------------------------------------

class HubRow(Adw.ActionRow):
    def __init__(self, section, app):
        super().__init__(title=GLib.markup_escape_text(app.name))
        self.section, self.app, self.cancel = section, app, None
        win = section.win
        self.icon = Gtk.Image(pixel_size=40)
        self.add_prefix(self.icon)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.progress = Gtk.ProgressBar(visible=False, valign=Gtk.Align.CENTER, width_request=120,
                                        show_text=True)
        self.open_btn = text_button("Open", lambda: win.open_hub(self.app))
        self.main_btn = text_button("", self.on_main)
        self.remove_btn = flat_button("user-trash-symbolic", "Uninstall", lambda: section.uninstall(self.app))
        self.web_btn = flat_button("web-browser-symbolic", "Release page", self.on_web)
        for w in (self.progress, self.open_btn, self.main_btn, self.remove_btn, self.web_btn):
            box.append(w)
        if section.removable or app.section == "mine":
            where = "My Apps" if app.section == "mine" else "Library"
            box.append(flat_button("list-remove-symbolic", f"Remove from {where} (does not uninstall)",
                                   lambda: section.remove_from_library(self.app)))
        self.add_suffix(box)
        self.sync()

    def sync(self):
        app, busy = self.app, self.section.win.busy
        set_hub_icon(self.icon, app)
        self.set_subtitle(GLib.markup_escape_text(f"{app.summary} · {app.status_text()}" if app.summary
                                                  else app.status_text()))
        downloading = self.cancel is not None
        self.open_btn.set_visible(bool(app.installed) and not downloading)
        # App Hub cannot uninstall itself while running (use apt remove app-hub).
        self.remove_btn.set_visible(bool(app.installed) and not downloading and app.package != "app-hub")
        self.remove_btn.set_sensitive(not busy)
        self.main_btn.remove_css_class("suggested-action")
        if downloading:
            self.main_btn.set_label("Cancel")
            self.main_btn.set_visible(True)
            self.main_btn.set_sensitive(True)
        elif app.update_available or not app.installed:
            self.main_btn.set_label("Update" if app.installed else "Install")
            self.main_btn.add_css_class("suggested-action")
            self.main_btn.set_visible(True)
            self.main_btn.set_sensitive(app.installable and not busy)
        else:
            self.main_btn.set_visible(False)

    def on_main(self):
        if self.cancel is not None:
            self.cancel.set()
        else:
            self.section.install([self.app])

    def on_web(self):
        url = self.app.release.url if self.app.release else self.app.repo_url
        Gtk.UriLauncher.new(url).launch(self.section.win, None, None, None)

    def set_progress(self, done, total):
        self.progress.set_visible(True)
        if total:
            self.progress.set_fraction(done / total)
            self.progress.set_text(f"{core.human_size(done)} / {core.human_size(total)}")
        else:
            self.progress.pulse()


class HubSection:
    """A group of GitHub-release apps with Install / Update / Open / Uninstall."""

    def __init__(self, win, title, description, apps, removable=False, on_removed=None, extra=None):
        self.win, self.apps, self.removable, self.on_removed = win, [], removable, on_removed
        self.group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title),
                                          description=GLib.markup_escape_text(description))
        self.update_all_btn = text_button("Update All", lambda: self.install(
            [a for a in self.apps if a.update_available]), "suggested-action")
        self.update_all_btn.set_visible(False)
        header = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        header.append(self.update_all_btn)
        if extra:
            header.append(extra)
        self.group.set_header_suffix(header)
        self.rows = {}
        self.set_apps(apps)

    def set_apps(self, apps):
        for row in self.rows.values():
            self.group.remove(row)
        self.apps, self.rows = apps, {}
        for app in self.apps:
            self.rows[app.id] = HubRow(self, app)
            self.group.add(self.rows[app.id])

    def refresh(self):
        core.refresh_installed(self.apps)
        for app in self.apps:
            app.release, app.error = None, ""
        self.sync()
        _thread(core.check_all, self.apps, lambda app: _idle(self.sync))

    def sync(self):
        for row in self.rows.values():
            row.sync()
        self.update_all_btn.set_visible(sum(a.update_available for a in self.apps) > 1)
        self.update_all_btn.set_sensitive(not self.win.busy)
        self.win.sync_update_everything()

    def install(self, apps):
        apps = [a for a in apps if a.installable]
        if not apps:
            return

        def work(log):
            staged = []
            for app in apps:
                row = self.rows[app.id]
                row.cancel = threading.Event()
                _idle(row.sync)
                log(f"Downloading {app.release.asset_name}")
                try:
                    staged.append((app, core.download(
                        app, lambda d, t, r=row: _idle(r.set_progress, d, t), row.cancel)))
                except core.Cancelled:
                    log(f"{app.name}: download cancelled")
                finally:
                    row.cancel = None
                    _idle(row.progress.set_visible, False)
                    _idle(row.sync)
            if not staged:
                return 0, ""
            return core.install_staged(staged, log)
        names = ", ".join(a.name for a in apps)
        self.win.run_job(f"Install {names}", work)

    def uninstall(self, app):
        if app.extension:
            dialog = Adw.AlertDialog(
                heading=f"Uninstall {app.name}?",
                body="The extension is turned off and removed. Log out and back in to unload it."
                     + (" It is also removed from the login screen." if app.gdm else ""))
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("remove", "Uninstall")
            dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect("response", lambda _d, r: r == "remove" and self.win.run_job(
                f"Uninstall {app.name}", lambda log: core.uninstall_extension(app, log)))
            dialog.present(self.win)
            return
        pkg = app.installed_as or app.package
        sysapp = self.win.installed.get(f"apt:{pkg}") or inventory.SysApp(
            "apt", pkg, app.name, version=app.installed or "", hub_id=app.id)
        self.win.confirm_uninstall(sysapp)

    def remove_from_library(self, app):
        library.remove(app.id)
        self.win.toast(f"{app.name} removed from {'My Apps' if app.section == 'mine' else 'the Library'}")
        if self.on_removed:
            self.on_removed()


class FavoriteRow(Adw.ActionRow):
    def __init__(self, page, recipe):
        super().__init__(title=GLib.markup_escape_text(recipe.name))
        self.page, self.recipe = page, recipe
        self.add_prefix(make_icon(recipe.icon, 40))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(recipe.channel, recipe.channel_label))
        self.open_btn = text_button("Open", self.on_open)
        self.install_btn = text_button("Install", lambda: page.win.install_recipe(recipe), "suggested-action")
        box.append(self.open_btn)
        box.append(self.install_btn)
        box.append(flat_button("starred-symbolic", "Remove from Favorites", lambda: page.unfavorite(recipe)))
        self.add_suffix(box)
        self.sync()

    def installed_app(self):
        return self.page.win.installed.get(self.recipe.installed_key())

    def sync(self):
        inst = self.installed_app()
        known = self.page.win.inventory_loaded
        if inst:
            state = f"Installed {inst.version}".strip()
        elif not known:
            state = "checking…"
        else:
            state = "Not installed"
        self.set_subtitle(GLib.markup_escape_text(f"{self.recipe.describe()} · {state}"))
        self.open_btn.set_visible(bool(inst and inst.desktop))
        self.install_btn.set_visible(known and not inst)
        self.install_btn.set_sensitive(not self.page.win.busy)

    def on_open(self):
        inst = self.installed_app()
        if inst:
            launch_desktop(inst.desktop)


class HomePage:
    def __init__(self, win):
        self.win = win
        catalog = core.load_catalog(include_library=False)
        self.self_hub = HubSection(win, "App Hub", "This app · github.com/Hafij-DEU/Application-hub",
                                   [a for a in catalog if a.section == "self"])
        add_btn = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Add your own app from GitHub")
        add_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add App"))
        add_btn.connect("clicked", lambda *_: AddAppDialog(win, self, "mine").present(win))
        add_btn.set_visible(core.is_admin())   # admin PIN entered in Preferences
        self.add_btn = add_btn
        self.hub = HubSection(win, "My Apps", "Installed from each app's latest GitHub release",
                              self.my_apps(), on_removed=self.reload, extra=add_btn)
        self.ext_hub = HubSection(win, "Extensions",
                                  "GNOME Shell extensions · log out and back in after installing or updating",
                                  [a for a in catalog if a.section == "extension"])
        core.refresh_installed(self.apps)

        self.fav_group = Adw.PreferencesGroup(
            title="Favorites", description="Apps you starred. Install them on any machine with one click.")
        self.fav_install_btn = text_button("Install All", self.install_all_favorites)
        self.fav_group.set_header_suffix(self.fav_install_btn)
        self.fav_rows = []
        self.fav_empty = Adw.ActionRow(title="No favorites yet",
                                       subtitle="Star apps in All Apps or AI Install to keep them here")
        self.fav_empty.add_prefix(Gtk.Image(icon_name="non-starred-symbolic"))
        self.fav_group.add(self.fav_empty)
        self.widget, _ = scrolled_page(self.self_hub.group, self.hub.group, self.ext_hub.group,
                                       self.fav_group)
        self.reload_favorites()
        self.refresh()

    @property
    def apps(self):
        return self.self_hub.apps + self.hub.apps + self.ext_hub.apps

    @staticmethod
    def my_apps():
        catalog = core.load_catalog(include_library=False)
        ids = {a.id for a in catalog}
        return [a for a in catalog if a.section not in ("self", "extension")] + [
            a for a in library.load_apps() if a.section == "mine" and a.id not in ids]

    def reload(self):
        """Re-read My Apps after an app was added or removed."""
        self.hub.set_apps(self.my_apps())
        self.hub.refresh()
        if hasattr(self.win, "library_page"):
            self.win.library_page.reload()   # a moved Library app may go back there

    def refresh(self):
        self.self_hub.refresh()
        self.hub.refresh()
        self.ext_hub.refresh()

    def sync(self):
        self.self_hub.sync()
        self.hub.sync()
        self.ext_hub.sync()
        for row in self.fav_rows:
            row.sync()
        missing = [r for r in self.fav_rows if self.win.inventory_loaded and not r.installed_app()]
        self.fav_install_btn.set_visible(len(missing) > 1)
        self.fav_install_btn.set_sensitive(not self.win.busy)

    # favorites
    def reload_favorites(self):
        for row in self.fav_rows:
            self.fav_group.remove(row)
        self.fav_rows = [FavoriteRow(self, r) for r in favorites.load()]
        for row in self.fav_rows:
            self.fav_group.add(row)
        self.fav_empty.set_visible(not self.fav_rows)
        self.sync()

    def unfavorite(self, recipe):
        favorites.remove(recipe)
        self.win.favorites_changed()

    def install_all_favorites(self):
        todo = [r.recipe for r in self.fav_rows if not r.installed_app()]
        if todo:
            self.win.install_recipes(todo, f"Install {len(todo)} favorites")


# --------------------------------------------------------------------------
# Library: any GitHub project that publishes .deb releases, grouped by owner
# --------------------------------------------------------------------------

class LibraryPage:
    def __init__(self, win):
        self.win = win
        self.sections = []
        intro = Gtk.Label(wrap=True, xalign=0, hexpand=True, label=(
            "Apps from GitHub projects that publish .deb releases. Add a project by its GitHub "
            "link, a whole account or organization, or ask the AI by name."))
        intro.add_css_class("dim-label")
        top = Gtk.Box(spacing=8)
        top.append(intro)
        add_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        add_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add App"))
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: AddAppDialog(win, self).present(win))
        top.append(add_btn)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.widget, _ = scrolled_page(top, self.box)
        self.reload()

    @property
    def apps(self):
        return [a for sec in self.sections for a in sec.apps]

    def reload(self):
        while child := self.box.get_first_child():
            self.box.remove(child)
        apps = [a for a in library.load_apps() if a.section != "mine"]
        groups = {}
        for app in apps:
            groups.setdefault(app.owner, []).append(app)
        self.sections = []
        for owner, members in groups.items():
            sec = HubSection(self.win, owner, f"github.com/{owner}", members, removable=True,
                             on_removed=self.reload)
            self.sections.append(sec)
            self.box.append(sec.group)
        if not apps:
            self.box.append(Adw.StatusPage(icon_name="view-app-grid-symbolic", title="Library is empty",
                                           description="Press Add App to add a GitHub project"))
        self.refresh()

    def refresh(self):
        for sec in self.sections:
            sec.refresh()

    def sync(self):
        for sec in self.sections:
            sec.sync()

    def after_job(self):
        for sec in self.sections:
            core.refresh_installed(sec.apps)
            sec.sync()


class AddAppDialog(Adw.Dialog):
    """Find GitHub projects with .deb releases and add them to the Library."""

    def __init__(self, win, page, section=""):
        super().__init__(title="Add to My Apps" if section == "mine" else "Add App",
                         content_width=640, content_height=560)
        self.win, self.page, self.section, self.found = win, page, section, []
        self.where = "My Apps" if section == "mine" else "the Library"
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text=(
            "github.com/owner/repo, github.com/orgs/name, or an app name"))
        self.entry.connect("activate", lambda *_: self.search())
        self.find_btn = text_button("Find", self.search, "suggested-action")
        bar = Gtk.Box(spacing=8)
        bar.append(self.entry)
        bar.append(self.find_btn)
        hint = Gtk.Label(wrap=True, xalign=0, label=(
            "Paste a repository link, or your account link (github.com/your-name) to list all your "
            "apps at once. A repository is added if its latest release has a .deb for this computer. "
            "An app name is looked up with Gemini (AI) and every answer is checked on GitHub."))
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.spinner = Gtk.Spinner()
        status_box = Gtk.Box(spacing=8)
        status_box.append(self.spinner)
        status_box.append(self.status)
        self.results = Adw.PreferencesGroup()
        self.add_all_btn = text_button("Add All", self.add_all, "suggested-action")
        self.add_all_btn.set_visible(False)
        self.results.set_header_suffix(self.add_all_btn)
        self.result_rows = []
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                       margin_start=18, margin_end=18)
        for w in (bar, hint, status_box, self.results):
            body.append(w)
        tv = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=body, vexpand=True,
                                                         hscrollbar_policy=Gtk.PolicyType.NEVER))
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)

    def set_status(self, text, busy=False):
        self.status.set_text(text)
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)

    def search(self):
        text = self.entry.get_text().strip()
        if not text:
            return
        owner, repo = library.parse_github(text)
        if not owner and not ai.api_key():
            self.set_status("Paste a GitHub link, or add a Gemini API key in Preferences to search by name.")
            return
        for row in self.result_rows:
            self.results.remove(row)
        self.result_rows, self.found = [], []
        self.add_all_btn.set_visible(False)
        self.find_btn.set_sensitive(False)
        self.set_status("Looking…", True)

        def work():
            known = library.known_repos()
            items, notes = [], []
            try:
                if owner and repo:
                    targets = [(f"{owner}/{repo}", "", "")]
                elif owner:
                    _idle(self.set_status, f"Listing projects of {owner}…", True)
                    targets = [(r["repo"], "", "") for r in library.owner_repos(owner)]
                else:
                    _idle(self.set_status, "Asking Gemini…", True)
                    targets = [(s["repo"], s.get("name", ""), s.get("summary", ""))
                               for s in ai.suggest_repos(text)]
            except core.HubError as e:
                _idle(self.done, [], [str(e)])
                return
            for i, (full, name, summary) in enumerate(targets, 1):
                if full.lower() in known:
                    notes.append(f"{full}: already in App Hub")
                    continue
                _idle(self.set_status, f"Checking {full} ({i}/{len(targets)})…", True)
                try:
                    items.append(library.resolve_repo(full, name, summary))
                except (core.HubError, KeyError) as e:
                    if owner and not repo:
                        continue  # most repos of an account have no .deb; don't list them all
                    notes.append(f"{full}: {e}")
            _idle(self.done, items, notes)
        _thread(work)

    def done(self, items, notes):
        self.find_btn.set_sensitive(True)
        self.found = items
        for e in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(e["name"]),
                                subtitle=GLib.markup_escape_text(
                                    f"{e['summary']}\n{e['repo']} · package {e['package']} · {e['version']}"))
            row.set_subtitle_lines(3)
            btn = text_button("Add", lambda e=e, r=row: self.add([e], [r]), "suggested-action")
            row.add_suffix(btn)
            row.add_btn = btn
            self.result_rows.append(row)
            self.results.add(row)
        for note in notes:
            row = Adw.ActionRow(title=GLib.markup_escape_text(note))
            row.add_css_class("dim-label")
            self.result_rows.append(row)
            self.results.add(row)
        self.add_all_btn.set_visible(len(items) > 1)
        n = len(items)
        known = notes and all(note.endswith("already in App Hub") for note in notes)
        self.set_status(f"Found {n} app{'s' if n != 1 else ''} with a .deb release." if n
                        else "These apps are already in App Hub." if known
                        else "No installable .deb release found.")

    def add(self, entries, rows):
        for r in rows:
            r.add_btn.set_sensitive(False)
            r.add_btn.set_label("Adding…")
        self.set_status("Adding…", True)

        def work():
            for e in entries:
                library.add(library.finish_entry(dict(e), self.section))
            _idle(finished)

        def finished():
            for r in rows:
                r.add_btn.set_label("Added")
            names = ", ".join(e["name"] for e in entries)
            self.set_status(f"Added {names} to {self.where}.")
            self.page.reload()
            self.win.toast(f"Added {names}")
        _thread(work)

    def add_all(self):
        rows = [r for r in self.result_rows if getattr(r, "add_btn", None) and r.add_btn.get_sensitive()]
        entries = [e for e, r in zip(self.found, [r for r in self.result_rows if hasattr(r, "add_btn")])
                   if r in rows]
        if entries:
            self.add(entries, rows)


# --------------------------------------------------------------------------
# All Apps: every installed app + recently uninstalled
# --------------------------------------------------------------------------

class SysAppRow(Adw.ActionRow):
    def __init__(self, page, app):
        super().__init__(title=GLib.markup_escape_text(app.name),
                         subtitle=GLib.markup_escape_text(app.subtitle()))
        self.page, self.app = page, app
        self.set_subtitle_lines(2)
        self.add_prefix(make_icon(app.icon, 40))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(app.channel, app.channel_label))
        self.update_btn = text_button("Update", lambda: page.win.update_apps([app]), "suggested-action")
        self.update_btn.set_visible(bool(app.update))
        box.append(self.update_btn)
        if app.desktop:
            box.append(flat_button("media-playback-start-symbolic", "Open",
                                   lambda: launch_desktop(app.desktop)))
        self.recipe = favorites.from_sysapp(app)
        self.star = flat_button("non-starred-symbolic", "Add to Favorites", self.toggle_star)
        self.star.set_visible(self.recipe is not None)
        box.append(self.star)
        self.trash = flat_button("user-trash-symbolic", app.protected or "Uninstall",
                                 lambda: page.win.confirm_uninstall(app))
        box.append(self.trash)
        self.add_suffix(box)
        self.search_text = f"{app.name} {app.id} {app.source}".lower()
        self.sync()

    def sync(self):
        busy = self.page.win.busy
        self.update_btn.set_sensitive(not busy)
        self.trash.set_sensitive(not busy and not self.app.protected)
        if self.recipe:
            fav = favorites.is_favorite(self.recipe.key)
            self.star.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
            self.star.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")

    def toggle_star(self):
        self.page.win.toggle_favorite(self.recipe)


class RemovedRow(Adw.ActionRow):
    def __init__(self, page, entry):
        super().__init__(title=GLib.markup_escape_text(entry.name))
        self.page, self.entry = page, entry
        bits = [f"removed {entry.when()}", entry.version, entry.source]
        self.set_subtitle(GLib.markup_escape_text(" · ".join(b for b in bits if b)))
        self.add_prefix(make_icon(entry.icon, 40, "package-x-generic"))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(entry.channel, entry.channel_label))
        self.recipe = favorites.from_removed(entry)
        self.star = flat_button("non-starred-symbolic", "Add to Favorites",
                                lambda: page.win.toggle_favorite(self.recipe))
        self.star.set_visible(self.recipe is not None)
        box.append(self.star)
        if entry.reinstall_note and entry.channel == "apt":
            # Not in any software source: let the AI look for where to get it.
            self.install_btn = text_button("Find with AI", lambda: page.win.ai_search(entry.name))
            self.install_btn.set_tooltip_text(entry.reinstall_note)
            self.ai_only = True
        else:
            self.install_btn = text_button("Install", lambda: page.win.reinstall(entry), "suggested-action")
            self.install_btn.set_tooltip_text(entry.reinstall_note or "")
            self.ai_only = False
        box.append(self.install_btn)
        self.add_suffix(box)
        self.search_text = f"{entry.name} {entry.id}".lower()
        self.sync()

    def sync(self):
        self.install_btn.set_sensitive(self.ai_only or (not self.page.win.busy and not self.entry.reinstall_note))
        if self.recipe:
            fav = favorites.is_favorite(self.recipe.key)
            self.star.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")


class AppsPage:
    FILTERS = [("all", "All channels"), ("apt", "Deb"), ("snap", "Snap"),
               ("flatpak", "Flatpak"), ("appimage", "AppImage"), ("updates", "Updates")]

    def __init__(self, win):
        self.win = win
        self.rows, self.removed_rows = [], []
        self.apps, self.removed = [], []

        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search installed apps", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.apply_filter())
        self.filter = Gtk.DropDown.new_from_strings([label for _, label in self.FILTERS])
        self.filter.connect("notify::selected", lambda *_: self.apply_filter())
        bar.append(self.search)
        bar.append(self.filter)

        self.status = Adw.ActionRow(title="Looking for installed apps…")
        self.status.add_prefix(Gtk.Spinner(spinning=True))
        self.installed_group = Adw.PreferencesGroup(title="Installed")
        self.update_all_btn = text_button("Update All", self.update_all, "suggested-action")
        self.update_all_btn.set_visible(False)
        self.installed_group.set_header_suffix(self.update_all_btn)
        self.installed_group.add(self.status)
        self.removed_group = Adw.PreferencesGroup(
            title="Recently Uninstalled",
            description=f"Apps removed in the last {history.KEEP_DAYS} days, by any tool")
        self.removed_group.set_visible(False)
        self.system_group = Adw.PreferencesGroup(
            title="System Updates", description="Other packages with updates in your software sources")
        self.system_group.set_visible(False)
        self.system_rows = []
        self.widget, _ = scrolled_page(bar, self.installed_group, self.system_group, self.removed_group)
        self.updates = {}   # key -> (version, update), kept across quick reloads
        self.loading = False

    def load(self, check_updates=True):
        self.status.set_title("Looking for installed apps…" if not self.rows else "Refreshing…")
        self.status.set_visible(True)
        self.loading = True

        def work():
            cache = inventory.open_apt_cache()
            catalog = core.load_catalog()
            apps = inventory.list_all(cache, catalog, updates=False)
            removed = history.recent({a.key for a in apps}, cache, catalog)
            hub_pkgs = {pkg for a in catalog for pkg in [a.package, *a.replaces]}
            self.win.system_upgrades = inventory.system_upgrades(
                cache, {a.id for a in apps if a.channel == "apt"} | hub_pkgs)
            if check_updates:
                _idle(self.show, apps, removed, False)
                inventory.apply_updates(apps, catalog)
                self.updates = {a.key: (a.version, a.update) for a in apps if a.update}
            else:
                for a in apps:
                    old = self.updates.get(a.key)
                    if old and old[0] == a.version and not a.update:
                        a.update = old[1]
            _idle(self.show, apps, removed, True)
        _thread(work)

    def show(self, apps, removed, final):
        self.apps, self.removed = apps, removed
        for row in self.rows:
            self.installed_group.remove(row)
        for row in self.removed_rows:
            self.removed_group.remove(row)
        self.rows = [SysAppRow(self, a) for a in apps]
        for row in self.rows:
            self.installed_group.add(row)
        self.removed_rows = [RemovedRow(self, e) for e in removed]
        for row in self.removed_rows:
            self.removed_group.add(row)
        self.removed_group.set_visible(bool(removed))
        n_up = sum(bool(a.update) for a in apps)
        self.installed_group.set_title(f"Installed ({len(apps)})")
        self.installed_group.set_description(
            (f"{n_up} update{'s' if n_up != 1 else ''} available" if n_up else "Everything is up to date")
            if final else "Checking for updates…")
        sysup = self.win.system_upgrades
        for row in self.system_rows:
            self.system_group.remove(row)
        self.system_rows = []
        for name, old, new, phased in sysup:
            note = " · staged rollout, arrives later" if phased else ""
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(f"{old} → {new}{note}"))
            row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))
            row.add_suffix(badge("apt", "Deb"))
            self.system_rows.append(row)
            self.system_group.add(row)
        self.system_group.set_title(f"System Updates ({len(sysup)})")
        self.system_group.set_visible(bool(sysup))
        # One button, one password: every app and system update.
        ready = n_up + sum(1 for s in sysup if not s[3])
        self.update_all_btn.set_label(f"Update All ({ready})" if ready else "Update All")
        self.update_all_btn.set_visible(final and bool(ready or sysup))
        self.status.set_visible(False)
        self.loading = not final
        self.win.inventory_ready(apps)
        self.apply_filter()

    def apply_filter(self):
        text = self.search.get_text().lower().strip()
        mode = self.FILTERS[self.filter.get_selected()][0]
        for row in self.rows:
            a = row.app
            ok = (not text or text in row.search_text) and \
                 (mode == "all" or a.channel == mode or (mode == "updates" and bool(a.update)))
            row.set_visible(ok)
        for row in self.removed_rows:
            row.set_visible((not text or text in row.search_text) and mode in ("all", row.entry.channel))

    def sync(self):
        self.update_all_btn.set_sensitive(not self.win.busy)
        for row in self.rows + self.removed_rows:
            row.sync()

    def update_all(self):
        self.win.confirm_update_everything()


# --------------------------------------------------------------------------
# Updates: every pending update, one by one or all at once
# --------------------------------------------------------------------------

CHANNEL_NAMES = {"hub": "GitHub", "apt": "Deb", "snap": "Snap", "flatpak": "Flatpak"}


def notes_text(notes, limit=1500):
    """GitHub release notes as plain text for a label."""
    lines = []
    for line in notes.replace("\r", "").splitlines():
        line = line.rstrip()
        stripped = line.lstrip("#").strip() if line.startswith("#") else line
        if stripped.startswith(("* ", "- ")):
            stripped = "• " + stripped[2:]
        lines.append(stripped.replace("**", "").replace("`", ""))
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


class UpdateRow(Adw.ExpanderRow):
    def __init__(self, page, pending):
        p = pending
        super().__init__(title=GLib.markup_escape_text(p.label))
        self.page, self.pending = page, p
        when = ""
        if p.kind == "hub" and p.obj.release and p.obj.release.published:
            when = " · released " + p.obj.release.published[:10]
        self.set_subtitle(GLib.markup_escape_text(f"{p.old or '?'}  →  {p.new}{when}"))
        if p.kind == "hub":
            icon = Gtk.Image(pixel_size=40)
            set_hub_icon(icon, p.obj)
        else:
            icon = make_icon(p.obj.icon, 40)
        self.add_prefix(icon)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.progress = Gtk.ProgressBar(visible=False, valign=Gtk.Align.CENTER, width_request=110,
                                        show_text=True)
        box.append(self.progress)
        box.append(badge(p.kind, CHANNEL_NAMES[p.kind]))
        self.btn = text_button("Update", lambda: page.win.update_pending([p]), "suggested-action")
        box.append(self.btn)
        self.add_suffix(box)
        # What's new: release notes for GitHub apps, the source for the others.
        if p.kind == "hub":
            rel = p.obj.release
            body = notes_text(rel.notes) if rel.notes else "No release notes were published."
            size = f"Download {core.human_size(rel.size)} · {rel.asset_name}" if rel.size else rel.asset_name
            body = f"{body}\n\n{size}"
            link = rel.url
        else:
            src = p.obj.source or p.obj.remote or ""
            body = f"{CHANNEL_NAMES[p.kind]} package {p.obj.id}" + (f" from {src}" if src else "")
            link = ""
        lbl = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True, margin_top=10,
                        margin_bottom=10, margin_start=14, margin_end=14,
                        wrap_mode=Pango.WrapMode.WORD_CHAR)
        lbl.add_css_class("release-notes")
        self.add_row(lbl)
        if link:
            web = Adw.ActionRow(title="Open release page", subtitle=GLib.markup_escape_text(link),
                                activatable=True)
            web.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            web.connect("activated", lambda *_: Gtk.UriLauncher.new(link).launch(page.win, None, None, None))
            self.add_row(web)
        self.sync()

    def sync(self):
        self.btn.set_sensitive(not self.page.win.busy)

    def set_progress(self, done, total):
        self.progress.set_visible(True)
        if total:
            self.progress.set_fraction(done / total)
            self.progress.set_text(f"{core.human_size(done)} / {core.human_size(total)}")
        else:
            self.progress.pulse()


class UpdatesPage:
    def __init__(self, win):
        self.win = win
        self.rows, self.sys_rows, self.signature = [], [], None
        self.checked_at = None

        self.check_row = Adw.ActionRow(title="Checking for updates…")
        self.check_spinner = Gtk.Spinner()
        self.check_row.add_prefix(self.check_spinner)
        self.check_btn = text_button("Check Now", self.check)
        self.check_row.add_suffix(self.check_btn)
        top = Adw.PreferencesGroup()
        top.add(self.check_row)

        self.apps_group = Adw.PreferencesGroup(
            title="App Updates", description="Click an app to see what's new")
        self.update_all_btn = text_button("Update All", win.confirm_update_everything, "suggested-action")
        self.apps_group.set_header_suffix(self.update_all_btn)
        self.sys_group = Adw.PreferencesGroup(
            title="System Updates", description="Other packages from your software sources")
        self.empty = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Everything is up to date",
                                    description="Your apps and system have no pending updates")
        self.empty.set_visible(False)
        self.widget, _ = scrolled_page(top, self.empty, self.apps_group, self.sys_group)
        self.sync()

    def check(self):
        """Check every app again: GitHub releases, then Deb, Snap and Flatpak."""
        self.checked_at = None
        self.win.home.refresh()
        self.win.library_page.refresh()
        self.win.apps_page.load()
        self.sync()

    def hub_progress(self):
        apps = self.win.hub_apps()
        done = sum(1 for a in apps if a.release is not None or a.error)
        return done, len(apps)

    def sync(self):
        win = self.win
        if not hasattr(win, "apps_page"):
            return
        pending = win.pending_updates()
        sysup = win.system_upgrades
        done, total = self.hub_progress()
        checking = done < total or win.apps_page.loading or not win.inventory_loaded
        if checking:
            if done < total:
                text = f"Checking GitHub releases… {done} of {total} apps"
            else:
                text = "Checking Deb, Snap and Flatpak apps…"
            self.check_row.set_title(text)
            self.check_row.set_subtitle(f"{len(pending)} update{'s' if len(pending) != 1 else ''} found so far")
        else:
            if self.checked_at is None:
                self.checked_at = GLib.DateTime.new_now_local()
            n = len(pending) + sum(1 for s in sysup if not s[3])
            self.check_row.set_title(f"{n} update{'s' if n != 1 else ''} available" if n
                                     else "No updates ready to install" if sysup
                                     else "Everything is up to date")
            stale = sum(1 for a in win.hub_apps() if a.release and a.release.stale)
            failed = sum(1 for a in win.hub_apps() if a.error)
            note = ""
            if stale or failed:
                note = (f" · GitHub did not answer for {stale + failed} app"
                        f"{'s' if stale + failed != 1 else ''}; sign in under Menu ▸ Preferences or try later")
            self.check_row.set_subtitle(f"Checked {total} GitHub apps and "
                                        f"{len(win.apps_page.apps)} installed apps at "
                                        + self.checked_at.format("%H:%M") + note)
        self.check_spinner.set_visible(checking)
        self.check_spinner.set_spinning(checking)
        self.check_btn.set_sensitive(not checking and not win.busy)

        signature = tuple((p.kind, p.label, p.new) for p in pending) + tuple(sysup)
        if signature != self.signature:
            self.signature = signature
            self.rebuild(pending, sysup)
        for row in self.rows:
            row.sync()
        for row, btn in self.sys_rows:
            if btn:
                btn.set_sensitive(not win.busy)
        self.update_all_btn.set_sensitive(not win.busy)
        self.update_all_btn.set_visible(len(pending) + len(self.sys_rows) > 1)
        self.empty.set_visible(not checking and not pending and not sysup)
        self.apps_group.set_visible(bool(pending))
        self.sys_group.set_visible(bool(sysup))
        win.set_updates_badge(len(pending) + sum(1 for s in sysup if not s[3]))

    def rebuild(self, pending, sysup):
        for row in self.rows:
            self.apps_group.remove(row)
        for row, _ in self.sys_rows:
            self.sys_group.remove(row)
        self.rows = [UpdateRow(self, p) for p in pending]
        for row in self.rows:
            self.apps_group.add(row)
        self.apps_group.set_title(f"App Updates ({len(pending)})")
        self.sys_rows = []
        for name, old, new, phased in sysup:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(f"{old}  →  {new}"))
            row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic", pixel_size=24))
            box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            box.append(badge("apt", "Deb"))
            p = updates.Pending(name, "apt", old, new, inventory.SysApp("apt", name, name, version=old,
                                                                         update=new))
            if phased:
                lbl = Gtk.Label(label="Staged rollout")
                lbl.add_css_class("dim-label")
                box.append(lbl)
                btn = text_button("Install Early", lambda p=p: self.win.update_pending([p]))
                btn.set_tooltip_text("Ubuntu rolls this update out gradually to catch problems first; "
                                     "it arrives here on its own later.")
            else:
                btn = text_button("Update", lambda p=p: self.win.update_pending([p]), "suggested-action")
            box.append(btn)
            row.add_suffix(box)
            self.sys_rows.append((row, btn))
            self.sys_group.add(row)
        self.sys_group.set_title(f"System Updates ({len(sysup)})")

    def row_for(self, app):
        return next((r for r in self.rows if r.pending.obj is app), None)


# --------------------------------------------------------------------------
# AI Install
# --------------------------------------------------------------------------

class OptionRow(Adw.ActionRow):
    def __init__(self, page, option):
        r = option.recipe
        super().__init__(title=GLib.markup_escape_text(r.describe()),
                         subtitle=GLib.markup_escape_text(("✓ " if option.verified else "✗ ") + option.status))
        self.set_title_lines(2)
        self.page, self.option = page, option
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(r.channel, r.channel_label))
        self.star = flat_button("non-starred-symbolic", "Add to Favorites",
                                lambda: page.win.toggle_favorite(r))
        box.append(self.star)
        self.install_btn = text_button("Install", lambda: page.win.confirm_recipe(r, option.status),
                                       "suggested-action" if option.verified else None)
        box.append(self.install_btn)
        self.add_suffix(box)
        self.sync()

    def sync(self):
        self.install_btn.set_sensitive(self.option.verified and not self.page.win.busy)
        self.star.set_sensitive(self.option.verified)
        fav = favorites.is_favorite(self.option.recipe.key)
        self.star.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")


class AIPage:
    def __init__(self, win):
        self.win = win
        self.banner = Adw.Banner(title="Add a Gemini API key to search the web for install methods",
                                 button_label="Preferences")
        self.banner.connect("button-clicked", lambda *_: win.show_preferences())

        intro = Gtk.Label(wrap=True, xalign=0, label=(
            "Name an app or describe what you need. Gemini searches the web for official ways to "
            "install it, and App Hub checks each answer against apt, the Snap Store, Flathub and "
            "GitHub before offering it. Commands written by the AI are never run."))
        intro.add_css_class("dim-label")
        bar = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry(placeholder_text="e.g. obsidian, or “a light video editor”",
                                     hexpand=True)
        self.entry.connect("activate", lambda *_: self.search())
        self.find_btn = text_button("Find", self.search, "suggested-action")
        bar.append(self.entry)
        bar.append(self.find_btn)
        self.spinner_row = Gtk.Box(spacing=8, visible=False)
        self.spinner = Gtk.Spinner()
        self.spinner_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.spinner_row.append(self.spinner)
        self.spinner_row.append(self.spinner_label)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        page, _ = scrolled_page(intro, bar, self.spinner_row, self.results)
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.append(self.banner)
        self.widget.append(page)
        self.option_rows = []
        self.sync_key()

    def sync_key(self):
        self.banner.set_revealed(not ai.api_key())

    def search(self):
        query = self.entry.get_text().strip()
        if not query:
            return
        if not ai.api_key():
            self.win.show_preferences()
            return
        self.find_btn.set_sensitive(False)
        self.spinner_row.set_visible(True)
        self.spinner.set_spinning(True)
        self.spinner_label.set_text("Searching the web with Gemini…")

        def work():
            try:
                answer = ai.find(query, lambda s: _idle(self.spinner_label.set_text, s))
                _idle(self.show, answer, None)
            except core.HubError as e:
                _idle(self.show, None, str(e))
        _thread(work)

    def show(self, answer, error):
        self.find_btn.set_sensitive(True)
        self.spinner.set_spinning(False)
        self.spinner_row.set_visible(False)
        while child := self.results.get_first_child():
            self.results.remove(child)
        self.option_rows = []
        if error:
            self.win.alert("AI search failed", error)
            return
        if not answer.apps:
            self.results.append(Adw.StatusPage(icon_name="system-search-symbolic",
                                               title="Nothing found", description="Try another name"))
        for found in answer.apps:
            desc = found.summary
            notes = found.options[0].notes if found.options else ""
            if notes:
                desc += f"\nNote: {notes}"
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(found.name),
                                         description=GLib.markup_escape_text(desc))
            if found.homepage.startswith("https://"):
                group.set_header_suffix(Gtk.LinkButton(uri=found.homepage, label="Website",
                                                       valign=Gtk.Align.CENTER))
            if not found.options:
                group.add(Adw.ActionRow(title="No install method found"))
            for opt in found.options:
                row = OptionRow(self, opt)
                self.option_rows.append(row)
                group.add(row)
            self.results.append(group)
        if answer.sources:
            group = Adw.PreferencesGroup(title="Sources")
            exp = Adw.ExpanderRow(title=f"{len(answer.sources)} web pages Gemini used")
            for title, uri in answer.sources[:12]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title))
                row.add_suffix(Gtk.LinkButton(uri=uri, label="Open", valign=Gtk.Align.CENTER))
                exp.add_row(row)
            group.add(exp)
            self.results.append(group)

    def sync(self):
        for row in self.option_rows:
            row.sync()


# --------------------------------------------------------------------------
# Cleaner
# --------------------------------------------------------------------------

class CleanerPage:
    def __init__(self, win):
        self.win = win
        self.groups, self.enabled = [], {}
        intro = Gtk.Label(wrap=True, xalign=0, hexpand=True, label=(
            "Finds what uninstalled apps left behind. Your files are moved to the Trash, "
            "so you can restore them. System items need your password."))
        intro.add_css_class("dim-label")
        top = Gtk.Box(spacing=8)
        top.append(intro)
        self.scan_btn = text_button("Scan", self.scan)
        top.append(self.scan_btn)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.clean_btn = Gtk.Button(label="Clean Selected", halign=Gtk.Align.CENTER)
        self.clean_btn.add_css_class("destructive-action")
        self.clean_btn.add_css_class("pill")
        self.clean_btn.connect("clicked", lambda *_: self.confirm())
        self.clean_btn.set_visible(False)
        self.widget, _ = scrolled_page(top, self.box, self.clean_btn)
        self.status = Adw.StatusPage(icon_name="user-trash-symbolic", title="Find leftovers",
                                     description="Press Scan to look for leftovers of removed apps")
        self.box.append(self.status)
        self.scanned = False

    def scan(self):
        self.scan_btn.set_sensitive(False)
        self.clean_btn.set_visible(False)
        while child := self.box.get_first_child():
            self.box.remove(child)
        self.status.set_title("Scanning…")
        self.status.set_description("Checking folders, packages, snaps and Flatpaks")
        self.box.append(self.status)

        def work():
            cache = inventory.open_apt_cache()
            apps = list(self.win.installed.values()) if self.win.inventory_loaded else None
            _idle(self.show, cleaner.scan(apps, cache))
        _thread(work)

    def show(self, groups):
        self.scanned = True
        self.scan_btn.set_sensitive(True)
        self.groups = groups
        self.enabled = {g.id: bool(g.items) for g in groups if not g.granular}
        while child := self.box.get_first_child():
            self.box.remove(child)
        total = sum(g.size for g in groups)
        count = sum(len(g.items) for g in groups)
        if not count:
            self.status.set_title("All clean")
            self.status.set_description("Nothing left behind by removed apps")
            self.box.append(self.status)
            return
        group = Adw.PreferencesGroup(title=f"Found {count} items · {core.human_size(total)}")
        for g in groups:
            group.add(self.group_row(g))
        self.box.append(group)
        self.clean_btn.set_visible(True)
        self.update_label()

    def group_row(self, g):
        exp = Adw.ExpanderRow(title=g.title, subtitle=GLib.markup_escape_text(
            f"{len(g.items)} items · {core.human_size(g.size)}\n{g.description}" if g.items
            else f"Nothing found\n{g.description}"))
        exp.set_subtitle_lines(3)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=bool(g.items))
        exp.add_prefix(check)
        item_checks = []
        if g.granular:
            check.set_active(bool(g.items) and all(i.selected for i in g.items))
        else:
            check.set_active(self.enabled.get(g.id, False))

        def on_group(btn):
            if g.granular:
                for i, c in zip(g.items, item_checks):
                    i.selected = btn.get_active()
                    c.set_active(btn.get_active())
            else:
                self.enabled[g.id] = btn.get_active()
            self.update_label()
        check.connect("toggled", on_group)
        exp.set_enable_expansion(bool(g.items))
        for item in g.items[:300]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item.label),
                                subtitle=GLib.markup_escape_text(item.detail))
            if item.size:
                row.add_suffix(Gtk.Label(label=core.human_size(item.size), css_classes=["dim-label"]))
            if g.granular:
                c = Gtk.CheckButton(active=item.selected, valign=Gtk.Align.CENTER)

                def on_item(btn, it=item):
                    it.selected = btn.get_active()
                    self.update_label()
                c.connect("toggled", on_item)
                row.add_prefix(c)
                row.set_activatable_widget(c)
                item_checks.append(c)
            exp.add_row(row)
        return exp

    def update_label(self):
        size = cleaner.selected_size(self.groups, self.enabled)
        self.clean_btn.set_label(f"Clean Selected ({core.human_size(size)})" if size else "Clean Selected")

    def confirm(self):
        plan, user, local = cleaner.clean_actions(self.groups, self.enabled)
        if not (plan or user or local):
            self.win.toast("Nothing selected")
            return
        lines = []
        for g in self.groups:
            n = sum(i.selected for i in g.items) if g.granular else (len(g.items) if self.enabled.get(g.id) else 0)
            if n:
                lines.append(f"• {g.title}: {n}")
        dialog = Adw.AlertDialog(heading="Clean these leftovers?", body="\n".join(lines))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clean", "Clean")
        dialog.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, resp):
            if resp == "clean":
                self.win.run_job("Clean leftovers", lambda log: inventory.execute(plan, user, local, log),
                                 lambda rc: self.scan())
        dialog.connect("response", on_response)
        dialog.present(self.win)

    def sync(self):
        self.clean_btn.set_sensitive(not self.win.busy)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="App Hub", default_width=1040, default_height=740)
        self.busy = False
        self.installed = {}          # inventory key -> SysApp
        self.inventory_loaded = False
        self.system_upgrades = []    # [(package, old, new)] apt upgrades outside the app list
        self.log_buffer = Gtk.TextBuffer()

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        header.pack_start(flat_button("view-refresh-symbolic", "Refresh", self.refresh_current))
        header.pack_start(flat_button("document-open-symbolic", "Install a .deb file", self.pick_deb))
        menu = Gio.Menu()
        menu.append("Install .deb File…", "win.install-deb")
        menu.append("Update Everything…", "win.update-everything")
        menu.append("Activity Log", "win.log")
        menu.append("Preferences", "win.preferences")
        menu.append("About App Hub", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.busy_spinner = Gtk.Spinner(visible=False, tooltip_text="Working…")
        header.pack_end(self.busy_spinner)
        view.add_top_bar(header)
        # Every pending update (My Apps, Library, Deb, Snap, Flatpak), one password.
        self.everything_banner = Adw.Banner(button_label="Update Everything", revealed=False)
        self.everything_banner.connect("button-clicked", lambda *_: self.confirm_update_everything())
        view.add_top_bar(self.everything_banner)

        for name, cb in (("log", self.show_log), ("preferences", self.show_preferences),
                         ("update-everything", self.confirm_update_everything),
                         ("install-deb", self.pick_deb)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_a, f=cb: f())
            self.add_action(act)

        self.home = HomePage(self)
        self.library_page = LibraryPage(self)
        self.apps_page = AppsPage(self)
        self.updates_page = UpdatesPage(self)
        self.ai_page = AIPage(self)
        self.cleaner_page = CleanerPage(self)
        self.stack.add_titled_with_icon(self.home.widget, "home", "My Apps", "starred-symbolic")
        self.updates_tab = self.stack.add_titled_with_icon(self.updates_page.widget, "updates", "Updates",
                                                           "software-update-available-symbolic")
        self.stack.add_titled_with_icon(self.library_page.widget, "library", "Library", "view-app-grid-symbolic")
        self.stack.add_titled_with_icon(self.apps_page.widget, "apps", "All Apps", "view-grid-symbolic")
        self.stack.add_titled_with_icon(self.ai_page.widget, "ai", "AI Install", "system-search-symbolic")
        self.stack.add_titled_with_icon(self.cleaner_page.widget, "cleaner", "Cleaner", "user-trash-symbolic")
        self.stack.connect("notify::visible-child-name", self.on_page)
        view.set_content(self.stack)
        view.add_bottom_bar(self.make_disk_bar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.apps_page.load()
        self.update_disk()
        GLib.timeout_add_seconds(30, lambda: self.update_disk() or True)

    # --- free space ---

    def make_disk_bar(self):
        bar = Gtk.Box(spacing=10)
        bar.add_css_class("disk-bar")
        bar.append(Gtk.Image(icon_name="drive-harddisk-symbolic"))
        self.disk_label = Gtk.Label(xalign=0)
        bar.append(self.disk_label)
        self.disk_level = Gtk.ProgressBar(width_request=180, valign=Gtk.Align.CENTER)
        self.disk_level.add_css_class("disk-level")
        bar.append(self.disk_level)
        self.home_label = Gtk.Label(xalign=0, visible=False)
        self.home_label.add_css_class("dim-label")
        bar.append(self.home_label)
        bar.append(Gtk.Box(hexpand=True))
        clean = Gtk.Button(label="Free Up Space", tooltip_text="Open the Cleaner")
        clean.add_css_class("flat")
        clean.connect("clicked", lambda *_: self.stack.set_visible_child_name("cleaner"))
        bar.append(clean)
        return bar

    def update_disk(self):
        free, total = disk_usage("/")
        if not total:
            return
        used = 1 - free / total
        self.disk_label.set_markup(f"Ubuntu: <b>{core.human_size(free)} free</b> of {core.human_size(total)}")
        self.disk_level.set_fraction(used)
        for cls, on in (("warning", 0.85 <= used < 0.95), ("error", used >= 0.95)):
            (self.disk_level.add_css_class if on else self.disk_level.remove_css_class)(cls)
        self.disk_level.set_tooltip_text(f"{used:.0%} used ({core.human_size(total - free)})")
        home = os.path.expanduser("~")
        try:
            separate = os.stat(home).st_dev != os.stat("/").st_dev
        except OSError:
            separate = False
        if separate:
            hfree, htotal = disk_usage(home)
            self.home_label.set_text(f"Home: {core.human_size(hfree)} free of {core.human_size(htotal)}")
        self.home_label.set_visible(separate)

    # --- shared UI ---

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def log(self, line):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), line + "\n")

    def show_log(self):
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                            left_margin=12, right_margin=12)
        view.add_css_class("log-view")
        tv = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=view, vexpand=True))
        tv.add_top_bar(Adw.HeaderBar())
        dialog = Adw.Dialog(title="Activity Log", content_width=760, content_height=480, child=tv)
        dialog.present(self)

    def show_preferences(self):
        settings = ai.load_settings()
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="AI Install (Gemini)",
            description="The key is saved in ~/.config/app-hub/settings.json, readable only by you. "
                        "The GEMINI_API_KEY environment variable overrides it.")
        key_row = Adw.PasswordEntryRow(title="Gemini API key", text=settings.get("gemini_api_key", ""))
        model_row = Adw.EntryRow(title="Model", text=settings.get("gemini_model") or ai.DEFAULT_MODEL)
        group.add(key_row)
        group.add(model_row)
        link = Adw.ActionRow(title="Get a free API key", subtitle="aistudio.google.com/apikey",
                             activatable=True)
        link.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        link.connect("activated", lambda *_: Gtk.UriLauncher.new(
            "https://aistudio.google.com/apikey").launch(self, None, None, None))
        group.add(link)
        page.add(group)
        gh = Adw.PreferencesGroup(
            title="GitHub",
            description="Without sign-in GitHub answers 60 update checks per hour per network; "
                        "signed in it answers 5000. The token needs no permissions.")
        gh_login = Adw.SwitchRow(title="Use my GitHub CLI login",
                                 subtitle="Takes the token from “gh auth login” when gh is installed",
                                 active=settings.get("use_gh_login", True))
        gh_token = Adw.PasswordEntryRow(title="Personal access token (optional)",
                                        text=settings.get("github_token", ""))
        gh.add(gh_login)
        gh.add(gh_token)
        tok_link = Adw.ActionRow(title="Create a token", subtitle="github.com/settings/personal-access-tokens", activatable=True)
        tok_link.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        tok_link.connect("activated", lambda *_: Gtk.UriLauncher.new(
            "https://github.com/settings/personal-access-tokens/new").launch(self, None, None, None))
        gh.add(tok_link)
        page.add(gh)
        if core.admin_pin():
            page.add(self.admin_group(dialog))
        dialog.add(page)

        def on_closed(*_):
            data = ai.load_settings()
            data["gemini_api_key"] = key_row.get_text().strip()
            data["gemini_model"] = model_row.get_text().strip() or ai.DEFAULT_MODEL
            data["github_token"] = gh_token.get_text().strip()
            data["use_gh_login"] = gh_login.get_active()
            ai.save_settings(data)
            core.forget_token()
            self.ai_page.sync_key()
        dialog.connect("closed", on_closed)
        dialog.present(self)

    def admin_group(self, dialog):
        """Preferences ▸ Admin: the admin PIN turns on Add App in My Apps."""
        group = Adw.PreferencesGroup(title="Admin",
                                     description="The admin PIN shows Add App in My Apps on this computer.")
        pin_row = Adw.PasswordEntryRow(title="Admin PIN", show_apply_button=True)
        on_row = Adw.ActionRow(title="Admin mode is on", subtitle="Add App is shown in My Apps")
        on_row.add_prefix(Gtk.Image(icon_name="security-high-symbolic"))
        on_row.add_suffix(text_button("Lock", lambda: set_admin(False)))
        group.add(pin_row)
        group.add(on_row)

        def show():
            admin = core.is_admin()
            pin_row.set_visible(not admin)
            on_row.set_visible(admin)
            self.home.add_btn.set_visible(admin)

        def set_admin(on):
            data = ai.load_settings()
            if on:
                data["admin_unlock"] = core.admin_pin()["hash"]
            else:
                data.pop("admin_unlock", None)
            ai.save_settings(data)
            show()
            dialog.add_toast(Adw.Toast(title="Admin mode on: Add App is in My Apps" if on
                                       else "Admin mode off"))

        def apply(row):
            ok = core.check_admin_pin(row.get_text())
            row.set_text("")
            if ok:
                set_admin(True)
            else:
                dialog.add_toast(Adw.Toast(title="Wrong PIN"))
        pin_row.connect("apply", apply)
        show()
        return group

    def hub_apps(self):
        hub = list(self.home.apps) if hasattr(self, "home") else []
        if hasattr(self, "library_page"):
            hub += self.library_page.apps
        return hub

    def pending_updates(self):
        system = self.apps_page.apps if hasattr(self, "apps_page") else []
        return updates.collect(self.hub_apps(), system)

    def set_updates_badge(self, n):
        if hasattr(self, "updates_tab"):
            self.updates_tab.set_badge_number(n)
            self.updates_tab.set_needs_attention(n > 0)

    def update_pending(self, pending):
        """Update the given items (one password prompt), with download progress on their rows."""
        if self.busy or not pending:
            return

        def progress(app, done, total):
            row = self.updates_page.row_for(app)
            if row:
                _idle(row.set_progress, done, total)
        title = f"Update {pending[0].label}" if len(pending) == 1 else f"Update {len(pending)} apps"
        self.run_job(title, lambda log: updates.run(pending, log, progress=progress))

    def sync_update_everything(self):
        if not hasattr(self, "everything_banner"):
            return
        n = len(self.pending_updates())
        m = sum(1 for *_x, ph in self.system_upgrades if not ph)  # staged ones wait
        parts = []
        if n:
            parts.append(f"{n} app update{'s' if n != 1 else ''}")
        if m:
            parts.append(f"{m} system update{'s' if m != 1 else ''}")
        self.everything_banner.set_title(" and ".join(parts) + " available" if parts else "")
        on_updates = self.stack.get_visible_child_name() == "updates"
        self.everything_banner.set_revealed(bool(parts) and not self.busy and not on_updates)
        if hasattr(self, "updates_page"):
            self.updates_page.sync()

    def confirm_update_everything(self):
        pending = self.pending_updates()
        if self.busy:
            return
        names = {"hub": "GitHub", "apt": "Deb", "snap": "Snap", "flatpak": "Flatpak"}
        lines = [f"• {p.label}  {p.old} → {p.new}  ({names[p.kind]})" for p in pending]
        sysup = [s for s in self.system_upgrades if not s[3]]
        staged = [s for s in self.system_upgrades if s[3]]
        if sysup:
            lines.append(f"• System packages ({len(sysup)}): " + ", ".join(s[0] for s in sysup[:12])
                         + (" …" if len(sysup) > 12 else ""))
        if not lines:
            lines.append("No updates are ready right now. App Hub refreshes the package lists "
                         "and installs whatever is new.")
        total = len(pending) + len(sysup)
        dialog = Adw.AlertDialog(
            heading="Update everything?" if not total else
            f"Install {total} update{'s' if total != 1 else ''}?",
            body="You enter your password once for all of them.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        listing = Gtk.Label(label="\n".join(lines), xalign=0, wrap=True, selectable=True)
        box.append(Gtk.ScrolledWindow(child=listing, min_content_height=min(240, 22 * len(lines) + 10),
                                      hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True))
        system = Gtk.CheckButton(label="Include system updates (apt update + upgrade)", active=True)
        box.append(system)
        early = Gtk.CheckButton(visible=bool(staged), label=(
            f"Also install {len(staged)} staged update{'s' if len(staged) != 1 else ''} early"))
        early.set_tooltip_text(", ".join(s[0] for s in staged) + "\n\nUbuntu rolls these out to a "
                               "growing share of computers to catch problems first; normally they "
                               "arrive here later on their own.")
        system.bind_property("active", early, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        box.append(early)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update All")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")

        def on_response(_d, resp):
            if resp == "update":
                self.run_job("Update everything",
                             lambda log: updates.run(pending, log, system=system.get_active(),
                                                     phased=early.get_active()))
        dialog.connect("response", on_response)
        dialog.present(self)

    def set_busy(self, busy):
        self.busy = busy
        self.busy_spinner.set_visible(busy)
        self.busy_spinner.set_spinning(busy)
        self.sync_update_everything()
        for page in (self.home, self.library_page, self.apps_page, self.ai_page, self.cleaner_page):
            page.sync()

    def show_page(self, name):
        """Switch to a page by its --page name (updates, my-apps, …) or stack name."""
        name = PAGES.get(name, name)
        if self.stack.get_child_by_name(name):
            self.stack.set_visible_child_name(name)

    def on_page(self, *_):
        self.sync_update_everything()
        if self.stack.get_visible_child_name() == "cleaner" and not self.cleaner_page.scanned:
            self.cleaner_page.scan()

    def refresh_current(self):
        name = self.stack.get_visible_child_name()
        if name == "home":
            self.home.refresh()
        elif name == "library":
            self.library_page.refresh()
        elif name == "apps":
            self.apps_page.load()
        elif name == "updates":
            self.updates_page.check()
        elif name == "cleaner":
            self.cleaner_page.scan()

    def inventory_ready(self, apps):
        self.installed = {a.key: a for a in apps}
        self.inventory_loaded = True
        self.home.sync()
        self.sync_update_everything()

    def ai_search(self, query):
        self.stack.set_visible_child_name("ai")
        self.ai_page.entry.set_text(query)
        self.ai_page.search()

    def favorites_changed(self):
        self.home.reload_favorites()
        self.apps_page.sync()
        self.ai_page.sync()

    # --- jobs ---

    def run_job(self, title, work, done=None):
        """work(log) -> (rc, output), run in a thread; one job at a time."""
        if self.busy:
            self.toast("Wait for the current task to finish")
            return
        self.set_busy(True)
        self.log(f"== {title}")

        def thread():
            try:
                rc, out = work(lambda line: _idle(self.log, line))
            except core.HubError as e:
                rc, out = 1, str(e)
                _idle(self.log, out)
            except Exception as e:  # keep the window usable whatever happens
                rc, out = 1, f"{type(e).__name__}: {e}"
                _idle(self.log, out)
            _idle(self.job_done, title, rc, out, done)
        _thread(thread)

    def job_done(self, title, rc, out, done):
        self.set_busy(False)
        if rc == 0:
            self.toast(f"{title}: done")
        elif rc in core.AUTH_CANCELLED:
            self.toast("Authentication cancelled")
        else:
            tail = "\n".join((out or "").strip().splitlines()[-12:])
            self.alert(f"{title} failed", tail or f"exit code {rc}")
        if done:
            done(rc)
        core.refresh_installed(self.home.apps)
        self.home.sync()
        self.library_page.after_job()
        self.apps_page.load(check_updates=False)
        self.update_disk()

    # --- actions ---

    def open_hub(self, app):
        if app.extension:
            def work():
                try:
                    core.open_extension(app)
                except core.HubError as e:
                    _idle(self.toast, str(e))
            _thread(work)
            return
        path = f"/usr/share/applications/{app.desktop}" if app.desktop and not app.renamed else ""
        try:
            if not launch_desktop(path, app):
                self.toast(f"Could not open {app.name}")
        except core.HubError as e:
            self.toast(str(e))

    def toggle_favorite(self, recipe):
        if not recipe:
            return
        if favorites.is_favorite(recipe.key):
            favorites.remove(recipe)
            self.toast(f"{recipe.name} removed from Favorites")
        else:
            favorites.add(recipe)
            self.toast(f"{recipe.name} added to Favorites")
        self.favorites_changed()

    def confirm_uninstall(self, app):
        if app.protected or self.busy:
            return

        def prepare():
            extra = []
            if app.channel == "apt":
                extra = [p for p in inventory.apt_removal_preview(app.id) if p != app.id]
            _idle(show, extra)

        def show(extra):
            body = {
                "apt": "The package is removed with apt.",
                "snap": "The snap is removed. snapd keeps a snapshot of its data unless you delete it.",
                "flatpak": f"The Flatpak is removed from the {app.installation} installation.",
                "appimage": "The AppImage and its launcher are moved into App Hub's storage, so you "
                            "can reinstall it from Recently Uninstalled.",
            }[app.channel]
            dialog = Adw.AlertDialog(heading=f"Uninstall {app.name}?", body=body)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            if extra:
                risky = [p for p in extra if p in inventory.PROTECTED_PACKAGES or p.startswith("ubuntu-")]
                lbl = Gtk.Label(wrap=True, xalign=0, label=(
                    f"Also removes {len(extra)} package{'s' if len(extra) > 1 else ''}: "
                    + ", ".join(extra[:15]) + (" …" if len(extra) > 15 else "")
                    + ("\n\nWarning: this includes parts of the desktop. Only continue if you are sure."
                       if risky else "")))
                if risky:
                    lbl.add_css_class("error")
                box.append(lbl)
            purge = Gtk.CheckButton(label={
                "apt": "Also delete its system settings (purge)",
                "snap": "Also delete its data (no snapshot)",
                "flatpak": "Also delete its data in ~/.var/app",
            }.get(app.channel, ""), visible=app.channel != "appimage")
            box.append(purge)
            dialog.set_extra_child(box)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("remove", "Uninstall")
            dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect("response", lambda _d, r: r == "remove" and self.uninstall(app, purge.get_active()))
            dialog.present(self)
        _thread(prepare)

    def uninstall(self, app, purge):
        icon = icon_file(app.icon) if app.icon else ""
        if not icon and app.hub_id and os.path.exists(os.path.join(ICON_DIR, f"{app.hub_id}.png")):
            icon = os.path.join(ICON_DIR, f"{app.hub_id}.png")

        def work(log):
            rc, out = inventory.execute(*inventory.uninstall_actions(app, purge), log)
            if rc == 0:
                history.record(app, icon)
            return rc, out
        self.run_job(f"Uninstall {app.name}", work)

    def update_apps(self, apps):
        actions = []
        for app in apps:
            if app.hub_id:
                actions.append(favorites.install_actions(
                    favorites.Recipe("hub", app.hub_id, app.name, package=app.id)))
            else:
                actions.append(inventory.update_actions(app))
        names = apps[0].name if len(apps) == 1 else f"{len(apps)} apps"
        self.run_job(f"Update {names}", lambda log: inventory.execute(*inventory.combine(actions), log))

    def reinstall(self, entry):
        if entry.hub_id:
            actions = favorites.install_actions(favorites.Recipe("hub", entry.hub_id, entry.name,
                                                                 package=entry.id))
        else:
            actions = history.reinstall_actions(entry)

        def work(log):
            rc, out = inventory.execute(*actions, log)
            if rc == 0:
                history.forget(entry)
            return rc, out
        self.run_job(f"Install {entry.name}", work)

    def install_recipe(self, recipe):
        self.install_recipes([recipe], f"Install {recipe.name}")

    def install_recipes(self, recipes, title):
        try:
            actions = [favorites.install_actions(r) for r in recipes]
        except core.HubError as e:
            self.alert("Cannot install", str(e))
            return
        self.run_job(title, lambda log: inventory.execute(*inventory.combine(actions), log))

    def confirm_recipe(self, recipe, status):
        third_party = recipe.channel in ("github", "deb-url", "appimage-url")
        body = f"{recipe.describe()}\n{status}"
        if third_party:
            body += ("\n\nThis comes straight from the developer, not from a store that reviews apps. "
                     "Only install it if you trust the source.")
        dialog = Adw.AlertDialog(heading=f"Install {recipe.name}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "install" and self.install_recipe(recipe))
        dialog.present(self)

    # --- .deb files ---

    def pick_deb(self):
        flt = Gtk.FileFilter(name="Debian packages (.deb)")
        flt.add_pattern("*.deb")
        flt.add_mime_type("application/vnd.debian.binary-package")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog = Gtk.FileDialog(title="Install a .deb file", filters=store, default_filter=flt)

        def done(dlg, result):
            try:
                f = dlg.open_finish(result)
            except GLib.Error:
                return
            if f and f.get_path():
                self.open_deb(f.get_path())
        dialog.open(self, None, done)

    def open_deb(self, path):
        r = core._run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture", "Description",
                       "Installed-Size", "Maintainer"])
        if r.returncode != 0:
            self.alert("Not a .deb package", r.stderr.strip() or path)
            return
        info, key = {}, None
        for line in r.stdout.splitlines():
            if line[:1] in " \t" and key:
                continue
            key, _, value = line.partition(": ")
            info[key] = value.strip()
        pkg, ver, arch = info.get("Package", "?"), info.get("Version", "?"), info.get("Architecture", "")
        machine = core.dpkg_arch()
        foreign = core._run(["dpkg", "--print-foreign-architectures"]).stdout.split()
        if arch and arch not in ("all", machine, *foreign):
            self.alert("Wrong architecture", f"{pkg} is built for {arch}; this computer is {machine}.")
            return
        current = core.installed_version(pkg)
        if current:
            newer = core._run(["dpkg", "--compare-versions", ver, "gt", current]).returncode == 0
            action = f"Upgrades {current} → {ver}" if newer else (
                "Reinstalls the same version" if ver == current else f"Downgrades {current} → {ver}")
        else:
            action = f"New install · version {ver}"
        size = int(info.get("Installed-Size", "0") or 0) * 1024
        body = (f"{info.get('Description', '')}\n\nPackage: {pkg}\n{action}\n"
                f"Size: {core.human_size(size)}\nFrom: {info.get('Maintainer', 'unknown')}\n"
                f"File: {path}\n\nMissing dependencies are installed from your software sources.")
        dialog = Adw.AlertDialog(heading=f"Install {pkg}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, resp: resp == "install" and self.run_job(
            f"Install {pkg}", lambda log: core.run_helper([["install-deb", pkg, path]], log)))
        dialog.present(self)


# `app-hub --page NAME` and the app.show-page action: page name -> stack page.
PAGES = {"updates": "updates", "my-apps": "home", "library": "library",
         "all-apps": "apps", "ai": "ai", "cleaner": "cleaner"}


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        # Light/dark and accent come from the system settings.
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.DEFAULT)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)
        # Exported on D-Bus, so a second `app-hub --page X` (or
        # `gapplication action io.github.hafij_bge.AppHub show-page "'updates'"`)
        # switches the running window.
        show = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        show.connect("activate", lambda _a, param: self.show_page(param.get_string()))
        self.add_action(show)
        self.start_page = None   # --page for the first window
    def window(self):
        return self.get_active_window() or Window(self)

    def show_page(self, name):
        win = self.window()
        win.show_page(name)
        win.present()

    def do_activate(self):
        win = self.window()
        if self.start_page:
            win.show_page(self.start_page)
            self.start_page = None
        win.present()

    def do_open(self, files, _n, _hint):
        win = self.window()
        win.present()
        for f in files:
            if f.get_path() and f.get_path().endswith(".deb"):
                GLib.idle_add(lambda p=f.get_path(): win.open_deb(p) and False)
                break

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="App Hub", application_icon=APP_ID, version=__version__,
            developer_name="Md Hafijur Rahman", website="https://github.com/Hafij-DEU/Application-hub",
            comments="Install, update and uninstall apps from any channel, find install methods "
                     "with AI and clean up leftovers.",
        ).present(self.get_active_window())


def run(argv=None, page=None):
    app = Application()
    if page:
        # If App Hub is already running, ask it to show the page (run() then
        # raises its window); otherwise this process opens on that page.
        try:
            app.register(None)
        except GLib.Error as e:
            print(f"app-hub: {e.message}", file=sys.stderr)
            return 1
        if app.get_is_remote():
            app.activate_action("show-page", GLib.Variant("s", page))
        else:
            app.start_page = page
    return app.run(["app-hub", *(argv or [])])
