#!/usr/bin/env python3
"""Append current Instagram follower counts to this repository's CSV."""

import argparse
import csv
import fcntl
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent
CSV_PATH = REPO / "scene-tracker.csv"
LOG_PATH = REPO / "scene-tracker.log"
LOCK_PATH = REPO / ".git" / "scene-tracker.lock"
REMOTE = "origin"
BRANCH = "main"
INACTIVE_PREFIX = "!"
HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
FOLLOWER_DESCRIPTION_RE = re.compile(
    r"^([\d,.]+)\s*([KMB]?) Followers?\b", re.IGNORECASE
)
EMBEDDED_JSON_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)
FOLLOWER_MULTIPLIERS = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
REQUEST_DELAY_SECONDS = 2
RETRY_DELAYS = (0, 5, 15)
LOG_RUNS_TO_KEEP = 12
LOG_HEADER = "# Scene tracker rolling log (last 12 real runs)\n"
LOG_SEPARATOR = "\n---\n"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

LOGGER = logging.getLogger("scene-tracker")


class TrackerError(Exception):
    """An expected error that should stop the run without a traceback."""


class MetaDescriptionParser(HTMLParser):
    """Collect social-preview descriptions from an HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.descriptions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = dict(attrs)
        kind = attributes.get("name") or attributes.get("property")
        content = attributes.get("content")
        if kind in {"description", "og:description"} and content:
            self.descriptions.append(content)


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
        inactive = handle.startswith(INACTIVE_PREFIX)
        if inactive:
            handle = handle.removeprefix(INACTIVE_PREFIX)
        handle = handle.removeprefix("@")
        if not HANDLE_RE.fullmatch(handle):
            raise TrackerError(f"invalid Instagram handle in CSV header: {value!r}")
        handles.append(f"{INACTIVE_PREFIX}{handle}" if inactive else handle)

    folded_handles = [
        handle.removeprefix(INACTIVE_PREFIX).casefold() for handle in handles
    ]
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


def parse_follower_count(descriptions: list[str]) -> int | None:
    """Convert Instagram's displayed follower count to an integer."""
    for description in descriptions:
        match = FOLLOWER_DESCRIPTION_RE.match(description)
        if match:
            number = Decimal(match.group(1).replace(",", ""))
            multiplier = FOLLOWER_MULTIPLIERS[match.group(2).upper()]
            return int(number * multiplier)
    return None


def find_embedded_follower_count(value: object) -> int | None:
    """Find an exact follower count in Instagram's embedded query data."""
    if isinstance(value, dict):
        bbox = value.get("__bbox")
        result = bbox.get("result") if isinstance(bbox, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        user = data.get("xig_user_by_username") if isinstance(data, dict) else None
        count = user.get("follower_count") if isinstance(user, dict) else None
        if isinstance(count, int):
            return count
        for nested in value.values():
            count = find_embedded_follower_count(nested)
            if count is not None:
                return count
    elif isinstance(value, list):
        for nested in value:
            count = find_embedded_follower_count(nested)
            if count is not None:
                return count
    return None


def parse_embedded_follower_count(page: str) -> int | None:
    """Read the exact follower count embedded in a public profile page."""
    for script in EMBEDDED_JSON_RE.findall(page):
        try:
            count = find_embedded_follower_count(json.loads(script))
        except json.JSONDecodeError:
            continue
        if count is not None:
            return count
    return None


def fetch_public_page_follower_count(handle: str) -> tuple[int | None, bool]:
    """Return a follower count and whether it came from displayed metadata."""
    request = urllib.request.Request(
        f"https://www.instagram.com/{handle}/",
        headers={
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.8",
            "Cookie": (
                "sessionid=; mid=; ig_pr=1; ig_vw=1920; csrftoken=; s_network=; ds_user_id="
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "User-Agent": USER_AGENT,
        },
    )

    last_error = "the page did not expose a follower count"
    for delay in RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None, False
            last_error = f"HTTP {error.code} {error.reason}"
            continue
        except (OSError, UnicodeError) as error:
            last_error = str(error)
            continue

        count = parse_embedded_follower_count(page)
        if count is not None:
            return count, False

        parser = MetaDescriptionParser()
        parser.feed(page)
        count = parse_follower_count(parser.descriptions)
        if count is not None:
            return count, True

    raise TrackerError(f"public page lookup failed for @{handle}: {last_error}")


def collect_follower_counts(handles: list[str]) -> list[int | None]:
    """Fetch counts anonymously, leaving inactive or failed profiles blank."""
    counts: list[int | None] = []
    made_request = False
    for position, handle in enumerate(handles, start=1):
        if handle.startswith(INACTIVE_PREFIX):
            LOGGER.info(
                "Skipping inactive @%s (%d/%d)",
                handle.removeprefix(INACTIVE_PREFIX),
                position,
                len(handles),
            )
            counts.append(None)
            continue

        LOGGER.info("Fetching @%s (%d/%d)", handle, position, len(handles))
        if made_request:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            count, used_displayed_count = fetch_public_page_follower_count(handle)
        except TrackerError as error:
            LOGGER.warning("%s; recording a blank", error)
            counts.append(None)
            made_request = True
            continue
        made_request = True

        if count is None:
            LOGGER.warning(
                "@%s does not exist or is unavailable; recording a blank", handle
            )
            counts.append(None)
            continue
        if used_displayed_count:
            LOGGER.warning(
                "Exact count unavailable for @%s; using displayed count", handle
            )
        counts.append(count)

    return counts


def write_csv(rows: list[list[str]]) -> None:
    """Atomically replace scene-tracker.csv."""
    mode = CSV_PATH.stat().st_mode
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".scene-tracker.csv.", dir=REPO
    )
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


def write_run_log(timestamp: str, messages: str) -> None:
    """Append one run to the rolling log and retain only recent runs."""
    try:
        existing = LOG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = LOG_HEADER

    body = existing.removeprefix(LOG_HEADER).strip()
    if body.startswith("---"):
        body = body.removeprefix("---").strip()
    runs = body.split(LOG_SEPARATOR) if body else []
    runs.append(f"RUN {timestamp}\n{messages.strip()}")
    runs = runs[-LOG_RUNS_TO_KEEP:]
    content = LOG_HEADER.rstrip() + LOG_SEPARATOR + LOG_SEPARATOR.join(runs) + "\n"

    mode = LOG_PATH.stat().st_mode if LOG_PATH.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".scene-tracker.log.", dir=REPO
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as log_file:
            log_file.write(content)
            log_file.flush()
            os.fsync(log_file.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, LOG_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def run(*, dry_run: bool = False) -> None:
    """Perform one complete tracking run."""
    try:
        lock_file = LOCK_PATH.open("a", encoding="utf-8")
    except FileNotFoundError as error:
        raise TrackerError(f"not a Git checkout: {REPO}") from error

    with lock_file:
        log_buffer = io.StringIO()
        log_handler = logging.StreamHandler(log_buffer)
        log_formatter = logging.Formatter(
            "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        )
        log_formatter.converter = time.gmtime
        log_handler.setFormatter(log_formatter)
        LOGGER.addHandler(log_handler)
        original_log_level = LOGGER.level
        LOGGER.setLevel(logging.INFO)

        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TrackerError(
                "another scene-tracker run is already in progress"
            ) from error

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
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        new_row = [
            timestamp,
            *("" if count is None else str(count) for count in counts),
        ]
        rows.append(new_row)

        missing = sum(count is None for count in counts)
        LOGGER.info("Collected %d counts and %d blanks", len(counts) - missing, missing)

        if dry_run:
            LOGGER.info(
                "Dry run complete: %d counts and %d blanks; CSV was not changed",
                len(counts) - missing,
                missing,
            )
            LOGGER.removeHandler(log_handler)
            LOGGER.setLevel(original_log_level)
            return

        write_csv(rows)
        LOGGER.info("Appended follower counts at %s", timestamp)
        write_run_log(timestamp, log_buffer.getvalue())

        git("add", "--", CSV_PATH.name, LOG_PATH.name)
        git(
            "commit",
            "-m",
            f"Track Instagram followers at {timestamp}",
            "--",
            CSV_PATH.name,
            LOG_PATH.name,
        )
        git("push", REMOTE, f"HEAD:{BRANCH}")
        LOGGER.info("Committed and pushed %s", CSV_PATH.name)
        LOGGER.removeHandler(log_handler)
        LOGGER.setLevel(original_log_level)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate counts without changing the CSV, committing, or pushing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
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
