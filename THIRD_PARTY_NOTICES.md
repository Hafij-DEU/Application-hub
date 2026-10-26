# Third-party notices

App Hub is licensed GPL-3.0 (see LICENSE). It is plain Python and uses these system components,
installed through apt and not bundled:

| Component | Used for | License |
| --- | --- | --- |
| [PyGObject](https://pygobject.gnome.org/) | Python bindings for GTK | LGPL-2.1+ |
| [GTK 4](https://www.gtk.org/) | User interface | LGPL-2.1+ |
| [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) | Adaptive widgets, About dialog | LGPL-2.1+ |
| [python-apt](https://tracker.debian.org/pkg/python-apt) | Package cache queries | GPL-2.0+ |
| polkit (pkexec) | Running the root helper | LGPL-2.0+ |
| dpkg, apt | Installing and removing packages | GPL-2.0+ |

The app icons of the Library's default apps come from the [storytold](https://github.com/storytold)
repositories under the Apache License 2.0. Each app that App Hub installs keeps its own license.
The optional AI install feature sends your question to Google's Gemini API with your own key.
