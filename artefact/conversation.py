"""Bounded Responses tool cycle for the fictional store."""
from collections import OrderedDict
import json
import re
from .agent import Agent, FALLBACK
from .state import BudgetExhausted

_GREETING=re.compile(r'^(oi|ol[aá]|bom dia|boa tarde|boa noite)[!. ]*$',re.I)

class ConversationAgent:
    def __init__(self,snapshot,provider):
        self.snapshot=snapshot;self.provider=provider;self.history=OrderedDict()

    def answer(self,text,chat_id=None):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            return FALLBACK
        if _GREETING.fullmatch(text.strip()):
            return 'Olá! Como posso ajudar com instrumentos, pedidos ou políticas da loja?'
        previous=self.history.get(chat_id,[]) if chat_id is not None else []
        messages=list(previous)+[{'role':'user','content':text}]
        user_evidence=' '.join(str(m.get('content','')) for m in messages if m.get('role')=='user')
        try:
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
                evidence=Agent(self.snapshot,SinglePlan()).answer(user_evidence)
                if evidence==FALLBACK: return FALLBACK
                results.append({'type':'function_call_output','call_id':call_id,'output':evidence})
            if all(all(item.get('resultado') in (None, [], {}) for item in json.loads(result['output'])) for result in results):
                return 'Não encontrei informações suficientes para essa consulta.'
            followup=messages+output+results
            final=self.provider.request(followup,final=True)
            output2=final.get('output',[])
            if any(x.get('type')=='function_call' for x in output2 if isinstance(x,dict)): return FALLBACK
            segments=[part.get('text','') for item in output2 if isinstance(item,dict) and item.get('type')=='message' for part in item.get('content',[]) if isinstance(part,dict) and part.get('type')=='output_text']
            answer=' '.join(segments).strip()
            if not answer or len(answer)>1000: return FALLBACK
            # Numeric facts must occur in returned evidence; this is a guard, not proof of grounding.
            numbers=re.findall(r'\d+(?:[.,]\d+)?',answer)
            evidence_text=' '.join(x['output'] for x in results)
            if any(number not in evidence_text for number in numbers): return FALLBACK
            if any(c.get('name')=='orders' for c in calls):
                if re.search(r'pagamento.{0,30}(confirmad|aprovad|recebid|efetuad|pago)',answer,re.I):
                    return FALLBACK
            if chat_id is not None:
                self.history[chat_id]=(messages+[{'role':'assistant','content':answer}])[-6:]
                self.history.move_to_end(chat_id)
                if len(self.history)>100: self.history.popitem(last=False)
            return answer
        except (ValueError,TypeError,KeyError,AttributeError,TimeoutError,OSError,BudgetExhausted):
            return FALLBACK
