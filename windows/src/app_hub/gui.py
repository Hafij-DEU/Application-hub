"""GTK 4 / libadwaita window: My Apps, Updates, Library, All Apps, AI Install and Cleaner tabs.

The same window as App Hub on Linux, on Windows 11: it follows the Windows light/dark
setting and accent colour.
"""

import os
import re
import shutil
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import (APP_ID, __version__, ai, cleaner, core, favorites, helper, history, installer,  # noqa: E402
               inventory, ipc, leftovers, library, updates, winget, winsys)

ICON_DIR = os.path.join(core.LIB_DIR, "icons")
FALLBACK_ICON = "application-x-executable-symbolic"
CSS = """
/* Theme colors only, so App Hub follows the Windows accent and light/dark setting. */
.badge { font-size: 0.75em; font-weight: bold; padding: 2px 8px; border-radius: 999px;
         background: alpha(@accent_color, 0.15); color: @accent_color; }
.log-view { font-family: Consolas, monospace; font-size: 0.9em; }
.disk-bar { padding: 6px 12px; }
.disk-level trough { min-height: 8px; }
.disk-level.warning progress { background-color: @warning_color; }
.disk-level.error progress { background-color: @error_color; }
.release-notes { font-size: 0.92em; }

/* Cards and chips */
.section-title { font-weight: 800; font-size: 1.3em; }
.app-card { padding: 14px; border-radius: 16px; }
.app-card-wide { padding: 12px 14px; }
.app-card .app-name { font-weight: 700; font-size: 1.08em; margin-top: 4px; }
.app-card .app-summary { font-size: 0.9em; min-height: 2.6em; }
.app-card-icon { -gtk-icon-shadow: 0 2px 6px alpha(black, 0.18); }
.chip { font-size: 0.8em; font-weight: 600; padding: 3px 10px; border-radius: 999px;
        background: alpha(currentColor, 0.08); }
.chip.ok { background: alpha(@success_color, 0.15); color: @success_color; }
.chip.update { background: alpha(@accent_color, 0.18); color: @accent_color; }
.chip.error { background: alpha(@error_color, 0.15); color: @error_color; }
.sys-icon { border-radius: 8px; }
"""


def _idle(fn, *args):
    GLib.idle_add(lambda: fn(*args) and False)


def _thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def set_image(img, path, fallback=FALLBACK_ICON):
    if path and os.path.exists(path):
        try:
            img.set_from_paintable(Gdk.Texture.new_from_filename(path))
            return
        except GLib.Error:
            pass
    img.set_from_icon_name(fallback)


def make_icon(path, size=40, fallback=FALLBACK_ICON):
    img = Gtk.Image(pixel_size=size)
    set_image(img, path, fallback)
    return img


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


def open_uri(uri):
    try:
        winsys.open_path(uri)
    except OSError:
        pass


def set_hub_icon(img, app):
    """Icon of a GitHub-release app: bundled icon, Library icon, else the installed program's icon."""
    bundled = os.path.join(ICON_DIR, f"{app.id}.png")
    if os.path.exists(bundled):
        set_image(img, bundled)
    elif app.icon and os.path.exists(app.icon):
        set_image(img, app.icon)
    elif app.entry and app.entry.icon:
        set_image(img, winsys.icon_png(app.entry.icon))
    else:
        img.set_from_icon_name(FALLBACK_ICON)


def section_header(title, description, suffix=None):
    """Bold section title with a dim description and optional widgets on the right."""
    box = Gtk.Box(spacing=12, margin_bottom=4)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.END)
    text.append(Gtk.Label(label=title, xalign=0, css_classes=["section-title"]))
    if description:
        text.append(Gtk.Label(label=description, xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
    box.append(text)
    if suffix:
        suffix.set_valign(Gtk.Align.END)
        box.append(suffix)
    return box


def chip(text, kind=""):
    return Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END, max_width_chars=22,
                     css_classes=["chip"] + ([kind] if kind else []))


# --------------------------------------------------------------------------
# My Apps: App Hub catalog + Favorites
# --------------------------------------------------------------------------

class HubCard(Gtk.FlowBoxChild):
    """One GitHub-release app as a card: icon, name, summary, status chip, main button and a ⋮ menu."""

    def __init__(self, section, app, wide=False):
        super().__init__(focusable=False)
        self.section, self.app, self.cancel = section, app, None
        win = section.win
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "app-card"])
        self.set_child(card)

        top = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=48 if wide else 52, halign=Gtk.Align.START,
                              hexpand=not wide, css_classes=["app-card-icon"])
        top.append(self.icon)
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.START,
                                       tooltip_text="More", css_classes=["flat", "circular"])
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4)
        self.popover = Gtk.Popover(child=menu)
        self.menu_btn.set_popover(self.popover)

        def item(label, icon, cb, destructive=False):
            btn = Gtk.Button(css_classes=["flat"] + (["destructive-action"] if destructive else []))
            btn.set_child(Adw.ButtonContent(icon_name=icon, label=label, halign=Gtk.Align.START))
            btn.connect("clicked", lambda *_: (self.popover.popdown(), cb()))
            menu.append(btn)
            return btn
        item("Release page", "web-browser-symbolic", self.on_web)
        self.folder_item = item("Open Install Folder", "folder-symbolic", self.on_folder)
        if app.browser_extensions:
            item("Get Browser Extension", "application-x-addon-symbolic", self.on_get_extension)
            item("Open Extension Folder", "folder-symbolic", self.on_open_extension)
            item("Save Extension .zip to Downloads", "folder-download-symbolic", self.on_save_extension_zip)
        self.remove_item = item("Uninstall", "user-trash-symbolic", lambda: section.uninstall(self.app), True)
        if section.removable or app.section == "mine":
            where = "My Apps" if app.section == "mine" else "Library"
            item(f"Remove from {where}", "list-remove-symbolic", lambda: section.remove_from_library(self.app))
        name = Gtk.Label(label=app.name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                         max_width_chars=20, css_classes=["app-name"])
        self.summary = Gtk.Label(label=app.summary or "", xalign=0, yalign=0, wrap=True, lines=2,
                                 width_chars=18, max_width_chars=26, ellipsize=Pango.EllipsizeMode.END,
                                 css_classes=["dim-label", "app-summary"])
        self.chip = chip("")
        self.chip.set_halign(Gtk.Align.START)
        self.chip.set_valign(Gtk.Align.CENTER)
        self.progress = Gtk.ProgressBar(visible=False, show_text=True, valign=Gtk.Align.CENTER)
        buttons = Gtk.Box(spacing=6, homogeneous=not wide, valign=Gtk.Align.CENTER)
        self.open_btn = text_button("Open", lambda: win.open_hub(self.app))
        self.main_btn = text_button("", self.on_main)
        self.ext_btn = text_button("Extension", self.on_get_extension)
        self.ext_btn.set_tooltip_text("Download the newest browser extension into Documents and open its folder")
        self.ext_btn.set_visible(bool(app.browser_extensions))
        for b in (self.open_btn, self.ext_btn, self.main_btn):
            buttons.append(b)

        if wide:   # banner: icon · name/summary · chip · buttons · menu, in one line
            card.add_css_class("app-card-wide")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                           valign=Gtk.Align.CENTER)
            self.summary.set_lines(1)
            text.append(name)
            text.append(self.summary)
            top.append(text)
            top.append(self.chip)
            top.append(self.progress)
            top.append(buttons)
            top.append(self.menu_btn)
            card.append(top)
        else:
            top.append(self.menu_btn)
            card.append(top)
            card.append(name)
            self.summary.set_vexpand(True)
            card.append(self.summary)
            card.append(self.chip)
            card.append(self.progress)
            card.append(buttons)
        self.sync()

    def status(self):
        """(short chip text, chip style) for the card; the full text goes into the tooltip."""
        app = self.app
        installed = core.clean_version(app.installed) if app.installed else ""
        if app.error:
            return ("Installed " + installed if installed else "Unavailable"), "error"
        if app.release is None:
            return (installed or "Checking…"), ""
        if not app.release.asset_url:
            return (installed or "No Windows build"), ""
        if app.update_available:
            return f"{installed} → {app.release.version}", "update"
        if installed:
            return f"✓ {installed}", "ok"
        return f"{app.release.version} · {core.human_size(app.release.size)}", ""

    def sync(self):
        app, busy = self.app, self.section.win.busy
        set_hub_icon(self.icon, app)
        text, kind = self.status()
        self.chip.set_label(text)
        for k in ("ok", "update", "error"):
            self.chip.remove_css_class(k)
        if kind:
            self.chip.add_css_class(kind)
        self.set_tooltip_text(app.status_text())
        downloading = self.cancel is not None
        is_self = app.section == "self"
        # Only apps with a Windows build are offered (an installed one stays, to open or remove it).
        no_windows = bool(app.error or (app.release and not app.release.asset_url))
        self.set_visible(is_self or bool(app.installed) or not no_windows)
        installing = bool(getattr(self, "stage", ""))
        self.chip.set_visible(not downloading and not installing)
        self.open_btn.set_visible(bool(app.installed) and not downloading and not installing and not is_self)
        has_extension = bool(app.browser_extensions) and (app.release is None or bool(app.release.extras))
        self.ext_btn.set_visible(has_extension and not downloading and not installing)
        self.ext_btn.set_sensitive(not busy)
        # App Hub cannot uninstall itself while running (use Settings ▸ Apps).
        self.remove_item.set_visible(bool(app.entry) and not is_self)
        self.remove_item.set_sensitive(not busy and not downloading)
        self.folder_item.set_visible(bool(app.entry and app.entry.location and os.path.isdir(app.entry.location)))
        self.main_btn.remove_css_class("suggested-action")
        if downloading:
            self.main_btn.set_label("Cancel")
            self.main_btn.set_visible(True)
            self.main_btn.set_sensitive(True)
        elif installing:
            self.main_btn.set_visible(False)
        elif (app.update_available and not updates.app_ignored(app)) or not app.installed:
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

    def _ensure_release(self):
        if self.app.release is None or not self.app.release.extras:
            self.app.release = core.fetch_release(self.app)

    def on_get_extension(self):
        app, win = self.app, self.section.win

        def work(log):
            self._ensure_release()
            done = core.sync_browser_extensions(app, log, force=True)
            if not done:
                return 1, "No browser extension in this release"
            _idle(self.on_open_extension)
            return 0, ""
        win.run_job(f"Get the {app.name} browser extension", work)

    def on_open_extension(self):
        path = core.browser_extension_root(self.app)
        if not os.path.isdir(path):
            return self.on_get_extension()
        winsys.open_path(path)

    def on_save_extension_zip(self):
        app, win = self.app, self.section.win

        def work(log):
            self._ensure_release()
            paths = core.save_browser_extension_zips(app, log=log)
            _idle(winsys.reveal, paths[0])
            return 0, ""
        win.run_job(f"Save the {app.name} extension .zip", work)

    def on_web(self):
        open_uri(self.app.release.url if self.app.release else self.app.repo_url)

    def on_folder(self):
        if self.app.entry and self.app.entry.location:
            winsys.open_path(self.app.entry.location)

    def set_progress(self, done, total):
        self.progress.set_visible(True)
        if total:
            self.progress.set_fraction(done / total)
            self.progress.set_text(f"{core.human_size(done)} / {core.human_size(total)}")
        else:
            self.progress.pulse()

    def set_stage(self, text, percent=None):
        _stage(self, text, percent)
        self.sync()


def _stage(row, text, percent=None):
    """Show what happens to an app right now: a bar with text; it moves back and forth while the
    amount is unknown, and fills up when `percent` is known (e.g. winget's download)."""
    row.stage = text
    if text and percent is not None:
        if getattr(row, "pulse_id", 0):
            GLib.source_remove(row.pulse_id)
            row.pulse_id = 0
        row.progress.set_visible(True)
        row.progress.set_fraction(percent / 100)
        row.progress.set_text(text)
    elif text:
        row.progress.set_visible(True)
        row.progress.set_text(text)
        row.progress.pulse()
        if not getattr(row, "pulse_id", 0):
            row.pulse_id = GLib.timeout_add(120, lambda: row.progress.pulse() or True)
    else:
        row.progress.set_visible(False)
        if getattr(row, "pulse_id", 0):
            GLib.source_remove(row.pulse_id)
            row.pulse_id = 0


def stage_logger(log, rows, overall=None):
    """Wrap a job's log: lines from the install steps move each app's row through its stages
    (rows: {app name: row with set_stage}); overall(text) hears "Updating 3 of 7: 7-Zip".
    Returns (log function, clear function)."""
    current = [None]          # the app the last named line was about (winget's own lines name none)
    done, total = set(), len(rows)

    def tell(name):
        if overall and total > 1:
            _idle(overall, f"Updating {min(len(done) + 1, total)} of {total}: {name}" if len(done) < total
                  else f"Finishing {total} updates…")

    def on_line(line):
        if line.startswith("Done: "):
            log(line)
            name = line[len("Done: "):]
            if name in rows:
                done.add(name)
                current[0] = None
                _idle(rows[name].set_stage, "Done ✓", 100)
                nxt = next((n for n in rows if n not in done), None)
                if nxt:
                    tell(nxt)
            return
        if line.startswith(("Installing ", "Asking Windows")) and total > 1 and not done and current[0] is None:
            for name, row in rows.items():                   # everyone waits their turn
                if not getattr(row, "stage", ""):
                    _idle(row.set_stage, "Queued…")
        if line.startswith("Progress: "):
            for name, row in rows.items():
                if line.startswith(f"Progress: {name} "):
                    current[0] = row
                    tail = line[len(f"Progress: {name} "):]
                    percent = int(tail.split("%", 1)[0]) if tail.split("%", 1)[0].isdigit() else None
                    detail = tail.split("·", 1)[1].strip() if "·" in tail else ""
                    text = f"Downloading {percent}%" + (f" · {detail}" if detail and "/" in detail else "")
                    _idle(row.set_stage, text, percent)
            if line.endswith(("0%", "5%")) and " · " in line:
                log(line)                                  # every 5% in the Activity Log is enough
            return
        log(line)
        if line.startswith("Asking Windows for administrator permission"):
            for name, row in rows.items():
                if name not in done:
                    _idle(row.set_stage, "Waiting for administrator permission…")
            return
        if current[0] is not None and line.startswith(("Successfully verified installer hash",
                                                       "Starting package install", "Installing")):
            _idle(current[0].set_stage, "Installing…")
        for name, row in rows.items():
            if name not in line or name in done:
                continue
            if current[0] is not row:
                tell(name)
            current[0] = row
            if line.startswith("Closing "):
                _idle(row.set_stage, "Closing the app…")
            elif line.startswith(("Upgrade ", "Install ")) and "winget" in line:
                _idle(row.set_stage, "Starting…")
            elif line.startswith(("Installing ", "Unpacking ")):
                _idle(row.set_stage, "Installing…")
            elif "installed in" in line or "the installer stopped" in line:
                _idle(row.set_stage, "")

    def clear():
        for row in rows.values():
            _idle(row.set_stage, "")
    return on_line, clear


class HubSection:
    """A grid of GitHub-release app cards with Install / Update / Open / Uninstall."""

    def __init__(self, win, title, description, apps, removable=False, on_removed=None, extra=None,
                 wide=False):
        self.win, self.apps, self.removable, self.on_removed = win, [], removable, on_removed
        self.wide = wide
        self.update_all_btn = text_button("Update All", lambda: self.install(
            [a for a in self.apps if a.update_available and not updates.app_ignored(a)]), "suggested-action")
        self.update_all_btn.set_visible(False)
        self.update_all_btn.add_css_class("pill")
        header = Gtk.Box(spacing=6)
        header.append(self.update_all_btn)
        if extra:
            extra.add_css_class("pill")
            header.append(extra)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=1, max_children_per_line=1 if wide else 4,
                                column_spacing=12, row_spacing=12, valign=Gtk.Align.START)
        self.group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.group.append(section_header(title, description, header))
        self.group.append(self.flow)
        self.rows = {}
        self.set_apps(apps)

    def set_apps(self, apps):
        self.flow.remove_all()
        self.apps, self.rows = apps, {}
        for app in self.apps:
            self.rows[app.id] = HubCard(self, app, self.wide)
            self.flow.append(self.rows[app.id])
        self.flow.set_visible(bool(self.apps))

    def refresh(self):
        core.refresh_installed(self.apps)
        for app in self.apps:
            app.release, app.error = None, ""
        self.sync()
        _thread(core.check_all, self.apps, lambda app: _idle(self.sync))

    def sync(self):
        for row in self.rows.values():
            row.sync()
        self.update_all_btn.set_visible(sum(a.update_available and not updates.app_ignored(a) for a in self.apps) > 1)
        self.update_all_btn.set_sensitive(not self.win.busy)
        self.win.sync_update_everything()

    def install(self, apps):
        apps = [a for a in apps if a.installable]
        if not apps:
            return
        running = [(a.name, a.entry.location) for a in apps if a.entry and a.section != "self"]
        self.win.ensure_closed(running, lambda close: self._install(apps, close))

    def _install(self, apps, close):
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
                    if not staged or staged[-1][0] is not app:      # cancelled or failed: no bar left behind
                        _idle(row.progress.set_visible, False)
                    _idle(row.sync)
            if not staged:
                return 0, ""
            live, clear = stage_logger(log, {app.name: self.rows[app.id] for app, _ in staged})
            for app, _ in staged:
                _idle(self.rows[app.id].set_stage, "Preparing…")
            admin, user = updates.hub_steps(staged, close)
            try:
                rc, out = helper.execute([["keep-going"], *admin], [["keep-going"], *user], None, live)
            finally:
                clear()
            if rc == 0:
                for app, _ in staged:
                    if app.section != "self":
                        core.finish_install(app, log)
            for app, path in staged:
                if app.section == "self":
                    installer.start_self_update(path, log)
            return rc, out
        names = ", ".join(a.name for a in apps)
        self.win.run_job(f"Install {names}", work)

    def uninstall(self, app):
        if not app.entry:
            return
        sysapp = self.win.installed.get(f"{'portable' if app.entry.portable else 'desktop'}:{app.entry.ref}")
        if sysapp is None:
            sysapp = inventory.SysApp("portable" if app.entry.portable else "desktop", app.entry.ref, app.name,
                                      version=app.installed or "", scope="machine" if app.entry.machine else "user",
                                      hub_id=app.id, extra={"arp": app.entry})
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
        return self.page.win.find_installed(self.recipe)

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
        self.open_btn.set_visible(bool(inst and inst.launch))
        self.install_btn.set_visible(known and not inst)
        self.install_btn.set_sensitive(not self.page.win.busy)

    def on_open(self):
        inst = self.installed_app()
        if inst and inst.launch:
            self.page.win.launch(inst)


class HomePage:
    def __init__(self, win):
        self.win = win
        catalog = core.load_catalog(include_library=False)
        self.self_hub = HubSection(win, "App Hub", "This app · github.com/Hafij-DEU/Application-hub",
                                   [a for a in catalog if a.section == "self"], wide=True)
        add_btn = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Add your own app from GitHub")
        add_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add App"))
        add_btn.connect("clicked", lambda *_: AddAppDialog(win, self, "mine").present(win))
        self.add_btn = add_btn                 # shown in admin mode (set_admin)
        self.hub = HubSection(win, "My Apps", "Installed from each app's latest GitHub release",
                              self.my_apps(), on_removed=self.reload, extra=add_btn)
        core.refresh_installed(self.apps)

        self.fav_group = Adw.PreferencesGroup()
        self.fav_install_btn = text_button("Install All", self.install_all_favorites)
        self.fav_install_btn.add_css_class("pill")
        self.fav_rows = []
        self.fav_empty = Adw.ActionRow(title="No favorites yet",
                                       subtitle="Star apps in All Apps or AI Install to keep them here")
        self.fav_empty.add_prefix(Gtk.Image(icon_name="non-starred-symbolic"))
        self.fav_group.add(self.fav_empty)
        self.dl_group = Adw.PreferencesGroup()
        self.dl_rows = []

        def titled(title, description, group, suffix):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.append(section_header(title, description, suffix))
            box.append(group)
            return box
        self.dl_box = titled("Downloads",
                             "Admin only · Windows downloads of each GitHub release, including your own",
                             self.dl_group, flat_button("view-refresh-symbolic", "Refresh counts",
                                                        self.load_downloads))
        fav_box = titled("Favorites", "Apps you starred. Install them on any PC with one click.",
                         self.fav_group, self.fav_install_btn)
        self.widget, _ = scrolled_page(self.self_hub.group, self.hub.group, self.dl_box, fav_box)
        self.set_admin(core.is_admin())
        self.reload_favorites()
        self.refresh()

    @property
    def apps(self):
        return self.self_hub.apps + self.hub.apps

    @staticmethod
    def my_apps():
        catalog = core.load_catalog(include_library=False)
        ids = {a.id for a in catalog}
        return [a for a in catalog if a.section != "self"] + [
            a for a in library.load_apps() if a.section == "mine" and a.id not in ids]

    def set_admin(self, admin):
        """Admin mode shows Add App in My Apps and the Downloads section."""
        self.add_btn.set_visible(admin)
        self.dl_box.set_visible(admin)
        if admin and not self.dl_rows:
            self.load_downloads()

    def load_downloads(self):
        for row in self.dl_rows:
            self.dl_group.remove(row)
        self.dl_rows = []
        for app in self.apps:
            row = Adw.ExpanderRow(title=GLib.markup_escape_text(app.name), subtitle="counting…")
            icon = Gtk.Image(pixel_size=32)
            set_hub_icon(icon, app)
            row.add_prefix(icon)
            self.dl_group.add(row)
            self.dl_rows.append(row)

        def fill(row, stats, error):
            if error:
                row.set_subtitle(GLib.markup_escape_text(error))
                return
            total = sum(n for _, _, n in stats)
            row.set_subtitle(f"{total} download{'' if total == 1 else 's'} · {len(stats)} "
                             f"release{'' if len(stats) == 1 else 's'}")
            for tag, date, n in stats:
                child = Adw.ActionRow(title=GLib.markup_escape_text(tag), subtitle=date)
                child.add_suffix(Gtk.Label(label=str(n), css_classes=["numeric"]))
                row.add_row(child)

        def work(pairs):
            for app, row in pairs:
                try:
                    stats, error = core.download_stats(app), ""
                except core.HubError as e:
                    stats, error = [], str(e)
                _idle(fill, row, stats, error)
        _thread(work, list(zip(self.apps, self.dl_rows)))

    def reload(self):
        """Re-read My Apps after an app was added or removed."""
        self.hub.set_apps(self.my_apps())
        self.hub.refresh()
        if hasattr(self.win, "library_page"):
            self.win.library_page.reload()   # a moved Library app may go back there

    def refresh(self):
        self.self_hub.refresh()
        self.hub.refresh()

    def sync(self):
        self.self_hub.sync()
        self.hub.sync()
        for row in self.fav_rows:
            row.sync()
        missing = [r for r in self.fav_rows if self.win.inventory_loaded and not r.installed_app()]
        self.fav_install_btn.set_visible(len(missing) > 1)
        self.fav_install_btn.set_sensitive(not self.win.busy)

    def after_job(self):
        core.refresh_installed(self.apps)
        self.sync()

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
# Library: any GitHub project that publishes Windows releases, grouped by owner
# --------------------------------------------------------------------------

class LibraryPage:
    def __init__(self, win):
        self.win = win
        self.sections = []
        intro = Gtk.Label(wrap=True, xalign=0, hexpand=True, label=(
            "Apps from GitHub projects that publish Windows releases (.exe, .msi or a portable .zip). "
            "Add a project by its GitHub link, a whole account or organization, or ask the AI by name."))
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
    """Find GitHub projects with Windows releases and add them to the Library."""

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
            "apps at once. A repository is added if its latest release has a Windows installer or "
            "portable .zip for this PC. An app name is looked up with Gemini (AI) and every answer "
            "is checked on GitHub."))
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
                        continue  # most repos of an account have no Windows build; don't list them all
                    notes.append(f"{full}: {e}")
            _idle(self.done, items, notes)
        _thread(work)

    def done(self, items, notes):
        self.find_btn.set_sensitive(True)
        self.found = items
        for e in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(e["name"]),
                                subtitle=GLib.markup_escape_text(
                                    f"{e['summary']}\n{e['repo']} · {e['asset']} · {e['version']}"))
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
        self.set_status(f"Found {n} app{'s' if n != 1 else ''} with a Windows release." if n
                        else "These apps are already in App Hub." if known
                        else "No Windows release found.")

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
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, visible=page.selecting,
                                     active=app.key in page.selected, sensitive=not app.protected,
                                     tooltip_text=app.protected or "Select to uninstall")
        self.check.connect("toggled", lambda b: page.select(app, b.get_active()))
        self.add_prefix(self.check)
        icon = make_icon(app.icon, 36)
        icon.add_css_class("sys-icon")
        self.add_prefix(icon)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(app.channel, app.channel_label))
        self.update_btn = text_button("Update", lambda: page.win.update_apps([app]), "suggested-action")
        self.update_btn.set_visible(bool(app.update))
        box.append(self.update_btn)
        if app.launch:
            box.append(flat_button("media-playback-start-symbolic", "Open", lambda: page.win.launch(app)))
        if app.location and os.path.isdir(app.location):
            box.append(flat_button("folder-symbolic", "Open install folder",
                                   lambda: winsys.open_path(app.location)))
        self.recipe = favorites.from_sysapp(app)
        self.star = flat_button("non-starred-symbolic", "Add to Favorites", self.toggle_star)
        self.star.set_visible(self.recipe is not None)
        box.append(self.star)
        self.trash = flat_button("user-trash-symbolic", app.protected or "Uninstall",
                                 lambda: page.win.confirm_uninstall(app))
        box.append(self.trash)
        self.add_suffix(box)
        self.search_text = f"{app.name} {app.source} {app.winget_id}".lower()
        self.sync()

    def sync(self):
        busy = self.page.win.busy
        self.update_btn.set_sensitive(not busy)
        self.update_btn.set_visible(bool(self.app.update) and not updates.app_ignored(self.app))
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
        when = ("noticed " if entry.noticed else "removed ") + entry.when()
        bits = [when, entry.version, entry.source]
        self.set_subtitle(GLib.markup_escape_text(" · ".join(b for b in bits if b)))
        self.add_prefix(make_icon(entry.icon, 36, "package-x-generic-symbolic"))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(badge(entry.channel, entry.channel_label))
        self.recipe = favorites.from_removed(entry)
        self.star = flat_button("non-starred-symbolic", "Add to Favorites",
                                lambda: page.win.toggle_favorite(self.recipe))
        self.star.set_visible(self.recipe is not None)
        box.append(self.star)
        self.ai_only = False
        if entry.hub_id or entry.winget_id:
            self.install_btn = text_button("Install", lambda: page.win.reinstall(entry), "suggested-action")
        elif entry.channel == "store" and entry.family:
            self.install_btn = text_button("Open in Store", lambda: open_uri(
                f"ms-windows-store://pdp/?PFN={entry.family}"))
            self.ai_only = True
        else:
            # Not known to winget: let the AI look for where to get it.
            self.install_btn = text_button("Find with AI", lambda: page.win.ai_search(entry.name))
            self.install_btn.set_tooltip_text(entry.reinstall_note)
            self.ai_only = True
        box.append(self.install_btn)
        self.add_suffix(box)
        self.search_text = f"{entry.name} {entry.source}".lower()
        self.sync()

    def sync(self):
        self.install_btn.set_sensitive(self.ai_only or not self.page.win.busy)
        if self.recipe:
            fav = favorites.is_favorite(self.recipe.key)
            self.star.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")


class AppsPage:
    FILTERS = [("all", "All apps"), ("desktop", "Desktop"), ("store", "Store"),
               ("portable", "Portable"), ("updates", "Updates")]

    def __init__(self, win):
        self.win = win
        self.rows, self.removed_rows = [], []
        self.apps, self.removed = [], []

        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search installed apps", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.apply_filter())
        self.filter = Gtk.DropDown.new_from_strings([label for _, label in self.FILTERS])
        self.filter.connect("notify::selected", lambda *_: self.apply_filter())
        self.sort = Gtk.DropDown.new_from_strings([label for _, label in self.SORTS])
        self.sort.set_tooltip_text("Sort by")
        self.sort.connect("notify::selected", lambda *_: self.resort())
        self.select_btn = Gtk.ToggleButton(label="Select", tooltip_text="Select apps to uninstall together")
        self.select_btn.connect("toggled", lambda b: self.set_selecting(b.get_active()))
        bar.append(self.search)
        bar.append(self.filter)
        bar.append(self.sort)
        bar.append(self.select_btn)
        self.selecting, self.selected = False, set()

        self.status = Adw.ActionRow(title="Looking for installed apps…")
        self.status.add_prefix(Gtk.Spinner(spinning=True))
        self.installed_group = Adw.PreferencesGroup(title="Installed")
        self.update_all_btn = text_button("Update All", self.update_all, "suggested-action")
        self.update_all_btn.set_visible(False)
        self.installed_group.set_header_suffix(self.update_all_btn)
        self.installed_group.add(self.status)
        self.removed_group = Adw.PreferencesGroup(
            title="Recently Uninstalled",
            description=f"Apps removed in the last {history.KEEP_DAYS} days, by App Hub or anything else")
        self.removed_group.set_visible(False)
        self.system_group = Adw.PreferencesGroup(
            title="System Updates", description="Runtimes and other packages winget can update")
        self.system_group.set_visible(False)
        self.system_rows = []
        scrolled, _ = scrolled_page(bar, self.installed_group, self.system_group, self.removed_group)
        # Batch uninstall bar, shown in selection mode.
        self.sel_label = Gtk.Label(xalign=0, hexpand=True)
        self.sel_btn = text_button("Uninstall Selected", self.uninstall_selected, "destructive-action")
        self.sel_btn.add_css_class("pill")
        sel_bar = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=18, margin_end=18)
        sel_bar.append(self.sel_label)
        sel_bar.append(text_button("Select None", self.clear_selection))
        sel_bar.append(self.sel_btn)
        self.sel_revealer = Gtk.Revealer(child=sel_bar, reveal_child=False,
                                         transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.append(scrolled)
        self.widget.append(self.sel_revealer)
        self.updates = {}   # key -> (version, update, winget id, source), kept across quick reloads
        self.loading = False
        self.generation = 0

    def load(self, check_updates=True):
        self.status.set_title("Looking for installed apps…" if not self.rows else "Refreshing…")
        self.status.set_visible(True)
        self.loading = True
        self.generation += 1
        gen = self.generation

        def work():
            catalog = core.load_catalog()
            apps = inventory.list_all(catalog)
            if check_updates:
                _idle(self.show, apps, history.recent(apps, catalog), False, gen)
                if winget.available():
                    self.win.system_upgrades = inventory.apply_winget(apps)
                    _idle(self.show, apps, history.recent(apps, catalog), False, gen)
                    inventory.match_by_name(apps)       # apps winget did not link, by exact name
                self.updates = {a.key: (a.version, a.update, a.winget_id, a.winget_source,
                                        a.extra.get("by_name", False)) for a in apps}
                history.update_snapshot(apps)
            else:
                inventory.forget_arp()
                for a in apps:
                    old = self.updates.get(a.key)
                    if old:
                        a.winget_id, a.winget_source = old[2], old[3]
                        if old[4]:
                            a.extra["by_name"] = True
                        if old[0] == a.version:
                            a.update = old[1]
                history.update_snapshot(apps)
            _idle(self.show, apps, history.recent(apps, catalog), True, gen)
        _thread(work)

    def show(self, apps, removed, final, gen):
        if gen != self.generation:
            return
        self.apps, self.removed = self.sorted(apps), removed
        apps = self.apps
        self.final = final
        self.selected &= {a.key for a in apps}
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
        if not winget.available():
            desc = "Install “App Installer” from the Microsoft Store to see updates (winget)"
        elif final:
            desc = f"{n_up} update{'s' if n_up != 1 else ''} available" if n_up else "Everything is up to date"
        else:
            desc = "Checking for updates with winget…"
        self.installed_group.set_description(desc)
        sysup = self.win.system_upgrades
        for row in self.system_rows:
            self.system_group.remove(row)
        self.system_rows = []
        for r in sysup:
            row = Adw.ActionRow(title=GLib.markup_escape_text(r["name"]),
                                subtitle=GLib.markup_escape_text(f"{r['version']} → {r['available']} · {r['id']}"))
            row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))
            row.add_suffix(badge("winget", "winget"))
            self.system_rows.append(row)
            self.system_group.add(row)
        self.system_group.set_title(f"System Updates ({len(sysup)})")
        self.system_group.set_visible(bool(sysup))
        ready = n_up + len(sysup)
        self.update_all_btn.set_label(f"Update All ({ready})" if ready else "Update All")
        self.update_all_btn.set_visible(final and bool(ready))
        self.status.set_visible(False)
        self.loading = not final
        self.win.inventory_ready(apps)
        self.apply_filter()
        self.update_selection()

    # --- sorting and selection (batch uninstall) ---

    SORTS = [("name", "Name"), ("size", "Size"), ("date", "Newest first")]

    def sorted(self, apps):
        mode = self.SORTS[self.sort.get_selected()][0]
        if mode == "size":
            return sorted(apps, key=lambda a: (-a.size, a.name.lower()))
        if mode == "date":
            return sorted(apps, key=lambda a: a.installed_on or "0000", reverse=True)
        return sorted(apps, key=lambda a: a.name.lower())

    def resort(self):
        if self.rows:
            self.generation += 1
            self.show(self.apps, self.removed, getattr(self, "final", True), self.generation)

    def set_selecting(self, on):
        self.selecting = on
        if not on:
            self.selected.clear()
        for row in self.rows:
            row.check.set_visible(on)
            row.check.set_active(row.app.key in self.selected)
        self.update_selection()

    def select(self, app, on):
        (self.selected.add if on else self.selected.discard)(app.key)
        self.update_selection()

    def clear_selection(self):
        self.selected.clear()
        for row in self.rows:
            row.check.set_active(False)
        self.update_selection()

    def update_selection(self):
        chosen = [a for a in self.apps if a.key in self.selected]
        size = sum(a.size for a in chosen)
        self.sel_label.set_text((f"{len(chosen)} selected" + (f" · {core.human_size(size)}" if size else ""))
                                if chosen else "Tick the apps to remove")
        self.sel_btn.set_label(f"Uninstall {len(chosen)}" if chosen else "Uninstall Selected")
        self.sel_btn.set_sensitive(bool(chosen) and not self.win.busy)
        self.sel_revealer.set_reveal_child(self.selecting)

    def uninstall_selected(self):
        chosen = [a for a in self.apps if a.key in self.selected]
        if chosen:
            self.win.confirm_uninstall(chosen)
            self.select_btn.set_active(False)

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
        self.update_selection()

    def update_all(self):
        self.win.confirm_update_everything()


# --------------------------------------------------------------------------
# Uninstaller: confirm → the app's own uninstaller → Powerful Scan → done
# --------------------------------------------------------------------------

def uninstall_step(app, force_admin=False):
    """(step, needs administrator rights) that runs an app's uninstaller."""
    if app.channel == "store":
        return ["store-remove", app.id, app.name], False
    if app.channel == "portable":
        return ["remove-portable", app.extra["arp"].key[len(inventory.PORTABLE_PREFIX):]], False
    admin = (app.machine or force_admin) and not winsys.is_elevated()
    return ["uninstall", app.id, app.name], admin


def still_installed(app):
    if app.channel == "store":
        r = winsys.powershell("if (Get-AppxPackage | Where-Object { $_.PackageFullName -eq $env:AH_PKG }) { 'yes' }",
                              timeout=60, env=dict(os.environ, AH_PKG=app.id))
        return "yes" in r.stdout
    inventory.forget_arp()
    return inventory.find_entry(app.id) is not None


class UninstallDialog(Adw.Dialog):
    """Uninstall one or more apps the thorough way: restore point, the app's own uninstaller,
    then a Powerful Scan for leftovers (folders, registry, shortcuts, services, tasks)."""

    def __init__(self, win, apps):
        title = f"Uninstall {apps[0].name}" if len(apps) == 1 else f"Uninstall {len(apps)} Apps"
        super().__init__(title=title, content_width=660, content_height=640)
        self.win, self.apps = win, list(apps)
        self.targets = {a.key: leftovers.Target.from_sysapp(a) for a in self.apps}
        self.removed, self.stuck, self.items = [], [], []
        self.freed = 0
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT, vexpand=True)
        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)
        self.stack.add_named(self.confirm_page(), "confirm")
        self.stack.add_named(self.work_page(), "work")
        self.scan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_named(self.scan_box, "scan")
        self.done_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_named(self.done_box, "done")
        self.set_can_close(True)

    # --- pages ---

    def page(self, *children, bottom=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        for c in children:
            box.append(c)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        if bottom:
            bar = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8, margin_bottom=12, margin_start=18,
                          margin_end=18)
            for b in bottom:
                bar.append(b)
            outer.append(bar)
        return outer

    def confirm_page(self):
        group = Adw.PreferencesGroup(title="Apps to remove")
        for app in self.apps:
            entry = app.extra.get("arp") if isinstance(app.extra, dict) else None
            how = ""
            if app.channel == "desktop" and entry:
                kind = inventory.uninstall_kind(entry)
                exe = winsys.parse_icon_spec(entry.uninstall)[0] if entry.uninstall.startswith('"') else \
                    entry.uninstall.split(" /")[0]
                if kind == "interactive":
                    how = "its own uninstaller opens a window"
                elif exe and not os.path.exists(exe) and kind != "msi":
                    how = "its uninstaller is missing: use Force Removal after the scan"
                else:
                    how = "removed quietly"
            elif app.channel == "store":
                how = "Microsoft Store app, removed for your account"
            elif app.channel == "portable":
                how = "folder goes to the Recycle Bin"
            bits = [app.source, app.version, core.human_size(app.size) if app.size else "",
                    f"installed {app.installed_on}" if app.installed_on else "",
                    "for all users" if app.machine else "", how]
            row = Adw.ActionRow(title=GLib.markup_escape_text(app.name),
                                subtitle=GLib.markup_escape_text(" · ".join(b for b in bits if b)))
            row.set_subtitle_lines(3)
            row.add_prefix(make_icon(app.icon, 36))
            group.add(row)
        options = Adw.PreferencesGroup(title="Options")
        self.restore = Adw.SwitchRow(
            title="Create a restore point first",
            subtitle="Lets you undo the change with System Restore. Needs administrator permission.",
            active=False, visible=winsys.elevates_as_self() and any(a.channel == "desktop" for a in self.apps))
        self.auto = Adw.SwitchRow(
            title="Remove leftovers automatically",
            subtitle="Delete what the scan finds under the app's full name without asking", active=False)
        options.add(self.restore)
        options.add(self.auto)
        note = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"], label=(
            "After the uninstaller finishes, App Hub scans for leftovers: folders go to the Recycle Bin and "
            f"registry keys are backed up to {helper.REG_BACKUPS} before they are deleted."))
        go = text_button("Uninstall", self.start, "destructive-action")
        go.add_css_class("pill")
        cancel = text_button("Cancel", self.close)
        cancel.add_css_class("pill")
        return self.page(group, options, note, bottom=[cancel, go])

    def work_page(self):
        self.work_title = Gtk.Label(xalign=0, css_classes=["title-3"], label="Uninstalling…")
        self.work_spinner = Gtk.Spinner(spinning=True)
        head = Gtk.Box(spacing=10)
        head.append(self.work_spinner)
        head.append(self.work_title)
        self.log_buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=10,
                            right_margin=10, css_classes=["card", "log-view"])
        self.log_view = view
        frame = Gtk.ScrolledWindow(child=view, vexpand=True, min_content_height=320)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(head)
        box.append(frame)
        return box

    def log(self, line):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), line + "\n")
        self.log_view.scroll_to_mark(self.log_buffer.get_insert(), 0, False, 0, 0)

    def working(self, title):
        self.work_title.set_text(title)
        self.stack.set_visible_child_name("work")
        self.set_can_close(False)

    # --- 1. the apps' own uninstallers ---

    def start(self):
        if self.win.busy:
            self.win.toast("Wait for the current task to finish")
            return
        self.win.ensure_closed([(a.name, a.location) for a in self.apps if a.channel != "store"], self.run)

    def run(self, close):
        restore = self.restore.get_active() and self.restore.get_visible()
        admin, user = [["keep-going"]], [["keep-going"]]
        if restore:
            names = ", ".join(a.name for a in self.apps)[:120]
            admin.append(["restore-point", f"App Hub: before uninstalling {names}"])
        for app in self.apps:
            step, needs_admin = uninstall_step(app, force_admin=restore and app.channel == "desktop")
            steps = updates.close_step(app.name, app.location) if close and app.channel != "store" else []
            (admin if needs_admin else user).extend(steps + [step])
        self.working("Uninstalling…")
        before = list(self.win.installed.values())

        def work(log):
            rc, out = helper.execute(admin, user, None, log, admin_first=restore)
            if rc in core.AUTH_CANCELLED:
                return rc, out
            log("Checking what is still installed")
            for app in self.apps:
                if still_installed(app):
                    self.stuck.append(app)
                    log(f"{app.name} is still installed")
                else:
                    self.removed.append(app)
                    history.record(app)
            log("Powerful Scan: looking for leftovers")
            self.items = self.scan([self.targets[a.key] for a in self.removed], before)
            log(f"Found {len(self.items)} leftover item(s)")
            return 0, out
        self.win.run_job(f"Uninstall {', '.join(a.name for a in self.apps)}", work, self.after_uninstall,
                         quiet=True, also_log=self.log)

    def scan(self, targets, before):
        gone = {a.key for a in self.removed}
        staying = [a for a in before if a.key not in gone and a not in self.stuck]
        return leftovers.scan(targets, staying) if targets else []

    def after_uninstall(self, rc):
        self.set_can_close(True)
        if rc in core.AUTH_CANCELLED:
            self.work_spinner.set_spinning(False)
            self.work_title.set_text("Administrator permission was not given")
            self.stack.set_visible_child_name("confirm")
            return
        if self.auto.get_active() and self.items and not self.stuck:
            self.delete([i for i in self.items if i.selected])
            return
        self.show_scan()

    # --- 2. Powerful Scan ---

    def show_scan(self):
        while child := self.scan_box.get_first_child():
            self.scan_box.remove(child)
        parts = []
        if self.removed:
            parts.append(f"{', '.join(a.name for a in self.removed)} uninstalled.")
        total = sum(i.size for i in self.items)
        if self.items:
            parts.append(f"The Powerful Scan found {len(self.items)} leftover item"
                         f"{'s' if len(self.items) != 1 else ''}" + (f" ({core.human_size(total)})" if total else "")
                         + ".")
        elif self.removed:
            parts.append("Nothing was left behind.")
        head = Gtk.Label(wrap=True, xalign=0, label=" ".join(parts), css_classes=["title-4"])
        children = [head]
        if self.stuck:
            stuck = Adw.PreferencesGroup(
                title="Still installed",
                description="The uninstaller was cancelled, failed or is missing. Force Removal deletes the "
                            "app's folder, its entry in Settings ▸ Apps and its leftovers.")
            for app in self.stuck:
                row = Adw.ActionRow(title=GLib.markup_escape_text(app.name),
                                    subtitle=GLib.markup_escape_text(app.location or app.id))
                row.add_prefix(make_icon(app.icon, 32))
                row.add_suffix(text_button("Force Removal", lambda a=app: self.force(a), "destructive-action"))
                stuck.add(row)
            children.append(stuck)
        self.checks = []
        if self.items:
            group = Adw.PreferencesGroup(title="Leftovers")
            for kind, title in leftovers.KINDS.items():
                items = [i for i in self.items if i.kind == kind]
                if items:
                    group.add(self.kind_row(title, items))
            children.append(group)
            if any(i.admin for i in self.items):
                children.append(Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"], label=(
                    "Items for all users (Program Files, ProgramData, HKLM, services, tasks) need administrator "
                    "permission: one prompt.")))
        if not self.items and not self.stuck:
            return self.finish()
        self.delete_btn = text_button("", lambda: self.delete([i for i in self.items if i.selected]),
                                      "destructive-action")
        self.delete_btn.add_css_class("pill")
        self.delete_btn.set_visible(bool(self.items))
        skip = text_button("Skip" if self.items else "Close", self.finish)
        skip.add_css_class("pill")
        self.scan_box.append(self.page(*children, bottom=[skip, self.delete_btn]))
        self.update_delete()
        self.stack.set_visible_child_name("scan")

    def kind_row(self, title, items):
        exp = Adw.ExpanderRow(title=title)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=all(i.selected for i in items))
        exp.add_prefix(check)
        checks = []

        def on_all(btn):
            for i, c in zip(items, checks):
                i.selected = btn.get_active()
                c.set_active(btn.get_active())
            self.update_delete()
        check.connect("toggled", on_all)
        for item in items[:400]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item.label),
                                subtitle=GLib.markup_escape_text(item.detail))
            row.set_subtitle_lines(2)
            c = Gtk.CheckButton(active=item.selected, valign=Gtk.Align.CENTER)

            def on_item(btn, it=item):
                it.selected = btn.get_active()
                self.update_delete()
            c.connect("toggled", on_item)
            row.add_prefix(c)
            row.set_activatable_widget(c)
            if item.size:
                row.add_suffix(Gtk.Label(label=core.human_size(item.size), css_classes=["dim-label"]))
            if item.admin:
                row.add_suffix(Gtk.Image(icon_name="security-high-symbolic",
                                         tooltip_text="Needs administrator permission"))
            checks.append(c)
            exp.add_row(row)
        n = len(items)
        exp.set_subtitle(f"{n} item{'s' if n != 1 else ''}" + (
            f" · {core.human_size(sum(i.size for i in items))}" if any(i.size for i in items) else ""))
        exp.set_expanded(n <= 6)
        return exp

    def update_delete(self):
        if not getattr(self, "delete_btn", None):
            return
        sel = [i for i in self.items if i.selected]
        size = sum(i.size for i in sel)
        self.delete_btn.set_label(f"Delete {len(sel)} Selected" + (f" ({core.human_size(size)})" if size else ""))
        self.delete_btn.set_sensitive(bool(sel) and not self.win.busy)

    def force(self, app):
        """The uninstaller did not do it: remove the app's folder and entry ourselves."""
        target = self.targets[app.key]
        target.force = True
        self.working(f"Force-removing {app.name}…")
        before = list(self.win.installed.values())

        def work(log):
            log(f"Powerful Scan for {app.name}, including its install folder and Settings entry")
            found = leftovers.scan([target], [a for a in before if a.key != app.key and a not in self.removed])
            known = {tuple(i.step) for i in self.items}
            self.items += [i for i in found if tuple(i.step) not in known]
            self.stuck = [a for a in self.stuck if a is not app]
            self.removed.append(app)
            history.record(app)
            log(f"Found {len(found)} item(s)")
            return 0, ""
        self.win.run_job(f"Scan {app.name}", work, lambda rc: self.show_scan(), quiet=True, also_log=self.log)

    # --- 3. delete the leftovers ---

    def delete(self, items):
        if not items:
            return self.finish()
        admin, user = leftovers.plans(items)
        self.freed = sum(i.size for i in items)
        self.deleted = len(items)
        self.working("Removing leftovers…")

        def work(log):
            return helper.execute(admin, user, None, log)
        self.win.run_job("Remove leftovers", work, lambda rc: self.finish(rc), quiet=True, also_log=self.log)

    def finish(self, rc=0):
        self.set_can_close(True)
        while child := self.done_box.get_first_child():
            self.done_box.remove(child)
        names = ", ".join(a.name for a in self.removed) or "Nothing"
        lines = [f"{names} {'was' if len(self.removed) == 1 else 'were'} uninstalled." if self.removed
                 else "No app was removed."]
        if getattr(self, "deleted", 0):
            if rc in core.AUTH_CANCELLED:
                lines.append("Leftovers were kept: administrator permission was not given.")
            else:
                lines.append(f"{self.deleted} leftover item(s) removed" +
                             (f", {core.human_size(self.freed)} freed." if self.freed else "."))
                if rc:
                    lines.append("Some items could not be removed; see the Activity Log.")
        if self.stuck:
            lines.append("Still installed: " + ", ".join(a.name for a in self.stuck))
        status = Adw.StatusPage(icon_name="emblem-ok-symbolic" if not self.stuck else "dialog-warning-symbolic",
                                title="Uninstall Finished", description="\n".join(lines), vexpand=True)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        if os.path.isdir(helper.REG_BACKUPS) and any(i.kind == "registry" and i.selected for i in self.items):
            buttons.append(text_button("Registry Backups", lambda: winsys.open_path(helper.REG_BACKUPS)))
        close = text_button("Close", self.close, "suggested-action")
        close.add_css_class("pill")
        buttons.append(close)
        status.set_child(buttons)
        self.done_box.append(status)
        self.stack.set_visible_child_name("done")


# --------------------------------------------------------------------------
# Updates: every pending update, one by one or all at once
# --------------------------------------------------------------------------

CHANNEL_NAMES = {"hub": "GitHub", "winget": "winget"}


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
            icon = make_icon(p.obj.icon, 36)
        self.add_prefix(icon)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.progress = Gtk.ProgressBar(visible=False, valign=Gtk.Align.CENTER, width_request=110,
                                        show_text=True)
        box.append(self.progress)
        box.append(badge(p.kind, CHANNEL_NAMES[p.kind]))
        self.btn = text_button("Update", lambda: page.win.update_pending([p]), "suggested-action")
        box.append(self.btn)
        self.add_suffix(box)
        # What's new: release notes for GitHub apps, the package for winget ones.
        if p.kind == "hub":
            rel = p.obj.release
            body = notes_text(rel.notes) if rel.notes else "No release notes were published."
            size = f"Download {core.human_size(rel.size)} · {rel.asset_name}" if rel.size else rel.asset_name
            body = f"{body}\n\n{size}"
            link = rel.url
        else:
            scope = "for all users" if p.obj.machine else "for you"
            body = f"winget package {p.obj.winget_id} ({p.obj.winget_source or 'winget'}), installed {scope}"
            if isinstance(p.obj.extra, dict) and p.obj.extra.get("by_name"):
                body += ("\n\nwinget did not recognise the installed copy; App Hub found this package by the "
                         "app's exact name. Updating installs the newest version over the old one.")
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
            web.connect("activated", lambda *_: open_uri(link))
            self.add_row(web)
        # Patched or licence-locked apps must stay as they are: ignore their updates.
        hold = Adw.ActionRow(title="Ignore this update",
                             subtitle="For apps that must stay at their version (patched, licence-locked, …)")
        hold.add_prefix(Gtk.Image(icon_name="action-unavailable-symbolic"))
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        buttons.append(text_button("Skip This Version", lambda: page.ignore(p, False)))
        buttons.append(text_button("Don't Update This App", lambda: page.ignore(p, True)))
        hold.add_suffix(buttons)
        self.add_row(hold)
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

    def set_stage(self, text, percent=None):
        _stage(self, text, percent)
        self.btn.set_visible(not text)


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
            title="System Updates", description="Runtimes and other packages winget can update")
        self.wu_group = Adw.PreferencesGroup(title="Windows")
        self.wu_row = Adw.ActionRow(title="Windows Update", subtitle="Checking…")
        self.wu_row.add_prefix(Gtk.Image(icon_name="emblem-system-symbolic", pixel_size=24))
        self.wu_row.add_suffix(text_button("Open Windows Update", lambda: open_uri("ms-settings:windowsupdate")))
        self.wu_group.add(self.wu_row)
        # Microsoft Store apps: the Store updates them, App Hub can ask it to do so now.
        self.store_row = Adw.ActionRow(title="Microsoft Store apps",
                                       subtitle="The Microsoft Store updates these apps")
        self.store_row.add_prefix(Gtk.Image(icon_name="view-app-grid-symbolic", pixel_size=24))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.store_btn = text_button("Check for Updates", self.store_updates)
        self.store_btn.set_tooltip_text("Ask the Store to update all its apps now (administrator permission)")
        box.append(self.store_btn)
        box.append(flat_button("adw-external-link-symbolic", "Open the Store's downloads page",
                               lambda: open_uri("ms-windows-store://downloadsandupdates")))
        self.store_row.add_suffix(box)
        self.wu_group.add(self.store_row)
        # Apps with no update source App Hub can check (they update themselves or via Windows Update).
        self.other_row = Adw.ExpanderRow(title="Apps App Hub cannot check",
                                         subtitle="They update themselves, or through Windows Update")
        self.other_row.add_prefix(Gtk.Image(icon_name="dialog-question-symbolic", pixel_size=24))
        self.other_row.set_visible(False)
        self.other_children = []
        self.wu_group.add(self.other_row)
        self.empty = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Everything is up to date",
                                    description="Your apps have no pending updates")
        self.empty.set_visible(False)
        self.ignored_group = Adw.PreferencesGroup(
            title="Ignored Updates", description="Not offered and not in Update All · Allow brings them back")
        self.ignored_group.set_visible(False)
        self.ignored_rows = []
        self.widget, _ = scrolled_page(top, self.empty, self.apps_group, self.sys_group, self.wu_group,
                                       self.ignored_group)
        self.sync()
        self.check_windows_update()

    def check_windows_update(self):
        self.wu_row.set_subtitle("Asking Windows Update…")

        def work():
            r = winsys.powershell(
                "$s = New-Object -ComObject Microsoft.Update.Session; $q = $s.CreateUpdateSearcher(); "
                "$r = $q.Search(\"IsInstalled=0 and IsHidden=0 and Type='Software'\"); "
                "$r.Updates.Count; foreach ($u in $r.Updates) { $u.Title }", timeout=300)
            lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
            if r.returncode != 0 or not lines or not lines[0].isdigit():
                text = "Security and feature updates for Windows itself"
            elif lines[0] == "0":
                text = "Windows is up to date"
            else:
                n = int(lines[0])
                text = f"{n} update{'s' if n != 1 else ''} waiting: " + "; ".join(lines[1:4]) + (" …" if n > 3 else "")
            _idle(self.wu_row.set_subtitle, GLib.markup_escape_text(text))
        _thread(work)

    def check(self):
        """Check every app again: GitHub releases, then winget and Windows Update."""
        self.checked_at = None
        self.win.home.refresh()
        self.win.library_page.refresh()
        self.win.apps_page.load()
        self.check_windows_update()
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
                text = "Checking installed apps with winget…"
            self.check_row.set_title(text)
            self.check_row.set_subtitle(f"{len(pending)} update{'s' if len(pending) != 1 else ''} found so far")
        else:
            if self.checked_at is None:
                self.checked_at = GLib.DateTime.new_now_local()
            n = len(pending) + len(sysup)
            self.check_row.set_title(f"{n} update{'s' if n != 1 else ''} available" if n
                                     else "Everything is up to date")
            stale = sum(1 for a in win.hub_apps() if a.release and a.release.stale)
            failed = sum(1 for a in win.hub_apps() if a.error and "no release" not in a.error)
            note = ""
            if stale or failed:
                note = (f" · GitHub did not answer for {stale + failed} app"
                        f"{'s' if stale + failed != 1 else ''}; sign in under Menu ▸ Preferences or try later")
            if not winget.available():
                note += " · winget is missing, so only GitHub apps were checked"
            self.check_row.set_subtitle(f"Checked {total} GitHub apps and "
                                        f"{len(win.apps_page.apps)} installed apps at "
                                        + self.checked_at.format("%H:%M") + note)
        overall = getattr(self, "overall", "")
        if overall and win.busy:            # several updates running: which one, how many left
            self.check_row.set_title(overall)
            self.check_row.set_subtitle("Each app below shows its own progress · Menu ▸ Activity Log has "
                                        "every step")
        self.check_spinner.set_visible(checking or bool(overall and win.busy))
        self.check_spinner.set_spinning(checking or bool(overall and win.busy))
        self.check_btn.set_sensitive(not checking and not win.busy)

        signature = (tuple((p.kind, p.label, p.new) for p in pending)
                     + tuple((r["id"], r["available"]) for r in sysup))
        if signature != self.signature:
            self.signature = signature
            self.rebuild(pending, sysup)
        for row in self.rows:
            row.sync()
        for _row, btn in self.sys_rows:
            btn.set_sensitive(not win.busy)
        self.update_all_btn.set_sensitive(not win.busy)
        self.update_all_btn.set_visible(len(pending) + len(self.sys_rows) > 1)
        self.empty.set_visible(not checking and not pending and not sysup)
        self.apps_group.set_visible(bool(pending))
        self.sys_group.set_visible(bool(sysup))
        win.set_updates_badge(len(pending) + len(sysup))

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
        for r in sysup:
            row = Adw.ActionRow(title=GLib.markup_escape_text(r["name"]),
                                subtitle=GLib.markup_escape_text(f"{r['version']}  →  {r['available']}"))
            row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic", pixel_size=24))
            box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            box.append(badge("winget", "winget"))
            app = inventory.SysApp("desktop", r["id"], r["name"], version=r["version"], update=r["available"],
                                   winget_id=r["id"], winget_source=r["source"], scope="machine")
            p = updates.Pending(r["name"], "winget", r["version"], r["available"], app)
            btn = text_button("Update", lambda p=p: self.win.update_pending([p]), "suggested-action")
            box.append(btn)
            row.add_suffix(box)
            self.sys_rows.append((row, btn))
            self.sys_group.add(row)
        self.sys_group.set_title(f"System Updates ({len(sysup)})")
        self.show_ignored()

    def row_for(self, app):
        return next((r for r in self.rows if r.pending.obj is app), None)

    def set_overall(self, text):
        self.overall = text
        self.sync()

    def ignore(self, pending, always):
        updates.ignore(pending, always)
        self.win.toast(f"{pending.label}: " + ("updates are ignored" if always else f"{pending.new} is skipped"))
        self.signature = None
        self.win.sync_update_everything()
        self.win.home.sync()
        self.win.library_page.sync()
        self.win.apps_page.sync()

    def allow(self, key, name):
        updates.allow(key)
        self.win.toast(f"{name}: updates are offered again")
        self.signature = None
        self.win.sync_update_everything()
        self.win.home.sync()
        self.win.library_page.sync()
        self.win.apps_page.sync()

    def show_ignored(self):
        for row in self.ignored_rows:
            self.ignored_group.remove(row)
        self.ignored_rows = []
        items = updates.ignored(self.win.hub_apps(), self.win.apps_page.apps if hasattr(self.win, "apps_page") else [])
        items += [updates.Pending(r["name"], "winget", r["version"], r["available"],
                                  inventory.SysApp("desktop", r["id"], r["name"], winget_id=r["id"]))
                  for r in self.win.all_system_upgrades if updates.row_ignored(r)]
        for p in items:
            how = updates.ignored_how(p)
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.label), subtitle=GLib.markup_escape_text(
                f"{p.old} → {p.new} · " + ("all updates ignored" if how == "app" else "this version skipped")))
            row.add_suffix(text_button("Allow", lambda p=p: self.allow(p.key, p.label)))
            self.ignored_rows.append(row)
            self.ignored_group.add(row)
        self.ignored_group.set_title(f"Ignored Updates ({len(items)})")
        self.ignored_group.set_visible(bool(items))

    def store_updates(self):
        def work(log):
            rc, out = helper.execute([["store-updates"]], [], None, log)
            if rc == 0:
                _idle(open_uri, "ms-windows-store://downloadsandupdates")
            return rc, out
        self.win.run_job("Update Microsoft Store apps", work)

    def show_sources(self, apps):
        """Store apps count, and the apps with no update source App Hub can check."""
        store = [a for a in apps if a.channel == "store"]
        self.store_row.set_subtitle(f"{len(store)} app{'s' if len(store) != 1 else ''} from the Store "
                                    "(Photos, WhatsApp, …) are updated by the Microsoft Store" if store
                                    else "The Microsoft Store updates its apps")
        other = [a for a in apps if a.channel == "desktop" and not a.winget_id and not a.hub_id]
        for row in self.other_children:
            self.other_row.remove(row)
        self.other_children = []
        for a in other:
            row = Adw.ActionRow(title=GLib.markup_escape_text(a.name),
                                subtitle=GLib.markup_escape_text(" · ".join(x for x in (a.source, a.version) if x)))
            row.add_prefix(make_icon(a.icon, 28))
            self.other_children.append(row)
            self.other_row.add_row(row)
        self.other_row.set_title(f"Apps App Hub cannot check ({len(other)})")
        self.other_row.set_visible(bool(other))
        self.store_btn.set_sensitive(not self.win.busy)


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
            "install it, and App Hub checks each answer against winget, the Microsoft Store and "
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
                link = text_button("Website", lambda u=found.homepage: open_uri(u))
                link.add_css_class("flat")
                group.set_header_suffix(link)
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
                row.add_suffix(text_button("Open", lambda u=uri: open_uri(u)))
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
            "Finds what uninstalled apps left behind and space Windows wastes. Folders and shortcuts "
            "go to the Recycle Bin, so you can restore them. Items for all users need administrator "
            "permission (one prompt)."))
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
                                     description="Press Scan to look for leftovers and wasted space")
        self.box.append(self.status)
        self.scanned = False

    def scan(self):
        self.scan_btn.set_sensitive(False)
        self.clean_btn.set_visible(False)
        while child := self.box.get_first_child():
            self.box.remove(child)
        self.status.set_title("Scanning…")
        self.status.set_description("Checking app folders, shortcuts, temporary files and caches")
        self.box.append(self.status)

        def work():
            apps = list(self.win.apps_page.apps) if self.win.inventory_loaded else None
            try:
                groups = cleaner.scan(apps)
            except Exception as e:  # noqa: BLE001 - show the problem instead of hanging on "Scanning…"
                groups = []
                _idle(self.win.alert, "Scan failed", f"{type(e).__name__}: {e}")
            _idle(self.show, groups)
        _thread(work)

    def show(self, groups):
        self.scanned = True
        self.scan_btn.set_sensitive(True)
        self.groups = groups
        self.enabled = {g.id: bool(g.items) and g.default_on for g in groups if not g.granular}
        while child := self.box.get_first_child():
            self.box.remove(child)
        total = sum(g.size for g in groups)
        count = sum(len(g.items) for g in groups)
        if not count:
            self.status.set_title("All clean")
            self.status.set_description("Nothing left behind and nothing to clear")
            self.box.append(self.status)
            return
        group = Adw.PreferencesGroup(title=f"Found {count} items · {core.human_size(total)}")
        for g in groups:
            group.add(self.group_row(g))
        self.box.append(group)
        self.clean_btn.set_visible(True)
        self.update_label()

    def group_row(self, g):
        admin = any(i.admin for i in g.items)
        exp = Adw.ExpanderRow(title=g.title, subtitle=GLib.markup_escape_text(
            f"{len(g.items)} items · {core.human_size(g.size)}\n{g.description}" if g.items
            else f"Nothing found\n{g.description}"))
        exp.set_subtitle_lines(3)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=bool(g.items))
        exp.add_prefix(check)
        if admin:
            exp.add_suffix(Gtk.Image(icon_name="security-high-symbolic",
                                     tooltip_text="Some items need administrator permission"))
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
        admin, user = cleaner.clean_actions(self.groups, self.enabled)
        if len(admin) + len(user) <= 2:
            self.win.toast("Nothing selected")
            return
        lines = []
        for g in self.groups:
            n = sum(i.selected for i in g.items) if g.granular else (len(g.items) if self.enabled.get(g.id) else 0)
            if n:
                lines.append(f"• {g.title}: {n}")
        if len(admin) > 1:
            lines.append("\nWindows asks for administrator permission once.")
        dialog = Adw.AlertDialog(heading="Clean these items?", body="\n".join(lines))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clean", "Clean")
        dialog.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, resp):
            if resp == "clean":
                self.win.run_job("Clean up", lambda log: helper.execute(admin, user, None, log),
                                 lambda rc: self.scan())
        dialog.connect("response", on_response)
        dialog.present(self.win)

    def sync(self):
        self.clean_btn.set_sensitive(not self.win.busy)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    @property
    def system_upgrades(self):
        """winget 'system' updates (runtimes etc.) without the ignored ones."""
        return [r for r in self.all_system_upgrades if not updates.row_ignored(r)]

    @system_upgrades.setter
    def system_upgrades(self, rows):
        self.all_system_upgrades = list(rows)

    def __init__(self, application):
        super().__init__(application=application, title="App Hub", default_width=1040, default_height=740)
        self.busy = False
        self.installed = {}          # inventory key -> SysApp
        self.inventory_loaded = False
        self.all_system_upgrades = []    # winget rows with updates outside the app list
        self.log_buffer = Gtk.TextBuffer()

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        header.pack_start(flat_button("view-refresh-symbolic", "Refresh", self.refresh_current))
        header.pack_start(flat_button("document-open-symbolic", "Install a program file (.exe, .msi, .zip)",
                                      self.pick_file))
        menu = Gio.Menu()
        menu.append("Install a Program File…", "win.install-file")
        menu.append("Update Everything…", "win.update-everything")
        menu.append("Activity Log", "win.log")
        menu.append("Preferences", "win.preferences")
        menu.append("About App Hub", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.busy_spinner = Gtk.Spinner(visible=False, tooltip_text="Working…")
        header.pack_end(self.busy_spinner)
        view.add_top_bar(header)
        # Every pending update (My Apps, Library, winget), one UAC prompt.
        self.everything_banner = Adw.Banner(button_label="Update Everything", revealed=False)
        self.everything_banner.connect("button-clicked", lambda *_: self.confirm_update_everything())
        view.add_top_bar(self.everything_banner)

        for name, cb in (("log", self.show_log), ("preferences", self.show_preferences),
                         ("update-everything", self.confirm_update_everything),
                         ("install-file", self.pick_file)):
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
        free, total = winsys.disk_usage(winsys.SYSTEM_DRIVE)
        if not total:
            return
        used = 1 - free / total
        drive = winsys.SYSTEM_DRIVE.rstrip("\\")
        self.disk_label.set_markup(f"Windows ({drive}): <b>{core.human_size(free)} free</b> of {core.human_size(total)}")
        self.disk_level.set_fraction(used)
        for cls, on in (("warning", 0.85 <= used < 0.95), ("error", used >= 0.95)):
            (self.disk_level.add_css_class if on else self.disk_level.remove_css_class)(cls)
        self.disk_level.set_tooltip_text(f"{used:.0%} used ({core.human_size(total - free)})")
        home_drive = os.path.splitdrive(winsys.HOME)[0].upper()
        separate = home_drive and home_drive != drive.upper()
        if separate:
            hfree, htotal = winsys.disk_usage(home_drive + "\\")
            self.home_label.set_text(f"Your files ({home_drive}): {core.human_size(hfree)} free of "
                                     f"{core.human_size(htotal)}")
        self.home_label.set_visible(bool(separate))

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
            description="The key is saved in %APPDATA%\\App Hub\\settings.json, in your Windows account. "
                        "The GEMINI_API_KEY environment variable overrides it.")
        key_row = Adw.PasswordEntryRow(title="Gemini API key", text=settings.get("gemini_api_key", ""))
        model_row = Adw.EntryRow(title="Model", text=settings.get("gemini_model") or ai.DEFAULT_MODEL)
        group.add(key_row)
        group.add(model_row)
        link = Adw.ActionRow(title="Get a free API key", subtitle="aistudio.google.com/apikey",
                             activatable=True)
        link.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        link.connect("activated", lambda *_: open_uri("https://aistudio.google.com/apikey"))
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
        tok_link = Adw.ActionRow(title="Create a token", subtitle="github.com/settings/personal-access-tokens",
                                 activatable=True)
        tok_link.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        tok_link.connect("activated", lambda *_: open_uri("https://github.com/settings/personal-access-tokens/new"))
        gh.add(tok_link)
        page.add(gh)
        look = Adw.PreferencesGroup(title="Appearance")
        theme = Adw.ComboRow(title="Style", subtitle="Light or dark",
                             model=Gtk.StringList.new(["Follow Windows", "Light", "Dark"]))
        theme.set_selected({"system": 0, "light": 1, "dark": 2}.get(settings.get("theme", "system"), 0))
        look.add(theme)
        page.add(look)
        if core.admin_pin():
            page.add(self.admin_group(dialog))
        dialog.add(page)

        def on_theme(*_):
            data = ai.load_settings()
            data["theme"] = ("system", "light", "dark")[theme.get_selected()]
            ai.save_settings(data)
            self.get_application().apply_theme(force=True)
        theme.connect("notify::selected", on_theme)

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
            self.home.set_admin(admin)

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
        """Update the given items (one UAC prompt), with download progress on their rows."""
        if self.busy or not pending:
            return

        def progress(app, done, total):
            row = self.updates_page.row_for(app)
            if row:
                _idle(row.set_progress, done, total)
        title = f"Update {pending[0].label}" if len(pending) == 1 else f"Update {len(pending)} apps"
        self.ensure_closed(self.pending_folders(pending), lambda close: self.run_job(
            title, lambda log: self.live_update(pending, log, progress=progress, close=close)))

    def live_update(self, pending, log, **kw):
        """updates.run with each app's row on the Updates page showing what happens to it."""
        rows = {p.label: r for p in pending if (r := self.updates_page.row_for(p.obj))}
        live, clear = stage_logger(log, rows, overall=self.updates_page.set_overall)
        try:
            return updates.run(pending, live, **kw)       # stages start after the downloads
        finally:
            clear()
            _idle(self.updates_page.set_overall, "")

    @staticmethod
    def pending_folders(pending):
        out = []
        for p in pending:
            if p.kind == "hub" and p.obj.entry and p.obj.section != "self":
                out.append((p.label, p.obj.entry.location))
            elif p.kind == "winget" and p.obj.location:
                out.append((p.label, p.obj.location))
        return out

    def ensure_closed(self, apps, go):
        """apps: [(name, install folder)]. If any of them is running, ask before closing it; then
        go(close) runs (close=True: the plan closes those apps first)."""
        if self.busy:
            self.toast("Wait for the current task to finish")
            return

        def check():
            running = [(name, folder) for name, folder in apps if folder and winsys.running_in(folder)]
            _idle(ask, running)

        def ask(running):
            if not running:
                go(False)
                return
            names = ", ".join(name for name, _ in running)
            dialog = Adw.AlertDialog(
                heading=f"Close {names}?",
                body=f"{names} {'is' if len(running) == 1 else 'are'} running and must be closed first. "
                     "App Hub asks it to close, and ends it if it does not. Save your work in it first.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("close", "Close and Continue")
            dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("close")
            dialog.connect("response", lambda _d, r: r == "close" and go(True))
            dialog.present(self)
        _thread(check)

    def sync_update_everything(self):
        if not hasattr(self, "everything_banner"):
            return
        n = len(self.pending_updates())
        m = len(self.system_upgrades)
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
        lines = [f"• {p.label}  {p.old} → {p.new}  ({CHANNEL_NAMES[p.kind]})" for p in pending]
        sysup = list(self.system_upgrades)
        if sysup:
            lines.append(f"• System packages ({len(sysup)}): " + ", ".join(r["name"] for r in sysup[:12])
                         + (" …" if len(sysup) > 12 else ""))
        if not lines:
            self.toast("Everything is up to date")
            return
        total = len(pending) + len(sysup)
        dialog = Adw.AlertDialog(
            heading=f"Install {total} update{'s' if total != 1 else ''}?",
            body="Windows asks for administrator permission once for all of them. "
                 "Close the apps that are being updated.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        listing = Gtk.Label(label="\n".join(lines), xalign=0, wrap=True, selectable=True)
        box.append(Gtk.ScrolledWindow(child=listing, min_content_height=min(240, 22 * len(lines) + 10),
                                      hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True))
        system = Gtk.CheckButton(label="Include system updates (winget)", active=True, visible=bool(sysup))
        box.append(system)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update All")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")

        def on_response(_d, resp):
            if resp == "update":
                include = system.get_active()
                self.ensure_closed(self.pending_folders(pending), lambda close: self.run_job(
                    "Update everything",
                    lambda log: self.live_update(pending, log, system=include, system_rows=sysup, close=close)))
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
        if name and self.stack.get_child_by_name(name):
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
        if hasattr(self, "updates_page"):
            self.updates_page.show_sources(apps)
        self.home.sync()
        self.sync_update_everything()

    def find_installed(self, recipe):
        """The installed SysApp a favorite recipe stands for, or None."""
        apps = self.installed.values()
        if recipe.channel in ("winget", "msstore"):
            hit = next((a for a in apps if a.winget_id.lower() == recipe.id.lower()), None)
            if hit:
                return hit
        if recipe.channel == "hub":
            hit = next((a for a in apps if a.hub_id == recipe.id), None)
            if hit:
                return hit
        key = inventory.norm(recipe.name)
        return next((a for a in apps if inventory.norm(a.name) == key), None)

    def ai_search(self, query):
        self.stack.set_visible_child_name("ai")
        self.ai_page.entry.set_text(query)
        self.ai_page.search()

    def favorites_changed(self):
        self.home.reload_favorites()
        self.apps_page.sync()
        self.ai_page.sync()

    # --- jobs ---

    def run_job(self, title, work, done=None, quiet=False, also_log=None):
        """work(log) -> (rc, output), run in a thread; one job at a time. quiet: no toast or
        alert at the end (the caller shows the result); also_log(line) gets every log line too."""
        if self.busy:
            self.toast("Wait for the current task to finish")
            return
        self.set_busy(True)
        self.log(f"== {title}")

        def log(line):
            self.log(line)
            if also_log:
                also_log(line)

        def thread():
            try:
                rc, out = work(lambda line: _idle(log, line))
            except core.HubError as e:
                rc, out = 1, str(e)
                _idle(self.log, out)
            except winsys.ElevationCancelled:
                rc, out = 1223, ""
            except Exception as e:  # noqa: BLE001 - keep the window usable whatever happens
                rc, out = 1, f"{type(e).__name__}: {e}"
                _idle(self.log, out)
            _idle(self.job_done, title, rc, out, done, quiet)
        _thread(thread)

    def job_done(self, title, rc, out, done, quiet=False):
        self.set_busy(False)
        if quiet:
            pass
        elif rc == 0:
            self.toast(f"{title}: done")
        elif rc in core.AUTH_CANCELLED:
            self.toast("Administrator permission was not given")
        else:
            tail = "\n".join((out or "").strip().splitlines()[-12:])
            self.alert(f"{title} failed", tail or f"exit code {rc}")
        if done:
            done(rc)
        inventory.forget_arp()
        self.home.after_job()
        self.library_page.after_job()
        self.apps_page.load(check_updates=False)
        self.update_disk()

    # --- actions ---

    def launch(self, app):
        try:
            winsys.open_path(app.launch)
        except OSError as e:
            self.toast(f"Could not open {app.name}: {e.strerror or e}")

    def open_hub(self, app):
        try:
            core.launch(app)
        except (core.HubError, OSError) as e:
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

    def confirm_uninstall(self, apps):
        """Open the uninstaller for one app or a selection (protected apps are left out)."""
        apps = [a for a in (apps if isinstance(apps, (list, tuple)) else [apps]) if not a.protected]
        if not apps or self.busy:
            return
        UninstallDialog(self, apps).present(self)

    def update_apps(self, apps):
        apps = [a for a in apps if a.hub_id or a.winget_id]
        if apps:
            self.ensure_closed([(a.name, a.location) for a in apps], lambda close: self._update_apps(apps, close))

    def _update_apps(self, apps, close):
        actions = []
        for app in apps:
            if app.hub_id:
                actions.append(favorites.install_actions(favorites.Recipe("hub", app.hub_id, app.name),
                                                         close=close))
            else:
                admin, user = updates.winget_step(app, close=close)
                actions.append((admin, user, None))
        names = apps[0].name if len(apps) == 1 else f"{len(apps)} apps"
        self.run_job(f"Update {names}", lambda log: helper.execute(*helper.combine(actions), log))

    def reinstall(self, entry):
        recipe = favorites.from_removed(entry)
        if not recipe:
            return

        def work(log):
            rc, out = helper.execute(*helper.combine([favorites.install_actions(recipe)]), log)
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
        self.run_job(title, lambda log: helper.execute(*helper.combine(actions), log))

    def confirm_recipe(self, recipe, status):
        third_party = recipe.channel in ("github", "url")
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

    # --- program files (.exe, .msi, .zip) ---

    def pick_file(self):
        flt = Gtk.FileFilter(name="Programs (.exe, .msi, .msix, .zip)")
        for pattern in ("*.exe", "*.msi", "*.msix", "*.msixbundle", "*.zip"):
            flt.add_pattern(pattern)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog = Gtk.FileDialog(title="Install a program file", filters=store, default_filter=flt)
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(winsys.downloads_dir()))
        except (GLib.Error, TypeError):
            pass

        def done(dlg, result):
            try:
                f = dlg.open_finish(result)
            except GLib.Error:
                return
            if f and f.get_path():
                self.open_file(f.get_path())
        dialog.open(self, None, done)

    def open_file(self, path):
        if not os.path.isfile(path):
            self.alert("File not found", path)
            return

        def prepare():
            kind = installer.detect(path)
            info = winsys.version_info(path) if path.lower().endswith(".exe") else {}
            status, signer = winsys.signature(path)
            _idle(self.confirm_file, path, kind, info, status, signer)
        _thread(prepare)

    def confirm_file(self, path, kind, info, status, signer):
        base = os.path.basename(path)
        if not kind:
            self.alert("Not a program", f"{base} is not an installer, .msi, .msix or portable .zip.")
            return
        name = (info.get("ProductName") or re.sub(r"[-_ .]*(setup|installer|install|x64|win(64|32)?|"
                                                    r"\d+(\.\d+)+).*$", "", os.path.splitext(base)[0],
                                                    flags=re.IGNORECASE) or os.path.splitext(base)[0]).strip()
        version = info.get("ProductVersion") or core.version_from_name(base)
        signed = (f"Signed by {signer}" if status == "Valid" and signer
                  else "Not signed: Windows cannot tell who made it" if status == "NotSigned"
                  else f"Signature: {status}")
        lines = [f"{installer.KINDS.get(kind, kind)}", f"Version: {version}" if version else "",
                 f"From: {info['CompanyName']}" if info.get("CompanyName") else "", signed,
                 f"Size: {core.human_size(os.path.getsize(path))}", f"File: {path}"]
        if kind == "exe-installer":
            lines.append("\nIts own setup window opens; follow its steps.")
        elif kind in ("zip", "portable-exe"):
            lines.append("\nUnpacked to your Programs folder, with a Start menu shortcut and an entry in "
                         "Settings ▸ Apps.")
        dialog = Adw.AlertDialog(heading=f"Install {name}?", body="\n".join(x for x in lines if x))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, resp: resp == "install" and self.install_file(path, kind, name,
                                                                                            version))
        dialog.present(self)

    def install_file(self, path, kind, name, version):
        def work(log):
            # The helper only runs installers from App Hub's own folder: copy the file there.
            folder = os.path.join(core.CACHE_DIR, "local")
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(folder, os.path.basename(path))
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(target):
                shutil.copyfile(path, target)
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "app"
            app = core.App(id=slug, name=name, summary="", repo="")
            app.release = core.Release(tag=version, version=version, url="")
            if kind == "msix":
                plan = [["store-add", target, name]]
                return helper.execute([], plan, None, log)
            step = installer.install_step(target, kind, name, app)
            admin = installer.needs_admin(kind) and not winsys.is_elevated()
            rc, out = helper.execute([step] if admin else [], [] if admin else [step], None, log)
            if rc == 0:
                core._unlink(target)
            return rc, out
        self.run_job(f"Install {name}", work)


# `app-hub --page NAME`: page name -> stack page.
PAGES = {"updates": "updates", "my-apps": "home", "library": "library",
         "all-apps": "apps", "ai": "ai", "cleaner": "cleaner"}


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)
        self.start_page, self.start_file = None, ""
        self.theme_state = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        settings = Gtk.Settings.get_default()
        if settings and winsys.IS_WIN:
            settings.set_property("gtk-font-name", "Segoe UI 10")
        self.css = Gtk.CssProvider()
        self.css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.accent_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.accent_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.apply_theme()
        GLib.timeout_add_seconds(3, lambda: self.apply_theme() or True)
        ipc.serve(lambda msg: _idle(self.on_message, msg))

    def apply_theme(self, force=False):
        """Light/dark and the accent colour from Windows (or the Preferences choice)."""
        choice = ai.load_settings().get("theme", "system") if force or self.theme_state is None else \
            self.theme_state[0]
        dark = winsys.apps_use_dark() if choice == "system" else choice == "dark"
        accent = winsys.accent_color()
        state = (choice, dark, accent)
        if state == self.theme_state and not force:
            return
        self.theme_state = state
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        if accent:
            self.accent_css.load_from_string(
                f":root {{ --accent-bg-color: {accent}; }}\n@define-color accent_bg_color {accent};")
        else:
            self.accent_css.load_from_string("")

    def window(self):
        return self.get_active_window() or Window(self)

    def on_message(self, msg):
        win = self.window()
        if msg.get("page"):
            win.show_page(msg["page"])
        win.present()
        if msg.get("file"):
            win.open_file(msg["file"])

    def do_activate(self):
        win = self.window()
        if self.start_page:
            win.show_page(self.start_page)
            self.start_page = None
        win.present()
        if self.start_file:
            path, self.start_file = self.start_file, ""
            GLib.idle_add(lambda: win.open_file(path) and False)

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="App Hub", application_icon="", version=__version__,
            developer_name="Md Hafijur Rahman", website="https://github.com/Hafij-DEU/Application-hub",
            comments="Install, update and uninstall apps on Windows: GitHub releases, winget and the "
                     "Microsoft Store, find install methods with AI and clean up leftovers.",
            debug_info=f"{winsys.windows_version()} · {winsys.machine_arch()}",
        ).present(self.get_active_window())


def snapshot_pages(win, folder, pages, delay=12):
    """Save each page as a PNG (APP_HUB_SNAPSHOT=folder: README screenshots and visual checks)."""
    gi.require_version("Graphene", "1.0")
    gi.require_version("Gsk", "4.0")
    from gi.repository import Graphene, Gsk
    os.makedirs(folder, exist_ok=True)
    todo = list(pages)

    def shoot():
        if not todo:
            win.get_application().quit()
            return False
        name = todo.pop(0)
        if name == "uninstall":          # the uninstaller's first page for two apps (nothing runs)
            win.show_page("apps")
            apps = [a for a in win.apps_page.apps if not a.protected and a.channel == "desktop"][:2]
            if apps:
                UninstallDialog(win, apps).present(win)
        elif name == "select":            # All Apps in selection mode
            win.show_page("apps")
            win.apps_page.select_btn.set_active(True)
            for row in win.apps_page.rows[:3]:
                row.check.set_active(not row.app.protected)
        else:
            win.show_page(name)

        def capture():
            child = Gtk.Window.get_child(win)        # includes open dialogs
            w, h = child.get_width(), child.get_height()
            snap = Gtk.Snapshot()
            bg = Gdk.RGBA()
            bg.parse("#222226" if Adw.StyleManager.get_default().get_dark() else "#fafafb")  # window background
            snap.append_color(bg, Graphene.Rect().init(0, 0, w, h))
            Gtk.WidgetPaintable.new(child).snapshot(snap, w, h)
            node = snap.to_node()
            if node is not None:
                renderer = Gsk.CairoRenderer.new()
                renderer.realize_for_display(win.get_display())
                texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
                renderer.unrealize()
                texture.save_to_png(os.path.join(folder, f"{name}.png"))
            GLib.timeout_add(200, shoot)
            return False
        GLib.timeout_add(2500 if name == "cleaner" else 1200, capture)
        return False
    GLib.timeout_add_seconds(delay, shoot)


def run(page=None, file=""):
    app = Application()
    app.start_page = page
    app.start_file = file or ""
    shots = os.environ.get("APP_HUB_SNAPSHOT")
    if shots:
        pages = (os.environ.get("APP_HUB_SNAPSHOT_PAGES") or "my-apps,updates,library,all-apps,ai,cleaner").split(",")
        app.connect_after("activate", lambda a: snapshot_pages(a.get_active_window(), shots, pages,
                                                         int(os.environ.get("APP_HUB_SNAPSHOT_DELAY", "12"))))
    return app.run([sys.argv[0]])
