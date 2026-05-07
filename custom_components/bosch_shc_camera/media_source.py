"""Media source for Bosch Smart Home Camera event recordings.

Exposes downloaded events under HA's "Media" browser. Two backends:

* **Local** (``options['download_path']``, FCM-triggered saves)
  Files on HA's filesystem. Layout: ``{download_path}/{camera}/{stem}.{ext}``.

* **SMB / NAS** (``options['enable_smb_upload'] + smb_*``)
  Files on a remote SMB share. Layout follows the configured patterns; the
  default is ``{base_path}/{YYYY}/{MM}/{DD}/{camera}_{date}_{time}_{type}_{id}.{ext}``
  with all cameras sharing a day-folder.

Both backends are read-only and are served through ``/api/bosch_shc_camera/event/…``,
an authenticated ``HomeAssistantView`` with HTTP Range support so video clips can
seek. When only one backend is configured, the source/backend chooser is hidden
so users land directly on the meaningful content.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import raise_if_invalid_path

from .const import DEFAULT_OPTIONS, DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_PREFIX = f"/api/{DOMAIN}/event"
VIEW_REGISTERED_KEY = f"{DOMAIN}_media_view_registered"
SMB_SESSION_KEY = f"{DOMAIN}_smb_sessions"  # set of (server, username) already registered

# Filename pattern: "{Camera}_{YYYY-MM-DD}_{HH-MM-SS}_{TYPE}_{ID}.ext"
_FILE_RE = re.compile(
    r"^(?:(?P<camera>.+?)_)?(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})_(?P<etype>[A-Z_]+)_[0-9A-F]+\.(?P<ext>jpg|jpeg|mp4)$",
    re.IGNORECASE,
)
_DATE_DIR_RE = re.compile(r"^\d{2}$")  # YY-style two-digit dir name (year/month/day)
_YEAR_RE = re.compile(r"^\d{4}$")
# NVR segment files: "HH-MM.mp4" (5-min wall-aligned segments).
_NVR_SEG_RE = re.compile(r"^(?P<time>\d{2}-\d{2})\.mp4$")
_NVR_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHUNK = 256 * 1024


# ── helpers ──────────────────────────────────────────────────────────────────
def _safe_join(base: Path, relative: str) -> Path | None:
    try:
        raise_if_invalid_path(relative)
    except ValueError:
        return None
    base_abs = base.resolve()
    target = (base_abs / relative).resolve()
    try:
        target.relative_to(base_abs)
    except ValueError:
        return None
    return target


def _is_macos_junk(name: str) -> bool:
    return name.startswith("._") or name == ".DS_Store"


def _parse_filename(name: str) -> dict[str, str] | None:
    m = _FILE_RE.match(name)
    return m.groupdict() if m else None


def _format_event_title(parsed: dict[str, str]) -> str:
    cam = parsed.get("camera") or ""
    suffix = f"  ({cam})" if cam else ""
    return f"{parsed['time'].replace('-', ':')} — {parsed['etype']}{suffix}"


def _entry_title(hass: HomeAssistant, entry_id: str) -> str:
    cfg = hass.config_entries.async_get_entry(entry_id)
    return cfg.title if cfg else entry_id


# ── backends ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Source:
    entry_id: str
    kind: str  # "L" (local) or "S" (smb)
    label: str  # "Lokal" / "NAS …"


class _LocalBackend:
    """Read events from a local directory."""

    def __init__(self, base: str) -> None:
        self.base = Path(base)

    def list_cameras(self) -> list[str]:
        if not self.base.is_dir():
            return []
        return sorted(
            (p.name for p in self.base.iterdir() if p.is_dir() and not _is_macos_junk(p.name)),
            key=str.casefold,
        )

    def list_dates(self, camera: str) -> list[str]:
        cam_dir = _safe_join(self.base, camera)
        if cam_dir is None or not cam_dir.is_dir():
            return []
        dates: set[str] = set()
        for f in cam_dir.iterdir():
            if not f.is_file() or _is_macos_junk(f.name):
                continue
            parsed = _parse_filename(f.name)
            if parsed:
                dates.add(parsed["date"])
        return sorted(dates, reverse=True)

    def list_events(self, camera: str, date: str) -> list[tuple[str, str | None, dict[str, str]]]:
        """Return [(stem, image_filename_or_none, parsed)] for one (camera, date)."""
        cam_dir = _safe_join(self.base, camera)
        if cam_dir is None:
            return []
        groups: dict[str, dict[str, Any]] = {}
        for f in cam_dir.iterdir():
            if not f.is_file() or _is_macos_junk(f.name):
                continue
            parsed = _parse_filename(f.name)
            if not parsed or parsed["date"] != date:
                continue
            stem = f.stem
            ext = f.suffix.lower().lstrip(".")
            slot = groups.setdefault(stem, {"parsed": parsed, "files": {}})
            slot["files"][ext] = f.name
        out: list[tuple[str, str | None, dict[str, str]]] = []
        for stem in sorted(groups, reverse=True):
            files = groups[stem]["files"]
            video = files.get("mp4")
            image = files.get("jpg") or files.get("jpeg")
            preferred = video or image
            if preferred:
                out.append((preferred, image, groups[stem]["parsed"]))
        return out

    def resolve(self, *segments: str) -> Path | None:
        cur = self.base
        for s in segments:
            nxt = _safe_join(cur, s)
            if nxt is None:
                return None
            cur = nxt
        return cur if cur.is_file() else None


class _SmbBackend:
    """Read events from an SMB share via smbclient (requirements pulls smbprotocol)."""

    def __init__(self, hass: HomeAssistant, opts: dict[str, Any]) -> None:
        self.hass = hass
        self.server = (opts.get("smb_server") or "").strip()
        self.share = (opts.get("smb_share") or "").strip()
        self.username = (opts.get("smb_username") or "").strip()
        self.password = opts.get("smb_password") or ""
        base = (opts.get("smb_base_path") or "").strip().strip("/")
        self.base_parts: tuple[str, ...] = tuple(p for p in base.split("/") if p)

    @property
    def configured(self) -> bool:
        return bool(self.server and self.share)

    @property
    def label(self) -> str:
        return f"NAS \\\\{self.server}\\{self.share}"

    def _ensure_session(self) -> None:
        sessions = self.hass.data.setdefault(SMB_SESSION_KEY, set())
        key = (self.server, self.username)
        if key in sessions:
            return
        from smbclient import register_session
        register_session(self.server, username=self.username, password=self.password)
        sessions.add(key)

    def _path(self, *segments: str) -> str:
        all_parts = (self.share, *self.base_parts, *(s for s in segments if s))
        return "\\\\" + self.server + "\\" + "\\".join(all_parts)

    def _scandir_filtered(self, *segments: str, want_dirs: bool):
        from smbclient import scandir
        self._ensure_session()
        path = self._path(*segments)
        for e in scandir(path):
            if _is_macos_junk(e.name):
                continue
            if want_dirs and e.is_dir():
                yield e.name
            elif not want_dirs and e.is_file():
                yield e.name

    # tree (date-first, matches storage layout)
    def list_years(self) -> list[str]:
        return sorted(
            (n for n in self._scandir_filtered(want_dirs=True) if _YEAR_RE.match(n)),
            reverse=True,
        )

    def list_months(self, year: str) -> list[str]:
        return sorted(
            (n for n in self._scandir_filtered(year, want_dirs=True) if _DATE_DIR_RE.match(n)),
            reverse=True,
        )

    def list_days(self, year: str, month: str) -> list[str]:
        return sorted(
            (n for n in self._scandir_filtered(year, month, want_dirs=True) if _DATE_DIR_RE.match(n)),
            reverse=True,
        )

    def list_events(self, year: str, month: str, day: str) -> list[tuple[str, str | None, dict[str, str]]]:
        """Return [(preferred_filename, image_filename_or_none, parsed)]."""
        groups: dict[str, dict[str, Any]] = {}
        for name in self._scandir_filtered(year, month, day, want_dirs=False):
            parsed = _parse_filename(name)
            if not parsed:
                continue
            stem, _, ext = name.rpartition(".")
            slot = groups.setdefault(stem, {"parsed": parsed, "files": {}})
            slot["files"][ext.lower()] = name
        out: list[tuple[str, str | None, dict[str, str]]] = []
        for stem in sorted(groups, reverse=True):
            files = groups[stem]["files"]
            video = files.get("mp4")
            image = files.get("jpg") or files.get("jpeg")
            preferred = video or image
            if preferred:
                out.append((preferred, image, groups[stem]["parsed"]))
        return out

    def open_file(self, year: str, month: str, day: str, filename: str):
        """Return (file-like, size). Caller closes the file-like."""
        from smbclient import open_file, stat as smb_stat
        self._ensure_session()
        # Re-validate filename to block path traversal
        if "/" in filename or "\\" in filename or filename in (".", "..") or _is_macos_junk(filename):
            raise FileNotFoundError(filename)
        if not _parse_filename(filename):
            raise FileNotFoundError(filename)
        path = self._path(year, month, day, filename)
        st = smb_stat(path)
        return open_file(path, mode="rb"), st.st_size


class _NvrBackend:
    """Read continuous-recording segments from the local NVR base path.

    Layout: ``{base}/{Camera}/{YYYY-MM-DD}/HH-MM.mp4`` (Phase 1 MVP).
    """

    def __init__(self, base: str) -> None:
        self.base = Path(base)

    def list_cameras(self) -> list[str]:
        if not self.base.is_dir():
            return []
        return sorted(
            (p.name for p in self.base.iterdir() if p.is_dir() and not _is_macos_junk(p.name)),
            key=str.casefold,
        )

    def list_dates(self, camera: str) -> list[str]:
        cam_dir = _safe_join(self.base, camera)
        if cam_dir is None or not cam_dir.is_dir():
            return []
        return sorted(
            (
                d.name for d in cam_dir.iterdir()
                if d.is_dir() and _NVR_DATE_DIR_RE.match(d.name)
            ),
            reverse=True,
        )

    def list_segments(self, camera: str, date: str) -> list[tuple[str, str]]:
        """Return [(filename, label_HH:MM)] for one (camera, date)."""
        cam_dir = _safe_join(self.base, camera)
        if cam_dir is None:
            return []
        date_dir = _safe_join(cam_dir, date)
        if date_dir is None or not date_dir.is_dir():
            return []
        out: list[tuple[str, str]] = []
        for f in date_dir.iterdir():
            if not f.is_file() or _is_macos_junk(f.name):
                continue
            m = _NVR_SEG_RE.match(f.name)
            if not m:
                continue
            label = m.group("time").replace("-", ":")
            out.append((f.name, label))
        out.sort(reverse=True)
        return out

    def resolve(self, camera: str, date: str, filename: str) -> Path | None:
        cam_dir = _safe_join(self.base, camera)
        if cam_dir is None:
            return None
        date_dir = _safe_join(cam_dir, date)
        if date_dir is None:
            return None
        if not _NVR_DATE_DIR_RE.match(date) or not _NVR_SEG_RE.match(filename):
            return None
        target = _safe_join(date_dir, filename)
        return target if target is not None and target.is_file() else None


# ── source registry ──────────────────────────────────────────────────────────
def _enabled_sources(hass: HomeAssistant) -> list[tuple[_Source, _LocalBackend | _SmbBackend | _NvrBackend]]:
    out: list[tuple[_Source, _LocalBackend | _SmbBackend | _NvrBackend]] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coord = getattr(entry, "runtime_data", None)
        if coord is None:
            continue
        entry_id = entry.entry_id
        opts = coord.options
        # Per-entry filter: auto / local / smb / none
        filt = (opts.get("media_browser_source") or "auto").lower()
        if filt == "none":
            continue
        show_local = filt in ("auto", "local")
        show_smb = filt in ("auto", "smb")
        if show_local and opts.get("download_path"):
            base = (opts.get("download_path") or "").strip() or DEFAULT_OPTIONS.get("download_path", "")
            try:
                base_path = Path(base)
                # Create the directory on first access so the Media Browser
                # entry appears immediately after the user enables auto-download
                # — even before the first event has been downloaded. Without
                # this, the entry stayed hidden until the first poll cycle
                # actually wrote a file (the v10.7.1 fix only set the default
                # path; the directory still had to exist on disk).
                if not base_path.exists():
                    base_path.mkdir(parents=True, exist_ok=True)
                if base_path.is_dir():
                    out.append((_Source(entry_id, "L", "Lokal"), _LocalBackend(base)))
            except OSError:
                pass
        if show_smb and opts.get("enable_smb_upload") and (opts.get("upload_protocol") or "smb").lower() == "smb":
            smb = _SmbBackend(hass, opts)
            if smb.configured:
                out.append((_Source(entry_id, "S", smb.label), smb))
        # Mini-NVR continuous recording — always shown when enabled (filt
        # `auto`/`local` only, NAS-only filter hides it). Backend lives on the
        # local FS even though it might be a NAS bind-mount.
        if show_local and opts.get("enable_nvr"):
            base = (opts.get("nvr_base_path") or "/config/bosch_nvr").strip()
            try:
                base_path = Path(base)
                if base_path.is_dir():
                    out.append((_Source(entry_id, "N", "Aufnahmen"), _NvrBackend(base)))
            except OSError:
                pass
    return out


def _find_source(
    hass: HomeAssistant, entry_id: str, kind: str
) -> tuple[_Source, _LocalBackend | _SmbBackend] | None:
    for src, backend in _enabled_sources(hass):
        if src.entry_id == entry_id and src.kind == kind:
            return src, backend
    return None


# ── media source ────────────────────────────────────────────────────────────
async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    if not hass.data.get(VIEW_REGISTERED_KEY):
        hass.http.register_view(BoschCameraMediaView(hass))
        hass.data[VIEW_REGISTERED_KEY] = True
    return BoschCameraMediaSource(hass)


def _node(
    *,
    identifier: str,
    title: str,
    media_class: str = MediaClass.DIRECTORY,
    media_content_type: str = "",
    children: list[BrowseMediaSource] | None = None,
    children_media_class: str = MediaClass.DIRECTORY,
    can_play: bool = False,
    can_expand: bool = True,
    thumbnail: str | None = None,
) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=media_class,
        media_content_type=media_content_type,
        title=title,
        can_play=can_play,
        can_expand=can_expand,
        children=children,
        children_media_class=children_media_class,
        thumbnail=thumbnail,
    )


class BoschCameraMediaSource(MediaSource):
    name = "Bosch SHC Camera"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        if not item.identifier:
            raise Unresolvable("Cannot play the root folder")
        url = f"{URL_PREFIX}/{item.identifier}"
        mime, _ = mimetypes.guess_type(item.identifier)
        return PlayMedia(url, mime or "application/octet-stream")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        try:
            return await self.hass.async_add_executor_job(self._browse, item.identifier or "")
        except Unresolvable as err:
            raise BrowseError(str(err)) from err

    # ── browse dispatch ──────────────────────────────────────────────────────
    def _browse(self, identifier: str) -> BrowseMediaSource:
        sources = _enabled_sources(self.hass)
        if not sources:
            return _node(identifier="", title=self.name, children=[])

        # Group by entry to drive root-level skipping.
        by_entry: dict[str, list[tuple[_Source, Any]]] = {}
        for src, b in sources:
            by_entry.setdefault(src.entry_id, []).append((src, b))

        if not identifier:
            entry_ids = list(by_entry.keys())
            if len(entry_ids) == 1:
                only_entry = entry_ids[0]
                return self._browse_entry_root(only_entry, by_entry[only_entry], root=True)
            children = [
                _node(
                    identifier=eid,
                    title=_entry_title(self.hass, eid),
                )
                for eid in entry_ids
            ]
            return _node(identifier="", title=self.name, children=children)

        parts = identifier.split("/")
        entry_id = parts[0]
        if entry_id not in by_entry:
            raise Unresolvable(f"Unknown entry: {entry_id}")

        # Entry root view (lists sources if more than one)
        if len(parts) == 1:
            return self._browse_entry_root(entry_id, by_entry[entry_id], root=False)

        # Identifiers under a single-source entry omit the source token (the
        # tree skips the chooser level), so parts[1] is already a tree segment
        # (year for SMB / camera for local / camera for NVR). Detect that case
        # and pick the source implicitly from the entry's only backend.
        # Use the actual backend kind to distinguish a bare tree-segment from a
        # source-kind token — this handles backwards-compatible bookmarks
        # (old multi-source identifiers like "{entry_id}/L/cam") while also
        # working correctly for camera names that coincidentally equal "L"/"S"/"N".
        single_source = len(by_entry[entry_id]) == 1
        actual_kind = by_entry[entry_id][0][0].kind if single_source else None
        if single_source and parts[1] != actual_kind:
            src, backend = by_entry[entry_id][0]
            rest = parts[1:]
        else:
            kind = parts[1]
            match = _find_source(self.hass, entry_id, kind)
            if match is None:
                raise Unresolvable(f"Unknown source kind: {kind}")
            src, backend = match
            rest = parts[2:]

        if isinstance(backend, _LocalBackend):
            return self._browse_local(src, backend, rest, single_source=single_source)
        if isinstance(backend, _NvrBackend):
            return self._browse_nvr(src, backend, rest, single_source=single_source)
        return self._browse_smb(src, backend, rest, single_source=single_source)

    def _browse_entry_root(
        self,
        entry_id: str,
        sources_for_entry: list[tuple[_Source, Any]],
        *,
        root: bool,
    ) -> BrowseMediaSource:
        if len(sources_for_entry) == 1:
            src, backend = sources_for_entry[0]
            if isinstance(backend, _LocalBackend):
                return self._browse_local(src, backend, [], single_source=True, root=root)
            if isinstance(backend, _NvrBackend):
                return self._browse_nvr(src, backend, [], single_source=True, root=root)
            return self._browse_smb(src, backend, [], single_source=True, root=root)

        children = [
            _node(
                identifier=f"{entry_id}/{src.kind}",
                title=src.label,
            )
            for src, _ in sources_for_entry
        ]
        title = self.name if root else _entry_title(self.hass, entry_id)
        return _node(
            identifier="" if root else entry_id,
            title=title,
            children=children,
        )

    # ── local backend tree ──────────────────────────────────────────────────
    def _browse_local(
        self,
        src: _Source,
        backend: _LocalBackend,
        rest: list[str],
        *,
        single_source: bool,
        root: bool = False,
    ) -> BrowseMediaSource:
        prefix = src.entry_id if single_source else f"{src.entry_id}/{src.kind}"
        ident = lambda *parts: "/".join((prefix, *parts)) if parts else prefix

        if not rest:
            children = [
                _node(identifier=ident(cam), title=cam) for cam in backend.list_cameras()
            ]
            title = self.name if root else (
                _entry_title(self.hass, src.entry_id) if single_source else src.label
            )
            return _node(
                identifier="" if root else prefix,
                title=title,
                children=children,
            )

        camera = rest[0]
        if len(rest) == 1:
            children = [
                _node(identifier=ident(camera, d), title=d) for d in backend.list_dates(camera)
            ]
            return _node(identifier=ident(camera), title=camera, children=children)

        if len(rest) == 2:
            date = rest[1]
            children = []
            for fname, image, parsed in backend.list_events(camera, date):
                ext = fname.rsplit(".", 1)[-1].lower()
                mime = "video/mp4" if ext == "mp4" else "image/jpeg"
                mc = MediaClass.VIDEO if ext == "mp4" else MediaClass.IMAGE
                thumb = (
                    f"{URL_PREFIX}/{ident(camera, image)}" if image else None
                )
                children.append(_node(
                    identifier=ident(camera, fname),
                    title=_format_event_title(parsed),
                    media_class=mc,
                    media_content_type=mime,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumb,
                ))
            return _node(
                identifier=ident(camera, date),
                title=date,
                children=children,
                children_media_class=MediaClass.VIDEO,
            )

        raise Unresolvable(f"Cannot browse: {'/'.join(rest)}")

    # ── nvr backend tree (camera/date/segment) ──────────────────────────────
    def _browse_nvr(
        self,
        src: _Source,
        backend: _NvrBackend,
        rest: list[str],
        *,
        single_source: bool,
        root: bool = False,
    ) -> BrowseMediaSource:
        prefix = src.entry_id if single_source else f"{src.entry_id}/{src.kind}"
        ident = lambda *parts: "/".join((prefix, *parts)) if parts else prefix

        if not rest:
            children = [
                _node(identifier=ident(cam), title=cam) for cam in backend.list_cameras()
            ]
            title = self.name if root else (
                _entry_title(self.hass, src.entry_id) if single_source else src.label
            )
            return _node(
                identifier="" if root else prefix,
                title=title,
                children=children,
            )

        camera = rest[0]
        if len(rest) == 1:
            children = [
                _node(identifier=ident(camera, d), title=d) for d in backend.list_dates(camera)
            ]
            return _node(identifier=ident(camera), title=camera, children=children)

        if len(rest) == 2:
            date = rest[1]
            children = []
            for fname, label in backend.list_segments(camera, date):
                children.append(_node(
                    identifier=ident(camera, date, fname),
                    title=label,
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    can_play=True,
                    can_expand=False,
                ))
            return _node(
                identifier=ident(camera, date),
                title=date,
                children=children,
                children_media_class=MediaClass.VIDEO,
            )

        raise Unresolvable(f"Cannot browse: {'/'.join(rest)}")

    # ── smb backend tree (date-first: year/month/day/event) ─────────────────
    def _browse_smb(
        self,
        src: _Source,
        backend: _SmbBackend,
        rest: list[str],
        *,
        single_source: bool,
        root: bool = False,
    ) -> BrowseMediaSource:
        prefix = src.entry_id if single_source else f"{src.entry_id}/{src.kind}"
        ident = lambda *parts: "/".join((prefix, *parts)) if parts else prefix

        if not rest:  # years
            children = [_node(identifier=ident(y), title=y) for y in backend.list_years()]
            title = self.name if root else (
                _entry_title(self.hass, src.entry_id) if single_source else src.label
            )
            return _node(
                identifier="" if root else prefix,
                title=title,
                children=children,
            )

        if len(rest) == 1:  # months
            year = rest[0]
            children = [_node(identifier=ident(year, m), title=m) for m in backend.list_months(year)]
            return _node(identifier=ident(year), title=year, children=children)

        if len(rest) == 2:  # days
            year, month = rest
            children = [
                _node(identifier=ident(year, month, d), title=f"{year}-{month}-{d}")
                for d in backend.list_days(year, month)
            ]
            return _node(identifier=ident(year, month), title=f"{year}-{month}", children=children)

        if len(rest) == 3:  # events
            year, month, day = rest
            children = []
            for fname, image, parsed in backend.list_events(year, month, day):
                ext = fname.rsplit(".", 1)[-1].lower()
                mime = "video/mp4" if ext == "mp4" else "image/jpeg"
                mc = MediaClass.VIDEO if ext == "mp4" else MediaClass.IMAGE
                thumb = (
                    f"{URL_PREFIX}/{ident(year, month, day, image)}" if image else None
                )
                children.append(_node(
                    identifier=ident(year, month, day, fname),
                    title=_format_event_title(parsed),
                    media_class=mc,
                    media_content_type=mime,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumb,
                ))
            return _node(
                identifier=ident(year, month, day),
                title=f"{year}-{month}-{day}",
                children=children,
                children_media_class=MediaClass.VIDEO,
            )

        raise Unresolvable(f"Cannot browse: {'/'.join(rest)}")


# ── HTTP view ────────────────────────────────────────────────────────────────
class BoschCameraMediaView(HomeAssistantView):
    """Serve event jpg/mp4 files from local FS or via SMB. Auth required."""

    name = f"api:{DOMAIN}:event"
    url = URL_PREFIX + "/{entry_id}/{location:.*}"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, location: str
    ) -> web.StreamResponse:
        parts = [p for p in location.split("/") if p]
        if not parts:
            raise web.HTTPNotFound

        # Identifier shapes (with optional source token):
        #   {entry_id}/L/{camera}/{filename}              (local, multi-source)
        #   {entry_id}/{camera}/{filename}                (local, single-source)
        #   {entry_id}/S/{Y}/{M}/{D}/{filename}           (smb, multi-source)
        #   {entry_id}/{Y}/{M}/{D}/{filename}             (smb, single-source)
        #   {entry_id}/N/{camera}/{YYYY-MM-DD}/{file}.mp4 (nvr, multi-source)
        #   {entry_id}/{camera}/{YYYY-MM-DD}/{file}.mp4   (nvr, single-source)
        head = parts[0]
        if head in ("L", "S", "N"):
            kind = head
            tail = parts[1:]
        elif _YEAR_RE.match(head):
            kind = "S"
            tail = parts
        elif len(parts) >= 3 and _NVR_DATE_DIR_RE.match(parts[1]):
            # camera/YYYY-MM-DD/HH-MM.mp4 → NVR single-source.
            kind = "N"
            tail = parts
        else:
            kind = "L"
            tail = parts

        match = _find_source(self.hass, entry_id, kind)
        if match is None:
            raise web.HTTPNotFound
        _src, backend = match

        if isinstance(backend, _LocalBackend):
            if len(tail) != 2:
                raise web.HTTPNotFound
            camera, filename = tail
            return await self._serve_local(request, backend, camera, filename)

        if isinstance(backend, _NvrBackend):
            if len(tail) != 3:
                raise web.HTTPNotFound
            camera, date, filename = tail
            return await self._serve_nvr(request, backend, camera, date, filename)

        if len(tail) != 4:
            raise web.HTTPNotFound
        year, month, day, filename = tail
        return await self._serve_smb(request, backend, year, month, day, filename)

    # local path → web.FileResponse handles Range natively
    async def _serve_local(
        self, request: web.Request, backend: _LocalBackend, camera: str, filename: str
    ) -> web.StreamResponse:
        if not _parse_filename(filename):
            raise web.HTTPNotFound
        path = await self.hass.async_add_executor_job(backend.resolve, camera, filename)
        if path is None:
            raise web.HTTPNotFound
        mime, _ = mimetypes.guess_type(str(path))
        if mime not in ("image/jpeg", "video/mp4"):
            raise web.HTTPNotFound
        return web.FileResponse(path)

    # nvr path → web.FileResponse handles Range natively (mp4 only)
    async def _serve_nvr(
        self, request: web.Request, backend: _NvrBackend,
        camera: str, date: str, filename: str,
    ) -> web.StreamResponse:
        if not _NVR_DATE_DIR_RE.match(date) or not _NVR_SEG_RE.match(filename):
            raise web.HTTPNotFound
        path = await self.hass.async_add_executor_job(
            backend.resolve, camera, date, filename,
        )
        if path is None:
            raise web.HTTPNotFound
        return web.FileResponse(path)

    # smb path → manual stream with Range support
    async def _serve_smb(
        self,
        request: web.Request,
        backend: _SmbBackend,
        year: str,
        month: str,
        day: str,
        filename: str,
    ) -> web.StreamResponse:
        if not (_YEAR_RE.match(year) and _DATE_DIR_RE.match(month) and _DATE_DIR_RE.match(day)):
            raise web.HTTPNotFound
        try:
            fobj, size = await self.hass.async_add_executor_job(
                backend.open_file, year, month, day, filename
            )
        except FileNotFoundError as err:
            raise web.HTTPNotFound from err
        except OSError as err:
            _LOGGER.warning("SMB open failed for %s/%s/%s/%s: %s", year, month, day, filename, err)
            raise web.HTTPNotFound from err

        try:
            mime, _ = mimetypes.guess_type(filename)
            mime = mime or "application/octet-stream"

            start, end = 0, size - 1
            status = 200
            range_header = request.headers.get("Range", "")
            if range_header.startswith("bytes="):
                spec = range_header[6:].strip()
                s, _, e = spec.partition("-")
                try:
                    if s:
                        start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                    if 0 <= start <= end < size:
                        status = 206
                    else:
                        start, end, status = 0, size - 1, 200
                except ValueError:
                    start, end, status = 0, size - 1, 200

            length = end - start + 1
            headers = {
                "Content-Type": mime,
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            }
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"

            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)

            if start > 0:
                await self.hass.async_add_executor_job(fobj.seek, start)
            remaining = length
            while remaining > 0:
                chunk_size = min(remaining, _CHUNK)
                chunk = await self.hass.async_add_executor_job(fobj.read, chunk_size)
                if not chunk:
                    break
                await response.write(chunk)
                remaining -= len(chunk)
            await response.write_eof()
            return response
        finally:
            await self.hass.async_add_executor_job(fobj.close)
