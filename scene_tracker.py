#!/usr/bin/env python3
"""Append current Instagram follower counts to this repository's CSV."""

import argparse
import csv
import fcntl
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
CSV_PATH = REPO / "scene-tracker.csv"
LOCK_PATH = REPO / ".git" / "scene-tracker.lock"
REMOTE = "origin"
BRANCH = "main"
INSTALOADER_VERSION = "4.15.3"
HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,30}")

LOGGER = logging.getLogger("scene-tracker")


class TrackerError(Exception):
    """An expected error that should stop the run without a traceback."""


def git(*args: str) -> str:
    """Run Git in this repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), *args],
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise TrackerError("git is not installed") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        message = f"git {' '.join(args)} failed"
        if detail:
            message = f"{message}: {detail}"
        raise TrackerError(message) from error
    return result.stdout.strip()


def read_csv() -> tuple[list[list[str]], list[str]]:
    """Read the CSV and pad historical rows to the current header width."""
    try:
        with CSV_PATH.open("r", encoding="utf-8", newline="") as csv_file:
            rows = list(csv.reader(csv_file))
    except FileNotFoundError as error:
        raise TrackerError(f"CSV not found: {CSV_PATH}") from error

    if not rows or not rows[0]:
        raise TrackerError("CSV is empty")

    header = rows[0]
    if header[0] != "Date":
        raise TrackerError("the first CSV header must be 'Date'")
    if len(header) == 1:
        raise TrackerError("the CSV header contains no Instagram handles")

    handles = []
    for value in header[1:]:
        handle = value.strip()
        if handle.startswith("@"):
            handle = handle[1:]
        if not HANDLE_RE.fullmatch(handle):
            raise TrackerError(f"invalid Instagram handle in CSV header: {value!r}")
        handles.append(handle)

    folded_handles = [handle.casefold() for handle in handles]
    if len(set(folded_handles)) != len(folded_handles):
        raise TrackerError("the CSV header contains duplicate Instagram handles")

    width = len(header)
    for line_number, row in enumerate(rows[1:], start=2):
        if not row:
            raise TrackerError(f"blank CSV row at line {line_number}")
        if len(row) > width:
            raise TrackerError(
                f"CSV row {line_number} has {len(row)} cells but the header has {width}"
            )
        row.extend([""] * (width - len(row)))

    return rows, handles


def collect_follower_counts(handles: list[str]) -> list[int | None]:
    """Fetch follower counts anonymously, leaving only nonexistent profiles blank."""
    try:
        import instaloader
        from instaloader.exceptions import ProfileNotExistsException
    except ImportError as error:
        raise TrackerError(
            "Instaloader is not installed; run: "
            f'python3 -m pip install "instaloader=={INSTALOADER_VERSION}"'
        ) from error

    if instaloader.__version__ != INSTALOADER_VERSION:
        LOGGER.warning(
            "Tested with Instaloader %s; running %s",
            INSTALOADER_VERSION,
            instaloader.__version__,
        )

    loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1, request_timeout=30)

    counts: list[int | None] = []
    for position, handle in enumerate(handles, start=1):
        LOGGER.info("Fetching @%s (%d/%d)", handle, position, len(handles))
        try:
            profile = instaloader.Profile.from_username(loader.context, handle)
        except ProfileNotExistsException:
            LOGGER.warning("@%s does not exist or is unavailable; recording a blank", handle)
            counts.append(None)
            continue
        except instaloader.InstaloaderException as error:
            raise TrackerError(f"Instagram lookup failed for @{handle}: {error}") from error

        counts.append(profile.followers)

    if counts.count(None) > max(1, len(handles) // 10):
        raise TrackerError("too many profiles were unavailable; refusing to append a row")

    return counts


def write_csv(rows: list[list[str]]) -> None:
    """Atomically replace scene-tracker.csv."""
    mode = CSV_PATH.stat().st_mode
    descriptor, temporary_name = tempfile.mkstemp(prefix=".scene-tracker.csv.", dir=REPO)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file, lineterminator="\n")
            writer.writerows(rows)
            csv_file.flush()
            os.fsync(csv_file.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, CSV_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def run(*, dry_run: bool = False) -> None:
    """Perform one complete tracking run."""
    try:
        lock_file = LOCK_PATH.open("a", encoding="utf-8")
    except FileNotFoundError as error:
        raise TrackerError(f"not a Git checkout: {REPO}") from error

    with lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TrackerError("another scene-tracker run is already in progress") from error

        branch = git("branch", "--show-current")
        if branch != BRANCH:
            raise TrackerError(f"expected branch {BRANCH!r}, found {branch!r}")
        if git("status", "--porcelain", "--untracked-files=no"):
            raise TrackerError("tracked files have local changes; refusing to pull")

        LOGGER.info("Pulling %s/%s", REMOTE, BRANCH)
        git("pull", "--ff-only", REMOTE, BRANCH)

        rows, handles = read_csv()
        counts = collect_follower_counts(handles)
        timestamp = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        new_row = [timestamp, *("" if count is None else str(count) for count in counts)]
        rows.append(new_row)

        if dry_run:
            missing = sum(count is None for count in counts)
            LOGGER.info(
                "Dry run complete: %d counts and %d blanks; CSV was not changed",
                len(counts) - missing,
                missing,
            )
            return

        write_csv(rows)
        LOGGER.info("Appended follower counts at %s", timestamp)

        git("add", "--", CSV_PATH.name)
        git("commit", "-m", f"Track Instagram followers at {timestamp}", "--", CSV_PATH.name)
        git("push", REMOTE, f"HEAD:{BRANCH}")
        LOGGER.info("Committed and pushed %s", CSV_PATH.name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate counts without changing the CSV, committing, or pushing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        run(dry_run=args.dry_run)
    except TrackerError as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected failure")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
