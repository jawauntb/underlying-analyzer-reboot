"""Print the Robinhood tokens the trade watcher needs, once, on your own machine.

    pip install robin_stocks
    python scripts/robinhood_token.py

Logs in with robin_stocks (username, password, and the app's MFA prompt), then prints
the access token, refresh token and device token from the cached session so they can
be set on the Railway service as ROBINHOOD_TOKEN, ROBINHOOD_REFRESH_TOKEN and
ROBINHOOD_DEVICE_TOKEN. Nothing is written anywhere but your terminal and
robin_stocks' own ~/.tokens cache. The watcher never places orders with them; the only
non-GET it makes is the token refresh.
"""

from __future__ import annotations

import getpass
import pickle
import sys
from pathlib import Path


def main() -> int:
    try:
        import robin_stocks.robinhood as rh
    except ImportError:
        print("pip install robin_stocks first")
        return 2
    username = input("robinhood email: ").strip()
    password = getpass.getpass("robinhood password: ")
    rh.login(username, password, store_session=True)
    cache = Path.home() / ".tokens" / "robinhood.pickle"
    if not cache.exists():
        print("login did not cache a session; nothing to print")
        return 1
    with cache.open("rb") as handle:
        data = pickle.load(handle)  # noqa: S301 - our own cache file
    for env, key in (
        ("ROBINHOOD_TOKEN", "access_token"),
        ("ROBINHOOD_REFRESH_TOKEN", "refresh_token"),
        ("ROBINHOOD_DEVICE_TOKEN", "device_token"),
    ):
        print(f"{env}={data.get(key, '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
