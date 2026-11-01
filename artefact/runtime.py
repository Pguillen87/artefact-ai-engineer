"""Opt-in demo runtime. No network calls occur until explicitly started."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import time

from .agent import Agent, OpenAIPlanner, TelegramAdapter
from .data import StructuralError, load_dataset
from .telegram import TelegramClient, TelegramError

@dataclass(frozen=True)
class Config:
    data_dir: Path
    model: str
    api_key: str = field(repr=False)
    telegram_token: str = field(repr=False)


def read_config(env):
    required=('ARTEFACT_DATA_DIR','OPENAI_MODEL','OPENAI_API_KEY','TELEGRAM_BOT_TOKEN')
    if env.get('ENABLE_REAL_LLM') != '1':
        raise ValueError('ENABLE_REAL_LLM=1 is required after approving external API use')
    missing=[key for key in required if not env.get(key)]
    if missing:
        raise ValueError('Missing environment variables: '+', '.join(missing))
    return Config(Path(env['ARTEFACT_DATA_DIR']),env['OPENAI_MODEL'],env['OPENAI_API_KEY'],env['TELEGRAM_BOT_TOKEN'])


def main():
    try:
        config=read_config(os.environ)
        snapshot=load_dataset(config.data_dir,verify_baseline=True)
        client=TelegramClient(config.telegram_token)
        adapter=TelegramAdapter(Agent(snapshot,OpenAIPlanner(config.api_key,config.model)),client.send)
    except (ValueError,StructuralError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    offset=0
    try:
        while True:
            try:
                for update in client.get_updates(offset):
                    adapter.handle(update)
                    if type(update.get('update_id')) is int:
                        offset=max(offset,update['update_id']+1)
            except TelegramError:
                time.sleep(3)
    except KeyboardInterrupt:
        return 0


if __name__=='__main__':
    raise SystemExit(main())
