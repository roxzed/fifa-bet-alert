"""
Testa cada etapa do pipeline de forma isolada.

Uso:
    python scripts/test_pipeline.py
"""

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

OK = "[OK]"
FAIL = "[FALHOU]"
WARN = "[AVISO]"
SKIP = "[SKIP]"

results = {}


def header(n: int, title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  ETAPA {n}: {title}")
    print(f"{'='*55}")


async def test_api_connection():
    """Etapa 1: BetsAPI - auth + endpoints v1 e v2."""
    header(1, "BetsAPI — Conexão e Dados")
    from src.api.betsapi_client import BetsAPIClient

    api = BetsAPIClient(
        token=settings.betsapi_token,
        base_url=settings.betsapi_base_url,
        v2_url=settings.betsapi_v2_url,
    )

    # 1a. Jogos encerrados hoje (v2)
    today = date.today().strftime("%Y%m%d")
    print(f"\n[1a] Jogos encerrados hoje via v2 (league {settings.default_league_id})...")
    try:
        ended = await api.get_ended_events(settings.default_league_id, day=today, use_v2=True)
        print(f"  {OK} {len(ended)} jogos encerrados hoje")
        if ended:
            ev = ended[-1]
            print(f"  Último: {ev.home_name} {ev.home_score}-{ev.away_score} {ev.away_name}")
            if ev.home_team:
                print(f"  Times: {ev.home_team} vs {ev.away_team}")
        results["1a_ended"] = True
    except Exception as e:
        print(f"  {FAIL} {e}")
        results["1a_ended"] = False

    # 1b. Jogos upcoming
    print(f"\n[1b] Jogos agendados (upcoming)...")
    upcoming = []
    try:
        upcoming = await api.get_upcoming_events(settings.default_league_id)
        print(f"  {OK} {len(upcoming)} jogos agendados")
        if upcoming:
            ev = upcoming[0]
            print(f"  Próximo: {ev.home_name} vs {ev.away_name} às {ev.scheduled_time}")
        results["1b_upcoming"] = True
    except Exception as e:
        print(f"  {FAIL} {e}")
        results["1b_upcoming"] = False

    # 1c. Jogos inplay
    print(f"\n[1c] Jogos ao vivo (inplay)...")
    try:
        inplay = await api.get_inplay_events(settings.default_league_id)
        print(f"  {OK} {len(inplay)} jogos ao vivo")
        if inplay:
            ev = inplay[0]
            print(f"  Em jogo: {ev.home_name} vs {ev.away_name}")
        results["1c_inplay"] = True
    except Exception as e:
        print(f"  {FAIL} {e}")
        results["1c_inplay"] = False

    # 1d. Bet365 inplay (scraping de odds)
    print(f"\n[1d] Bet365 inplay (odds de gols)...")
    try:
        b365 = await api.bet365_get_inplay_esoccer()
        print(f"  {OK} {len(b365)} eventos bet365 inplay")
        if b365:
            ev = b365[0]
            print(f"  Exemplo: {ev.home_player} vs {ev.away_player} (FI={ev.fi})")

            # Tentar pegar odds de um evento
            try:
                odds = await api.bet365_get_player_goals_odds(ev.fi)
                if odds:
                    print(f"  {OK} {len(odds)} linhas de gols encontradas")
                    for o in odds[:4]:
                        print(f"    {o.player_name} Over {o.line} @ {o.over_odds}")
                else:
                    print(f"  {WARN} Nenhuma odd de gols disponível")
            except Exception as e:
                print(f"  {WARN} Odds fetch falhou: {e}")
        results["1d_bet365"] = True
    except Exception as e:
        print(f"  {FAIL} {e}")
        results["1d_bet365"] = False

    await api.close()
    return ended, upcoming


async def test_db_and_gamewatcher(ended_events):
    """Etapa 2: Banco de dados e GameWatcher."""
    header(2, "Database + GameWatcher")
    from src.db.database import async_session_factory, get_session
    from src.db.repositories import MatchRepository, PlayerRepository

    sf = async_session_factory
    match_repo = MatchRepository(sf)
    player_repo = PlayerRepository(sf)

    if True:
        # 2a. Verificar dados no banco
        print(f"\n[2a] Estado do banco de dados...")
        from sqlalchemy import text
        r = await match_repo.execute_query(text("SELECT COUNT(*) FROM matches"))
        total_matches = r.scalar()
        r = await match_repo.execute_query(text("SELECT COUNT(*) FROM matches WHERE is_return_match = true AND score_home IS NOT NULL"))
        total_pairs = r.scalar()
        r = await match_repo.execute_query(text("SELECT COUNT(*) FROM players WHERE total_return_matches > 0"))
        total_players = r.scalar()
        r = await match_repo.execute_query(text("SELECT COUNT(*) FROM method_stats"))
        total_method_stats = r.scalar()
        r = await match_repo.execute_query(text("SELECT COUNT(*) FROM alerts"))
        total_alerts = r.scalar()

        print(f"  Matches: {total_matches}")
        print(f"  Pares validados: {total_pairs}")
        print(f"  Players com stats: {total_players}")
        print(f"  method_stats rows: {total_method_stats}")
        print(f"  Alerts: {total_alerts}")

        all_ok = total_matches > 0 and total_players > 0 and total_method_stats > 0
        print(f"  {OK if all_ok else FAIL} Banco {'populado' if all_ok else 'com dados faltando'}")
        results["2a_db"] = all_ok

        # 2b. Simular processamento de um jogo encerrado
        print(f"\n[2b] Simular detecção de jogo encerrado...")
        if ended_events:
            ev = ended_events[-1]
            existing = await match_repo.get_by_api_event_id(ev.id)
            if existing:
                print(f"  {OK} Jogo já existe no DB (id={existing.id}, status={existing.status})")
            else:
                print(f"  {OK} Jogo novo — GameWatcher criaria registro")
            results["2b_detect"] = True

            # Checar se identifica perdedor corretamente
            if ev.home_score is not None and ev.away_score is not None:
                if ev.home_score == ev.away_score:
                    print(f"  Resultado: empate ({ev.home_score}-{ev.away_score}), sem pair matching")
                elif ev.home_score < ev.away_score:
                    print(f"  Perdedor: {ev.home_name} ({ev.home_score}-{ev.away_score})")
                else:
                    print(f"  Perdedor: {ev.away_name} ({ev.home_score}-{ev.away_score})")
        else:
            print(f"  {SKIP} Sem jogos encerrados para testar")
            results["2b_detect"] = None


async def test_pair_matcher(ended_events, upcoming_events):
    """Etapa 3: PairMatcher."""
    header(3, "PairMatcher — Encontrar jogo de volta")
    from src.db.database import async_session_factory, get_session
    from src.db.repositories import MatchRepository
    from sqlalchemy import text

    sf = async_session_factory
    match_repo = MatchRepository(sf)

    if True:
        # 3a. Checar pares existentes no DB
        print(f"\n[3a] Pares recentes no banco...")
        r = await match_repo.execute_query(text("""
            SELECT m2.id, m1.player_home, m1.player_away,
                   m1.score_home || '-' || m1.score_away as g1_score,
                   m2.score_home || '-' || m2.score_away as g2_score,
                   m2.started_at
            FROM matches m2
            JOIN matches m1 ON m2.pair_match_id = m1.id
            WHERE m2.is_return_match = true
            ORDER BY m2.id DESC LIMIT 3
        """))
        rows = r.fetchall()
        if rows:
            print(f"  {OK} {len(rows)} pares recentes encontrados:")
            for row in rows:
                print(f"    Par #{row.id}: {row.player_home} vs {row.player_away} | G1: {row.g1_score} -> G2: {row.g2_score}")
            results["3a_pairs_db"] = True
        else:
            print(f"  {WARN} Nenhum par recente")
            results["3a_pairs_db"] = False

        # 3b. Verificar se existe jogo de volta nos upcoming para um jogo recente
        print(f"\n[3b] Simular busca de jogo de volta...")
        if ended_events and upcoming_events:
            # Pegar um jogo encerrado não-empate
            test_event = None
            for ev in reversed(ended_events):
                if ev.home_score is not None and ev.away_score is not None and ev.home_score != ev.away_score:
                    test_event = ev
                    break

            if test_event:
                players = {test_event.home_name.lower().strip(), test_event.away_name.lower().strip()}
                found_return = False
                for up in upcoming_events:
                    up_players = {up.home_name.lower().strip(), up.away_name.lower().strip()}
                    if up_players == players:
                        found_return = True
                        print(f"  {OK} Jogo de volta encontrado nos upcoming!")
                        print(f"    G1: {test_event.home_name} {test_event.home_score}-{test_event.away_score} {test_event.away_name}")
                        print(f"    G2: {up.home_name} vs {up.away_name} às {up.scheduled_time}")
                        break
                if not found_return:
                    # Pode já ter acontecido ou ainda não agendado — não é erro
                    print(f"  {WARN} Nenhum upcoming com mesmos jogadores (pode ja ter passado)")
                    print(f"    G1: {test_event.home_name} vs {test_event.away_name}")
                results["3b_find_return"] = found_return or None  # None = skip, not failure
            else:
                print(f"  {SKIP} Todos jogos recentes são empates")
                results["3b_find_return"] = None
        else:
            print(f"  {SKIP} Sem dados suficientes")
            results["3b_find_return"] = None


async def test_odds_monitor():
    """Etapa 4: OddsMonitor — bet365 odds."""
    header(4, "OddsMonitor — Odds bet365")
    from src.api.betsapi_client import BetsAPIClient

    api = BetsAPIClient(
        token=settings.betsapi_token,
        base_url=settings.betsapi_base_url,
        v2_url=settings.betsapi_v2_url,
    )

    print(f"\n[4a] Buscar eventos bet365 inplay...")
    try:
        events = await api.bet365_get_inplay_esoccer()
        if not events:
            print(f"  {WARN} Nenhum evento bet365 inplay agora (pode não ter jogos)")
            results["4a_bet365_inplay"] = None
            await api.close()
            return

        print(f"  {OK} {len(events)} eventos encontrados")

        # 4b. Testar fetch de odds de gols de um evento
        print(f"\n[4b] Buscar odds de gols do jogador...")
        for ev in events:
            try:
                odds = await api.bet365_get_player_goals_odds(ev.fi)
                if odds:
                    print(f"  {OK} Odds encontradas para {ev.home_player} vs {ev.away_player}:")
                    # Agrupar por jogador
                    players_odds = {}
                    for o in odds:
                        if o.player_name not in players_odds:
                            players_odds[o.player_name] = []
                        players_odds[o.player_name].append(f"O{o.line}@{o.over_odds:.2f}")

                    for pname, lines in players_odds.items():
                        print(f"    {pname}: {' | '.join(lines)}")
                    results["4b_player_odds"] = True
                    break
                else:
                    continue
            except Exception as e:
                print(f"  {WARN} Erro ao buscar odds de {ev.fi}: {e}")
                continue
        else:
            print(f"  {WARN} Nenhum evento com odds de gols disponíveis")
            results["4b_player_odds"] = None

    except Exception as e:
        print(f"  {FAIL} {e}")
        results["4a_bet365_inplay"] = False

    await api.close()


async def test_stats_engine():
    """Etapa 5: StatsEngine — cálculo de probabilidade."""
    header(5, "StatsEngine + AlertEngine")
    from src.db.database import async_session_factory, get_session
    from src.db.repositories import (
        AlertRepository, MatchRepository, MethodStatsRepository,
        PlayerRepository, TeamStatsRepository,
    )
    from src.core.stats_engine import StatsEngine

    sf = async_session_factory
    stats = StatsEngine(
        match_repo=MatchRepository(sf),
        player_repo=PlayerRepository(sf),
        alert_repo=AlertRepository(sf),
        method_stats_repo=MethodStatsRepository(sf),
        team_stats_repo=TeamStatsRepository(sf),
    )

    if True:
        # 5a. Base probability (global)
        print(f"\n[5a] Probabilidade base (global)...")
        r25, r35, r45, n = await stats.get_base_probability()
        print(f"  O2.5: {r25:.1%} | O3.5: {r35:.1%} | O4.5: {r45:.1%} | n={n}")
        if n == 0:
            print(f"  {FAIL} method_stats 'global' vazio! Rodar backfill_method_stats.py")
            results["5a_base_prob"] = False
        else:
            print(f"  {OK} Dados reais (n={n})")
            results["5a_base_prob"] = True

        # 5b. Loss type probability
        print(f"\n[5b] Probabilidade por tipo de derrota...")
        for lt in ["tight", "medium", "blowout"]:
            r25, r35, r45, n = await stats.get_loss_type_probability(lt)
            print(f"  {lt:8s}: O2.5={r25:.1%} O3.5={r35:.1%} n={n}")
        results["5b_loss_type"] = True

        # 5c. Player probability (testar com jogador conhecido)
        print(f"\n[5c] Probabilidade de jogador específico...")
        from sqlalchemy import text
        r = await stats.matches.execute_query(text("""
            SELECT stat_key, total_samples, hit_rate_25, hit_rate_35
            FROM method_stats
            WHERE stat_type = 'player' AND total_samples >= 30
            ORDER BY total_samples DESC LIMIT 1
        """))
        row = r.fetchone()
        if row:
            player_name = row.stat_key.replace("player_general_", "")
            r25, r35, r45, n = await stats.get_player_probability(player_name)
            print(f"  {player_name}: O2.5={r25:.1%} O3.5={r35:.1%} n={n}")
            print(f"  {OK} Player com Bayesian update aplicado")
            results["5c_player"] = True
        else:
            print(f"  {WARN} Nenhum player com 30+ samples")
            results["5c_player"] = None

        # 5d. Cold start check
        print(f"\n[5d] Cold start status...")
        cold_done = await stats.is_cold_start_complete()
        progress = await stats.get_cold_start_progress()
        print(f"  Cold start completo: {cold_done}")
        print(f"  Dias coletados: {progress.days_collected} / {progress.cold_start_days}")
        print(f"  Total jogos: {progress.total_games} | Pares: {progress.total_pairs}")
        if cold_done:
            print(f"  {OK} Sistema ATIVO para alertas")
        else:
            print(f"  {FAIL} Sistema em cold start — alertas desligados")
        results["5d_cold_start"] = cold_done

        # 5e. Simulação completa de evaluate_opportunity
        print(f"\n[5e] Simulação de evaluate_opportunity...")
        if row:
            try:
                evaluation = await stats.evaluate_opportunity(
                    losing_player=player_name,
                    opponent_player="test_opponent",
                    game1_score_winner=3,
                    game1_score_loser=1,
                    over25_odds=1.80,
                    over35_odds=2.50,
                    over45_odds=4.00,
                    over15_odds=1.30,
                    match_time=datetime.now(timezone.utc),
                    loser_team="Test Team",
                    opponent_team="Opp Team",
                    odds_history=[],
                    loser_goals_g1=1,
                )
                print(f"  should_alert: {evaluation.should_alert}")
                print(f"  best_line: {evaluation.best_line}")
                print(f"  true_prob: {evaluation.true_prob:.1%}" if evaluation.true_prob else "  true_prob: N/A")
                print(f"  edge: {evaluation.edge_val:.1%}" if evaluation.edge_val else "  edge: N/A")
                print(f"  EV: {evaluation.expected_value_val:.1%}" if evaluation.expected_value_val else "  EV: N/A")
                print(f"  reason: {evaluation.reason}")
                print(f"  {OK} Pipeline de avaliação funcionando")
                results["5e_evaluate"] = True
            except Exception as e:
                print(f"  {FAIL} Erro na avaliação: {e}")
                import traceback
                traceback.print_exc()
                results["5e_evaluate"] = False
        else:
            print(f"  {SKIP} Sem player para testar")
            results["5e_evaluate"] = None


async def test_telegram():
    """Etapa 6: Telegram — envio de mensagem de teste."""
    header(6, "Telegram — Envio")
    from src.telegram.bot import TelegramNotifier

    notifier = TelegramNotifier(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    print(f"\n[6a] Enviar mensagem de teste...")
    try:
        msg_id = await notifier.send_message(
            "🔧 <b>Teste de Pipeline</b>\n\n"
            "Se você está vendo esta mensagem, o Telegram está funcionando!"
        )
        if msg_id:
            print(f"  {OK} Mensagem enviada (ID: {msg_id})")
            results["6a_telegram"] = True
        else:
            print(f"  {WARN} send_message retornou None")
            results["6a_telegram"] = False
    except Exception as e:
        print(f"  {FAIL} {e}")
        results["6a_telegram"] = False


async def test_validator():
    """Etapa 7: Validator — buscar matches não validados."""
    header(7, "Validator — Pós-jogo")
    from src.db.database import async_session_factory, get_session
    from src.db.repositories import MatchRepository
    from sqlalchemy import text

    sf = async_session_factory
    match_repo = MatchRepository(sf)

    if True:
        print(f"\n[7a] Return matches não validados...")
        try:
            unvalidated = await match_repo.get_unvalidated_return_matches()
            print(f"  {OK} {len(unvalidated)} matches aguardando validação")
            if unvalidated:
                for m in unvalidated[:3]:
                    print(f"    Match #{m.id}: {m.player_home} vs {m.player_away} (status={m.status})")
            results["7a_unvalidated"] = True
        except Exception as e:
            print(f"  {FAIL} {e}")
            import traceback
            traceback.print_exc()
            results["7a_unvalidated"] = False

        # 7b. Checar se alertas recentes foram validados
        print(f"\n[7b] Alertas recentes...")
        try:
            r = await match_repo.execute_query(text("""
                SELECT id, losing_player, best_line, star_rating,
                       validated_at, actual_goals, sent_at
                FROM alerts
                ORDER BY id DESC LIMIT 5
            """))
            rows = r.fetchall()
            if rows:
                print(f"  Últimos {len(rows)} alertas:")
                for row in rows:
                    status = "validado" if row.validated_at else "pendente"
                    result_str = f"gols={row.actual_goals}" if row.actual_goals is not None else "sem resultado"
                    print(f"    #{row.id}: {row.losing_player} {row.best_line} ⭐{row.star_rating} [{status}] {result_str}")
            else:
                print(f"  Nenhum alerta no banco (normal se sistema acabou de ligar)")
            results["7b_alerts"] = True
        except Exception as e:
            print(f"  {FAIL} {e}")
            results["7b_alerts"] = False


async def main():
    print("\n" + "=" * 55)
    print("  TESTE COMPLETO DO PIPELINE — FIFA BET ALERT")
    print("=" * 55)
    print(f"  Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Liga: {settings.default_league_name} (ID: {settings.default_league_id})")
    print(f"  Cold start days: {settings.cold_start_days}")
    print(f"  Min global sample: {settings.min_global_sample}")

    # Etapa 1
    ended, upcoming = await test_api_connection()

    # Etapa 2
    await test_db_and_gamewatcher(ended)

    # Etapa 3
    await test_pair_matcher(ended, upcoming)

    # Etapa 4
    await test_odds_monitor()

    # Etapa 5
    await test_stats_engine()

    # Etapa 6
    await test_telegram()

    # Etapa 7
    await test_validator()

    # Resumo final
    print("\n" + "=" * 55)
    print("  RESUMO")
    print("=" * 55)
    for key, val in results.items():
        if val is True:
            status = OK
        elif val is False:
            status = FAIL
        elif val is None:
            status = SKIP
        else:
            status = str(val)
        print(f"  {key:25s} {status}")

    failures = [k for k, v in results.items() if v is False]
    if failures:
        print(f"\n  ⚠️  {len(failures)} FALHA(S): {', '.join(failures)}")
    else:
        print(f"\n  ✅ Todas as etapas OK!")


if __name__ == "__main__":
    asyncio.run(main())
