"""Render commercial facts only from explicit tool result fields."""
from decimal import Decimal, InvalidOperation


def _money(value):
    amount=Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01')):
        raise ValueError('invalid monetary value')
    return 'R$ ' + f'{amount:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def render_results(results):
    lines=[]
    status_labels={'pending':'pendente','confirmed':'confirmado','shipped':'enviado','delivered':'entregue','cancelled':'cancelado'}
    for entry in results:
        name=entry['consulta'];data=entry['resultado']
        if name in ('search_products','recommend_products'):
            if not isinstance(data,list): raise ValueError('invalid products')
            for item in data:
                if not isinstance(item,dict) or type(item.get('stock_quantity')) is not int or type(item.get('available')) is not bool:
                    raise ValueError('invalid product')
                availability='disponível' if item['available'] else 'indisponível'
                lines.append(f"{item['name']}: {_money(item['price_brl'])}; {availability} no cadastro, estoque registrado de {item['stock_quantity']} unidade(s).")
                if item.get('review_fields'):
                    lines.append('Há detalhes técnicos que precisam de confirmação com um vendedor.')
            if data: lines.append('Preço e estoque são do cadastro; a data de atualização não está informada.')
        elif name=='orders':
            if not isinstance(data,list): raise ValueError('invalid orders')
            for order in data:
                status=status_labels.get(order.get('status'))
                if status is None: raise ValueError('unknown order status')
                lines.append(f"Pedido {order['order_id']}: o status cadastrado é {status}; total registrado {_money(order['total_brl'])}.")
                if order.get('items'):
                    lines.append('Itens: '+', '.join(f"{item['quantity']} × {item['name']}" for item in order['items'])+'.')
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
        elif name=='promotion':
            if data is None: continue
            if data.get('status')=='active_in_snapshot':
                value=Decimal(str(data['discount_percent']))
                if not value.is_finite() or not 0 <= value <= 100: raise ValueError('invalid discount')
                lines.append(f"Desconto de {format(value, 'f')}% sinalizado no cadastro; vigência atual não confirmada.")
            elif data.get('status')=='ambiguous': lines.append('Há promoções conflitantes no cadastro; é necessário confirmar a condição.')
            else: lines.append('Não há promoção disponível identificada no cadastro consultado.')
        else: raise ValueError('unknown result')
    return '\n'.join(lines) if lines else 'Não encontrei informações suficientes para essa consulta.'
