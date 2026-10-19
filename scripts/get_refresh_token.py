"""Run this ONCE on your own computer to get the Google refresh token.

What it does:
  1. Opens your browser with the Google sign-in page.
  2. You sign in and click "Allow" (the app only asks for the Blogger scope).
  3. Google sends a refresh token back. This script prints it.

You need `client_secret.json` next to this project (download it from the Google
Cloud console: APIs & Services -> Credentials -> your OAuth 2.0 Client ->
"Download JSON").

Copy the three printed values into your `.env` file, and into GitHub Secrets
when you run the agent on GitHub Actions.

Usage:
    python scripts/get_refresh_token.py
    python scripts/get_refresh_token.py --client-secret path/to/client_secret.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow "python scripts/get_refresh_token.py" from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from src.config import BLOGGER_SCOPE, setup_logging  # noqa: E402

# Classic OAuth scopes. SCOPES is a list so you can add more later if needed.
SCOPES = [BLOGGER_SCOPE]

# Places we look for client_secret.json, in order.
CLIENT_SECRET_CANDIDATES = (
    PROJECT_ROOT / "client_secret.json",
    Path.cwd() / "client_secret.json",
    Path(__file__).resolve().parent / "client_secret.json",
    Path.home() / ".credentials" / "client_secret.json",
)


def find_client_secret(explicit: str | None) -> Path:
    """Return the path to client_secret.json or explain how to get it."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"File not found: {path}")
        return path

    for candidate in CLIENT_SECRET_CANDIDATES:
        if candidate.is_file():
            return candidate

    raise SystemExit(
        "Could not find client_secret.json.\n"
        "Looked in:\n  " + "\n  ".join(str(p) for p in CLIENT_SECRET_CANDIDATES) + "\n\n"
        "How to get it:\n"
        "  1. Open https://console.cloud.google.com/\n"
        "  2. Create a project (or pick one) and enable the Blogger API.\n"
        "  3. Set 'OAuth consent screen' to your brand, then publish it "
        "(status: In production).\n"
        "  4. Go to Credentials -> Create Credentials -> OAuth client ID -> Desktop app.\n"
        "  5. Click 'Download JSON' and save the file as client_secret.json here.\n"
        "Then run this script again."
    )


def run_local_login(client_secret_path: Path) -> dict[str, str]:
    """Open the browser, do the consent flow and return the OAuth values."""
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)

    print("\nA browser tab will open. Sign in with the Google account that owns")
    print("your Blogger blog, then click 'Allow'.\n")

    try:
        # port=0 lets the OS pick a free port, so it works even if 8080 is busy.
        creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    except OSError:
        # Some machines (locked-down Windows setups) cannot open a local server.
        print("Could not start a local web server, falling back to the console flow.")
        print("Go to this link, sign in, and paste the code back here:")
        creds = flow.run_console(prompt="consent")

    if not creds.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token.\n"
            "Fix this before continuing:\n"
            "  - In the Google Cloud console open 'OAuth consent screen' and publish it "
            "(status: In production).\n"
            "  - If the app was added under 'Your apps' with a test account, "
            "make sure that account is listed as a test user.\n"
            "Then run this script again. (We also force prompt='consent' so Google "
            "always issues a new refresh token.)"
        )

    client_config = flow.client_config
    return {
        "client_id": client_config["client_id"],
        "client_secret": client_config["client_secret"],
        "refresh_token": creds.refresh_token,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Path to client_secret.json (default: search common locations).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    client_secret_path = find_client_secret(args.client_secret)
    print(f"Using OAuth client file: {client_secret_path}")

    values = run_local_login(client_secret_path)

    print("\n" + "=" * 72)
    print("SUCCESS - copy these three lines into your .env file:")
    print("=" * 72)
    print(f"GOOGLE_CLIENT_ID={values['client_id']}")
    print(f"GOOGLE_CLIENT_SECRET={values['client_secret']}")
    print(f"GOOGLE_REFRESH_TOKEN={values['refresh_token']}")
    print("=" * 72)
    print("Then add the same three names as GitHub Secrets if you run on GitHub Actions.")
    print("Keep them private: never commit .env to git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())