from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

STATE_FILE = DATA_DIR / "state.json"
OUTPUT_FILE = OUTPUT_DIR / "proxies.txt"


# ============================================================
# CONFIG
# ============================================================

RETENTION_HOURS = 12

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION"]

CHANNELS_RAW = os.environ.get("TG_CHANNELS", "")


# ============================================================
# REGEX
# ============================================================

PROXY_REGEX = re.compile(
    r"""
    (?:
        https?://
    )?
    (?:
        t\.me/proxy
        |
        telegram\.me/proxy
    )
    \?
    [^\s<>"'`()\[\]{}]+

    |

    tg://proxy\?
    [^\s<>"'`()\[\]{}]+
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ============================================================
# TIME
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ============================================================
# FILE SYSTEM
# ============================================================

def ensure_directories() -> None:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def create_initial_state() -> None:
    if STATE_FILE.exists():
        return

    STATE_FILE.write_text(
        json.dumps(
            {
                "channels": {},
                "proxies": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    create_initial_state()

    try:
        state = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):
        state = {}

    if not isinstance(state, dict):
        state = {}

    if not isinstance(
        state.get("channels"),
        dict,
    ):
        state["channels"] = {}

    if not isinstance(
        state.get("proxies"),
        dict,
    ):
        state["proxies"] = {}

    return state


def save_state(state: dict) -> None:
    temporary_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    temporary_file.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(
        STATE_FILE
    )


# ============================================================
# CHANNEL CONFIG
# ============================================================

def parse_channels() -> list[str]:
    result: list[str] = []

    for item in CHANNELS_RAW.split(","):
        item = item.strip()

        if not item:
            continue

        # https://t.me/channel
        if item.startswith(
            "https://t.me/"
        ):
            item = item[len("https://t.me/"):]

        # http://t.me/channel
        elif item.startswith(
            "http://t.me/"
        ):
            item = item[len("http://t.me/"):]

        # https://telegram.me/channel
        elif item.startswith(
            "https://telegram.me/"
        ):
            item = item[len("https://telegram.me/"):]

        # @channel
        item = item.lstrip("@")
        item = item.strip("/")

        if item:
            result.append(item)

    # Remove duplicate channels
    return list(
        dict.fromkeys(result)
    )


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_candidate(value: str) -> str:
    value = value.strip()

    # Markdown / quote / HTML wrappers
    value = value.strip(
        "`'\"“”‘’<>[](){}"
    )

    # Common trailing punctuation
    value = value.rstrip(
        ".,;:!?،؛؟)]}>\"'`"
    )

    return value.strip()


# ============================================================
# PROXY NORMALIZATION
# ============================================================

def normalize_proxy(
    raw_value: str,
) -> str | None:

    value = clean_candidate(
        raw_value
    )

    if not value:
        return None

    # --------------------------------------------------------
    # TG:// PROXY
    # --------------------------------------------------------

    if value.lower().startswith(
        "tg://proxy?"
    ):
        parsed = urlparse(value)

        params = parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

        server = params.get(
            "server",
            [None],
        )[0]

        port = params.get(
            "port",
            [None],
        )[0]

        secret = params.get(
            "secret",
            [None],
        )[0]

        if not server or not port or not secret:
            return None

        try:
            port_number = int(port)
        except ValueError:
            return None

        if not 1 <= port_number <= 65535:
            return None

        server = server.strip()
        secret = secret.strip().lower()

        if not server:
            return None

        if len(secret) < 16:
            return None

        return (
            "tg://proxy"
            f"?server={server}"
            f"&port={port_number}"
            f"&secret={secret}"
        )

    # --------------------------------------------------------
    # ADD HTTPS TO BARE t.me
    # --------------------------------------------------------

    lower = value.lower()

    if lower.startswith(
        "t.me/proxy?"
    ):
        value = "https://" + value

    elif lower.startswith(
        "telegram.me/proxy?"
    ):
        value = "https://" + value

    # --------------------------------------------------------
    # HTTP / HTTPS
    # --------------------------------------------------------

    if not value.lower().startswith(
        (
            "https://t.me/proxy?",
            "http://t.me/proxy?",
            "https://telegram.me/proxy?",
            "http://telegram.me/proxy?",
        )
    ):
        return None

    parsed = urlparse(value)

    hostname = (
        parsed.hostname or ""
    ).lower()

    pathname = (
        parsed.path or ""
    ).lower()

    if hostname not in {
        "t.me",
        "telegram.me",
    }:
        return None

    if pathname != "/proxy":
        return None

    params = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    server = params.get(
        "server",
        [None],
    )[0]

    port = params.get(
        "port",
        [None],
    )[0]

    secret = params.get(
        "secret",
        [None],
    )[0]

    if not server or not port or not secret:
        return None

    try:
        port_number = int(port)
    except ValueError:
        return None

    if not 1 <= port_number <= 65535:
        return None

    server = server.strip()
    secret = secret.strip().lower()

    if not server:
        return None

    if len(server) > 253:
        return None

    if len(secret) < 16:
        return None

    # --------------------------------------------------------
    # CANONICAL FORMAT
    # --------------------------------------------------------

    return (
        "https://t.me/proxy"
        f"?server={server}"
        f"&port={port_number}"
        f"&secret={secret}"
    )


# ============================================================
# PROXY FINGERPRINT
# ============================================================

def get_proxy_fingerprint(
    proxy: str,
) -> str:

    return hashlib.sha256(
        proxy.encode("utf-8")
    ).hexdigest()


# ============================================================
# EXTRACT FROM TEXT
# ============================================================

def extract_from_text(
    text: str | None,
) -> set[str]:

    if not text:
        return set()

    found: set[str] = set()

    for match in PROXY_REGEX.finditer(text):

        candidate = match.group(0)

        normalized = normalize_proxy(
            candidate
        )

        if normalized:
            found.add(normalized)

    return found


# ============================================================
# EXTRACT FROM MESSAGE ENTITIES
# ============================================================

def extract_from_entities(
    message,
) -> set[str]:

    found: set[str] = set()

    entities = (
        getattr(
            message,
            "entities",
            None,
        )
        or []
    )

    for entity in entities:

        # Hidden URL:
        # [Proxy](https://t.me/proxy?...)


        url = getattr(
            entity,
            "url",
            None,
        )

        if url:

            normalized = normalize_proxy(
                url
            )

            if normalized:
                found.add(
                    normalized
                )

    return found


# ============================================================
# EXTRACT FROM BUTTONS
# ============================================================

def extract_from_buttons(
    message,
) -> set[str]:

    found: set[str] = set()

    buttons = getattr(
        message,
        "buttons",
        None,
    )

    if not buttons:
        return found

    for row in buttons:

        for button in row:

            url = getattr(
                button,
                "url",
                None,
            )

            if not url:
                continue

            normalized = normalize_proxy(
                url
            )

            if normalized:
                found.add(
                    normalized
                )

    return found


# ============================================================
# COMPLETE MESSAGE EXTRACTION
# ============================================================

def extract_from_message(
    message,
) -> set[str]:

    found: set[str] = set()

    # Normal text / caption
    found.update(
        extract_from_text(
            getattr(
                message,
                "message",
                None,
            )
        )
    )

    # Markdown / HTML hidden URLs
    found.update(
        extract_from_entities(
            message
        )
    )

    # Inline buttons
    found.update(
        extract_from_buttons(
            message
        )
    )

    return found


# ============================================================
# CHANNEL METADATA
# ============================================================

def get_channel_key(
    entity,
) -> str:

    # Stable Telegram peer ID
    return str(
        utils.get_peer_id(
            entity
        )
    )


def get_channel_metadata(
    entity,
) -> dict:

    return {
        "id": getattr(
            entity,
            "id",
            None,
        ),
        "username": getattr(
            entity,
            "username",
            None,
        ),
        "title": getattr(
            entity,
            "title",
            None,
        ),
    }


# ============================================================
# STORE NEW PROXY
# ============================================================

def add_proxy(
    state: dict,
    proxy: str,
    channel_key: str,
    message,
) -> bool:

    proxies = state["proxies"]

    fingerprint = get_proxy_fingerprint(
        proxy
    )

    # Already globally known.
    if fingerprint in proxies:
        return False

    proxies[fingerprint] = {
        "proxy": proxy,

        "first_seen": iso_now(),

        "source": {
            "channel": channel_key,
            "message_id": getattr(
                message,
                "id",
                None,
            ),
            "message_date": (
                getattr(
                    message,
                    "date",
                    None,
                ).isoformat()
                if getattr(
                    message,
                    "date",
                    None,
                )
                else None
            ),
        },
    }

    return True


# ============================================================
# PROCESS MESSAGE
# ============================================================

def process_message(
    state: dict,
    message,
    channel_key: str,
) -> int:

    extracted = extract_from_message(
        message
    )

    if not extracted:
        return 0

    added = 0

    for proxy in extracted:

        if add_proxy(
            state=state,
            proxy=proxy,
            channel_key=channel_key,
            message=message,
        ):
            added += 1

            print(
                f"[NEW PROXY] {proxy}"
            )

    return added


# ============================================================
# INITIAL CHANNEL SCAN
# ============================================================

async def initial_scan_channel(
    client: TelegramClient,
    entity,
    state: dict,
    channel_key: str,
) -> tuple[int, int | None]:

    print(
        "[INITIAL SCAN]"
        f" channel={channel_key}"
    )

    cutoff = (
        utc_now()
        - timedelta(
            hours=RETENTION_HOURS
        )
    )

    latest_message_id: int | None = None
    added_count = 0
    scanned_count = 0

    # Newest -> oldest
    async for message in client.iter_messages(
        entity
    ):

        message_id = getattr(
            message,
            "id",
            None,
        )

        if (
            message_id is not None
            and (
                latest_message_id is None
                or message_id > latest_message_id
            )
        ):
            latest_message_id = message_id

        message_date = getattr(
            message,
            "date",
            None,
        )

        if message_date is None:
            continue

        if message_date.tzinfo is None:
            message_date = message_date.replace(
                tzinfo=timezone.utc
            )

        # We reached messages older than 12 hours.
        if message_date < cutoff:
            break

        scanned_count += 1

        added_count += process_message(
            state,
            message,
            channel_key,
        )

    return (
        added_count,
        latest_message_id,
    )


# ============================================================
# INCREMENTAL CHANNEL SCAN
# ============================================================

async def incremental_scan_channel(
    client: TelegramClient,
    entity,
    state: dict,
    channel_key: str,
    last_message_id: int,
) -> tuple[int, int]:

    # --------------------------------------------------------
    # Get only the latest message
    # --------------------------------------------------------

    latest = await client.get_messages(
        entity,
        limit=1,
    )

    if not latest:
        return 0, last_message_id

    latest_message = latest[0]

    latest_message_id = getattr(
        latest_message,
        "id",
        last_message_id,
    )

    # --------------------------------------------------------
    # Nothing new
    # --------------------------------------------------------

    if latest_message_id <= last_message_id:

        print(
            f"[SKIP]"
            f" channel={channel_key}"
            f" last_id={last_message_id}"
        )

        return (
            0,
            last_message_id,
        )

    print(
        f"[UPDATE]"
        f" channel={channel_key}"
        f" old_id={last_message_id}"
        f" new_id={latest_message_id}"
    )

    added_count = 0
    newest_seen_id = last_message_id

    # --------------------------------------------------------
    # Only messages AFTER last_message_id
    #
    # reverse=True means oldest -> newest.
    #
    # Telethon documents that with reverse=True,
    # min_id behaves as the lower bound.
    # --------------------------------------------------------

    async for message in client.iter_messages(
        entity,
        min_id=last_message_id,
        reverse=True,
    ):

        message_id = getattr(
            message,
            "id",
            None,
        )

        if message_id is None:
            continue

        if message_id <= last_message_id:
            continue

        if message_id > newest_seen_id:
            newest_seen_id = message_id

        added_count += process_message(
            state,
            message,
            channel_key,
        )

    return (
        added_count,
        newest_seen_id,
    )


# ============================================================
# CLEAN EXPIRED PROXIES
# ============================================================

def remove_expired_proxies(
    state: dict,
) -> int:

    proxies = state["proxies"]

    expiration_time = (
        utc_now()
        - timedelta(
            hours=RETENTION_HOURS
        )
    )

    expired_fingerprints: list[str] = []

    for fingerprint, item in proxies.items():

        first_seen_raw = item.get(
            "first_seen"
        )

        if not first_seen_raw:

            expired_fingerprints.append(
                fingerprint
            )

            continue

        try:
            first_seen = parse_iso(
                first_seen_raw
            )

        except ValueError:

            expired_fingerprints.append(
                fingerprint
            )

            continue

        if first_seen <= expiration_time:

            expired_fingerprints.append(
                fingerprint
            )

    for fingerprint in expired_fingerprints:

        del proxies[
            fingerprint
        ]

    return len(
        expired_fingerprints
    )


# ============================================================
# WRITE SUBSCRIPTION
# ============================================================

def write_subscription(
    state: dict,
) -> int:

    proxies = state["proxies"]

    # Newest first
    items = sorted(
        proxies.values(),
        key=lambda item: item.get(
            "first_seen",
            "",
        ),
        reverse=True,
    )

    lines = []

    for item in items:

        proxy = item.get(
            "proxy"
        )

        if proxy:
            lines.append(
                proxy
            )

    output = (
        "\n".join(lines)
        + ("\n" if lines else "")
    )

    OUTPUT_FILE.write_text(
        output,
        encoding="utf-8",
        newline="\n",
    )

    return len(lines)


# ============================================================
# UPDATE CHANNEL STATE
# ============================================================

def update_channel_state(
    state: dict,
    channel_key: str,
    entity,
    last_message_id: int,
) -> None:

    old = state["channels"].get(
        channel_key,
        {},
    )

    state["channels"][channel_key] = {

        "id": getattr(
            entity,
            "id",
            old.get("id"),
        ),

        "username": getattr(
            entity,
            "username",
            old.get("username"),
        ),

        "title": getattr(
            entity,
            "title",
            old.get("title"),
        ),

        "last_message_id": int(
            last_message_id
        ),

        "last_checked_at": iso_now(),
    }


# ============================================================
# PROCESS ONE CHANNEL
# ============================================================

async def process_channel(
    client: TelegramClient,
    channel: str,
    state: dict,
) -> tuple[int, bool]:

    print()
    print(
        "=" * 60
    )

    print(
        f"[CHANNEL] {channel}"
    )

    try:

        entity = await client.get_entity(
            channel
        )

    except Exception as exc:

        print(
            f"[ERROR] Cannot resolve "
            f"{channel}: {exc}"
        )

        return 0, False

    channel_key = get_channel_key(
        entity
    )

    existing_state = (
        state["channels"].get(
            channel_key
        )
    )

    # ========================================================
    # FIRST RUN FOR THIS CHANNEL
    # ========================================================

    if not existing_state:

        added_count, latest_id = (
            await initial_scan_channel(
                client=client,
                entity=entity,
                state=state,
                channel_key=channel_key,
            )
        )

        if latest_id is None:
            latest_id = 0

        update_channel_state(
            state=state,
            channel_key=channel_key,
            entity=entity,
            last_message_id=latest_id,
        )

        print(
            f"[INITIAL DONE]"
            f" channel={channel}"
            f" latest_id={latest_id}"
            f" added={added_count}"
        )

        return (
            added_count,
            True,
        )

    # ========================================================
    # INCREMENTAL RUN
    # ========================================================

    raw_last_id = existing_state.get(
        "last_message_id",
        0,
    )

    try:
        last_message_id = int(
            raw_last_id
        )

    except (
        TypeError,
        ValueError,
    ):
        last_message_id = 0

    added_count, newest_seen_id = (
        await incremental_scan_channel(
            client=client,
            entity=entity,
            state=state,
            channel_key=channel_key,
            last_message_id=last_message_id,
        )
    )

    # IMPORTANT:
    # Even if there was no proxy in the new
    # messages, we still save newest_seen_id.
    #
    # Therefore the same messages will NEVER
    # be scanned again on the next run.

    update_channel_state(
        state=state,
        channel_key=channel_key,
        entity=entity,
        last_message_id=newest_seen_id,
    )

    return (
        added_count,
        True,
    )


# ============================================================
# MAIN COLLECTION
# ============================================================

async def collect() -> None:

    ensure_directories()

    state = load_state()

    channels = parse_channels()

    if not channels:
        raise RuntimeError(
            "TG_CHANNELS is empty."
        )

    print(
        "[START]"
    )

    print(
        f"[CHANNELS] {len(channels)}"
    )

    # ========================================================
    # CONNECT TELEGRAM
    # ========================================================

    client = TelegramClient(
        StringSession(
            SESSION_STRING
        ),
        API_ID,
        API_HASH,
    )

    await client.connect()

    try:

        authorized = (
            await client.is_user_authorized()
        )

        if not authorized:
            raise RuntimeError(
                "Telegram session is NOT authorized."
            )

        print(
            "[TELEGRAM] Authorized"
        )

        total_added = 0
        successful_channels = 0

        # ====================================================
        # CHANNELS
        # ====================================================

        for channel in channels:

            try:

                added, success = (
                    await process_channel(
                        client=client,
                        channel=channel,
                        state=state,
                    )
                )

                total_added += added

                if success:
                    successful_channels += 1

                # Save after every channel.
                #
                # If a later channel fails, previously
                # processed channels are not lost.

                save_state(
                    state
                )

            except FloodWaitError as exc:

                print(
                    f"[FLOOD WAIT]"
                    f" channel={channel}"
                    f" seconds={exc.seconds}"
                )

                # Save current progress
                save_state(
                    state
                )

                # Don't crash the entire run.
                continue

            except Exception as exc:

                print(
                    f"[ERROR]"
                    f" channel={channel}"
                    f" error={exc}"
                )

                # Preserve everything processed so far.
                save_state(
                    state
                )

                continue

        # ====================================================
        # REMOVE EXPIRED
        # ====================================================

        expired_count = (
            remove_expired_proxies(
                state
            )
        )

        # ====================================================
        # WRITE SUBSCRIPTION
        # ====================================================

        active_count = (
            write_subscription(
                state
            )
        )

        # ====================================================
        # FINAL SAVE
        # ====================================================

        state["meta"] = {
            "last_run_at": iso_now(),
            "retention_hours": RETENTION_HOURS,
            "total_channels": len(
                channels
            ),
            "successful_channels": (
                successful_channels
            ),
            "active_proxies": active_count,
            "new_proxies": total_added,
            "expired_proxies": expired_count,
        }

        save_state(
            state
        )

        print()
        print(
            "=" * 60
        )

        print(
            "[DONE]"
        )

        print(
            f"New proxies: {total_added}"
        )

        print(
            f"Expired proxies: {expired_count}"
        )

        print(
            f"Active proxies: {active_count}"
        )

        print(
            f"Successful channels:"
            f" {successful_channels}/{len(channels)}"
        )

        print(
            "=" * 60
        )

    finally:

        await client.disconnect()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(
        collect()
    )