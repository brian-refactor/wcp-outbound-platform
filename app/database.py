from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# Railway (and most PaaS) set DATABASE_URL with the plain postgresql:// scheme.
# psycopg3 requires postgresql+psycopg://, so normalise it here.
_db_url = settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(_db_url)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
