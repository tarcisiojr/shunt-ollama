---
name: bulk-reader
description: "Delega leitura de arquivos grandes, diffs extensos ou vários arquivos a um modelo Ollama local, que devolve bullets ancorados em linha. Use para entender, resumir ou responder perguntas sobre código/docs acima do limiar do hook (SHUNT_MIN_LINES), sempre que um hook do shunt negar uma leitura, ao explorar um projeto desconhecido ou revisar um diff grande."
---

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "<pergunta específica>" --paths <arquivo|diretório> [...]
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "..." --glob 'src/**/*.rs'
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "o que mudou e onde?" --cmd "git diff main"
git diff | ${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "..." --stdin
```

Fluxo em duas fases:

1. **Pergunte primeiro.** Faça uma pergunta específica ("quais funções tocam em autenticação
   e onde", "liste os subcomandos e o que cada um faz"). Os arquivos vão para o modelo local,
   nunca para o seu contexto: a chamada custa 0 tokens da sua janela e dezenas de segundos de
   espera. Pergunta vaga faz o modelo enumerar tudo e devolver quase o volume original.
   Para um follow-up, chame de novo com os mesmos paths, que também sai de graça.
2. **Leia cirurgicamente depois.** A resposta vem como `- Nome (path:Lini-Lfim): ...`. Use esses
   números num `Read` com `offset`/`limit` no trecho que vai editar. Leituras de até
   `SHUNT_ALWAYS_FREE` linhas (25 por padrão) nunca são bloqueadas nem contabilizadas, então
   cabem quantas vezes precisar. Confira linhas e valores exatos antes de editar: o modelo
   local pode errar por algumas linhas.

Não tente contornar um deny fatiando o arquivo. A primeira leitura de até `SHUNT_EDIT_WINDOW`
linhas (80) de cada arquivo passa livre, mas da segunda em diante ela entra no acumulado da
sessão, e o acumulado bloqueia igual. Para **localizar** um símbolo, use `grep -n` ou `rg`.
Arquivos maiores que a janela do modelo são fatiados automaticamente em partes, cada uma com a
numeração original.
