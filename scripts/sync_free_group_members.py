"""Lista TODOS os membros do grupo free via Telethon (User API) e popula
a tabela free_group_members.

Por que User API e não Bot API?
    A Bot API do Telegram NÃO permite listar membros de um grupo (limitação
    de privacidade). A User API permite — usando sua conta de usuário.

Modos de uso:

    INTERATIVO (terminal local):
        python scripts/sync_free_group_members.py
        → Pede telefone e código no próprio terminal.

    NÃO-INTERATIVO (controle por arquivo, pra rodar via agente):
        export TELETHON_PHONE=+5511999998888
        python scripts/sync_free_group_members.py
        → Lê código do arquivo tmp/telethon_code.txt (cria a pasta automaticamente).
        → Lê senha 2FA (se existir) de tmp/telethon_password.txt.

Após o 1º login, cria lenda_user.session e não pede mais nada.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Garante que a raiz do projeto está no sys.path quando rodado direto
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.tl.types import User  # noqa: E402

from src.config import settings  # noqa: E402
from src.db.database import async_session_factory, init_db  # noqa: E402
from src.db.models import FreeGroupMember  # noqa: E402


SESSION_NAME = "lenda_user"
TMP_DIR = PROJECT_ROOT / "tmp"
CODE_FILE = TMP_DIR / "telethon_code.txt"
PASSWORD_FILE = TMP_DIR / "telethon_password.txt"


def _read_file_when_ready(path: Path, prompt: str, timeout: int = 600) -> str:
    """Aguarda um arquivo ser criado (até timeout segundos), lê e remove."""
    print(f"\n>>> Aguardando {prompt}...")
    print(f"    Esperando arquivo: {path}")
    print(f"    (timeout: {timeout}s)")
    waited = 0
    while not path.exists():
        if waited >= timeout:
            raise TimeoutError(f"Timeout esperando {path}")
        # noinspection PyUnresolvedReferences
        import time
        time.sleep(1)
        waited += 1
    value = path.read_text(encoding="utf-8").strip()
    try:
        path.unlink()
    except OSError:
        pass
    return value


async def main() -> None:
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    free_group_id = settings.telegram_free_group_id

    if not api_id or not api_hash:
        print(
            "ERRO: configure TELEGRAM_API_ID e TELEGRAM_API_HASH no .env.\n"
            "Pega em: https://my.telegram.org → API development tools."
        )
        sys.exit(1)
    if not free_group_id:
        print("ERRO: configure TELEGRAM_FREE_GROUP_ID no .env.")
        sys.exit(1)

    await init_db()

    TMP_DIR.mkdir(exist_ok=True)

    print(f"Conectando ao Telegram (API ID {api_id})...")

    phone_env = os.environ.get("TELETHON_PHONE", "").strip()
    non_interactive = bool(phone_env)

    client = TelegramClient(SESSION_NAME, int(api_id), api_hash)

    if non_interactive:
        # Modo não-interativo: pega phone do env, code de arquivo.
        await client.connect()
        if not await client.is_user_authorized():
            print(f"Enviando code request para {phone_env}...")
            await client.send_code_request(phone_env)
            print(">>> Verifica o Telegram, vai chegar um código de 5 dígitos.")
            code = _read_file_when_ready(CODE_FILE, "código do Telegram")
            try:
                await client.sign_in(phone=phone_env, code=code)
            except Exception as e:
                if "password" in str(e).lower() or "two-step" in str(e).lower() \
                        or "SESSION_PASSWORD_NEEDED" in str(e):
                    pw = _read_file_when_ready(
                        PASSWORD_FILE, "senha 2FA do Telegram"
                    )
                    await client.sign_in(password=pw)
                else:
                    raise
    else:
        await client.start()  # interativo (input no terminal)

    me = await client.get_me()
    print(f"Logado como: {me.first_name} (@{me.username or '-'})")

    chat_id_int = int(free_group_id)
    print(f"Buscando grupo {chat_id_int}...")
    try:
        entity = await client.get_entity(chat_id_int)
    except Exception as e:
        print(f"ERRO ao buscar grupo: {e}")
        print("Sua conta de usuário precisa ser membro do grupo free.")
        await client.disconnect()
        sys.exit(1)

    print(f"Grupo: {getattr(entity, 'title', '?')}")
    print("Listando participantes...")

    inserted = 0
    updated = 0
    skipped_bots = 0

    async with async_session_factory() as session:
        async for participant in client.iter_participants(entity):
            if not isinstance(participant, User):
                continue
            if participant.bot:
                skipped_bots += 1
                continue

            existing = await session.get(FreeGroupMember, participant.id)
            if existing:
                existing.first_name = participant.first_name or existing.first_name
                existing.username = participant.username or existing.username
                existing.last_seen = datetime.utcnow()
                updated += 1
            else:
                session.add(FreeGroupMember(
                    user_id=participant.id,
                    first_name=participant.first_name or "user",
                    username=participant.username,
                    last_seen=datetime.utcnow(),
                ))
                inserted += 1
        await session.commit()

    await client.disconnect()

    print()
    print("=" * 50)
    print("[OK] Sync concluido.")
    print(f"   Novos cadastrados: {inserted}")
    print(f"   Atualizados:       {updated}")
    print(f"   Bots ignorados:    {skipped_bots}")
    print(f"   Total processados: {inserted + updated}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
