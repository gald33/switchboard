"""The Switchboard viewer: a read-only window on your rooms.

Installed separately from `agent-switchboard` on purpose. See the package
docstring in `viewer.py`, and `extras/viewer/README.md`.
"""

from .viewer import Room, discover, main, snapshot, summarise

__all__ = ["Room", "discover", "main", "snapshot", "summarise"]
