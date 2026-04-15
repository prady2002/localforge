"""Header parsing, credential storage, and interactive auth for the cloud module.

Auth headers are NEVER hardcoded.  The user pastes raw HTTP request headers
from their browser's DevTools (including the request line) and they are cached
locally with restrictive permissions (0o600) until they expire.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

logger = logging.getLogger(__name__)
console = Console()

# Headers that httpx manages — we do NOT forward them.
_SKIP_HEADERS = frozenset({
    "content-length",
    "accept-encoding",
    "host",
    "connection",
})

# Default TTL for cached credentials (4 hours).
DEFAULT_AUTH_TTL_SECONDS = 4 * 60 * 60


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def parse_raw_headers(raw_text: str) -> dict[str, Any]:
    """Parse raw HTTP request headers as copied from browser DevTools.

    Expected format (first line is the request line, rest are ``Key: Value``)::

        POST /api/some/path?param=value HTTP/1.1
        Accept: */*
        Cookie: session=abc123; token=xyz
        Host: your-api-host.example.com
        ...

    The request line (``METHOD /path HTTP/1.x``) is required to extract the
    API path.  Copy it from the browser Network tab along with the headers.

    Returns a dict with:
        ``base_url``   – constructed from Host + scheme (https)
        ``api_path``   – the full request path including query string
        ``headers``    – dict of header-name → value (ready for httpx)
        ``parsed_at``  – Unix timestamp of when parsing happened
    """
    lines = [ln.rstrip("\r") for ln in raw_text.strip().splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Empty header text — nothing to parse.")

    # --- First line: request line (optional — user might omit it) --------
    first = lines[0]
    api_path = ""
    query_string = ""
    start_idx = 0

    if first.upper().startswith(("GET ", "POST ", "PUT ", "PATCH ", "DELETE ", "OPTIONS ")):
        parts = first.split()
        if len(parts) >= 2:
            raw_path = parts[1]
            if "?" in raw_path:
                api_path, query_string = raw_path.split("?", 1)
            else:
                api_path = raw_path
                query_string = ""
        start_idx = 1

    # --- Remaining lines: headers ----------------------------------------
    headers: dict[str, str] = {}
    host = ""

    for line in lines[start_idx:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        lower_key = key.lower()

        if lower_key in _SKIP_HEADERS and lower_key != "host":
            continue

        if lower_key == "host":
            host = value
            continue

        headers[key] = value

    if not host:
        # Try to extract host from Origin or Referer
        origin = headers.get("Origin", "")
        if origin:
            host = origin.replace("https://", "").replace("http://", "").split("/")[0]
        else:
            referer = headers.get("Referer", "")
            if referer:
                host = referer.replace("https://", "").replace("http://", "").split("/")[0]

    if not host:
        raise ValueError(
            "Could not determine the Host from the pasted headers. "
            "Make sure you copied all request headers including the Host line."
        )

    base_url = f"https://{host}"
    full_path = f"{api_path}?{query_string}" if query_string else api_path

    return {
        "base_url": base_url,
        "api_path": full_path,
        "headers": headers,
        "parsed_at": time.time(),
    }


def validate_headers(parsed: dict[str, Any]) -> tuple[bool, str]:
    """Validate that parsed headers contain the minimum required fields."""
    headers = parsed.get("headers", {})

    # Must have a base URL (Host header)
    if not parsed.get("base_url"):
        return False, "Missing base URL — check that the Host header is included."

    # Must have an API path (request line)
    if not parsed.get("api_path"):
        return False, (
            "Missing API path — make sure you copy the first line of the request "
            "(e.g. 'POST /api/... HTTP/1.1') along with the headers."
        )

    # Cookie is essential for auth
    cookie = headers.get("Cookie", "")
    if not cookie:
        return False, "Missing 'Cookie' header — authentication will fail."

    return True, "Headers look valid."


def mask_sensitive(value: str, visible: int = 12) -> str:
    """Return a masked version of *value* for safe logging."""
    if len(value) <= visible:
        return value
    return value[:visible] + "..." + f"({len(value)} chars)"


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------


class CredentialStore:
    """Manages cached auth credentials on disk.

    Resolution order for storage path:
        1. ``<repo_root>/.localforge/cloud_auth.json``  (if repo_root given and file already exists)
        2. ``~/.localforge/cloud_auth.json``             (default, shared across repos)
    """

    def __init__(
        self,
        repo_path: Path | None = None,
        ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._repo_path = repo_path

        # Resolve storage path
        self._global_path = Path.home() / ".localforge" / "cloud_auth.json"

        if repo_path:
            self._local_path = Path(repo_path) / ".localforge" / "cloud_auth.json"
        else:
            self._local_path = None

    @property
    def path(self) -> Path:
        """Return the active storage path (local-override first, then global)."""
        if self._local_path and self._local_path.is_file():
            return self._local_path
        return self._global_path

    def save(self, parsed_headers: dict[str, Any]) -> None:
        """Persist parsed headers to disk with restrictive permissions."""
        target = self._global_path
        target.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "version": 1,
            "saved_at": time.time(),
            **parsed_headers,
        }

        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # Set file permissions to user-only (0o600) on platforms that support it
        if sys.platform != "win32":
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        else:
            # On Windows, attempt to restrict ACLs via icacls (best-effort)
            try:
                import subprocess
                username = os.environ.get("USERNAME", "")
                if username:
                    subprocess.run(
                        [
                            "icacls", str(target),
                            "/inheritance:r",
                            f"/grant:r", f"{username}:(R,W)",
                        ],
                        capture_output=True,
                        timeout=5,
                    )
            except Exception:
                pass

        logger.info("Saved cloud auth to %s", target)

    def load(self) -> dict[str, Any] | None:
        """Load cached headers. Returns None if no cache exists."""
        p = self.path
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "headers" not in data:
                logger.warning("Corrupt cloud_auth.json — ignoring")
                return None
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read cloud_auth.json: %s", exc)
            return None

    def is_expired(self, data: dict[str, Any] | None = None) -> bool:
        """Check if the cached credentials have exceeded the TTL."""
        if data is None:
            data = self.load()
        if data is None:
            return True
        saved_at = data.get("saved_at", 0)
        age = time.time() - saved_at
        return age > self.ttl_seconds

    def clear(self) -> None:
        """Delete cached credentials."""
        for p in (self._global_path, self._local_path):
            if p and p.is_file():
                p.unlink(missing_ok=True)
                logger.info("Cleared cloud auth: %s", p)

    def prompt_for_headers(self) -> dict[str, Any]:
        """Interactive prompt: ask the user to paste raw request headers.

        Returns the parsed + validated + saved header dict.
        """
        console.print(
            Panel(
                "[bold yellow]Cloud Authentication Required[/bold yellow]\n\n"
                "1. Open the cloud chat web app in your browser\n"
                "2. Open DevTools (F12) → Network tab\n"
                "3. Send any message in the chat\n"
                "4. Find the API request, right-click → Copy → [bold]Copy request headers[/bold]\n"
                "   (include the first request line: POST /api/... HTTP/1.1)\n"
                "5. Paste below and press [bold]Enter[/bold] twice (empty line) to finish\n\n"
                "[dim]Your credentials are stored locally and never transmitted outside your machine.[/dim]",
                border_style="yellow",
                expand=False,
            )
        )

        lines: list[str] = []
        console.print("[bold yellow]Paste request headers below:[/bold yellow]")
        empty_count = 0
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                empty_count += 1
                if empty_count >= 2:
                    break
                lines.append(line)
            else:
                empty_count = 0
                lines.append(line)

        raw = "\n".join(lines)
        if not raw.strip():
            raise ValueError("No headers pasted. Cannot authenticate.")

        parsed = parse_raw_headers(raw)
        ok, msg = validate_headers(parsed)
        if not ok:
            console.print(f"[bold red]Validation failed:[/bold red] {msg}")
            raise ValueError(msg)

        self.save(parsed)
        console.print("[bold green]Authentication saved successfully.[/bold green]")
        cookie = parsed["headers"].get("Cookie", "")
        console.print(f"[dim]Cookie: {mask_sensitive(cookie, 20)}[/dim]")
        return parsed
