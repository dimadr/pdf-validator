"""Explicit first-admin bootstrap. No default credentials, no password in argv."""
import argparse
import getpass
import hashlib
from email_validator import validate_email
from sqlalchemy import select, func
from common.models import User
from backend.database import SessionLocal, require_backend_schema
from backend.routers.auth import get_password_hash


def create_initial_admin(db, username, password, email=None):
    require_backend_schema(db)
    if not username or len(username)>50 or username!=username.strip():
        raise ValueError('Username must contain 1 to 50 characters without surrounding whitespace')
    if email is not None:
        email=validate_email(email,check_deliverability=False).normalized
    lock=int.from_bytes(hashlib.sha256(b'project1-initial-admin').digest()[:8],'big',signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock)))
    if db.scalar(select(User.id).where(User.role=='admin',User.is_active.is_(True)).limit(1)):
        raise ValueError('An active administrator already exists; use authenticated user management')
    if db.scalar(select(User.id).where(User.username==username)):
        raise ValueError('Username already exists')
    user=User(username=username,password_hash=get_password_hash(password),email=email,role='admin',is_active=True)
    db.add(user);db.flush()
    return user


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username',required=True)
    parser.add_argument('--email')
    args=parser.parse_args()
    first=getpass.getpass('New administrator password: ')
    if first!=getpass.getpass('Repeat password: '):
        parser.error('Passwords do not match')
    with SessionLocal() as db:
        try:
            create_initial_admin(db,args.username,first,args.email)
            db.commit()
        except Exception as exc:
            db.rollback()
            from fastapi import HTTPException
            if isinstance(exc,ValueError):
                raise SystemExit(str(exc)) from None
            if isinstance(exc,HTTPException):
                raise SystemExit(exc.detail) from None
            raise SystemExit('Administrator bootstrap failed; transaction rolled back') from None
    print('Initial administrator created')


if __name__=='__main__':
    main()
