"""Encryption and rotation for stored OAuth credentials and upload URLs."""
import argparse
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def keyring(value: str) -> MultiFernet:
    keys = [key.strip() for key in value.split(",")]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{43}=", key) for key in keys):
        raise ValueError("Invalid Fernet keyring")
    return MultiFernet([Fernet(key.encode("ascii")) for key in keys])


def token_vault() -> MultiFernet:
    from .config import secret_value, settings
    return keyring(secret_value(settings.token_encryption_keys))


def rotate_tokens(db, vault: MultiFernet) -> int:
    from sqlalchemy import select

    from .models import ConnectedAccount, Publication
    count = 0
    for model, fields in (
        (ConnectedAccount, ("access_token", "refresh_token")),
        (Publication, ("upload_checkpoint",)),
    ):
        for row in db.scalars(select(model).with_for_update()):
            for field in fields:
                value = getattr(row, field)
                if value:
                    setattr(row, field, vault.rotate(value.encode()).decode())
                    count += 1
    db.commit()
    return count


def main() -> None:
    from .db import SessionLocal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["rotate"])
    parser.parse_args()
    with SessionLocal() as db:
        try:
            count = rotate_tokens(db, token_vault())
        except (ValueError, InvalidToken):
            raise SystemExit("Rotation failed. Check that the keyring includes every previous key.") from None
    print(f"Rotated {count} encrypted values.")


if __name__ == "__main__":
    main()
