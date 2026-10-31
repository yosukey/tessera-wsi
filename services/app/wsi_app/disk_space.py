"""The one free-space rule every large write in the app is checked against.

Several paths write gigabytes onto a volume the running install shares — a
slide upload (spooled, then copied under ``wsi/``), a restore / slide-import
archive (spooled, copied into ``BACKUP_DIR/.staging``, extracted), a backup or
a slide export (written into ``BACKUP_DIR``), an imported slide's WSI (copied
onto the data volume), the reset stage's DB snapshot. ``BACKUP_DIR`` and the
upload spool (``TMPDIR`` = ``DATA_DIR/.upload-tmp`` under systemd) default to
the data volume, so every one of them competes with the live DB, its WAL, the
job records and every other upload. A check per path, each with its own
idea of "fits" (1.1x plus 256 MiB, exactly 1x, 2x with no reserve, ...),
would leave an operator unable to predict which request a nearly-full disk
would refuse.

So every check asks the same question here — *will* ``nbytes`` *fit at*
``path`` *and still leave the install a reserve?* — and only the quantity
(``nbytes``) stays the caller's: the backup's estimate counting the DB twice
plus the WAL, a slide upload's spool + staged copy, an extraction's running
total, and so on. The reserve is :func:`reserve_for`.

Best-effort throughout: a volume whose free space cannot be read (``OSError``)
or which reports no capacity at all (``total == 0``: some FUSE mounts, /proc)
is *unknown*, never *full*, and the check passes. Refusing every write on such
a volume would be worse than letting the write's own ENOSPC handling — which
every caller keeps — decide. These checks only exist to fail fast, before
gigabytes are written, on the obvious cases.

No imports from the rest of ``wsi_app``: backup.py, slide_transfer.py,
admin_helpers.py and the routers all import this module, so it must not
import any of them back.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Optional

# The reserve a write must leave free on its volume: 10 % of what it writes,
# and never less than 256 MiB. The fraction scales with the write (a 500 GB
# restore leaves 50 GB, room for the DB / WAL to keep growing while the
# operator reacts); the floor is what keeps a small write meaningful — on a
# small install 10 % of a few-MB backup is a few hundred KB, no reserve at
# all — and is a few minutes of analytics / job-record / WAL growth on a busy
# install. Not a setting: one rule an operator can read in operations.md
# beats a knob per path, and the only per-install floor
# (TOUR_ASSET_MIN_FREE_BYTES, a documented setting of its own) is passed as
# an explicit override.
RESERVE_FRACTION = 0.1
RESERVE_MIN_BYTES = 256 * 1024 * 1024


def reserve_for(nbytes: int) -> int:
    """The free space a write of ``nbytes`` must leave behind on its volume."""
    return max(int(max(0, nbytes) * RESERVE_FRACTION), RESERVE_MIN_BYTES)


def free_bytes(path: str) -> Optional[int]:
    """Bytes free (to this unprivileged process) on the volume holding
    ``path``, or None when that cannot be known: the path cannot be statted,
    or the filesystem reports no capacity at all (``total <= 0``, which some
    FUSE mounts and pseudo filesystems do — that is "unknown", not "full")."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return usage.free


@dataclass(frozen=True)
class SpaceShortfall:
    """A write that will not fit: ``needed`` bytes plus a ``reserve`` left
    free were asked of the volume holding ``path``, which has ``free``."""

    path: str
    needed: int
    reserve: int
    free: int

    @property
    def required(self) -> int:
        return self.needed + self.reserve

    def message(self, what: str = "volume") -> str:
        """One wording for every refusal (the routers / jobs report it as the
        error text; the frontend shows the localised code beside it).
        ``what`` names the volume ("backup volume", "staging volume", …);
        callers keep the "insufficient free space on <what>" prefix existing
        callers and tests match on."""
        where = f" under {self.path}" if self.path else ""
        return (
            f"insufficient free space on {what}: {self.needed} bytes needed plus "
            f"a {self.reserve}-byte reserve left free for the running install, "
            f"but only {self.free} bytes free{where}"
        )

    def as_params(self) -> dict:
        """The figures as the API reports them (plan_import's ``data_space``)."""
        return {
            "needed_bytes": self.needed,
            "free_bytes": self.free,
            "reserve_bytes": self.reserve,
        }


def shortfall_given(
    nbytes: int,
    free: Optional[int],
    path: str = "",
    reserve: Optional[int] = None,
) -> Optional[SpaceShortfall]:
    """The shortfall of writing ``nbytes`` onto a volume measured at ``free``
    bytes earlier, or None when it fits — or when ``free`` is None (unknown).

    For a caller that measures once and checks many times, as an extraction
    does against its running total. ``reserve`` overrides :func:`reserve_for`;
    only an operator-configured floor (``TOUR_ASSET_MIN_FREE_BYTES``) uses it.
    A zero-byte write still needs the reserve free; callers that mean "size
    unknown" by 0 skip the check themselves."""
    if free is None:
        return None
    nbytes = max(0, int(nbytes))
    r = reserve_for(nbytes) if reserve is None else max(0, int(reserve))
    if nbytes + r > free:
        return SpaceShortfall(path=path, needed=nbytes, reserve=r, free=free)
    return None


def check(path: str, nbytes: int, reserve: Optional[int] = None) -> Optional[SpaceShortfall]:
    """Measure the volume holding ``path`` and return the shortfall of writing
    ``nbytes`` onto it (see :func:`shortfall_given`), None when it fits or the
    free space is unknown."""
    return shortfall_given(nbytes, free_bytes(path), path, reserve)
