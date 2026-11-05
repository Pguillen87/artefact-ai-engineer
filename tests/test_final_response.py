import json
import unittest

from artefact.context import ConversationContext, PendingCatalogSearch
from artefact.final_response import (apply_final_response, communication_context,
                                     validated_final_response,
                                     validated_final_response_with_reason,
                                     validated_final_response_with_diagnostics)


def _provider_response(value):
    return {'output': [{
        'type': 'message',
        'content': [{'type': 'output_text', 'text': json.dumps(value, ensure_ascii=False)}],
    }]}


class FinalResponseTests(unittest.TestCase):
    def test_rejection_reasons_are_specific_and_never_include_model_text(self):
        base = {
            'opening': 'Separei algumas opções para você.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
        }
        cases = [
            (_provider_response({**base, 'evidence_refs': ['E9']}), {},
             'evidence_refs_mismatch'),
            (_provider_response({**base, 'opening': 'Yamaha custa R$ 999,00.'}), {},
             'opening_contains_digit'),
            (_provider_response({**base, 'opening': 'Yamaha custa.'}), {},
             'opening_contains_commercial_claim'),
            (_provider_response({**base, 'next_step': 'Quanto você pretende gastar?'}),
             {'known_context': {'budget_already_provided': True}}, 'repeats_known_budget'),
            (_provider_response({**base, 'product_order': [2, 3]}),
             {'product_ids': [2, 3, 4, 5, 6], 'require_all_product_ids': True},
             'incomplete_product_order'),
            ({'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'not valid JSON'}]}]}, {}, 'invalid_json'),
            ({'output': []}, {}, 'response_text_missing'),
        ]

        for response, kwargs, expected in cases:
            with self.subTest(reason=expected):
                result, reason = validated_final_response_with_reason(response, ['E1'], **kwargs)
                self.assertIsNone(result)
                self.assertEqual(reason, expected)
                self.assertNotIn('Yamaha', reason)
                self.assertNotIn('999', reason)

        accepted, reason = validated_final_response_with_reason(_provider_response(base), ['E1'])
        self.assertIsNotNone(accepted)
        self.assertIsNone(reason)

    def test_diagnostics_identify_only_safe_guard_metadata(self):
        raw_text = json.dumps({
            'opening': 'Yamaha custa.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
        }, ensure_ascii=False)

        result, diagnostics = validated_final_response_with_diagnostics(
            _provider_response({
                'opening': 'Yamaha custa.',
                'next_step': 'Qual dessas opções você prefere?',
                'evidence_refs': ['E1'],
            }), ['E1'], [4, 5])

        self.assertIsNone(result)
        self.assertEqual(diagnostics['reason_code'], 'opening_contains_commercial_claim')
        self.assertEqual(diagnostics['guard'], 'commercial_claim')
        self.assertEqual(diagnostics['field'], 'opening')
        self.assertTrue(diagnostics['response_text_present'])
        self.assertEqual(diagnostics['response_text_length'], len(raw_text))
        self.assertEqual(diagnostics['validated_evidence_refs'], ['E1'])
        self.assertEqual(diagnostics['validated_product_ids'], [])
        self.assertNotIn('Yamaha', str(diagnostics))
        self.assertNotIn('999', str(diagnostics))

    def test_accepts_natural_intro_and_contextual_question(self):
        result = validated_final_response(_provider_response({
            'opening': 'Separei algumas opções para você.',
            'next_step': 'Você prefere um violão de nylon ou de aço?',
            'evidence_refs': ['E1'],
        }), ['E1'])

        self.assertEqual(result['opening'], 'Separei algumas opções para você.')
        self.assertEqual(result['next_step'], 'Você prefere um violão de nylon ou de aço?')

    def test_generated_selection_and_order_must_use_visible_product_ids(self):
        allowed = [2, 6, 3]
        result = validated_final_response(_provider_response({
            'opening': 'Encontrei opções para você.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
            'product_order': [3, 6, 2],
        }), ['E1'], allowed)

        self.assertEqual(result['product_order'], (3, 6, 2))
        selected = validated_final_response(_provider_response({
            'opening': 'Encontrei opções para você.',
            'next_step': 'Qual delas você prefere?',
            'evidence_refs': ['E1'],
            'product_order': [3, 2],
        }), ['E1'], allowed)
        self.assertEqual(selected['product_order'], (3, 2))
        self.assertIsNone(validated_final_response(_provider_response({
            'opening': 'Encontrei opções para você.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
            'product_order': [3, 3, 2],
        }), ['E1'], allowed))

    def test_five_product_page_requires_every_visible_product_when_ordered(self):
        allowed = [2, 6, 3, 8, 9]
        base = {
            'opening': 'Encontrei opções para você.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
        }

        ordered = validated_final_response(_provider_response({
            **base, 'product_order': [9, 3, 2, 8, 6],
        }), ['E1'], allowed, require_all_product_ids=True)
        incomplete = validated_final_response(_provider_response({
            **base, 'product_order': [9, 3, 2, 8],
        }), ['E1'], allowed, require_all_product_ids=True)

        self.assertEqual(ordered['product_order'], (9, 3, 2, 8, 6))
        self.assertIsNone(incomplete)

    def test_nullable_product_order_from_strict_provider_schema_means_no_reorder(self):
        result = validated_final_response(_provider_response({
            'opening': 'Separei algumas opções para você.',
            'next_step': 'Qual dessas opções você prefere?',
            'evidence_refs': ['E1'],
            'product_order': None,
        }), ['E1'], [2, 3])

        self.assertIsNotNone(result)
        self.assertIsNone(result['product_order'])

    def test_rejects_bad_references_and_commercial_claims(self):
        base = {
            'opening': 'Separei algumas opções para você.',
            'next_step': 'Qual delas você prefere?',
            'evidence_refs': ['E1'],
        }
        invalid = [
            {**base, 'evidence_refs': ['E9']},
            {**base, 'opening': 'Yamaha Violão Nylon custa R$ 720,00.'},
            {**base, 'opening': 'O Yamaha está indisponível.'},
            {**base, 'opening': 'Este modelo tem frete grátis e garantia vitalícia.'},
            {**base, 'next_step': 'Quer comprar agora por R$ 650,00?'},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(validated_final_response(_provider_response(value), ['E1'], [2, 3]))

    def test_does_not_repeat_a_budget_question_after_customer_supplied_it(self):
        response = _provider_response({
            'opening': 'Encontrei opções dentro do que você informou.',
            'next_step': 'Quanto você pretende gastar?',
            'evidence_refs': ['E1'],
        })

        self.assertIsNone(validated_final_response(
            response, ['E1'], [2], known_context={'budget_already_provided': True}))

    def test_communication_context_carries_grounded_preferences_without_price_values(self):
        current = ConversationContext(pending_search=PendingCatalogSearch(
            category='Violões', brand='Yamaha', features=('nylon',), max_price=650,
        ))

        context = communication_context(current)

        self.assertEqual(context['category'], 'Violões')
        self.assertEqual(context['brand'], 'Yamaha')
        self.assertEqual(context['features'], ['nylon'])
        self.assertTrue(context['budget_already_provided'])
        self.assertNotIn('max_price', context)

    def test_does_not_repeat_known_category_brand_or_feature_questions(self):
        base = {
            'opening': 'Separei os resultados para você.',
            'evidence_refs': ['E1'],
        }
        context = {'category': 'Violões', 'brand': 'Yamaha', 'features': ['nylon']}
        for question in ('Que tipo de instrumento você procura?',
                         'Qual marca você prefere?',
                         'Você prefere nylon ou aço?'):
            with self.subTest(question=question):
                self.assertIsNone(validated_final_response(
                    _provider_response({**base, 'next_step': question}), ['E1'], [2], context))

    def test_applies_valid_prose_without_changing_canonical_facts(self):
        facts = ('Encontrei estas opções disponíveis no catálogo da loja:\n'
                 '• Yamaha Violão Nylon: R$ 650,00.\n'
                 '• Tagima Violão Aço: R$ 720,00.\n\n'
                 'Que tipo de instrumento ou categoria te interessa?')
        response = validated_final_response(_provider_response({
            'opening': 'Separei algumas opções para você.',
            'next_step': 'Você prefere um violão de nylon ou de aço?',
            'evidence_refs': ['E1'],
            'product_order': [3, 2],
        }), ['E1'], [2, 3])

        answer = apply_final_response(facts, response)

        self.assertTrue(answer.startswith('Separei algumas opções para você.\n\n'))
        self.assertIn('Yamaha Violão Nylon: R$ 650,00.', answer)
        self.assertIn('Tagima Violão Aço: R$ 720,00.', answer)
        self.assertTrue(answer.endswith('Você prefere um violão de nylon ou de aço?'))
        self.assertEqual(answer.count('?'), 1)


if __name__ == '__main__':
    unittest.main()
