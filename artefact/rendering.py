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


def _render_local_shipping_quote(quote):
    if not isinstance(quote, dict):
        raise ValueError('invalid local shipping quote')
    delivery = quote.get('delivery')
    if delivery is not None:
        if (not isinstance(delivery, dict) or delivery.get('destination') != 'Campo Grande/MS' or
                type(delivery.get('minimum_business_days')) is not int or
                type(delivery.get('maximum_business_days')) is not int or
                delivery['minimum_business_days'] < 1 or
                delivery['maximum_business_days'] < delivery['minimum_business_days'] or
                type(delivery.get('date_guaranteed')) is not bool):
            raise ValueError('invalid local delivery policy')
    if quote.get('status') == 'clarification':
        prompts = {
            'destination': 'Qual é a cidade/UF ou o CEP de entrega? A cotação local automática cobre Campo Grande/MS.',
            'product_and_destination': 'Para cotar, informe o nome/modelo do instrumento e o CEP de entrega. A cotação local automática cobre Campo Grande/MS.',
            'product_missing': 'Qual é o nome ou modelo do instrumento que deseja cotar?',
            'product_ambiguous': 'Não consegui identificar um produto exato nesta consulta. Confirme o nome ou modelo que deseja cotar.',
            'price_basis': 'O valor informado diverge do preço efetivo do catálogo. Quer que eu calcule com o preço cadastrado?',
            'product_unavailable': 'Esse produto está indisponível para venda no snapshot, então não calculei o frete. Qual outro modelo você quer consultar?',
            'price_unavailable': 'Não encontrei um preço efetivo confiável para esse produto no catálogo. Confirme o preço com a loja antes de calcular o frete.',
        }
        reason = quote.get('reason')
        if reason not in prompts:
            raise ValueError('unknown shipping clarification')
        if reason == 'destination' and isinstance(quote.get('product_name'), str):
            lines = [f"Para cotar o frete de {quote['product_name']}, qual é o CEP de entrega? "
                     'A cotação automática cobre Campo Grande/MS; outros destinos precisam de cotação da loja.']
        else:
            lines = [prompts[reason]]
    elif quote.get('status') == 'quoted':
        name = quote.get('product_name')
        destination = quote.get('destination')
        price_basis = quote.get('price_basis')
        if (not isinstance(name, str) or destination != 'Campo Grande/MS' or
                price_basis not in {'promotion_in_snapshot', 'catalog_checkout'}):
            raise ValueError('invalid local shipping quote details')
        product_price = Decimal(str(quote.get('product_price_brl')))
        freight = Decimal(str(quote.get('freight_brl')))
        total = Decimal(str(quote.get('total_brl')))
        if (any(not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01'))
                for amount in (product_price, freight, total)) or total != product_price + freight):
            raise ValueError('invalid local shipping amounts')
        basis = ('Preço promocional registrado no snapshot' if price_basis == 'promotion_in_snapshot'
                 else 'Preço efetivo de checkout registrado no catálogo')
        lines = [f'{name}: {basis} {_money(product_price)}.',
                 f'Frete para {destination}: {_money(freight)}.',
                 f'Total estimado do item com frete: {_money(total)}.']
    else:
        raise ValueError('unknown local shipping quote status')
    if (delivery is not None and quote.get('status') == 'quoted') or (
            delivery is not None and quote.get('status') == 'clarification' and
            quote.get('reason') in {'product_missing', 'product_ambiguous', 'price_basis', 'product_unavailable', 'price_unavailable'}):
        days = (f"{delivery['minimum_business_days']} a {delivery['maximum_business_days']} dias úteis")
        qualifier = '' if delivery['date_guaranteed'] else '; a data exata não é garantida'
        lines.append(f"Prazo normal para {delivery['destination']}: {days}{qualifier}.")
    return lines


def _render_catalog_overview(data):
    if not isinstance(data, dict):
        raise ValueError('invalid catalog overview')
    categories = data.get('categories')
    examples = data.get('examples')
    if not isinstance(categories, (list, tuple)) or not isinstance(examples, (list, tuple)) or len(examples) > 3:
        raise ValueError('invalid catalog overview')
    lines = ['No catálogo, há estas categorias com produtos disponíveis:']
    for category in categories:
        if (not isinstance(category, dict) or not isinstance(category.get('name'), str) or
                type(category.get('available_count')) is not int or category['available_count'] <= 0):
            raise ValueError('invalid catalog category')
        count = category['available_count']
        lines.append(f"• {category['name']}: {count} produto{'s' if count != 1 else ''} disponível{'is' if count != 1 else ''}.")
    if examples:
        lines.append('Alguns exemplos e seus preços registrados:')
        seen = set()
        for item in examples:
            category_id = item.get('category_id') if isinstance(item, dict) else None
            if (type(category_id) is not int or category_id in seen or
                    not isinstance(item.get('category_name'), str) or not isinstance(item.get('name'), str) or
                    item.get('available') is not True or not isinstance(item.get('price_quote'), dict)):
                raise ValueError('invalid catalog example')
            seen.add(category_id)
            quote = item['price_quote']
            effective = quote.get('checkout_price_brl')
            if effective is None:
                raise ValueError('catalog example has no price')
            lines.append(f"• {item['name']} ({item['category_name']}): {_money(effective)}.")
            if quote.get('status') == 'active_in_snapshot':
                lines.append('  Promoção registrada no snapshot; a vigência atual não foi confirmada.')
    else:
        lines.append('Não há exemplos com preço disponível no cadastro atual.')
    lines.append('Que tipo de instrumento ou categoria te interessa?')
    return '\n'.join(lines)


def _render_product_detail(item):
    if (not isinstance(item, dict) or not isinstance(item.get('name'), str) or
            type(item.get('stock_quantity')) is not int or type(item.get('available')) is not bool):
        raise ValueError('invalid product detail')
    if item.get('price_withheld'):
        lines = [f"{item['name']}: detalhes comerciais ocultos conforme sua solicitação."]
        availability = 'disponível' if item['available'] else 'indisponível'
        lines.append(f"Disponibilidade: {availability} no cadastro, estoque registrado de {item['stock_quantity']} unidade(s).")
    else:
        quote = item.get('price_quote')
        lines = [_render_price_quote(item['name'], quote, item['available'])]
        availability = 'disponível' if item['available'] else 'indisponível'
        lines.append(f"Disponibilidade: {availability} no cadastro, estoque registrado de {item['stock_quantity']} unidade(s).")
    specs = item.get('specs')
    if not isinstance(specs, dict):
        raise ValueError('invalid product specifications')
    safe_specs = []
    for key, value in specs.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            continue
        label = key.replace('_', ' ').strip()
        rendered = str(value).replace('\r', ' ').replace('\n', ' ').strip()
        if label and rendered and len(label) <= 40 and len(rendered) <= 100:
            safe_specs.append(f'{label}: {rendered}')
    if safe_specs:
        lines.append('Especificações cadastradas: ' + '; '.join(safe_specs) + '.')
    elif item.get('review_fields'):
        lines.append('Os detalhes técnicos precisam de confirmação com um vendedor.')
    return '\n'.join(lines)


def render_results(results):
    lines=[]
    status_labels={'pending':'pendente','confirmed':'confirmado','shipped':'enviado','delivered':'entregue','cancelled':'cancelado'}
    for entry in results:
        name=entry['consulta'];data=entry['resultado']
        if name == 'catalog_overview':
            lines.append(_render_catalog_overview(data))
        elif name == 'product_detail':
            if data is not None:
                lines.append(_render_product_detail(data))
        elif name in ('search_products','recommend_products','cheapest_product'):
            if not isinstance(data,list): raise ValueError('invalid products')
            search_context = entry.get('search_context')
            if not data and search_context is not None and name in {'search_products', 'recommend_products'}:
                if not isinstance(search_context, dict):
                    raise ValueError('invalid empty search context')
                category = search_context.get('category')
                query = search_context.get('query')
                if category is not None and not isinstance(category, str):
                    raise ValueError('invalid empty search category')
                if query is not None and not isinstance(query, str):
                    raise ValueError('invalid empty search query')
                maximum = search_context.get('max_price_brl')
                limit = ''
                if maximum is not None:
                    amount = _money(maximum)
                    limit = (f' dentro do orçamento de {amount}'
                             if search_context.get('price_limit_type') == 'budget'
                             else f' com preço até {amount}')
                available = search_context.get('available_only') is True
                if query:
                    subject = f'resultados para {query}'
                elif category:
                    if name == 'recommend_products':
                        subject = f'alternativas disponíveis na categoria {category}'
                    elif available:
                        subject = f'produtos disponíveis na categoria {category}'
                    else:
                        subject = f'produtos da categoria {category}'
                else:
                    subject = 'produtos para os filtros informados'
                lines.append(f'Não encontrei {subject}{limit} nesta consulta ao catálogo.')
                if search_context.get('no_result_count', 0) >= 2:
                    lines.append('Ainda não encontrei opções com esses filtros. Quer que eu peça ajuda a um vendedor para verificar o catálogo?')
                else:
                    lines.append('Qual filtro você gostaria de alterar? Só amplio a busca depois da sua confirmação.')
                continue
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
            if data is not None:
                local_quote = data.get('local_quote')
                if local_quote is None:
                    lines.append(data['summary'])
                else:
                    lines.extend(_render_local_shipping_quote(local_quote))
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
            elif data.get('reason') == 'recommendation_scope_missing':
                lines.append('Qual tipo ou categoria de instrumento você quer que eu recomende? Não executei a recomendação sem esse contexto.')
            else:
                lines.append('Não realizei a cotação: esclareça o produto, a base do valor ou a restrição indicada antes de simular parcelas.')
        elif name=='execution_clarification':
            lines.append('Nesta consulta, não consegui validar os argumentos da cotação e não a executei; esclareça essa parte e informe um único valor em reais e a quantidade de parcelas desejada.')
        elif name=='clarification':
            if not isinstance(data,dict):
                raise ValueError('invalid clarification')
            operation=data.get('tool')
            if operation=='search_products':
                lines.append('Não consegui identificar essa busca com segurança. Qual instrumento ou modelo você quer ver? Se tiver um limite de preço, me conte também.')
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
