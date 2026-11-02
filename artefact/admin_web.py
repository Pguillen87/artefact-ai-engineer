"""Local-only administrative UI. Uploads are processed in memory, never executed."""
from __future__ import annotations
import base64
import binascii
from email.parser import BytesParser
from email.policy import default
from html import escape
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .admin import CatalogAdmin, MAX_BYTES, _plain
from .data import StructuralError, load_dataset


def make_server(host, port, admin, password):
    if not password or len(password) < 16: raise ValueError('ARTEFACT_ADMIN_PASSWORD deve ter ao menos 16 caracteres')
    server=ThreadingHTTPServer((host,port),Handler)
    server.admin=admin; server.password=password; server.csrf=secrets.token_urlsafe(32)
    return server


class Handler(BaseHTTPRequestHandler):
    server_version='ArtefactAdmin'; sys_version=''
    def log_message(self, *args): pass
    def _send(self,status,body='',extra=None):
        raw=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def _host(self):
        return self.headers.get('Host','') in {f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}'}
    def _guard(self):
        if not self._host(): self._send(400,'Host inválido');return False
        value=self.headers.get('Authorization','')
        try:
            kind, encoded=value.split(' ',1)
            decoded=base64.b64decode(encoded,validate=True).decode('utf-8')
            ok=kind.lower()=='basic' and hmac.compare_digest(decoded.encode(),('admin:'+self.server.password).encode())
        except (ValueError,UnicodeError,binascii.Error): ok=False
        if not ok: self._send(401,'Autenticação necessária',{'WWW-Authenticate':'Basic realm="Artefact Admin", charset="UTF-8"'});return False
        return True
    def _form(self,action,fields,label):
        inputs=''.join(f'<input type="hidden" name="{escape(k)}" value="{escape(str(v),quote=True)}">' for k,v in fields.items())
        return f'<form method="post" action="{action}">{inputs}<input type="hidden" name="csrf" value="{self.server.csrf}"><button>{label}</button></form>'
    def do_GET(self):
        if not self._guard(): return
        try:
            target=urlsplit(self.path)
            if target.path=='/':
                body='<h1>Catálogo administrativo</h1><p>Importe CSV canônico e revise antes de publicar.</p>'
                body+=f'<form method="post" action="/upload" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.server.csrf}"><input name="file" type="file" accept=".csv" required><button>Validar e importar</button></form><h2>Lotes</h2><ul>'
                for b in self.server.admin.batches(): body+=f'<li><a href="/batch?id={b["id"]}">Lote {b["id"]}</a> — {escape(b["status"])}</li>'
                body+='</ul><h2>Versões publicadas</h2><ul>'
                for v in self.server.admin.versions():
                    if v['active']: body+=f'<li>Versão {v["id"]} (ativa)</li>'
                    else: body+=f'<li>Versão {v["id"]}<form method="post" action="/rollback"><input type="hidden" name="csrf" value="{self.server.csrf}"><input type="hidden" name="version" value="{v["id"]}"><label>Motivo <input name="reason" maxlength="1000" required></label><button>Ativar versão</button></form></li>'
                body+='</ul>'
            elif target.path=='/batch':
                batch=int(parse_qs(target.query)['id'][0]); info=self.server.admin.preview(batch)
                body=f'<h1>Lote {batch}</h1><p>Estado: {escape(info["status"])}</p><h2>Produtos a adicionar</h2>'
                for p in info['products']:
                    body+='<pre>'+escape(json.dumps(_plain(p),ensure_ascii=False,default=str,indent=2))+'</pre>'
                body+='<h2>Conflitos por campo</h2>'
                for issue in info['issues']:
                    body+='<pre>'+escape(json.dumps(issue,ensure_ascii=False,indent=2))+'</pre>'
                    if info['status']=='staged' and not issue['decision']:
                        body+=f'<form method="post" action="/decide"><input type="hidden" name="csrf" value="{self.server.csrf}"><input type="hidden" name="batch" value="{batch}"><input type="hidden" name="issue" value="{issue["id"]}"><label>Justificativa <input name="justification" maxlength="1000" required></label><button>Aceitar esta exceção</button></form><p>Para corrigir o conflito, envie um novo CSV com os dados corrigidos.</p>'
                if not info['issues']: body+='<p>Nenhum conflito detectado.</p>'
                if info['status']=='staged':
                    body+=f'<form method="post" action="/approve"><input type="hidden" name="csrf" value="{self.server.csrf}"><input type="hidden" name="batch" value="{batch}"><label>Revisão humana <input name="review" maxlength="1000" required></label><button>Aprovar lote</button></form>'
                if info['status']=='approved': body+=self._form('/publish',{'batch':batch},'Publicar lote')
                body+='<p><a href="/">Voltar</a></p>'
            else: self._send(404,'Não encontrado');return
            self._send(200,'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Artefact Admin</title><body>'+body+'</body></html>')
        except (sqlite3.Error,OSError): self._send(503,'Serviço temporariamente indisponível')
        except (ValueError,KeyError,IndexError,StructuralError): self._send(400,'Consulta inválida')
    def do_POST(self):
        if not self._guard(): return
        if self.headers.get('Origin') != 'http://'+self.headers.get('Host',''):
            self._send(403,'Origem inválida');return
        try:
            size=int(self.headers.get('Content-Length','-1'))
            if size < 0 or size > MAX_BYTES+65536: self._send(413,'Arquivo grande demais');return
            raw=self.rfile.read(size);content_type=self.headers.get('Content-Type','')
            files={}; fields={}
            if content_type.startswith('multipart/form-data;'):
                msg=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                if not msg.is_multipart(): raise ValueError()
                for part in msg.iter_parts():
                    key=part.get_param('name',header='content-disposition');filename=part.get_filename()
                    if key in fields or key in files: raise ValueError()
                    data=part.get_payload(decode=True)
                    if filename is not None: files[key]=(filename,data)
                    else: fields[key]=data.decode('utf-8')
            elif content_type=='application/x-www-form-urlencoded':
                parsed=parse_qs(raw.decode('utf-8'),keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in parsed.values()): raise ValueError()
                fields={k:v[0] for k,v in parsed.items()}
            else: raise ValueError()
            if not hmac.compare_digest(fields.get('csrf',''),self.server.csrf): self._send(403,'CSRF inválido');return
            if self.path=='/upload':
                filename,data=files['file']
                if not filename.lower().endswith('.csv'): raise ValueError('Envie arquivo .csv')
                batch=self.server.admin.stage(data); location=f'/batch?id={batch}'
            elif self.path=='/decide':
                batch=int(fields['batch']);self.server.admin.decide(batch,fields['issue'],'accept',fields['justification']);location=f'/batch?id={batch}'
            elif self.path=='/approve':
                batch=int(fields['batch']);self.server.admin.approve(batch,fields['review']);location=f'/batch?id={batch}'
            elif self.path=='/publish':
                self.server.admin.publish(int(fields['batch']));location='/'
            elif self.path=='/rollback':
                self.server.admin.rollback(int(fields['version']),fields['reason']);location='/'
            else: self._send(404,'Não encontrado');return
            self._send(303,'',{'Location':location})
        except (sqlite3.Error,OSError): self._send(503,'Serviço temporariamente indisponível')
        except (ValueError,KeyError,UnicodeError,TypeError,StructuralError): self._send(400,'Entrada inválida ou lote não publicável')


def validate_paths(admin_path, catalog_path, data_dir):
    roots=(Path(data_dir).resolve(),Path(__file__).resolve().parent.parent)
    for path in (admin_path,catalog_path):
        target=Path(path).resolve()
        if any(target==root or root in target.parents for root in roots):
            raise ValueError('Bancos operacionais devem ficar fora do repositório e dos dados originais')


def main():
    try:
        data=Path(os.environ['ARTEFACT_DATA_DIR'])
        validate_paths(os.environ['ARTEFACT_ADMIN_DB'],os.environ['ARTEFACT_CATALOG_DB'],data)
        if len(os.environ.get('ARTEFACT_ADMIN_PASSWORD','')) < 16: raise ValueError('Senha administrativa insuficiente')
        baseline=load_dataset(data,verify_baseline=True)
        admin=CatalogAdmin(os.environ['ARTEFACT_ADMIN_DB'],os.environ['ARTEFACT_CATALOG_DB'],baseline)
        server=make_server(os.environ.get('ARTEFACT_ADMIN_BIND','127.0.0.1'),int(os.environ.get('ARTEFACT_ADMIN_PORT','8080')),admin,os.environ.get('ARTEFACT_ADMIN_PASSWORD',''))
        server.serve_forever()
    except (KeyError,ValueError,OSError,sqlite3.Error,StructuralError) as exc:
        print('Administração não iniciada:',type(exc).__name__)
        return 2
    except KeyboardInterrupt: return 0

if __name__=='__main__': raise SystemExit(main())
