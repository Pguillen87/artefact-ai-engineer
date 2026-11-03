"""Windows-host Telegram demo using local Codex and the verified baseline."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from .agent import TelegramAdapter
from .codex_local import CodexLocalProvider, CodexQuota
from .conversation import ConversationAgent
from .review_client import configured_review_client
from .admin import published_snapshot
from .data import StructuralError, load_dataset
from .state import State
from .telegram import TelegramClient, TelegramError
from .telemetry import configure_logging, event, operation_id

@dataclass(frozen=True)
class HostConfig:
    data_dir: Path
    state_path: Path
    max_calls: int
    model: str
    token: str = field(repr=False)
    allowed_chat_id: int = field(repr=False)

def read_host_config(env):
    if env.get('ENABLE_CODEX_TELEGRAM')!='1':
        raise ValueError('ENABLE_CODEX_TELEGRAM=1 required for the authorized test')
    names=('TELEGRAM_BOT_TOKEN','TELEGRAM_ALLOWED_CHAT_ID','ARTEFACT_DATA_DIR','CODEX_DEMO_STATE_PATH','CODEX_DEMO_MAX_CALLS','CODEX_DEMO_MODEL')
    missing=[key for key in names if not env.get(key)]
    if missing: raise ValueError('Missing environment variables: '+', '.join(missing))
    try:
        chat=int(env['TELEGRAM_ALLOWED_CHAT_ID']);limit=int(env['CODEX_DEMO_MAX_CALLS'])
        if chat <= 0 or limit <= 0: raise ValueError()
    except ValueError:
        raise ValueError('Chat ID and call limit must be positive integers') from None
    data_dir=Path(env['ARTEFACT_DATA_DIR']).resolve()
    state_path=Path(env['CODEX_DEMO_STATE_PATH']).resolve()
    if state_path == data_dir or data_dir in state_path.parents:
        raise ValueError('State database must be outside the original data directory')
    return HostConfig(data_dir,state_path,limit,env['CODEX_DEMO_MODEL'],env['TELEGRAM_BOT_TOKEN'],chat)


def poll_once(client,adapter,offset):
    for update in client.get_updates(offset):
        if not isinstance(update,dict) or type(update.get('update_id')) is not int:
            raise TelegramError('Invalid update identifier')
        message=update.get('message')
        chat=message.get('chat',{}) if isinstance(message,dict) else {}
        if isinstance(chat,dict) and chat.get('type')=='private' and chat.get('id') == adapter.allowed_chat_id:
            adapter.handle(update)
        else:
            event(operation_id(),'telegram','update','access','ignored')
        offset=max(offset,update['update_id']+1)
    return offset


def main():
    configure_logging()
    try:
        config=read_host_config(os.environ)
        executable=shutil.which('codex.exe') if os.name=='nt' else shutil.which('codex')
        if not executable: raise ValueError('Codex CLI executable not found')
        snapshot=load_dataset(config.data_dir,verify_baseline=True)
        state=State(config.state_path)
        quota=CodexQuota(config.state_path,config.max_calls)
        provider=CodexLocalProvider(executable,quota,model=config.model)
        client=TelegramClient(config.token)
        agent=ConversationAgent(snapshot,provider,snapshot_loader=(lambda: published_snapshot(Path(os.environ['ARTEFACT_CATALOG_DB']),snapshot)) if os.environ.get('ARTEFACT_CATALOG_DB') else None,review_submit=configured_review_client(os.environ))
        adapter=TelegramAdapter(agent,client.send,state=state,allowed_chat_id=config.allowed_chat_id)
    except (ValueError,OSError,StructuralError,sqlite3.Error) as exc:
        print(str(exc),file=sys.stderr);return 2
    print('Demo iniciada; somente o chat privado autorizado será processado.',flush=True)
    offset=0
    try:
        while True:
            try: offset=poll_once(client,adapter,offset)
            except TelegramError as exc:
                event(operation_id(),'telegram','poll','transport','failure',error=exc)
                print('Falha temporária no Telegram; nova tentativa.',file=sys.stderr,flush=True)
                time.sleep(3)
    except KeyboardInterrupt:
        return 0

if __name__=='__main__': raise SystemExit(main())
