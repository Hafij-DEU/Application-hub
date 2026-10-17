# App Hub

App manager for Ubuntu and Debian: install, update and uninstall apps from GitHub releases,
apt, Snap, Flatpak and AppImage, from one window. Download the `.deb` from
[Releases](https://github.com/Hafij-DEU/Application-hub/releases). App Hub shows itself in its own section
above My Apps and updates itself from the same page.

App manager for Ubuntu/Debian. Tabs:

* **My Apps**: installs and updates these apps from their latest GitHub release, plus your **Favorites**.
  **Add App** (in the My Apps header) adds your own app: paste its GitHub link, or your account
  link (`github.com/your-name`) to list all your projects that publish a `.deb`. Added apps are
  kept in `~/.config/app-hub/library.json`; `app-hub add --mine owner/repo` does the same.

  | App | Repo | Package |
  |---|---|---|
  | Craft Suite | Hafij-BGE/software | `craft-suite` |
  | Markd | Hafij-BGE/markd | `markd` |
  | Draco Download Manager | Hafij-BGE/draco-dm | `draco-dm` |
  | DocScan (scanner) | Hafij-DEU/Scan | `docscan` |
  | Extendo (second monitor) | Hafij-BGE/Second-monitor | `extendo` |
  | Astra Player (video player, was Kiln Player) | Hafij-DEU/video-player | `astra-player` |

* **Updates**: checks every app (GitHub releases of My Apps and Library, Deb, Snap, Flatpak and
  system packages) and lists each pending update with old → new version, its channel and, for
  GitHub apps, the release notes. Update one at a time with its own button, or all at once.
  The tab shows how many updates are waiting.
* **Library**: apps from any GitHub project that publishes `.deb` releases, grouped by GitHub
  owner, with the same Install / Update / Open / Uninstall as My Apps. It starts with 13 apps
  (PhotoCraft, VectorCraft, FilmCraft, LightCraft, PdfCraft, EffectCraft, DesignCraft, WordCraft,
  DeckCraft, GridCraft, CADCraft, SoundCraft, FigCraft from github.com/storytold).
  **Add App** takes a repository link, a whole account or organization (lists every project with a
  `.deb` release), or an app name (Gemini suggests repositories; each one is checked on GitHub).
  App Hub reads the package name from the release `.deb` header (an HTTP range request, no full
  download) and the icon from the repository. Your additions are kept in
  `~/.config/app-hub/library.json`. `app-hub add owner/repo` does the same from a terminal.
* **Update Everything**: when any update is pending, a banner under the title bar (and Update All
  in All Apps, and Menu ▸ Update Everything…) updates everything at once: My Apps, Library, Deb,
  Snap, Flatpak and other system packages (`apt update` + `apt upgrade`, on by default). Downloads
  run first, then all root work goes to the helper in one plan, so you type the password once. One
  failing app does not stop the others. All Apps lists the pending system packages.
  Terminal: `app-hub update-all [--apps-only] [--yes]`.
* **All Apps**: every installed desktop app with its channel (Deb + its repository, Snap + channel,
  Flatpak + remote, AppImage + folder). You can update, open, star or uninstall each one.
  **Recently Uninstalled** lists apps removed in the last 60 days by any tool (read from apt's
  history, `flatpak history`, snapd and App Hub's own record) and has an Install button.
* **AI Install**: ask Gemini (with Google Search) how to install an app. Gemini may only answer
  with an apt package, snap, Flatpak, GitHub repo, or https `.deb`/AppImage link. App Hub checks
  each one (apt cache, Snap Store, `flatpak remote-info`, GitHub API, HTTPS HEAD) and you confirm
  before anything installs. Commands written by the AI are shown as notes and never run.
  Add the key under Menu ▸ Preferences (saved to `~/.config/app-hub/settings.json`, mode 600)
  or set `GEMINI_API_KEY`.
* **Cleaner**: leftovers of removed apps: their folders in `~/.config`, `~/.local/share`,
  `~/.cache`, `~/.var/app` and `~/snap` (moved to the Trash), plus removed packages' config files
  (`purge`), unused dependencies (`autoremove`), old snap revisions, unused Flatpak runtimes,
  downloaded `.deb` files (`apt clean`) and App Hub's own storage. A folder is offered only when
  its name matches a removed app, no installed app uses that name, and nothing wrote to it after
  the app was removed.

**GitHub sign-in**: without it GitHub answers only 60 update checks per hour per network. App Hub
uses your GitHub CLI login (`gh auth login`) when `gh` is installed, or a token saved under
Menu ▸ Preferences (it needs no permissions), and then gets 5000. When GitHub cannot be asked,
App Hub shows the last answer and says so. If an app's newest release has no `.deb` for this
system, App Hub offers the newest release that has one.

The bar at the bottom shows the free space on the Ubuntu disk (and on `/home` when it is a
separate partition), with **Free Up Space** to open the Cleaner.

**Install .deb** (header button or menu, or double-click a `.deb` and choose App Hub) shows the package,
version change and size, then installs it with `apt`, so dependencies come along.

## Install

```
sudo apt install ./app-hub_1.5.1_all.deb
```

## Terminal

```
app-hub list [--json]       # your GitHub apps: installed vs latest
app-hub update-all --dry-run [--json]   # pending app + system updates, installs nothing
app-hub install markd       app-hub update       app-hub remove extendo
app-hub apps [--channel snap]   # every installed app with channel and updates
app-hub recent              # recently uninstalled
app-hub install-deb FILE.deb
app-hub ai "pdf editor"     # Gemini suggestions, verified
app-hub favorites
app-hub --page updates      # open the window on a page (updates, my-apps, library,
                            # all-apps, ai, cleaner); switches the open window if running
```

## How root access works

Everything that needs root goes through `/usr/lib/app-hub/app-hub-helper`, started with
`pkexec` (one password prompt per task). It takes a JSON list of steps (`apt-install`,
`apt-remove`, `apt-purge`, `install-deb`, `snap-remove`, `flatpak-uninstall`, …) and checks every
package, snap, ref and channel name against a strict pattern, so nothing can be passed as an
option. `app-hub-helper --simulate PLAN` prints the commands and runs apt in simulate mode.

Build the package with `./build-deb.sh`.

## Credits

The Library's default app icons (PhotoCraft, VectorCraft, FilmCraft, LightCraft, PdfCraft,
EffectCraft, DesignCraft, WordCraft, DeckCraft, GridCraft, CADCraft, SoundCraft, FigCraft) come from
the [storytold](https://github.com/storytold) repositories and are used under the Apache License 2.0.
