"""
Album Picker version - the single source of truth, shared by the GUI,
pick_album.py, albumfinder.py and build_exe.py.

Semantic versioning, MAJOR.MINOR.PATCH (see "Versioning" in README.md):
  * PATCH - bug fixes and small tweaks that add nothing new
  * MINOR - new features or settings (PATCH resets to 0)
  * MAJOR - breaking changes to the CSV / config formats (stays 0 until 1.0)

Bump it in every commit that changes the app; the git hooks in .githooks/
enforce that and tag each new version as vX.Y.Z.
"""

__version__ = "0.5.1"
