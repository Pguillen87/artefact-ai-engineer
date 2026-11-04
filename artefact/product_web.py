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
import time
from http.cookies import SimpleCookie
from .web_ui import CSS, JS
from .web_security import same_origin
from .admin_web import Handler, make_server
from .admin import CatalogAdmin
from .data import Snapshot, SCHEMAS, StructuralError

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
        port = os.environ.get('ARTEFACT_PUBLIC_PORT', str(self.server.server_port))
        return self.headers.get('Host', '') in {f'localhost:{port}', f'127.0.0.1:{port}'}

    def _session_token(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            return cookie['artefact_session'].value if 'artefact_session' in cookie else ''
        except Exception:
            return ''

    def _authenticated(self):
        now = time.time()
        with self.server.auth_lock:
            expired = [key for key, until in self.server.login_sessions.items() if until <= now]
            for key in expired:
                del self.server.login_sessions[key]
            return self.server.login_sessions.get(self._session_token(), 0) > now

    def _guard(self):
        if not self._host():
            self._send(400, 'Endereço inválido.'); return False
        if not self._authenticated():
            self._send(303, '', {'Location': '/login'}); return False
        return True

    def _send(self, status, body='', extra=None):
        if body and not body.startswith('<!doctype') and not body.startswith('{') and not (extra or {}).get('Content-Type', '').startswith(('text/javascript', 'application/json')):
            body = self.layout(body)
        if body.startswith('{'):
            extra = {**(extra or {}), 'Content-Type': 'application/json; charset=utf-8'}
        raw = body.encode('utf-8')
        self.send_response(status)
        headers = {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(raw)),
                   'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                   'Referrer-Policy': 'same-origin'}
        headers.update(extra or {})
        for key, value in headers.items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(raw)

    def layout(self, body, *, login=False):
        logout = '' if login else self.form('/logout', '', 'Sair')
        return ('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Empório da Música · Atendimento</title><style>' + CSS + '</style></head><body>'
                '<header><div><small>ARTEFACT / EMPÓRIO DA MÚSICA</small><h1>Central de atendimento</h1>'
                '<p>Uma boa conversa começa com a informação certa.</p></div>' + logout + '</header><main>'
                + body + '</main><script src="/app.js" defer></script></body></html>')

    def form(self, action, content, label, *, attrs=''):
        return (f'<form method="post" action="{action}" {attrs}><input type="hidden" name="csrf" '
                f'value="{self.server.csrf}">' + content + f'<button>{label}</button></form>')

    def _login_page(self, error=''):
        content = ('<section class="card login"><span class="brand">EMPÓRIO DA MÚSICA</span>'
                   '<h2>Bem-vindo de volta</h2><p class="muted">Entre para preparar os dados e demonstrar o atendimento.</p>')
        if error: content += '<p class="notice error" role="alert">' + escape(error) + '</p>'
        content += self.form('/login', '<label for="username">Login</label><input id="username" name="username" autocomplete="username" required autofocus>'
                             '<label for="password">Senha</label><input id="password" name="password" type="password" autocomplete="current-password" required>', 'Entrar')
        return self.layout(content + '</section>', login=True)

    @staticmethod
    def friendly(error):
        code = str(error)
        return {'provider_missing': 'Configure e teste a IA antes de iniciar uma conversa.',
                'dataset_missing': 'Importe os seis CSVs antes de conversar.',
                'provider_unavailable': 'A IA desta conversa não está disponível. Confira a configuração.',
                'provider_test_failed': 'Não foi possível conectar à IA. Confira a chave e o modelo e tente novamente.',
                'budget_exhausted': 'O limite de uso da IA foi atingido. Confira a configuração avançada.',
                'session_not_found': 'Esta conversa não está mais disponível. Inicie uma nova conversa.',
                'dataset_unavailable': 'Os dados desta conversa não estão disponíveis.',
                'agent_unavailable': 'O atendimento está temporariamente indisponível. Tente novamente.'}.get(code, 'Não foi possível concluir a ação. Confira os dados e tente novamente.')

    def api(self,method,path,payload=None):
        token=bootstrap_secret(self.server.secret_dir/'service-token')
        request=Request(self.server.agent_url+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
        try:
            with urlopen(request,timeout=5 if method == 'GET' else 90) as response:return json.load(response)
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

    def _settings(self, query):
        active = self.server.datasets.active_version()
        state = self.server.provider_config.status()
        models = self.server.provider_config.models()
        selected = query.get('provider', [state['active'] or 'google'])[0]
        if selected not in models: selected = 'google'
        saved = state['providers'].get(selected, {})
        body = '<aside class="settings" aria-label="Configurações do atendimento">'
        body += '<details class="card" id="import" open><summary>Importar arquivos<small>Os dados que orientam o atendimento</small></summary>'
        body += '<p class="hint">' + ('Base pronta para atendimento.' if active else 'Sem dataset ativo. Selecione os seis CSVs para começar.') + '</p>'
        body += self.form('/dataset/import', '<label for="datasets">Arquivos CSV</label><input id="datasets" type="file" name="datasets" accept=".csv" multiple required>'
                          '<ul id="file-list" class="file-list"></ul><p class="hint">Categories, customers, order_items, orders, products e promotions.</p>',
                          'Importar CSVs', attrs='enctype="multipart/form-data"') + '</details>'
        admin = self.server.admin
        body += '<details class="card" id="seller" open><summary>Contato do vendedor<small>Para continuar a conversa com a loja</small></summary>'
        body += self.form('/seller-contact', '<label for="seller-name">Nome do vendedor</label><input id="seller-name" name="name" maxlength="100" value="' + escape(admin.seller_name(), quote=True) + '">'
                          '<label for="seller-phone">Telefone</label><input id="seller-phone" type="tel" name="contact" maxlength="16" required value="' + escape(admin.seller_contact(), quote=True) + '">', 'Salvar') + '</details>'
        providers = ''.join(f'<option value="{name}"' + (' selected' if name == selected else '') + f'>{label}</option>' for name, label in [('google', 'Google Gemini'), ('openai', 'OpenAI')])
        options = ''.join('<option value="' + escape(model, quote=True) + '"' + (' selected' if model == saved.get('model') else '') + '>' + escape(model) + '</option>' for model in models[selected])
        fields = '<label for="provider">Provedor</label><select id="provider" name="provider">' + providers + '</select><label for="model">Modelo</label><select id="model" name="model">' + options + '</select>'
        fields += '<label for="api-key">Chave API</label><input id="api-key" name="api_key" type="password" autocomplete="new-password" placeholder="' + ('Chave salva — deixe vazio para manter' if saved.get('has_key') else 'Informe sua chave API') + '">'
        body += '<details class="card" id="ai" open><summary>Configuração de IA<small>Escolha quem responde às mensagens</small></summary>'
        body += self.form('/ai/save', fields, 'Salvar')
        body += '<p class="hint">' + ('IA pronta para atendimento.' if state['active'] == selected and saved.get('verified') else 'Após salvar, abra Avançado para testar e ativar.') + '</p>'
        advanced = '<input type="hidden" name="provider" value="' + selected + '"><div class="advanced-grid">'
        for name, title, default in [('max_calls', 'Limite de chamadas', '100'), ('max_usd', 'Orçamento (USD)', '1'), ('input_price', 'USD / milhão de tokens de entrada', '1'), ('output_price', 'USD / milhão de tokens de saída', '5')]:
            advanced += f'<div><label for="{name}">{title}</label><input id="{name}" name="{name}" type="number" min="' + ('1' if name == 'max_calls' else '0.000001') + '" step="' + ('1' if name == 'max_calls' else 'any') + '" required value="' + escape(str(saved.get(name, default)), quote=True) + '"></div>'
        body += '<details class="advanced"><summary>Avançado</summary>' + self.form('/ai/limits', advanced + '</div>', 'Salvar limites')
        body += '<p class="hint">Os limites permanecem ativos. O custo é estimado pelos preços informados.</p>'
        body += self.form('/ai/test', '<input type="hidden" name="provider" value="' + selected + '">', 'Testar e ativar')
        body += '<p class="hint">O teste faz uma pequena chamada real à API.</p></details></details></aside>'
        return body

    def _chat(self, query):
        state = self.server.provider_config.status()
        provider = state['providers'].get(state['active'], {})
        ready = self.server.datasets.active_version() is not None and provider.get('has_key') and provider.get('verified')
        body = '<section class="card chat" id="chat"><div class="chat-top"><div><h2>Converse com o Empório</h2><p class="muted">Produtos, pedidos e dúvidas sobre a loja.</p></div>'
        if ready: body += self.form('/chat/create', '', 'Nova conversa')
        body += '</div>'
        sessions = []; session = None; error = ''; missing_session = False
        try:
            result = self.api('GET', '/sessions')
            sessions = result.get('sessions', []) if isinstance(result, dict) else result
            sid = query.get('id', [''])[0]
            if sid: session = self.api('GET', '/sessions/' + sid)
        except ValueError as exc:
            error = self.friendly(exc)
            missing_session = str(exc) == 'session_not_found'
        if sessions:
            body += '<details class="history"><summary>Conversas anteriores</summary><ul>'
            for index, item in enumerate(sessions, 1):
                body += '<li><a href="/?id=' + escape(str(item['id']), quote=True) + '#chat">Conversa ' + str(index) + '</a></li>'
            body += '</ul></details>'
        body += '<div class="transcript" id="transcript" role="log" aria-label="Mensagens">'
        messages = session.get('messages', []) if session else []
        for message in messages:
            user = message['role'] == 'user'
            body += '<article class="bubble ' + ('user' if user else 'assistant') + '"><div class="role">' + ('Você' if user else 'Empório da Música') + '</div><div class="text">' + escape(message.get('content', message.get('text', ''))) + '</div></article>'
        if not messages:
            body += '<div class="empty"><span class="mark" aria-hidden="true">♫</span><h3>Como podemos ajudar?</h3><p class="muted">Pergunte sobre um instrumento, acompanhe um pedido ou tire uma dúvida sobre a loja.</p></div>'
        body += '</div>'
        if error: body += '<p class="notice error chat-error" role="alert">' + escape(error) + '</p>'
        if ready:
            sid = str(session['id']) if session else ('' if missing_session else query.get('id', [''])[0])
            body += '<div class="composer">' + self.form('/chat/send', '<input type="hidden" name="id" value="' + escape(sid, quote=True) + '"><label for="message">Sua mensagem</label><textarea id="message" name="message" rows="2" maxlength="2000" required placeholder="Escreva sua mensagem…">' + escape(query.get('draft', [''])[0]) + '</textarea>', 'Enviar')
            body += '<p class="hint">Enter para enviar · Shift+Enter para uma nova linha</p></div>'
        else:
            body += '<div class="composer"><p class="hint">' + ('Importe os seis CSVs para começar.' if self.server.datasets.active_version() is None else 'Salve a configuração de IA e use Testar e ativar em Avançado.') + '</p></div>'
        body += '<p id="operation-status" role="status" aria-live="polite"></p></section>'
        return body

    def _get(self):
        if not self._host(): self._send(400, 'Endereço inválido.'); return
        route = urlsplit(self.path)
        if route.path == '/app.js': self._send(200, JS, {'Content-Type': 'text/javascript; charset=utf-8'}); return
        if route.path == '/login':
            if self._authenticated(): self._send(303, '', {'Location': '/'})
            else: self._send(200, self._login_page())
            return
        if not self._guard(): return
        query = parse_qs(route.query)
        if route.path in {'/catalog', '/batch', '/ai', '/chat'}:
            section = {'/catalog': 'import', '/batch': 'import', '/ai': 'ai', '/chat': 'chat'}[route.path]
            self._send(303, '', {'Location': '/' + ('?' + urlencode({k:v[0] for k,v in query.items() if k in ('provider','id')}) if query else '') + '#' + section}); return
        if route.path != '/': self._send(404, 'Página não encontrada.'); return
        try:
            body = ''
            notices = {'imported': 'Arquivos importados. A base está pronta.', 'saved': 'Configuração salva.', 'activated': 'IA testada e ativada.'}
            if query.get('notice', [''])[0] in notices:
                body += '<p class="notice" role="status">' + notices[query['notice'][0]] + '</p>'
            body += '<div class="workspace">' + self._settings(query) + self._chat(query) + '</div>'
            self._send(200, self.layout(body))
        except (ValueError, sqlite3.Error, OSError): self._send(503, '<p class="notice error">Não foi possível carregar o atendimento. Tente novamente.</p>')

    def _read_form(self):
        size = int(self.headers.get('Content-Length', '-1'))
        if not 0 < size <= 31 * 1024 * 1024: raise ValueError('Envio vazio ou maior que 31 MiB.')
        raw = self.rfile.read(size); ctype = self.headers.get('Content-Type', ''); fields = {}; files = []
        if ctype.startswith('multipart/form-data;'):
            msg = BytesParser(policy=default).parsebytes(('Content-Type: ' + ctype + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
            if not msg.is_multipart(): raise ValueError('Formulário inválido.')
            for part in msg.iter_parts():
                name = part.get_param('name', header='content-disposition'); filename = part.get_filename(); data = part.get_payload(decode=True)
                if filename is not None:
                    if name != 'datasets': raise ValueError('Campo de arquivo inválido.')
                    files.append((filename, data))
                elif name in fields: raise ValueError('Campo duplicado.')
                else: fields[name] = data.decode('utf-8')
        elif ctype.split(';')[0] == 'application/x-www-form-urlencoded':
            parsed = parse_qs(raw.decode(), keep_blank_values=True, strict_parsing=True)
            if any(len(v) != 1 for v in parsed.values()): raise ValueError('Campo duplicado.')
            fields = {k:v[0] for k,v in parsed.items()}
        else: raise ValueError('Formulário inválido.')
        return fields, files

    def _post(self):
        if self.path == '/internal/reviews':
            return self._review_request()
        if not self._host(): self._send(400, 'Endereço inválido.'); return
        if self.path != '/login' and not self._guard(): return
        if not same_origin(self.headers): self._send(403, 'Origem inválida. Reabra o painel e tente novamente.'); return
        fields = {}; sid = ''
        try:
            fields, files = self._read_form()
            if not hmac.compare_digest(fields.get('csrf', ''), self.server.csrf): self._send(403, 'Sessão inválida. Reabra o painel.'); return
            if self.path == '/login':
                username = fields.get('username', '')
                password = fields.get('password', '')
                if not (hmac.compare_digest(username.encode(), b'admin') and hmac.compare_digest(password.encode(), self.server.password.encode())):
                    self._send(401, self._login_page('Login ou senha incorretos.')); return
                token = secrets.token_urlsafe(32)
                with self.server.auth_lock:
                    self.server.login_sessions.pop(self._session_token(), None)
                    self.server.login_sessions[token] = time.time() + 8 * 60 * 60
                self._send(303, '', {'Location': '/', 'Set-Cookie': 'artefact_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800'}); return
            if self.path == '/logout':
                with self.server.auth_lock: self.server.login_sessions.pop(self._session_token(), None)
                self._send(303, '', {'Location': '/login', 'Set-Cookie': 'artefact_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'}); return
            location = '/?notice=saved'
            if self.path == '/dataset/import':
                self.server.datasets.import_files(files, preserve_legacy=self.server.datasets.active_version() is None)
                self.refresh_admin(); location = '/?notice=imported#import'
            elif self.path == '/dataset/activate': self.server.datasets.activate(int(fields['version']))
            elif self.path == '/seller-contact':
                self.refresh_admin(); self.server.admin.set_seller(fields.get('name', self.server.admin.seller_name()), fields['contact'])
            elif self.path == '/ai/save':
                provider = fields['provider']
                self.server.provider_config.save_partial(provider, fields['model'], fields.get('api_key') or None)
                location += '&provider=' + provider + '#ai'
            elif self.path == '/ai/limits':
                provider = fields['provider']; saved = self.server.provider_config.status()['providers'].get(provider)
                if not saved: raise ValueError('Salve primeiro o provedor, modelo e chave API.')
                self.server.provider_config.save_partial(provider, saved['model'], **{k:fields[k] for k in ('max_calls','max_usd','input_price','output_price')})
                location += '&provider=' + provider + '#ai'
            elif self.path in ('/ai/test', '/ai/activate'):
                provider = fields['provider']
                if self.path == '/ai/test': self.api('POST', '/provider-test', {'provider': provider})
                self.server.provider_config.activate(provider); location = '/?notice=activated&provider=' + provider + '#ai'
            elif self.path == '/ai/remove': self.server.provider_config.remove_key(fields['provider'])
            elif self.path == '/chat/create':
                result = self.api('POST', '/sessions', {}); location = '/?id=' + result['id'] + '#chat'
            elif self.path == '/chat/send':
                sid = fields.get('id')
                if not sid: sid = self.api('POST', '/sessions', {})['id']
                self.api('POST', '/sessions/' + sid + '/messages', {'message': fields['message']})
                location = '/?id=' + sid + '#chat'
            elif self.path == '/chat/delete': self.api('DELETE', '/sessions/' + fields['id']); location = '/#chat'
            else: self._send(404, 'Ação não encontrada.'); return
            self._send(303, '', {'Location': location})
        except StructuralError as exc:
            description = str(exc)
            for original, translated in [('invalid header', 'cabeçalho inválido'), ('malformed row', 'linha malformada'), ('invalid typed field or duplicate key', 'campo inválido ou identificador duplicado'), ('invalid encoding or CSV', 'codificação ou CSV inválido'), ('missing reference to', 'referência ausente em'), ('source hash mismatch', 'conteúdo diferente do arquivo oficial')]:
                description = description.replace(original, translated)
            self._send(400, '<p class="notice error" role="alert">' + escape(description) + '</p><a href="/">Voltar ao atendimento</a>')
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            error = '<p class="notice error" role="alert">' + escape(self.friendly(exc)) + '</p>'
            if self.path == '/chat/send':
                query = {'id': [sid or fields.get('id', '')], 'draft': [fields.get('message', '')]}
                try:
                    self._send(400, self.layout(error + '<div class="workspace">' + self._settings(query) + self._chat(query) + '</div>'))
                except (ValueError, OSError, sqlite3.Error):
                    self._send(503, error + '<a href="/">Voltar ao atendimento</a>')
            else:
                self._send(400, error + '<a href="/">Voltar ao atendimento</a>')
        except (OSError, sqlite3.Error): self._send(503, 'Armazenamento indisponível. Tente novamente.')


def make_product_server(host,port,root,password,*,secret_dir=None,catalog_path=None,admin_path=None,agent_url='http://agent:8081'):
    from .dataset_store import DatasetStore
    from .provider_config import ProviderConfig
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    catalog_path=Path(catalog_path or root/'catalog.sqlite');admin_path=Path(admin_path or root/'admin.sqlite')
    empty=Snapshot(MappingProxyType({key:() for key in SCHEMAS}),(),MappingProxyType({}))
    admin=CatalogAdmin(admin_path,catalog_path,empty)
    server=make_server(host,port,admin,password)
    server.operation_lock=threading.RLock()
    server.login_sessions={}
    server.auth_lock=threading.Lock()
    server.RequestHandlerClass=ProductHandler;server.secret_dir=Path(secret_dir or root/'secrets')
    server.secret_dir.mkdir(parents=True,exist_ok=True)
    server.review_token=bootstrap_secret(server.secret_dir/'service-token')
    server.datasets=DatasetStore(catalog_path)
    server.provider_config=ProviderConfig(root/'providers.sqlite',server.secret_dir/'master.key')
    server.agent_url=agent_url
    if server.datasets.active_version() is not None:
        server.admin=CatalogAdmin(admin_path,catalog_path,server.datasets.load(),dataset_store=server.datasets)
    return server


def main():
    root=Path(os.environ.get('ARTEFACT_CONFIG_DIR','/config'))
    secret_dir=Path(os.environ.get('ARTEFACT_SECRET_DIR','/secrets'))
    password=os.environ.get('ARTEFACT_ADMIN_PASSWORD') or 'admin'
    bootstrap_secret(secret_dir/'service-token')
    server=make_product_server('0.0.0.0',8080,root,password,secret_dir=secret_dir,catalog_path=os.environ.get('ARTEFACT_CATALOG_DB','/catalog/catalog.sqlite'),admin_path=os.environ.get('ARTEFACT_ADMIN_DB','/admin/admin.sqlite'),agent_url=os.environ.get('ARTEFACT_AGENT_URL','http://agent:8081'))
    server.serve_forever()

if __name__=='__main__':main()
