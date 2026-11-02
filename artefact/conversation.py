"""Bounded Responses tool cycle for the fictional store."""
from collections import OrderedDict
from decimal import InvalidOperation
import json
import re
import sqlite3
from .agent import Agent, FALLBACK
from .state import BudgetExhausted
from .rendering import render_results

_GREETING=re.compile(r'^(oi|ol[aá]|bom dia|boa tarde|boa noite)[!. ]*$',re.I)

class ConversationAgent:
    def __init__(self,snapshot,provider,*,snapshot_loader=None,review_submit=None):
        self.snapshot=snapshot;self.provider=provider;self.snapshot_loader=snapshot_loader;self.review_submit=review_submit;self.history=OrderedDict()

    def answer(self,text,chat_id=None):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            return FALLBACK
        if text.strip() == '/start' or _GREETING.fullmatch(text.strip()):
            return 'Olá! Como posso ajudar com instrumentos, pedidos ou políticas da loja?'
        if re.fullmatch(r'(o que voc[eê] faz|quem [eé] voc[eê]|como voc[eê] pode ajudar)[?! .]*',text.strip(),re.I):
            return 'Posso ajudar com produtos, pedidos fictícios e políticas da loja.'
        previous=self.history.get(chat_id,[]) if chat_id is not None else []
        messages=list(previous)+[{'role':'user','content':text}]
        user_evidence=' '.join(str(m.get('content','')) for m in messages if m.get('role')=='user')
        try:
            snapshot=self.snapshot_loader() if self.snapshot_loader else self.snapshot
            response=self.provider.request(messages)
            output=response.get('output')
            if not isinstance(output,list): return FALLBACK
            calls=[x for x in output if isinstance(x,dict) and x.get('type')=='function_call']
            if not calls:
                return 'Posso ajudar com produtos, pedidos e políticas da loja. O que você gostaria de saber?'
            if len(calls)>3: return FALLBACK
            seen=set();results=[]
            for call in calls:
                call_id=call.get('call_id');name=call.get('name')
                if not isinstance(call_id,str) or not call_id: return FALLBACK
                args=json.loads(call.get('arguments',''))
                if not isinstance(args,dict): return FALLBACK
                args={k:v for k,v in args.items() if v is not None}
                signature=json.dumps([name,args],sort_keys=True,ensure_ascii=True)
                if signature in seen: return FALLBACK
                seen.add(signature)
                class SinglePlan:
                    def plan(self,_): return [(name,args)]
                evidence=Agent(snapshot,SinglePlan(),self.review_submit).answer(text if name=='request_technical_review' else user_evidence)
                if evidence==FALLBACK: return FALLBACK
                results.append({'type':'function_call_output','call_id':call_id,'output':evidence})
            if all(all(item.get('resultado') in (None, [], {}) for item in json.loads(result['output'])) for result in results):
                return 'Não encontrei informações suficientes para essa consulta.'
            # Never publish model-written commercial assertions: bind each value to its typed field.
            evidence=[item for result in results for item in json.loads(result['output'])]
            answer=render_results(evidence)
            if len(answer)>3500:
                return 'A resposta é longa demais. Por favor, refine sua consulta.'
            if chat_id is not None:
                self.history[chat_id]=(messages+[{'role':'assistant','content':answer}])[-6:]
                self.history.move_to_end(chat_id)
                if len(self.history)>100: self.history.popitem(last=False)
            return answer
        except (ValueError,TypeError,KeyError,AttributeError,TimeoutError,OSError,InvalidOperation,BudgetExhausted,sqlite3.Error):
            return FALLBACK
