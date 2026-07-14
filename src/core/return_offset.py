"""Estimador do intervalo tipico entre o G1 e a volta (mediana historica).

Usado pelo watch preditivo pra estimar quando a volta comeca quando a
BetsAPI nao expoe o jogo futuro a tempo.
"""

from __future__ import annotations

import statistics

from loguru import logger


async def estimate_return_offset_minutes(
    match_repo, *, fallback_min: float, min_sample: int = 20
) -> float:
    """Mediana de time_between_games dos pares de volta. Fallback se amostra rasa."""
    try:
        gaps = await match_repo.get_return_time_gaps(low=20, high=120)
    except Exception as e:
        logger.warning(f"estimate_return_offset falhou ({e}); usando fallback {fallback_min}")
        return fallback_min
    valid = [g for g in gaps if g is not None and 20 <= g <= 120]
    if len(valid) < min_sample:
        logger.info(
            f"Offset da volta: amostra {len(valid)} < {min_sample} — usando fallback "
            f"{fallback_min}min"
        )
        return fallback_min
    med = statistics.median(valid)
    logger.info(f"Offset da volta estimado: {med:.1f}min (n={len(valid)})")
    return float(med)
