# Empório da Música — central de atendimento

Modo: Operate. Protótipo local para o desafio Artefact. A interface serve ao atendimento e à preparação dos dados; não é uma landing page.

## Direção

Manter a identidade azul e verde da central, com navegação discreta e espaço próprio para conversar. A hierarquia separa tarefas; evita uma pilha de formulários ao lado do chat.

- Azul profundo `#142d3b`: texto e navegação.
- Verde petróleo `#087f7c`: ações principais e foco da tarefa.
- Branco `#ffffff`: superfícies de trabalho.
- Cinza claro `#f2f5f6`: fundo da aplicação.
- Cinza azulado `#526b78`: texto secundário.
- Verde claro `#d9eeea`: seleção da navegação.

Tipografia: Segoe UI, com Calibri e sans-serif como alternativas locais. Corpo 15 px, campos 16 px no celular, títulos 25–28 px. Títulos e textos alinhados à esquerda; estado vazio do chat centralizado. A marca tipográfica é o único elemento expressivo.

## Composição

```text
Navegação       Área ativa
Chat            Conversa + histórico + mensagem na parte inferior
Arquivos        Importação múltipla + arquivos escolhidos + resultado
Configurações   IA + vendedor; limites recolhidos em Avançado
```

Uma única central, sem duplicação do fluxo de importação. Cada aba renderiza apenas sua tarefa. No celular, a navegação passa para o topo e os formulários ficam em uma coluna. O chat não depende de abrir os formulários.

## Estados e ações

- A ausência de dados ou chave deve explicar a pendência e oferecer um link direto para resolvê-la.
- Salvar não faz inferência. Testar e ativar é uma ação explícita, visível junto à configuração da IA.
- Erros permanecem no formulário correspondente e preservam valores não secretos. Nunca refletir a chave API no HTML.
- Feedback fica junto à ação. Bloquear envio duplicado enquanto uma solicitação estiver em andamento.
- Preservar foco visível, contraste, navegação por teclado, labels, estado ativo da navegação e redução de movimento.
