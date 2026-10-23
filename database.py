
import os

from dotenv import load_dotenv

try:
    from sqlalchemy import URL, create_engine  # type: ignore[reportMissingImports]
    from sqlalchemy.orm import declarative_base, sessionmaker  # type: ignore[reportMissingImports]
except ImportError as exc:
    raise RuntimeError(
        "SQLAlchemy is required. Install the backend dependencies first."
    ) from exc

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "voice_agent_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_PASSWORD:
    raise RuntimeError(
        "DB_PASSWORD is missing. Check your backend .env file."
    )

DATABASE_URL = URL.create(
    drivername="postgresql+psycopg",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
