"""Testes do AlertFreeRepository."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base
from src.db.repositories import AlertFreeRepository


class Database:
    """Temporary test database wrapper."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = None
        self.session_factory = None

    async def create_tables(self) -> None:
        """Create all tables."""
        self.engine = create_async_engine(
            self.url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        """Close the engine."""
        if self.engine:
            await self.engine.dispose()


async def _repo(tmp_path):
    """Create a test database and return AlertFreeRepository."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/free.db")
    await db.create_tables()
    return AlertFreeRepository(db.session_factory), db


async def test_create_update_odds_e_validate(tmp_path):
    """Test AlertFree create, update_odds, and validate."""
    repo, db = await _repo(tmp_path)
    a = await repo.create(
        match_id=1, losing_player="Sena", opponent_player="Bosko",
        game1_score="1-3", line="over15", rate=0.80, hits=16, n_h2h=20,
        recent_hits=6,
    )
    assert a.id is not None and a.status == "pending"
    assert await repo.exists_for_match(1) is True
    await repo.update_odds(a.id, entry_odd=1.75, max_odd=2.10)
    await repo.update_telegram_message_id(a.id, 555)
    v = await repo.validate(a.id, actual_goals=3, hit=True, status="green")
    assert v.status == "green" and v.entry_odd == 1.75 and v.telegram_message_id == 555
    await db.close()


async def test_get_validated_since(tmp_path):
    """Test get_validated_since filtering by date."""
    repo, db = await _repo(tmp_path)
    a = await repo.create(
        match_id=2, losing_player="X", opponent_player="Y",
        game1_score="0-2", line="over25", rate=0.7, hits=14, n_h2h=20,
        recent_hits=5,
    )
    await repo.validate(a.id, actual_goals=3, hit=True, status="green")
    assert len(await repo.get_validated_since(datetime.utcnow() - timedelta(days=1))) == 1
    await db.close()
