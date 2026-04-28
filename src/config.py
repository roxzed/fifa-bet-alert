from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # BetsAPI
    betsapi_token: str = ""
    betsapi_base_url: str = "https://api.betsapi.com/v1"
    betsapi_v2_url: str = "https://api.betsapi.com/v2"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_admin_chat_id: str = ""  # Private chat for status/regime msgs
    telegram_group_id: str = ""
    telegram_group_v2_id: str = ""  # Grupo do Method 2
    telegram_free_group_id: str = ""  # Grupo FREE (vazio = FREE inativo, so VIP recebe)
    free_min_true_prob: float = 0.80  # tp_conservative minimo pra alerta ir pro FREE
    free_max_per_day: int = 2         # cap diario BRT de alertas no FREE

    # Database
    database_url: str = "postgresql+asyncpg://localhost/fifa_bet"

    # Logging
    log_level: str = "INFO"
    file_log_level: str = "DEBUG"  # nível dos arquivos de log (default DEBUG para forensics)

    # Liga
    default_league_name: str = "Esoccer Battle - 8 mins play"
    default_league_id: str = "22614"    # Liga atual (sem ano) – usa v2 API
    historical_league_id: str = "42648"  # Liga 2025 – usa v1 API

    # Polling intervals (seconds)
    poll_interval_seconds: int = 180
    odds_poll_interval_seconds: int = 15

    # Stats Engine Thresholds
    min_edge: float = 0.20
    min_ev: float = 0.03
    min_true_prob: float = 0.60
    min_global_sample: int = 500
    min_player_sample: int = 30   # general player stats (elevated from 5)
    min_h2h_sample: int = 7       # H2H player specific (7 G2 losses vs same opponent)
    min_team_sample: int = 10
    min_odds: float = 1.60
    max_odds: float = 4.00
    kelly_fraction: float = 0.25
    cold_start_days: int = 83
    regime_window: int = 50
    regime_degraded_z: float = -2.0
    regime_warning_z: float = -1.5

    # Timezone
    timezone: str = "America/Sao_Paulo"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
