#!/usr/bin/env python3
"""Generate an ENCRYPTION_KEY value for encrypted cookie storage."""

from cryptography.fernet import Fernet


def main() -> int:
    print(Fernet.generate_key().decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
