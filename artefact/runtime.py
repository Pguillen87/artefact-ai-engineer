"""Opt-in demo runtime. No network calls occur until explicitly started."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import sys
import time

from .agent import TelegramAdapter
from .conversation import ConversationAgent
from .review_client import configured_review_client
from .admin import published_snapshot
from .data import StructuralError, load_dataset
from .provider import ResponsesProvider
from .state import State
from .telegram import TelegramClient, TelegramError

@dataclass(frozen=True)
class Config:
    data_dir: Path
    model: str
    state_path: Path
    budget: Decimal
    max_calls: int
    input_price: Decimal
    output_price: Decimal
    api_key: str = field(repr=False)
    telegram_token: str = field(repr=False)


def read_config(env):
    required=('ARTEFACT_DATA_DIR','OPENAI_MODEL','OPENAI_API_KEY','TELEGRAM_BOT_TOKEN','ARTEFACT_STATE_PATH','OPENAI_BUDGET_USD','OPENAI_MAX_CALLS','OPENAI_INPUT_USD_PER_MILLION','OPENAI_OUTPUT_USD_PER_MILLION')
    if env.get('ENABLE_REAL_LLM') != '1':
        raise ValueError('ENABLE_REAL_LLM=1 is required after approving external API use')
    missing=[key for key in required if not env.get(key)]
    if missing:
        raise ValueError('Missing environment variables: '+', '.join(missing))
    try:
        budget=Decimal(env['OPENAI_BUDGET_USD']);max_calls=int(env['OPENAI_MAX_CALLS'])
        input_price=Decimal(env['OPENAI_INPUT_USD_PER_MILLION']);output_price=Decimal(env['OPENAI_OUTPUT_USD_PER_MILLION'])
        if any(not n.is_finite() or n<=0 for n in (budget,input_price,output_price)) or max_calls<=0:
            raise ValueError('budget, rates, and call limit must be positive')
    except (InvalidOperation,ValueError):
        raise ValueError('Invalid budget, rates, or call limit') from None
    return Config(Path(env['ARTEFACT_DATA_DIR']),env['OPENAI_MODEL'],Path(env['ARTEFACT_STATE_PATH']),budget,max_calls,input_price,output_price,env['OPENAI_API_KEY'],env['TELEGRAM_BOT_TOKEN'])


def main():
    try:
        config=read_config(os.environ)
        snapshot=load_dataset(config.data_dir,verify_baseline=True)
        client=TelegramClient(config.telegram_token)
        state=State(config.state_path)
        provider=ResponsesProvider(config.api_key,config.model,state,config.budget,config.max_calls,config.input_price,config.output_price)
        adapter=TelegramAdapter(ConversationAgent(snapshot,provider,snapshot_loader=(lambda: published_snapshot(Path(os.environ['ARTEFACT_CATALOG_DB']),snapshot)) if os.environ.get('ARTEFACT_CATALOG_DB') else None,review_submit=configured_review_client(os.environ)),client.send,state=state)
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
