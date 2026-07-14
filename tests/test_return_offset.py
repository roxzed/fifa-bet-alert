"""Testes do estimador de offset G1->volta (mediana historica)."""

from unittest.mock import AsyncMock, MagicMock

from src.core.return_offset import estimate_return_offset_minutes


def _repo(values: list[int]):
    """match_repo cujo agregador retorna os time_between_games dados."""
    repo = MagicMock()
    repo.get_return_time_gaps = AsyncMock(return_value=values)
    return repo


async def test_mediana_com_amostra_suficiente():
    gaps = [50, 55, 60, 60, 65, 70] + [58] * 20  # 26 pares
    repo = _repo(gaps)
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert 55 <= off <= 60  # mediana proxima de 58


async def test_fallback_com_amostra_pequena():
    repo = _repo([55, 60, 62])  # so 3 pares < min_sample
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert off == 58.0


async def test_ignora_outliers_fora_de_20_120():
    # valores fora da faixa nao devem entrar (o repo ja filtra, mas garantimos)
    gaps = [5, 200] + [60] * 25
    repo = _repo(gaps)
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert 55 <= off <= 65
