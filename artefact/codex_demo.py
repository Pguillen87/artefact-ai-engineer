"""Local Codex demo with synthetic data only; never loads challenge CSVs."""
import csv
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .codex_local import CodexLocalProvider, CodexQuota
from .conversation import ConversationAgent
from .data import SCHEMAS, load_dataset


def synthetic_snapshot():
    rows={
      'categories':['1','Teclados e Pianos','Categoria sintética'],
      'customers':['1','Pessoa Exemplo','000','pessoa@example.invalid','Cidade Exemplo'],
      'products':['1','880.00','Teclado Exemplo','1','Teclado sintético para demonstração','4','active',json.dumps({'keys':'61'}),'2026-01-01'],
      'orders':['1','1','2026-01-01','pending','830.00','pix','','',''],
      'order_items':['1','1','1'],
      'promotions':['1','1','10','Promoção sintética','1'],
    }
    with tempfile.TemporaryDirectory(prefix='artefact-fixture-') as directory:
        root=Path(directory)
        for table,header in SCHEMAS.items():
            with (root/(table+'.csv')).open('w',encoding='utf-8',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(header);writer.writerow(rows[table])
        return load_dataset(root)


def main():
    if os.environ.get('CODEX_DEMO_APPROVED')!='1':
        print('Defina CODEX_DEMO_APPROVED=1 após confirmar uso da cota Codex.',file=sys.stderr);return 2
    path=os.environ.get('CODEX_DEMO_STATE_PATH');limit=os.environ.get('CODEX_DEMO_MAX_CALLS')
    executable=shutil.which('codex.exe') if os.name=='nt' else shutil.which('codex')
    if not path or not limit or not executable:
        print('São necessários CODEX_DEMO_STATE_PATH, CODEX_DEMO_MAX_CALLS e Codex CLI no PATH.',file=sys.stderr);return 2
    try:
        quota=CodexQuota(path,int(limit));agent=ConversationAgent(synthetic_snapshot(),CodexLocalProvider(executable,quota))
    except (ValueError,OSError):
        print('Configuração local inválida.',file=sys.stderr);return 2
    print('Demonstração sintética. Digite uma pergunta por linha; encerre com EOF.')
    for line in sys.stdin:
        if line.strip():
            print(agent.answer(line.strip(),chat_id=1),flush=True)
            print(f'Chamadas Codex reservadas: {quota.usage()}',file=sys.stderr,flush=True)
    return 0

if __name__=='__main__': raise SystemExit(main())
