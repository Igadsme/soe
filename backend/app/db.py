from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from .config import settings

url = settings.database_url
if url.startswith("sqlite:///"):
    url = url.replace("sqlite:///", "sqlite+aiosqlite:///")
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_async_engine(url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as value:
        yield value
