---
name: shunt-stats
description: "Mostra e interpreta as estatísticas do shunt-ollama: quanto contexto os bloqueios pouparam, quantas negativas viraram delegação ao modelo local, quanto de cada arquivo entrou no contexto, se houve leitura em fatias e se as delegações renderam. Use quando o usuário perguntar como o plugin está se comportando, se está valendo a pena, quanto economizou, ou ao investigar por que uma leitura foi bloqueada."
---

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats                      # tudo
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats --since 2026-09-01   # a partir de uma data
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats --version 0.6.0      # uma versão do plugin
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats --file install.sh    # um arquivo
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats --top 20             # mais linhas por seção
${CLAUDE_PLUGIN_ROOT}/scripts/shunt-stats --follow [--all]     # ao vivo: negativas, delegações, pareamento
```

## Como ler a saída

São cinco blocos, e cada um responde a uma pergunta diferente.

**Comparação por versão.** Uma linha por versão do plugin, porque hooks antigos continuam
rodando em sessões já abertas. A coluna que importa é a **conversão**: a fração de negativas
seguidas de uma chamada ao modelo local em até dez minutos. Conversão alta significa que o
plugin está desviando a leitura; conversão baixa significa que ele só está freando, e o Claude
está desistindo da informação em vez de delegar. Taxa de bloqueio alta com conversão zero é o
pior cenário, não o melhor.

**Cobertura por arquivo e sessão.** Quanto de cada arquivo chegou ao contexto, em bytes, que é
a unidade do orçamento. A coluna `fatias` conta leituras pequenas. Um arquivo grande com
cobertura alta montada quase toda em fatias indica leitura em volta do orçamento, e a seção de
fatiamento lista esses casos explicitamente.

**Decisões dos hooks.** O motivo de cada decisão. `small-file` é arquivo abaixo do limiar, fora
de alcance. `counted` é leitura debitada do orçamento. `escape` é leitura de edição após o
orçamento acabar. `binary` é arquivo que não é texto, liberado. `heredoc` e `unresolved:$VAR`
são comandos que o parser não consegue analisar, então são pontos cegos, não aprovações.

**bulk-read.** Tokens que foram para o Ollama contra tokens devolvidos ao Claude, e a resposta
como porcentagem do conteúdo lido. Acima de 50% a delegação rendeu pouco, e acima de 100% não
houve economia alguma: a causa quase sempre é pergunta ampla em vez de específica.

## Ao vivo

`--follow` é um `tail -f` filtrado: imprime cada negativa com os bytes que ficaram fora, cada
delegação com `pin`, `pout` e razão, e pareia as duas quando a delegação vem em até dez minutos.
Negativa que passa dos dez minutos sem delegação sai como "sem delegação": o Claude desistiu.
Não rode isso de dentro de uma sessão do Claude, porque bloqueia até Ctrl-C; sugira ao usuário
abrir num terminal ao lado. `--all` acrescenta `allow` e `skip` para investigar fatiamento.

## Ao relatar para o usuário

Cite conversão e cobertura, não apenas a taxa de bloqueio, que isolada engana. Se a versão mais
recente tiver menos de trinta eventos, diga que a amostra é pequena. Números do log são de
sessões reais; não os apresente como projeção.
