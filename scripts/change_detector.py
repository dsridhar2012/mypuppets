#!/usr/bin/env python3
"""Poll this workspace and send desktop notifications for file changes."""

import argparse
import codecs
import difflib
import hashlib
import math
import os
import stat
import sys
import textwrap
import time
import tkinter as tk
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
        directories[:] = [
            name for name in directories if name not in {".git", "__pycache__"}
        ]

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


class BalloonNotifications:
    WIDTH = 540
    HEIGHT = 460
    SLIDE_SECONDS = 1.35
    FLOAT_SECONDS = 5.0
    POP_SECONDS = 0.75
    SHRINK_FRACTION = 0.3

    PALETTES = {
        "positive": ("#075638", "#0c9d60", "#55e4a0", "#dcffed"),
        "negative": ("#81253a", "#d83e57", "#ff8194", "#fff0f3"),
        "neutral": ("#29457f", "#4d74d1", "#91b0ff", "#eef3ff"),
    }

    GLITTER_COLORS = (
        "#ffe36e", "#ff8fc8", "#8cecff", "#bd9bff",
        "#8dffbd", "#ffad69", "#ffffff", "#77b7ff",
        "#ff7890", "#f5ff91", "#a5a0ff", "#80ffe7",
    )

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.pending: list[tuple[str, str, str, str | None]] = []
        self.visible = False

    @staticmethod
    def _tone(action: str, detail: str | None = None) -> str:
        if action == "modified" and detail:
            if detail.startswith("- ") or "; - " in detail:
                return "negative"
            if detail.startswith("+ ") or "; + " in detail:
                return "positive"
        if action in {"added", "created", "restored"}:
            return "positive"
        if action in {"deleted", "removed"}:
            return "negative"
        return "neutral"

    def show(self, path: str, action: str, kind: str, detail: str | None) -> None:
        self.pending.append((path, action, kind, detail))
        if not self.visible:
            self._show_next()

    def _show_next(self) -> None:
        if not self.pending:
            self.visible = False
            return

        self.visible = True
        path, action, kind, detail = self.pending.pop(0)
        tone = self._tone(action, detail)
        edge, body, gloss, shine = self.PALETTES[tone]
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.98)
        window.configure(bg="#101722")
        canvas = tk.Canvas(
            window,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg="#101722",
            bd=0,
            highlightthickness=0,
        )
        canvas.pack()
        center_x = self.WIDTH // 2
        center_y = 206
        canvas.create_oval(
            34, 20, self.WIDTH - 34, 390,
            fill=edge, outline="#ffffff", width=2, tags=("balloon",),
        )
        canvas.create_oval(
            44, 30, self.WIDTH - 44, 380,
            fill=body, outline=gloss, width=2, tags=("balloon",),
        )
        canvas.create_oval(
            79, 49, 160, 112, fill=gloss, outline="", tags=("balloon",)
        )
        canvas.create_oval(
            91, 56, 121, 78, fill="#ffffff", outline="", tags=("balloon",)
        )
        canvas.create_oval(
            132, 83, 145, 96, fill="#ffffff", outline="", tags=("balloon",)
        )
        canvas.create_polygon(
            center_x - 20, 372, center_x + 20, 372, center_x, 405,
            fill=body, outline=gloss, width=2, tags=("balloon",),
        )
        canvas.create_line(
            center_x, 404, center_x + 5, 433, center_x - 3, 448,
            fill=gloss, width=2, tags=("balloon",),
        )

        label = "DIRECTORY" if kind == "directory" else "FILE"
        if action == "modified" and tone == "negative":
            action_label = "LINES REMOVED"
        elif action == "modified" and tone == "positive":
            action_label = "LINES ADDED"
        else:
            action_label = action.upper()
        canvas.create_text(
            center_x, 132, text=f"{label}  ·  {action_label}",
            fill="#ffffff", font=("TkDefaultFont", 13, "bold"),
            tags=("balloon",),
        )
        canvas.create_text(center_x, 174, text=_clean_piece(path, limit=42),
                           fill="#ffffff", font=("TkDefaultFont", 19, "bold"),
                           width=360, justify="center", tags=("balloon",))
        if detail:
            safe_detail = _clean_piece(detail, limit=DETAIL_LIMIT)
            lines = textwrap.wrap(safe_detail, width=38, break_long_words=True)
            summary = "\n".join(lines[:3])
            if len(lines) > 2:
                summary = summary.rstrip("…") + "…"
            canvas.create_text(
                center_x, 224, text=summary, fill="#ffffff",
                font=("TkDefaultFont", 12), width=350, justify="center",
                tags=("balloon",),
            )

        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x = max(8, (screen_width - self.WIDTH) // 2)
        top = 28
        started = time.monotonic()
        balloon_scale = 1.0
        glitter: list[tuple[int, float, str]] = []
        for index, color in enumerate(self.GLITTER_COLORS):
            angle = index * (360 / len(self.GLITTER_COLORS))
            radians = math.radians(angle)
            glitter.append((
                canvas.create_line(
                    center_x, center_y, center_x, center_y,
                    fill=color, width=3, tags=("sparkle",),
                ),
                radians,
                "ray",
            ))
            glitter.append((
                canvas.create_oval(
                    center_x, center_y, center_x, center_y,
                    fill=color, outline="", tags=("sparkle",),
                ),
                radians + math.radians(15),
                "dot",
            ))
        canvas.itemconfigure("sparkle", state="hidden")

        def animate_pop() -> None:
            nonlocal balloon_scale
            pop_progress = min(
                1.0, (time.monotonic() - pop_started) / self.POP_SECONDS
            )
            if pop_progress < self.SHRINK_FRACTION:
                desired_scale = max(
                    0.03, 1 - pop_progress / self.SHRINK_FRACTION
                )
                factor = desired_scale / balloon_scale
                canvas.scale("balloon", center_x, center_y, factor, factor)
                balloon_scale = desired_scale
            else:
                canvas.itemconfigure("balloon", state="hidden")
                canvas.itemconfigure("sparkle", state="normal")
                glitter_progress = (
                    pop_progress - self.SHRINK_FRACTION
                ) / (1 - self.SHRINK_FRACTION)
                spread = 18 + glitter_progress * 165
                for item_id, angle, glitter_kind in glitter:
                    end_x = center_x + math.cos(angle) * spread
                    end_y = center_y + math.sin(angle) * spread
                    if glitter_kind == "ray":
                        canvas.coords(
                            item_id,
                            center_x + math.cos(angle) * spread * 0.62,
                            center_y + math.sin(angle) * spread * 0.62,
                            end_x,
                            end_y,
                        )
                    else:
                        canvas.coords(
                            item_id, end_x - 4, end_y - 4, end_x + 4, end_y + 4
                        )

            window.attributes("-alpha", max(0.0, 0.98 * (1 - pop_progress)))
            if pop_progress >= 1:
                window.destroy()
                self.root.after(120, self._show_next)
            else:
                window.after(16, animate_pop)

        def animate_slide() -> None:
            elapsed = time.monotonic() - started
            progress = min(1.0, elapsed / self.SLIDE_SECONDS)
            eased = 1 - (1 - progress) ** 3
            y = round(screen_height + 12 - (screen_height + 12 - top) * eased)
            window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
            if progress < 1:
                window.after(16, animate_slide)
                return

            window.after(round(self.FLOAT_SECONDS * 1000), begin_pop)

        def begin_pop() -> None:
            nonlocal pop_started
            pop_started = time.monotonic()
            animate_pop()

        window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{screen_height + 12}")
        pop_started = 0.0
        window.after(16, animate_slide)


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
    failure = False
    try:
        root_window = tk.Tk()
        root_window.withdraw()
        notifications = BalloonNotifications(root_window)
        previous = snapshot(root)
        print(f"Watching {root} (excluding .git); press Ctrl+C to stop.", flush=True)

        def poll() -> None:
            nonlocal previous, failure
            try:
                current = snapshot(root)
                for path, action, kind, detail in changes(previous, current):
                    notifications.show(path, action, kind, detail)
                previous = current
                root_window.after(max(1, round(args.interval * 1000)), poll)
            except OSError as error:
                failure = True
                print(f"Change detector error: {error}", file=sys.stderr, flush=True)
                root_window.destroy()

        root_window.after(0, poll)
        root_window.mainloop()
        if not failure:
            root_window.destroy()
    except KeyboardInterrupt:
        print("\nChange detector stopped.", flush=True)
    except OSError as error:
        print(f"Change detector error: {error}", file=sys.stderr, flush=True)
        return 1
    except tk.TclError as error:
        print(f"Change detector UI error: {error}", file=sys.stderr, flush=True)
        return 1
    return int(failure)


if __name__ == "__main__":
    raise SystemExit(main())
