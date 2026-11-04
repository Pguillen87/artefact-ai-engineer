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

Uma única central, sem duplicação do fluxo de importação. Cada aba renderiza apenas sua tarefa. No celular, um botão de três barras abre a navegação lateral com fundo sobreposto; Escape, fechar ou clicar fora devolve o foco ao botão. Os formulários ficam em uma coluna. O chat não depende de abrir os formulários.

O chat segue a referência de mensageiro solicitada: cabeçalho compacto, avatar tipográfico, fundo `#efeae2`, balões brancos e verdes `#d9f5c9`, envio verde `#128c7e` e cabeçalho móvel `#075e54`. Campo arredondado na parte inferior e rolagem apenas da conversa, respeitando a altura visível. A identidade da central permanece na navegação.

## Estados e ações

- A ausência de dados ou chave deve explicar a pendência e oferecer um link direto para resolvê-la.
- Salvar não faz inferência. Testar e ativar é uma ação explícita, visível junto à configuração da IA.
- Erros permanecem no formulário correspondente e preservam valores não secretos. Nunca refletir a chave API no HTML.
- Feedback fica junto à ação. Bloquear envio duplicado enquanto uma solicitação estiver em andamento.
- Envio do chat sem navegação: mensagem imediata, estado Respondendo e confirmação pelo backend. Falha incerta exige atualização, preserva rascunho por conversa na sessão do navegador e nunca dispara reenvio automático.
- Arquivos selecionados ficam neutros; os arquivos de uma base ativada recebem check verde e Importado. A confirmação de uma nova importação tem movimento breve, desativado com redução de movimento.
- Preservar foco visível, contraste, navegação por teclado, labels, estado ativo da navegação e redução de movimento.
