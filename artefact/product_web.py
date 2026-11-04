"""Docker administration shell; legacy product review remains available."""
from pathlib import Path
from html import escape
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from email.parser import BytesParser
from email.policy import default
import json
import os
import secrets
import sqlite3
import hmac
import threading
from .admin_web import Handler, make_server
from .admin import CatalogAdmin
from .data import Snapshot, SCHEMAS, StructuralError

CSS='''body{margin:0;background:#f2f5f7;color:#172c3a;font:16px system-ui,sans-serif}header{background:#102d3b;color:white;padding:24px 6%}header small{color:#92c8cc}nav{display:flex;gap:20px;flex-wrap:wrap;margin-top:18px}a{color:#087a83}header a{color:#c0e9e8}main{max-width:1100px;margin:auto;padding:30px 20px}section,.card{background:white;border:1px solid #dce4e8;border-radius:14px;padding:24px;margin:0 0 22px}h1{font-size:26px;margin:4px 0}h2{font-size:21px}label{display:block;margin:14px 0}input,select,textarea{box-sizing:border-box;width:100%;max-width:680px;padding:11px;border:1px solid #bac9d0;border-radius:7px;font:inherit}button{background:#087c82;color:white;border:0;border-radius:7px;padding:12px 20px;cursor:pointer;margin:8px 8px 0 0}button:disabled{opacity:.5}.muted{color:#526b79}.status{background:#e5f4ee;padding:14px;border-radius:8px}.message{white-space:pre-wrap;padding:16px;border-left:3px solid #1a9295;background:#f3f7f8;margin:12px 0}.role{font-size:12px;text-transform:uppercase;color:#456775}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #e0e8eb}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:600px){main{padding:15px 10px}section{padding:16px}nav{gap:12px}td,th{padding:7px}}'''


from .bootstrap_secret import bootstrap_secret


class ProductHandler(Handler):
    def do_GET(self):
        with self.server.operation_lock:
            self._get()

    def do_POST(self):
        if self.path.startswith('/chat/'):
            return self._post()
        with self.server.operation_lock:
            self._post()

    def _host(self):
        port=os.environ.get('ARTEFACT_PUBLIC_PORT',str(self.server.server_port))
        return self.headers.get('Host','') in {f'localhost:{port}',f'127.0.0.1:{port}'}

    def _send(self,status,body='',extra=None):
        if '<title>Artefact Admin</title>' in body:
            body=self.layout(body.partition('<body>')[2].partition('</body>')[0])
        if body and not body.startswith('<!doctype') and not body.startswith('{') and not (extra or {}).get('Content-Type','').startswith('text/javascript'):
            body=self.layout(body)
        raw=body.encode('utf-8');self.send_response(status)
        headers={'Content-Type':'text/html; charset=utf-8','Content-Length':str(len(raw)), 'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",'Referrer-Policy':'no-referrer'}
        headers.update(extra or {})
        for key,value in headers.items():self.send_header(key,value)
        self.end_headers();self.wfile.write(raw)

    def layout(self,body):
        return '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Artefact · Empório da Música</title><style>'+CSS+'</style><header><small>ARTEFACT / EMPÓRIO DA MÚSICA</small><h1>Central de atendimento</h1><nav><a href="/">Importação e versões</a><a href="/catalog">Catálogo e revisão</a><a href="/ai">Configuração de IA</a><a href="/chat">Chat de teste</a></nav></header><main>'+body+'<p id="operation-status" role="status" aria-live="polite"></p></main><script src="/app.js" defer></script></html>'

    def form(self,action,content,label):
        return f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{self.server.csrf}">{content}<button>{label}</button></form>'

    def api(self,method,path,payload=None):
        token=bootstrap_secret(self.server.secret_dir/'service-token')
        request=Request(self.server.agent_url+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
        try:
            with urlopen(request,timeout=90) as response:return json.load(response)
        except HTTPError as exc:
            try: code=json.load(exc).get('error','agent_unavailable')
            except (ValueError,OSError):code='agent_unavailable'
            raise ValueError(str(code)) from None
        except (URLError,OSError):raise ValueError('Agente indisponível. Confira o serviço Docker agent.') from None

    def refresh_admin(self):
        from .dataset_store import DatasetStore
        store=self.server.datasets
        if store.active_version() is not None:
            self.server.admin=CatalogAdmin(self.server.admin.admin_path,self.server.admin.serving_path,store.load(),dataset_store=store)

    def _get(self):
        route=urlsplit(self.path)
        if route.path in {'/batch','/catalog'}:
            if not self._guard():return
            try:self.refresh_admin()
            except (ValueError,sqlite3.Error):pass
            if route.path=='/catalog':self.path='/'
            return super().do_GET()
        if not self._guard():return
        try:
            if route.path=='/app.js':
                self._send(200,"document.addEventListener('submit',function(e){var s=document.getElementById('operation-status');s.textContent='Processando. Aguarde a resposta...';e.target.querySelectorAll('button').forEach(function(b){b.disabled=true;});});",{'Content-Type':'text/javascript; charset=utf-8'});return
            if route.path=='/':
                active=self.server.datasets.active_version()
                body='<section><h2>Importação de dados</h2><p class="status">'+('Sem dataset ativo. Importe os seis CSVs para habilitar o atendimento.' if active is None else f'Dataset ativo: versão {active}')+'</p><p>Selecione conjuntamente categories, customers, orders, order_items, products e promotions. Os arquivos são validados antes de qualquer ativação.</p>'
                body+=f'<form method="post" action="/dataset/import" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{self.server.csrf}"><label>Seis arquivos CSV<input type="file" name="datasets" accept=".csv" multiple required></label><label><input style="width:auto" type="checkbox" name="official" value="1"> Verificar hashes oficiais do desafio</label><label><input style="width:auto" type="checkbox" name="preserve_legacy" value="1"> Na primeira importação, migrar produtos incrementais e versões já existentes</label><button>Validar e ativar base</button></form></section><section><h2>Versões completas</h2>'
                for version in self.server.datasets.versions():
                    vid=version['id'];body+=f'<p>Versão {vid}</p>'+self.form('/dataset/activate',f'<input type="hidden" name="version" value="{vid}">','Ativar esta versão')
                if active is not None:
                    snapshot=self.server.datasets.load()
                    body+='<h3>Resumo da base ativa</h3><ul>'
                    for table,rows in snapshot.tables.items():body+='<li>'+escape(table)+': '+str(len(rows))+' registros</li>'
                    body+='</ul><h3>Observacoes para revisao humana</h3>'
                    for issue in snapshot.issues:body+='<p>Produto '+str(issue.product_id)+' / '+escape(issue.field)+' / '+escape(issue.rule)+'</p>'
                    if not snapshot.issues:body+='<p>Nenhum alerta detectado pelo scanner conservador.</p>'
                body+='</section>'
            elif route.path=='/ai':
                config=self.server.provider_config
                status=config.status()
                body='<section><h2>Configuração de IA</h2><p>As chaves são cifradas e nunca retornam ao navegador. Salvar não realiza inferência nem comprova acesso ao modelo.</p><pre>'+escape(json.dumps(status,ensure_ascii=False,default=str,indent=2))+'</pre>'
                models=config.models()
                body+='<p>Modelos compatíveis disponíveis:</p><pre>'+escape(json.dumps(models,ensure_ascii=False,default=str,indent=2))+'</pre>'
                selected=parse_qs(route.query).get('provider',['google'])[0]
                if selected not in models: selected='google'
                body+='<p>Selecionar provedor: <a href="/ai?provider=google">Google Gemini</a> · <a href="/ai?provider=openai">OpenAI</a></p>'
                saved=status['providers'].get(selected,{})
                options=''.join('<option value="'+escape(model)+'"'+(' selected' if model==saved.get('model') else '')+'>'+escape(model)+'</option>' for model in models[selected])
                fields=f'<input type="hidden" name="provider" value="{selected}"><p>Provedor: {escape(selected)}</p><label>Modelo<select name="model">{options}</select></label><label>Chave API (deixe vazia para manter a atual)<input name="api_key" type="password" autocomplete="new-password"></label><label>Limite de chamadas<input name="max_calls" type="number" value="100" min="1" required></label><label>Orçamento máximo em USD<input name="max_usd" value="1.00" required></label><label>USD por milhão de tokens de entrada<input name="input_price" value="1.00" required></label><label>USD por milhão de tokens de saída<input name="output_price" value="5.00" required></label>'
                for name,default in (('max_calls','100'),('max_usd','1.00'),('input_price','1.00'),('output_price','5.00')):
                    fields=fields.replace('name="'+name+'" type="number" value="'+default+'"','name="'+name+'" type="number" value="'+escape(str(saved.get(name,default)),quote=True)+'"').replace('name="'+name+'" value="'+default+'"','name="'+name+'" value="'+escape(str(saved.get(name,default)),quote=True)+'"')
                body+=self.form('/ai/save',fields,'Salvar configuração')
                body+='<p>O teste de conexão faz uma chamada real, pequena e potencialmente cobrada, usando os limites configurados.</p>'
                body+=self.form('/ai/test',f'<input type="hidden" name="provider" value="{selected}">','Testar conexão com a API real')
                body+=self.form('/ai/activate',f'<input type="hidden" name="provider" value="{selected}">','Ativar configuração testada')
                body+=self.form('/ai/remove','<label>Provedor<select name="provider"><option value="google">Google Gemini</option><option value="openai">OpenAI</option></select></label>','Excluir chave')+'</section>'
            elif route.path=='/chat':
                body='<section><h2>Chat de teste</h2><p class="muted">Cada conversa preserva a versão dos dados, o provedor e o modelo. Respostas comerciais são calculadas em Python.</p>'
                if self.server.datasets.active_version() is None:body+='<p class="status">Importe um dataset antes de iniciar uma conversa.</p>'
                else:
                    sessions=self.api('GET','/sessions')
                    if isinstance(sessions,dict):sessions=sessions.get('sessions',[])
                    body+=self.form('/chat/create','','Nova conversa')
                    for session in sessions:
                        sid=session['id'];body+=f'<p><a href="/chat?id={escape(sid)}">Conversa {escape(sid[:12])}</a></p>'
                    sid=parse_qs(route.query).get('id',[''])[0]
                    if sid:
                        session=self.api('GET','/sessions/'+sid)
                        body+='<p>'+escape(str({k:session.get(k) for k in ('dataset_version','provider','model')}))+'</p>'
                        for message in session.get('messages',[]):body+='<div class="message"><div class="role">'+escape(message['role'])+'</div>'+escape(message.get('content',message.get('text','')))+'</div>'
                        body+=self.form('/chat/send',f'<input type="hidden" name="id" value="{escape(sid)}"><label>Mensagem<textarea name="message" maxlength="2000" required rows="3"></textarea></label>','Enviar mensagem')
                        body+=self.form('/chat/delete',f'<input type="hidden" name="id" value="{escape(sid)}">','Excluir conversa')
                body+='</section>'
            else:self._send(404,'Página não encontrada');return
            self._send(200,self.layout(body))
        except (ValueError,sqlite3.Error,OSError) as exc:
            self._send(503,'<section><h2>Ação indisponível</h2><p>'+escape(str(exc) if isinstance(exc,ValueError) else 'Armazenamento indisponível')+'</p><a href="/">Voltar</a></section>')

    def _post(self):
        if not self.path.startswith(('/dataset/','/ai/','/chat/')):
            if self.path!='/internal/reviews' and not self._guard():return
            if self.path=='/internal/reviews' and not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+self.server.review_token):
                self._send(401,'Não autorizado');return
            try:self.refresh_admin()
            except (ValueError,sqlite3.Error):pass
            return super().do_POST()
        if not self._guard():return
        if self.headers.get('Origin')!='http://'+self.headers.get('Host',''):
            self._send(403,'Origem inválida');return
        try:
            size=int(self.headers.get('Content-Length','-1'))
            if not 0<size<=7*1024*1024:self._send(413,'Carga excede 7 MiB');return
            raw=self.rfile.read(size);ctype=self.headers.get('Content-Type','');fields={};files=[]
            if ctype.startswith('multipart/form-data;'):
                msg=BytesParser(policy=default).parsebytes(('Content-Type: '+ctype+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                if not msg.is_multipart():raise ValueError('Formulário inválido')
                for part in msg.iter_parts():
                    name=part.get_param('name',header='content-disposition');filename=part.get_filename();data=part.get_payload(decode=True)
                    if filename is not None:files.append((filename,data))
                    elif name in fields:raise ValueError('Campo duplicado')
                    else:fields[name]=data.decode('utf-8')
            else:
                parsed=parse_qs(raw.decode(),keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in parsed.values()):raise ValueError('Campo duplicado')
                fields={k:v[0] for k,v in parsed.items()}
            if not hmac.compare_digest(fields.get('csrf',''),self.server.csrf):self._send(403,'CSRF inválido');return
            location='/'
            if self.path=='/dataset/import':self.server.datasets.import_files(files,official=fields.get('official')=='1',preserve_legacy=fields.get('preserve_legacy')=='1')
            elif self.path=='/dataset/activate':self.server.datasets.activate(int(fields['version']))
            elif self.path=='/ai/save':
                from decimal import Decimal
                provider=fields['provider'];self.server.provider_config.save(provider,fields['model'],fields['api_key'] or None,max_calls=int(fields['max_calls']),max_usd=Decimal(fields['max_usd']),input_price=Decimal(fields['input_price']),output_price=Decimal(fields['output_price']))
                location='/ai?provider='+provider
            elif self.path=='/ai/activate':
                self.server.provider_config.activate(fields['provider']);location='/ai'
            elif self.path=='/ai/test':
                self.api('POST','/provider-test',{'provider':fields['provider']});location='/ai'
            elif self.path=='/ai/remove':self.server.provider_config.remove_key(fields['provider']);location='/ai'
            elif self.path=='/chat/create':
                result=self.api('POST','/sessions',{});location='/chat?id='+result['id']
            elif self.path=='/chat/send':
                self.api('POST','/sessions/'+fields['id']+'/messages',{'message':fields['message']});location='/chat?id='+fields['id']
            elif self.path=='/chat/delete':self.api('DELETE','/sessions/'+fields['id']);location='/chat'
            else:self._send(404,'Não encontrado');return
            self._send(303,'',{'Location':location})
        except (ValueError,KeyError,TypeError,UnicodeError,StructuralError) as exc:
            self._send(400,'<section><h2>Não foi possível concluir</h2><p>'+escape(str(exc) if isinstance(exc,ValueError) else 'Entrada inválida')+'</p><a href="/">Voltar</a></section>')
        except (OSError,sqlite3.Error):self._send(503,'Armazenamento indisponível')


def make_product_server(host,port,root,password,*,secret_dir=None,catalog_path=None,admin_path=None,agent_url='http://agent:8081'):
    from .dataset_store import DatasetStore
    from .provider_config import ProviderConfig
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    catalog_path=Path(catalog_path or root/'catalog.sqlite');admin_path=Path(admin_path or root/'admin.sqlite')
    empty=Snapshot(MappingProxyType({key:() for key in SCHEMAS}),(),MappingProxyType({}))
    admin=CatalogAdmin(admin_path,catalog_path,empty)
    server=make_server(host,port,admin,password)
    server.operation_lock=threading.RLock()
    server.RequestHandlerClass=ProductHandler;server.secret_dir=Path(secret_dir or root/'secrets')
    server.secret_dir.mkdir(parents=True,exist_ok=True)
    server.review_token=bootstrap_secret(server.secret_dir/'service-token')
    server.datasets=DatasetStore(catalog_path)
    server.provider_config=ProviderConfig(root/'providers.sqlite',server.secret_dir/'master.key')
    server.agent_url=agent_url
    return server


def main():
    root=Path(os.environ.get('ARTEFACT_CONFIG_DIR','/config'))
    secret_dir=Path(os.environ.get('ARTEFACT_SECRET_DIR','/secrets'))
    password=os.environ.get('ARTEFACT_ADMIN_PASSWORD') or bootstrap_secret(secret_dir/'admin-password')
    bootstrap_secret(secret_dir/'service-token')
    server=make_product_server('0.0.0.0',8080,root,password,secret_dir=secret_dir,catalog_path=os.environ.get('ARTEFACT_CATALOG_DB','/catalog/catalog.sqlite'),admin_path=os.environ.get('ARTEFACT_ADMIN_DB','/admin/admin.sqlite'),agent_url=os.environ.get('ARTEFACT_AGENT_URL','http://agent:8081'))
    server.serve_forever()

if __name__=='__main__':main()
