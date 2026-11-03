"""Render commercial facts only from explicit tool result fields."""
from decimal import Decimal


def _money(value):
    amount=Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01')):
        raise ValueError('invalid monetary value')
    return 'R$ ' + f'{amount:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def _render_price_quote(name, quote, available):
    if not isinstance(quote,dict): raise ValueError('invalid price quote')
    status=quote.get('status')
    if status=='missing_price':
        return f'{name}: o preço não está disponível no cadastro; não consigo cotar.'
    price=_money(quote.get('price_brl'))
    if status=='unavailable' or available is False:
        return f'{name}: preço cadastrado {price}, mas indisponível para venda no snapshot.'
    if status=='active_in_snapshot':
        discount=Decimal(str(quote.get('discount_percent')))
        if not discount.is_finite() or not 0 <= discount <= 100:
            raise ValueError('invalid promotion discount')
        promotional=_money(quote.get('promotional_price_brl'))
        return (f'{name}: preço cadastrado {price}; promoção de {format(discount, "f")}% registrada no snapshot, '
                f'preço promocional calculado {promotional}. O desconto Pix não acumula com a promoção; '
                f'a vigência atual não foi confirmada.')
    if status=='ambiguous':
        return (f'{name}: preço cadastrado {price}; há promoções conflitantes no snapshot, '
                'então não consigo confirmar preço promocional nem aplicar desconto Pix.')
    if status=='none_available':
        pix=_money(quote.get('pix_price_brl'))
        return (f'{name}: preço cadastrado {price}; sem promoção identificada no snapshot, '
                f'o preço Pix com 5% de desconto é {pix}. A vigência atual não foi confirmada.')
    raise ValueError('unknown price quote status')


def _render_parts(parts, count, total, minimum):
    if not isinstance(parts,(list,tuple)) or len(parts)!=count:
        raise ValueError('invalid installment parts')
    values=tuple(Decimal(str(value)) for value in parts)
    if any(not value.is_finite() or value < 0 or value != value.quantize(Decimal('0.01')) for value in values):
        raise ValueError('invalid installment amount')
    if sum(values)!=total:
        raise ValueError('installment amounts do not sum to total')
    if not isinstance(minimum,Decimal) or not minimum.is_finite() or minimum<=0:
        raise ValueError('invalid installment minimum')
    return values


def _parts_label(parts):
    if not parts: return ''
    if all(value==parts[0] for value in parts):
        return f'{len(parts)}x {_money(parts[0])}'
    return f"{len(parts)}x (" + ' + '.join(_money(value) for value in parts) + ')'


def _render_installment_quote(quote):
    if not isinstance(quote,dict): raise ValueError('invalid installment quote')
    status=quote.get('status')
    if status=='clarification':
        return 'Para calcular, informe um único valor em reais e a quantidade de parcelas desejada (de 1 a 12).'
    if status=='unavailable':
        return 'Não consigo cotar parcelas enquanto o produto estiver indisponível para venda.'
    if status=='ambiguous':
        return 'Não consigo cotar parcelas enquanto houver promoções conflitantes no cadastro.'
    if status=='missing_price':
        return 'Para cotar parcelas, primeiro é necessário confirmar o preço do produto.'
    if status not in {'eligible','below_minimum'}:
        raise ValueError('unknown installment quote status')
    amount=Decimal(str(quote.get('amount_brl')))
    count=quote.get('count')
    if not amount.is_finite() or amount < 0 or amount!=amount.quantize(Decimal('0.01')) or type(count) is not int or not 1<=count<=12:
        raise ValueError('invalid installment quote values')
    minimum=Decimal(str(quote.get('minimum_per_installment')))
    parts=_render_parts(quote.get('parts'),count,amount,minimum)
    if status=='eligible':
        if any(part<minimum for part in parts): raise ValueError('eligible installment is below its floor')
        return f'{_money(amount)}: parcelamento permitido em {_parts_label(parts)} sem juros.'
    if all(part>=minimum for part in parts): raise ValueError('rejected installment meets its floor')
    answer=(f'{_money(amount)}: não é permitido em {count}x; as parcelas {_parts_label(parts)} '
            f'não atingem o mínimo de {_money(minimum)} por parcela.')
    alternative=quote.get('alternative')
    if alternative is not None:
        if not isinstance(alternative,dict): raise ValueError('invalid installment alternative')
        alt_count=alternative.get('count')
        if type(alt_count) is not int or not 1<=alt_count<=12: raise ValueError('invalid alternative count')
        alt_floor=Decimal('50') if alt_count<=3 else Decimal('80') if alt_count<=6 else Decimal('100')
        alt_parts=_render_parts(alternative.get('parts'),alt_count,amount,alt_floor)
        if any(part<alt_floor for part in alt_parts): raise ValueError('invalid alternative installment floor')
        answer+=f' Alternativa válida: {_parts_label(alt_parts)} sem juros.'
    return answer


def render_results(results):
    lines=[]
    status_labels={'pending':'pendente','confirmed':'confirmado','shipped':'enviado','delivered':'entregue','cancelled':'cancelado'}
    for entry in results:
        name=entry['consulta'];data=entry['resultado']
        if name in ('search_products','recommend_products','cheapest_product'):
            if not isinstance(data,list): raise ValueError('invalid products')
            if name == 'recommend_products' and data: lines.append('Alternativas disponíveis:')
            for item in data:
                if not isinstance(item,dict) or type(item.get('stock_quantity')) is not int or type(item.get('available')) is not bool:
                    raise ValueError('invalid product')
                availability='disponível' if item['available'] else 'indisponível'
                if item.get('price_withheld'):
                    lines.append(f"{item['name']}: {availability} no cadastro; preço não exibido conforme sua restrição.")
                elif 'price_quote' in item:
                    lines.append(_render_price_quote(item['name'],item['price_quote'],item['available']))
                    lines.append(f"Disponibilidade: {availability} no cadastro, estoque registrado de {item['stock_quantity']} unidade(s).")
                elif item.get('price_brl') is not None:
                    lines.append(f"{item['name']}: {_money(item['price_brl'])}; {availability} no cadastro, estoque registrado de {item['stock_quantity']} unidade(s).")
                else:
                    lines.append(f"{item['name']}: o preço não está disponível no cadastro; não consigo cotar.")
                if item.get('installment_quote'):
                    lines.append(_render_installment_quote(item['installment_quote']))
                if item.get('review_fields'):
                    lines.append('Há detalhes técnicos que precisam de confirmação com um vendedor.')
            if data: lines.append('Preço e estoque são do cadastro; a data de atualização não está informada.')
        elif name=='request_technical_review':
            if not isinstance(data,dict): raise ValueError('invalid review')
            if data.get('clarification') in ('intent','product','field'):
                lines.append({'intent':'Você deseja registrar uma revisão técnica? Confirme o pedido e identifique o produto.', 'product':'Qual produto você deseja revisar? Informe o nome completo ou o ID do produto.', 'field':'Qual detalhe técnico você deseja revisar: marca, categoria, teclas ou cordas?'}[data['clarification']])
            elif data.get('registered') is True and type(data.get('case_id')) is int:
                lines.append('Sua solicitação de revisão técnica foi registrada para análise.')
            else: raise ValueError('invalid review')
        elif name=='orders':
            if not isinstance(data,list): raise ValueError('invalid orders')
            for order in data:
                status=status_labels.get(order.get('status'))
                if status is None: raise ValueError('unknown order status')
                lines.append(f"Pedido {order['order_id']}: o status cadastrado é {status}; total registrado {_money(order['total_brl'])}.")
                if order.get('items'):
                    lines.append('Itens: '+', '.join(f"{item['quantity']} × {item['name']}" for item in order['items'])+'.')
                comparison=order.get('catalog_comparison')
                if comparison is not None:
                    if not isinstance(comparison,dict) or comparison.get('basis')!='catalog_list_price':
                        raise ValueError('invalid catalog comparison')
                    catalog_total=comparison.get('catalog_total_brl')
                    difference=comparison.get('difference_brl')
                    if catalog_total is None or difference is None:
                        lines.append('Não foi possível comparar todos os itens com os preços-base atuais do catálogo.')
                    else:
                        delta=Decimal(str(difference))
                        if not delta.is_finite() or delta!=delta.quantize(Decimal('0.01')):
                            raise ValueError('invalid catalog delta')
                        direction='a mais que' if delta>0 else 'a menos que' if delta<0 else 'igual a'
                        lines.append(f"Soma separada pelos preços-base cadastrados hoje: {_money(catalog_total)}; "
                                     f"diferença de {_money(abs(delta))} {direction} o total histórico.")
                        lines.append('Essa comparação não altera o total histórico. O pedido não registra a causa; '
                                     'desconto ou preço anterior diferente são hipóteses, não fatos confirmados.')
                if order.get('payment_method'):
                    lines.append(f"Método de pagamento registrado: {order['payment_method']}; confirmação do pagamento não informada.")
                if order.get('estimated_delivery'):
                    lines.append(f"Previsão de entrega cadastrada: {order['estimated_delivery']}.")
                if order.get('tracking_code'):
                    lines.append(f"Código de rastreamento cadastrado: {order['tracking_code']}.")
                if order.get('delivered_at'):
                    lines.append(f"Data efetiva de entrega registrada: {order['delivered_at']}.")
                else:
                    lines.append('Data efetiva de entrega não informada; o status cadastrado não comprova o recebimento físico.')
        elif name=='policy':
            if data is not None: lines.append(data['summary'])
        elif name=='price_mismatch':
            if not isinstance(data,dict) or not isinstance(data.get('product_name'),str):
                raise ValueError('invalid price mismatch')
            reported=data.get('reported_amount_brl')
            catalog=data.get('catalog_amount_brl')
            if reported is None or catalog is None:
                lines.append(f"O valor informado para {data['product_name']} diverge do preço disponível no snapshot; "
                             'não consigo confirmar um preço efetivo válido para simulação. Consulte a loja.')
            else:
                subject = 'O total das parcelas informadas' if data.get('reported_per_installment') else 'O valor informado'
                statement=(f"{subject} {_money(reported)} diverge do preço efetivo do catálogo de {_money(catalog)}.")
                if data.get('catalog_quote_executed') is True:
                    lines.append(statement+' A simulação desta resposta usa somente o preço do catálogo.')
                elif data.get('catalog_quote_authorized') is True:
                    lines.append(statement+' A base do catálogo foi identificada, mas não executei uma simulação nesta resposta.')
                else:
                    lines.append(statement+' Esclareça a base desejada. Quer que eu simule com o preço efetivo do catálogo?')
        elif name=='commercial_clarification':
            if data.get('reason') == 'budget_unresolved':
                lines.append('Qual é o valor do orçamento exclusivo para os produtos? Não executei a busca sem um teto definido.')
            elif data.get('reason') == 'budget_includes_shipping':
                lines.append('O orçamento inclui frete. Confirme o limite exclusivo para produtos; não subtraí um frete sem verificação e não executei a busca.')
            else:
                lines.append('Não realizei a cotação: esclareça o produto, a base do valor ou a restrição indicada antes de simular parcelas.')
        elif name=='execution_clarification':
            lines.append('Nesta consulta, não consegui validar os argumentos da cotação e não a executei; esclareça essa parte e informe um único valor em reais e a quantidade de parcelas desejada.')
        elif name=='clarification':
            if not isinstance(data,dict):
                raise ValueError('invalid clarification')
            operation=data.get('tool')
            if operation=='search_products':
                lines.append('Não realizei a busca de produtos porque o filtro não correspondeu ao pedido; esclareça essa parte.')
            elif operation=='policy':
                if data.get('topic') == 'shipping':
                    lines.append('Não consegui responder à pergunta sobre frete; esclareça essa parte para que eu consulte a política.')
                else:
                    lines.append('Não consegui validar a consulta da política; esclareça essa parte para que eu a consulte.')
            else:
                lines.append('Não consegui validar uma das operações e não a executei; esclareça essa parte.')
        elif name=='installment_quote':
            if not isinstance(data,dict): raise ValueError('invalid installment quote')
            if data.get('basis') == 'hipotetico':
                lines.append('Simulação hipotética: o valor informado não representa preço de venda.')
            lines.append(_render_installment_quote(data))
        elif name=='promotion':
            if data is None: continue
            if data.get('price_withheld'):
                lines.append(f"{data['name']}: preço e condições comerciais não exibidos conforme sua restrição.")
            else:
                lines.append(_render_price_quote(data.get('name','Produto'),data,data.get('available')))
        else: raise ValueError('unknown result')
    return '\n'.join(dict.fromkeys(lines)) if lines else 'Não encontrei informações suficientes para essa consulta.'
