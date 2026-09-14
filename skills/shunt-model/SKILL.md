---
name: shunt-model
description: "Lista os modelos baixados no Ollama e troca o modelo que o shunt-ollama usa nas delegações (SHUNT_MODEL). Use quando o usuário pedir para ver, escolher, trocar ou comparar o modelo local do plugin, perguntar qual modelo está em uso, ou quiser testar outro modelo depois de um `ollama pull`."
---

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-model                 # lista os modelos, marca o atual
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-model current         # só o nome em uso
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-model set qwen3.5:4b  # grava em ~/.claude/settings.json
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-model unset           # volta ao padrão do plugin
```

## Fluxo

1. Rode a listagem. Ela mostra cada modelo baixado com o tamanho em disco e marca o que
   está em uso, dizendo de onde a escolha vem: `settings.json`, variável de ambiente da
   sessão ou padrão do plugin.
2. Apresente as opções ao usuário com `AskUserQuestion`, uma por modelo, com o tamanho na
   descrição e o atual marcado. Se ele mencionou um modelo que não aparece na lista, ofereça
   `ollama pull <nome>` antes, porque o `set` recusa nome não baixado.
3. Grave a escolha com `set`. O script edita só a chave `env.SHUNT_MODEL` do settings e
   preserva o resto do arquivo.
4. Avise que a troca vale para a próxima sessão do Claude Code: hooks e `bulk-read` leem a
   variável ao iniciar. Na sessão atual, `SHUNT_MODEL=<nome>` antes de um `bulk-read` avulso
   já usa o modelo novo.

## Para comparar modelos

Cada delegação grava `model=` no log, e o `shunt-stats` imprime o bloco **Comparação por
modelo**: delegações, tokens enviados e devolvidos, razão mediana da resposta, segundos por
delegação, tokens por segundo e a fatia de achados sem número de linha (`sem nº`). Use `shunt-stats --model <nome>` para isolar um deles. Um
modelo só é comparável depois de algumas delegações reais; com menos de dez, diga que a
amostra é pequena.

Critérios que costumam decidir: tempo por delegação (o Claude espera essa chamada), razão da
resposta (quanto menor, mais o modelo comprime), `sem nº` (achado sem número não serve ao
Claude, mesmo que a descrição seja boa) e memória, que o log não mede. Um modelo mais
lento que o atual raramente compensa, mesmo respondendo melhor.
