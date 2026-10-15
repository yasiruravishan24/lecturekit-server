"""Small command-line tool for the server owner (SERVER_PLAN.md section 6: no password reset by email).

    python -m server.admin set-password <email>
    python -m server.admin list-users
"""
import getpass
import sys

from sqlalchemy import select

from .db import make_session_factory
from .models import User
from .security import hash_password
from .settings import Settings


def main(argv: list[str]) -> int:
    settings = Settings.from_env()
    sf = make_session_factory(settings)
    if len(argv) == 2 and argv[0] == "set-password":
        email = argv[1].strip().lower()
        with sf() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                print(f"no user with email {email}")
                return 1
            pw = getpass.getpass("New password (at least 8 characters): ")
            if len(pw) < 8 or pw != getpass.getpass("Again: "):
                print("passwords differ or are shorter than 8 characters; nothing changed")
                return 1
            user.password_hash = hash_password(pw, settings.bcrypt_rounds)
            db.commit()
        print(f"password changed for {email}")
        return 0
    if argv == ["list-users"]:
        with sf() as db:
            for u in db.scalars(select(User).order_by(User.id)):
                print(u.id, u.email, "active" if u.is_active else "inactive", u.created_at)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
