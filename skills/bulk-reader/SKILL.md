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
   nunca para o seu contexto. Para um follow-up, chame de novo com os mesmos paths: custa zero.
2. **Leia cirurgicamente depois.** A resposta vem como `- Nome (path:Lini-Lfim): ...`. Use esses
   números num `Read` com `offset`/`limit` de até `SHUNT_EDIT_WINDOW` linhas (80 por padrão)
   só no trecho que vai editar. Confira linhas e valores exatos antes de editar: o modelo
   local pode errar por algumas linhas.

Não contorne um deny fatiando o arquivo com `sed -n` ou vários `Read` com offset: o hook soma
as fatias por sessão e bloqueia do mesmo jeito. Para **localizar** um símbolo, use `grep`/`rg`.
Arquivos maiores que a janela do modelo são fatiados automaticamente em partes, cada uma com a
numeração original.
