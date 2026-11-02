"""Narrow authenticated writer for the local review service; no queue read access."""
import json
import re
import urllib.request
from urllib.parse import urlsplit

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReviewClient:
    def __init__(self,url,token,transport=None):
        parsed=urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in {'admin','localhost','127.0.0.1'} or parsed.port!=8080 or parsed.path!='/internal/reviews' or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Destino da fila inválido')
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token): raise ValueError('Token da fila inválido')
        self.url=url;self._token=token;self._transport=transport or urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect()).open
    def __call__(self,product_id,field,reason,catalog_version):
        if type(product_id) is not int or product_id<=0 or field not in {'brand','category_id','specs.keys','specs.strings'} or reason!='inconsistencia_catalogo' or type(catalog_version) is not int or catalog_version<0:
            raise ValueError('Solicitação técnica inválida')
        body=json.dumps({'product_id':product_id,'field':field,'reason':reason,'catalog_version':catalog_version}).encode()
        request=urllib.request.Request(self.url,data=body,headers={'Content-Type':'application/json','Authorization':'Bearer '+self._token},method='POST')
        try:
            with self._transport(request,timeout=5) as response:
                data=json.loads(response.read(1025))
            if not isinstance(data,dict) or set(data)!={'id'} or type(data['id']) is not int or data['id']<=0: raise ValueError()
            return data['id']
        except (OSError,ValueError,TypeError,KeyError): raise OSError('Fila técnica indisponível') from None


def configured_review_client(env):
    url=env.get('ARTEFACT_REVIEW_URL');token=env.get('ARTEFACT_REVIEW_TOKEN')
    if not url and not token: return None
    if not url or not token: raise ValueError('ARTEFACT_REVIEW_URL e ARTEFACT_REVIEW_TOKEN devem ser configurados juntos')
    return ReviewClient(url,token)
