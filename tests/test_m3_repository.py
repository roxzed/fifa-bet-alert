"""Testes do get_h2h_player_goals — consulta H2H genérica pro M3."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base
from src.db.repositories import AlertV3Repository, MatchRepository


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


async def _make_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/test_m3.db")
    await db.create_tables()
    return db


async def _add_match(repo, home, away, sh, sa, when, status="ended", is_return_match=False):
    return await repo.create(
        league="Esoccer Battle - 8 mins play",
        player_home=home,
        player_away=away,
        score_home=sh,
        score_away=sa,
        status=status,
        started_at=when,
        is_return_match=is_return_match,
    )


async def test_h2h_player_goals_ordena_mais_recente_primeiro(tmp_path):
    db = await _make_db(tmp_path)
    repo = MatchRepository(db.session_factory)
    base = datetime(2026, 7, 1, 12, 0)
    # 3 jogos: Sena fez 2, depois 5, depois 1 (mais recente)
    await _add_match(repo, "Sena", "Bosko", 2, 0, base)
    await _add_match(repo, "Bosko", "Sena", 3, 5, base + timedelta(hours=1))
    await _add_match(repo, "Sena", "Bosko", 1, 4, base + timedelta(hours=2))

    goals = await repo.get_h2h_player_goals("Sena", "Bosko", limit=20)

    assert goals == [1, 5, 2]  # mais recente primeiro, lados home/away corretos
    await db.close()


async def test_h2h_player_goals_ignora_nao_encerrados_e_outros_confrontos(tmp_path):
    db = await _make_db(tmp_path)
    repo = MatchRepository(db.session_factory)
    base = datetime(2026, 7, 1, 12, 0)
    await _add_match(repo, "Sena", "Bosko", 2, 0, base)
    await _add_match(repo, "Sena", "Bosko", 9, 9, base + timedelta(hours=1), status="live")
    await _add_match(repo, "Sena", "llulle", 7, 0, base + timedelta(hours=2))

    goals = await repo.get_h2h_player_goals("Sena", "Bosko")

    assert goals == [2]
    await db.close()


async def test_h2h_player_goals_respeita_limit(tmp_path):
    db = await _make_db(tmp_path)
    repo = MatchRepository(db.session_factory)
    base = datetime(2026, 7, 1, 12, 0)
    for i in range(25):
        await _add_match(repo, "Sena", "Bosko", i, 0, base + timedelta(hours=i))

    goals = await repo.get_h2h_player_goals("Sena", "Bosko", limit=20)

    assert len(goals) == 20
    assert goals[0] == 24  # o mais recente
    await db.close()


async def test_alert_v3_create_dedupe_e_validate(tmp_path):
    db = await _make_db(tmp_path)
    repo = AlertV3Repository(db.session_factory)

    alert = await repo.create(
        match_id=1,
        losing_player="Sena",
        opponent_player="Bosko",
        game1_score="1-3",
        line="over25",
        odds=1.85,
        rate=0.70,
        hits=14,
        n_h2h=20,
        recent_hits=5,
    )
    assert alert.id is not None
    assert await repo.exists_for_line(1, "over25") is True
    assert await repo.exists_for_line(1, "over35") is False

    await repo.update_telegram_message_id(alert.id, 777)
    validated = await repo.validate(alert.id, actual_goals=3, hit=True, profit_flat=0.85)
    assert validated.hit is True
    assert validated.telegram_message_id == 777

    rows = await repo.get_all_by_match_id(1)
    assert len(rows) == 1
    await db.close()


async def test_get_unvalidated_return_matches_v3(tmp_path):
    db = await _make_db(tmp_path)
    match_repo = MatchRepository(db.session_factory)
    alert_repo = AlertV3Repository(db.session_factory)
    base = datetime(2026, 7, 1, 12, 0)

    # Volta com alerta v3 NAO validado -> deve retornar
    m1 = await _add_match(repo=match_repo, home="Sena", away="Bosko", sh=2, sa=1,
                          when=base, is_return_match=True)
    await alert_repo.create(
        match_id=m1.id,
        losing_player="Sena",
        opponent_player="Bosko",
        game1_score="1-3",
        line="over25",
        rate=0.70,
        hits=14,
        n_h2h=20,
        recent_hits=5,
    )

    # Volta com alerta v3 JA validado -> nao deve retornar
    m2 = await _add_match(repo=match_repo, home="Bosko", away="llulle", sh=3, sa=0,
                          when=base + timedelta(hours=1), is_return_match=True)
    validated = await alert_repo.create(
        match_id=m2.id,
        losing_player="llulle",
        opponent_player="Bosko",
        game1_score="0-2",
        line="over15",
        rate=0.80,
        hits=16,
        n_h2h=20,
        recent_hits=6,
    )
    await alert_repo.validate(validated.id, actual_goals=0, hit=False, profit_flat=-1.0)

    matches = await match_repo.get_unvalidated_return_matches_v3()

    assert [m.id for m in matches] == [m1.id]
    await db.close()
