"""Single source of truth for the application version.

Bump this before building a release. ``build.py`` reads it for the archive
name and ``gui.py`` / ``autodori.py`` read it for the title bar and logs, so
the version shown in the UI always matches the package that was built.
"""

VERSION = "v0.40"
