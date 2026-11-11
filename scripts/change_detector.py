#!/usr/bin/env python3
"""Poll this workspace and send desktop notifications for file changes."""

import argparse
import codecs
import difflib
import hashlib
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# Keep previews small enough for desktop notification services.
TEXT_PREVIEW_LIMIT = 256 * 1024
DETAIL_LIMIT = 220


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    digest: str | None = None
    is_text: bool = False
    text: str | None = None
    text_complete: bool = False
    error: str | None = None

    def signature(self) -> tuple[object, ...]:
        # Timestamps and inode numbers are intentionally excluded: touching or
        # replacing a file with identical content is not a content change.
        return (self.kind, self.mode, self.digest, self.is_text,
                self.text_complete, self.error)


def _read_file(path: Path, mode: int) -> Entry:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    collected: list[str] = []
    size = 0
    is_text = True
    text_complete = True

    try:
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                size += len(chunk)
                digest.update(chunk)
                if is_text:
                    try:
                        decoded = decoder.decode(chunk)
                        if "\x00" in decoded:
                            is_text = False
                        elif text_complete:
                            if size <= TEXT_PREVIEW_LIMIT:
                                collected.append(decoded)
                            else:
                                collected.clear()
                                text_complete = False
                    except UnicodeDecodeError:
                        is_text = False
                        collected.clear()
                        text_complete = False
            if is_text:
                try:
                    tail = decoder.decode(b"", final=True)
                    if "\x00" in tail:
                        is_text = False
                    elif text_complete:
                        collected.append(tail)
                except UnicodeDecodeError:
                    is_text = False
                    text_complete = False
    except OSError as error:
        message = error.strerror or error.__class__.__name__
        return Entry("file", mode, error=f"{message} (errno {error.errno})")

    return Entry(
        "file",
        mode,
        digest.hexdigest(),
        is_text,
        "".join(collected) if is_text and text_complete else None,
        text_complete,
    )


def snapshot(root: Path) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}

    def report_walk_error(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(root, onerror=report_walk_error):
        directories[:] = [name for name in directories if name != ".git"]

        for name in directories:
            path = Path(current, name)
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            entries[relative] = Entry("directory", stat.S_IMODE(info.st_mode))

        for name in filenames:
            path = Path(current, name)
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            entries[relative] = _read_file(path, stat.S_IMODE(info.st_mode))

    return entries


def _clean_piece(text: str, limit: int = 72) -> str:
    # Collapse line breaks and remove terminal/control characters from previews.
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text or "(blank line)"


def _text_detail(old: str | None, new: str | None, *, added: bool = False,
                 removed: bool = False) -> str:
    if old is None and new is None:
        return "text content changed (preview unavailable; file exceeds limit or was unreadable)"
    before = old.splitlines() if old is not None else []
    after = new.splitlines() if new is not None else []
    if added:
        changes = [("+", line) for line in after]
    elif removed:
        changes = [("-", line) for line in before]
    else:
        changes = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, before, after, autojunk=True
        ).get_opcodes():
            if tag in ("replace", "delete"):
                changes.extend(("-", line) for line in before[i1:i2])
            if tag in ("replace", "insert"):
                changes.extend(("+", line) for line in after[j1:j2])
    if not changes:
        return "text changed (line ending or final newline)"
    detail = "; ".join(f"{mark} {_clean_piece(line)}" for mark, line in changes[:4])
    if len(changes) > 4:
        detail += f"; … ({len(changes) - 4} more line(s))"
    if len(detail) > DETAIL_LIMIT:
        detail = detail[: DETAIL_LIMIT - 1] + "…"
    return detail


def _file_detail(old: Entry | None, new: Entry | None) -> str:
    if new is not None and new.error:
        return f"unreadable: {new.error}"
    if old is not None and old.error:
        if new is None:
            return f"deleted while unreadable: {old.error}"
        if new.is_text:
            preview = _text_detail(None, new.text, added=True)
            return f"readable again; {preview}"
        return "readable again; non-text content (previous content unavailable)"
    if old is not None and new is not None and old.kind != new.kind:
        return f"file type changed to {new.kind}"
    if old is not None and new is not None and old.digest == new.digest and old.mode != new.mode:
        return "file permissions changed"
    entry = new or old
    if entry is None:
        return "content unavailable"
    if not entry.is_text:
        return "non-text content changed"
    if old is None:
        return _text_detail(None, entry.text, added=True)
    if new is None:
        return _text_detail(old.text, None, removed=True)
    return _text_detail(old.text, new.text)


def notify(path: str, action: str, kind: str, detail: str | None = None) -> None:
    label = "Directory" if kind == "directory" else "File"
    message = f"{label} {action}: {_clean_piece(path, limit=120)}"
    if detail:
        message += f" — {detail}"
    if len(message) > DETAIL_LIMIT:
        message = message[: DETAIL_LIMIT - 1] + "…"
    subprocess.run(
        ["notify-send", "--app-name=Change detector", "Change detected", message],
        check=True,
    )


def changes(previous: dict[str, Entry], current: dict[str, Entry]):
    """Yield concise notification fields for one pair of snapshots."""
    for path in sorted(previous.keys() - current.keys()):
        entry = previous[path]
        detail = _file_detail(entry, None) if entry.kind == "file" else None
        yield path, "deleted", entry.kind, detail
    for path in sorted(current.keys() - previous.keys()):
        entry = current[path]
        detail = _file_detail(None, entry) if entry.kind == "file" else None
        yield path, "added", entry.kind, detail
    for path in sorted(previous.keys() & current.keys()):
        old, new = previous[path], current[path]
        if old.signature() != new.signature():
            detail = _file_detail(old, new) if new.kind == "file" else None
            yield path, "modified", new.kind, detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between checks (default: 1)",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    root = Path(__file__).resolve().parent.parent
    try:
        previous = snapshot(root)
        print(f"Watching {root} (excluding .git); press Ctrl+C to stop.", flush=True)
        while True:
            time.sleep(args.interval)
            current = snapshot(root)
            for path, action, kind, detail in changes(previous, current):
                notify(path, action, kind, detail)
            previous = current
    except KeyboardInterrupt:
        print("\nChange detector stopped.", flush=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Change detector error: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
