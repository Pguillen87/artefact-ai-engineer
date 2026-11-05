## Known limitations and future improvements

- The 17-case conversational sample does not establish general production readiness. The restricted company manual was not consulted during this retest.
- In the published conversational retest, `INSTALL-001` failed: the request to pay for the Shelby SU-21S in 12 installments returned a generic fallback. The expected maximum is 3 installments of R$ 53.30 for a total of R$ 159.90. A later code change adds deterministic catalog routing for this request and its local regression passes; no provider-backed retest was run after that change, so the original QA result remains recorded as-is.
- Catalog, promotion, stock, shipping, coming-soon status, and historical order answers reflect the frozen catalog v3 / cloned QA data. They do not establish current live availability or fulfillment.
- Historical order data was verified in a two-turn flow after the privacy gate. The gate was already correct in the baseline; the expanded historical lookup coverage does not indicate a gate fix. The demo asks for a registered contact but does not prove possession with an OTP.
- The project suite improved from 84/107 passing and 23 failing on the clean baseline to 86/107 passing and 21 failing on the evaluated version. The 21 current failures were already present in the clean baseline; two Pix failures were resolved, with no new failures in that set.
- After the later CI corrections, the complete local suite passed with 568 tests, 7 skipped and no failures. This is local unit-suite evidence, not a replacement for the published conversational retest.
- Agent regression unit tests (9/9) and fake harness checks (8/8) are separate layers and do not mean the project suite passed in full.
- Keep the Shelby installment regression in the local suite. If any new provider-backed conversation run is performed, report it separately; do not overwrite this 17-case retest.

## Limitações conhecidas e melhorias futuras

- A amostra conversacional de 17 casos não comprova prontidão geral para produção. O manual restrito da empresa não foi consultado neste reteste.
- No reteste conversacional publicado, `INSTALL-001` falhou: o pedido de parcelamento do Shelby SU-21S em 12 vezes retornou um fallback genérico. O máximo esperado é 3 parcelas de R$ 53,30, totalizando R$ 159,90. Uma alteração posterior adiciona uma rota determinística de catálogo para esse pedido e sua regressão local passou; não houve novo reteste com provedor após essa alteração, então o resultado original de QA permanece registrado sem alterações.
- As respostas de catálogo, promoção, estoque, frete, status “em breve” e pedidos históricos refletem o catálogo v3 congelado e os dados clonados de QA; não comprovam disponibilidade ou entrega em tempo real.
- Os dados históricos foram verificados em um fluxo de dois turnos após o gate de privacidade. O gate já estava correto no baseline; a ampliação da consulta histórica não representa correção do gate. A demonstração pede um contato cadastrado, mas não comprova a posse com OTP.
- A suíte do projeto passou de 84/107 aprovados e 23 falhas no baseline limpo para 86/107 aprovados e 21 falhas na versão avaliada. As 21 falhas atuais já existiam no baseline limpo; duas falhas de casos Pix foram resolvidas, sem novas falhas nesse conjunto.
- Após as correções posteriores da CI, a suíte local completa passou com 568 testes, 7 ignorados e nenhuma falha. Isso é evidência da suíte unitária local, não substitui o reteste conversacional publicado.
- Os testes unitários de regressão do agente (9/9) e os testes fake do harness (8/8) são camadas separadas e não significam que toda a suíte do projeto passou.
- Manter a regressão de parcelamento Shelby na suíte local. Se houver uma nova conversa com provedor, registrar o resultado separadamente, sem substituir o reteste de 17 casos.

### Matriz de achados

| Achado | Evidência | Limitação | Melhoria sugerida |
|---|---|---|---|
| Intenção de parcelamento | `INSTALL-001` retornou fallback genérico em vez de avaliar 12x e indicar o máximo de 3x | O caso continua falhando; R$ 159,90 não atende ao piso de R$ 100 por parcela para 7x–12x | Corrigir a identificação da intenção de parcelamento e adicionar regressão para o prompt literal. |
| Promoção e Pix | `PIX-001` informou 18%, preço de R$ 1.967,18 e não cumulatividade; duas falhas Pix da suíte foram resolvidas | A resposta usa snapshot e não confirma vigência atual | Preservar a regra de não acumular e exibir a idade/versão da fonte de preços. |
| Gate de privacidade | Quatro gates solicitaram e-mail ou telefone sem expor pedidos por nome | A demo não comprova posse do contato via OTP | Adicionar prova de posse antes de revelar dados em produção. |
| Histórico de pedidos | Quatro pedidos foram consultados após gate correto, em fluxo de dois turnos | Cobertura foi ampliada; baseline não verificava o histórico com o mesmo fluxo | Manter escopo limitado ao pedido autorizado e minimizar os campos retornados. |
| Pré-lançamento | `CATALOG-130` marcou o produto “em breve” e sem pronta entrega | Estoque do snapshot não representa disponibilidade para venda | Exibir status comercial e data/versão da fonte juntos. |
| Frete de instrumento pesado | `SHIP-003` solicitou cotação especial sem inventar taxa/prazo | Nenhuma cotação real foi executada | Manter encaminhamento à loja até confirmação. |
