"""IDs de alertas cancelados manualmente pelo owner.

Cancelados sao exibidos como "CANCELED" / "CANC" no dashboard e no /results,
com PL +0.00u, fora das estatisticas de WR/ROI/PL agregadas.

Pra cancelar mais alertas, adicionar o ID aqui.
"""

CANCELLED_ALERT_IDS: set[int] = {1436, 1447}
