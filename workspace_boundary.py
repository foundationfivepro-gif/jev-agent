"""Independent approved-root filesystem boundary for local MCP reads.

Operator-supplied JEV_WORKSPACE_ROOTS (os.pathsep-separated absolute paths) is
required. A tool's root parameter never grants access. POSIX descriptor-relative
O_NOFOLLOW traversal rejects symlinks, including roots, at the actual open.
Platforms without these primitives fail closed rather than fall back to resolve.
"""
from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path, PurePath

from privacy import PrivacyError, screen_outbound

DEFAULT_MAX_BYTES = 1_000_000
MAX_FILES = 2000
MAX_ENTRIES = 20000
_DENIED = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".codex", ".agents", ".npmrc", ".pypirc", ".netrc"}
_SECRET_NAMES = (".env", "id_rsa", "id_ed25519", "credential", "secret", "serviceaccount")
_SECRET_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".keystore"}


class WorkspaceError(ValueError):
    code = "workspace_denied"
    def __init__(self, reason: str = "path denied") -> None:
        allowed = {"path denied", "approved root required", "unsupported platform", "not a regular file", "file too large", "binary or invalid UTF-8", "scan limit exceeded", "file changed"}
        super().__init__("workspace_denied: " + (reason if reason in allowed else "path denied"))


def _parts_allowed(parts: tuple[str, ...]) -> bool:
    for part in parts:
        low = part.lower()
        if low in _DENIED or any(s in low for s in _SECRET_NAMES) or Path(low).suffix in _SECRET_SUFFIXES:
            return False
    return True


def _absolute(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() or ".." in p.parts or "\x00" in str(p):
        raise WorkspaceError()
    return p


class ApprovedWorkspace:
    """An already-authorized root; use configured_workspace for external callers."""
    def __init__(self, root: str | os.PathLike, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = _absolute(root)
        if any(p.lower() in _DENIED | {"secrets", "credentials", ".env"} for p in self.root.parts):
            raise WorkspaceError()
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 0 < max_bytes <= DEFAULT_MAX_BYTES:
            raise WorkspaceError()
        self.max_bytes = max_bytes
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise WorkspaceError("unsupported platform")
        # Validate without resolving symlinks, then re-open on each actual read.
        fd = self._root_fd()
        os.close(fd)

    def _root_fd(self) -> int:
        fd = -1
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self.root.parts[1:]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except OSError:
            if fd >= 0:
                os.close(fd)
            raise WorkspaceError() from None

    def relative(self, path: str | os.PathLike) -> Path:
        p = Path(path)
        if ".." in p.parts or not p.parts or "\x00" in str(p):
            raise WorkspaceError()
        if p.is_absolute():
            try:
                p = p.relative_to(self.root)
            except ValueError:
                raise WorkspaceError() from None
        if not p.parts or not _parts_allowed(p.parts):
            raise WorkspaceError()
        try:
            screen_outbound(str(p))
        except PrivacyError:
            raise WorkspaceError() from None
        return p

    def read_text(self, path: str | os.PathLike) -> str:
        rel = self.relative(path)
        fd = self._root_fd()
        try:
            for part in rel.parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            leaf = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = leaf
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise WorkspaceError("not a regular file")
            if before.st_size > self.max_bytes:
                raise WorkspaceError("file too large")
            raw = bytearray()
            while len(raw) <= self.max_bytes:
                block = os.read(fd, min(65536, self.max_bytes + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
            if len(raw) > self.max_bytes:
                raise WorkspaceError("file too large")
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise WorkspaceError("file changed")
            if any(byte < 32 and byte not in (9, 10, 12, 13) or byte == 127 for byte in raw):
                raise WorkspaceError("binary or invalid UTF-8")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError("binary or invalid UTF-8") from None
        except OSError:
            raise WorkspaceError() from None
        finally:
            os.close(fd)

    def gather(self, patterns: list[str] | None = None, *, suffixes: set[str] | None = None, skip_dirs: set[str] | None = None) -> list[Path]:
        if patterns is not None and (len(patterns) > 100 or any(not isinstance(p, str) or Path(p).is_absolute() or ".." in Path(p).parts for p in patterns)):
            raise WorkspaceError()
        paths: list[Path] = []
        visited = 0
        def walk(fd: int, parent: Path, depth: int = 0) -> None:
            nonlocal visited
            if depth > 64:
                raise WorkspaceError("scan limit exceeded")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        raise WorkspaceError("scan limit exceeded")
                    rel = parent / entry.name
                    if not _parts_allowed(rel.parts) or (skip_dirs and skip_dirs.intersection(rel.parts)):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            try:
                                walk(child, rel, depth + 1)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= self.max_bytes:
                            match = patterns is None or any(PurePath(rel).match(p) or (p.startswith("**/") and fnmatch.fnmatch(str(rel), p[3:])) for p in patterns)
                            if match and (patterns is not None or suffixes is None or rel.suffix.lower() in suffixes):
                                self.relative(rel)
                                paths.append(self.root / rel)
                                if len(paths) > MAX_FILES:
                                    raise WorkspaceError("scan limit exceeded")
                    except OSError:
                        # A disappearing/replaced entry never gets a permissive retry.
                        continue
        fd = self._root_fd()
        try:
            walk(fd, Path())
        finally:
            os.close(fd)
        return sorted(paths)


def configured_workspace(root: str | os.PathLike | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ApprovedWorkspace:
    """Validate caller-selected root against independent operator configuration."""
    entries = [p for p in os.environ.get("JEV_WORKSPACE_ROOTS", "").split(os.pathsep) if p]
    if not entries:
        raise WorkspaceError("approved root required")
    approved = [_absolute(p) for p in entries]
    if root is None:
        if len(approved) != 1:
            raise WorkspaceError("approved root required")
        selected = approved[0]
    else:
        selected = _absolute(root)
    for anchor in approved:
        try:
            rel = selected.relative_to(anchor)
        except ValueError:
            continue
        if not _parts_allowed(rel.parts):
            raise WorkspaceError()
        # Check the anchor independently, including symlinks in its ancestors.
        ApprovedWorkspace(anchor, max_bytes=max_bytes)
        return ApprovedWorkspace(selected, max_bytes=max_bytes)
    raise WorkspaceError()


def read_approved_text(path: str | os.PathLike, *, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, str]:
    """Return safe root-relative name and bounded content for a configured root."""
    p = Path(path)
    if not p.is_absolute():
        ws = configured_workspace(max_bytes=max_bytes)
        return str(ws.relative(p)), ws.read_text(p)
    roots = [r for r in os.environ.get("JEV_WORKSPACE_ROOTS", "").split(os.pathsep) if r]
    for root in roots:
        ws = configured_workspace(root, max_bytes=max_bytes)
        try:
            relative = ws.relative(p)
        except WorkspaceError:
            continue
        return str(relative), ws.read_text(relative)
    raise WorkspaceError("approved root required")
