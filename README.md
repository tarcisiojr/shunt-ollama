# shunt-ollama

Plugin para o [Claude Code](https://claude.com/claude-code) que **impede leituras grandes de
entrarem no contexto** e as delega a um modelo rodando localmente no [Ollama](https://ollama.com).

O modelo local lê o arquivo inteiro e devolve bullets ancorados em número de linha. Só a
resposta chega ao Claude, que depois abre com `Read` apenas o trecho que precisa editar.

```text
Claude quer ler um arquivo de 4141 linhas
        │
        ├─ hook bloqueia ──► bulk-read ──► Ollama (gemma4:e4b) lê as 4141 linhas
        │                                        │
        └──────────── 40 linhas de bullets ◄─────┘
                      "- refresh_if_needed (launcher.sh:L1195-L1260): renova o token quando..."
                              │
                              └─► Read com offset=1195 limit=65 para editar
```

## De onde surgiu

A Spotify publicou em [`spotify/portal-ai-plugins`](https://github.com/spotify/portal-ai-plugins)
um plugin chamado `shunt`, que intercepta leituras do Claude Code e as redireciona para o
Portal/AiKA, a plataforma interna deles. A ideia é boa e não depende da infraestrutura da
Spotify: qualquer modelo de apoio serve como leitor.

Este repositório é um port dessa ideia para o Ollama, cobrindo só o caminho de leitura e
resumo (o mode `bulk-reader` do original).

**O que veio do original:** o desenho de hooks `PreToolUse` que interceptam leituras grandes,
o conceito de "mode" como arquivo de system prompt, e o formato de mensagem em tags
`<file path="...">`.

**O que mudou:**

| | Original (Spotify) | Aqui |
|---|---|---|
| Transporte | Portal CLI (`aika:invoke-chat`) | API HTTP local do Ollama |
| Detecção em Bash | regex `^(cat\|head\|tail\|less\|more) ` | parser léxico (`shlex`) por segmento |
| Limiar | tamanho do arquivo | **linhas efetivas** + acumulado por sessão |
| Payload | argumento de linha de comando (limitado por `ARG_MAX`) | stdin do `curl` |
| Numeração | não | `cat -n` antes de enviar |
| Fontes | arquivos | arquivos, diretórios, globs, saída de comando, stdin |
| Formato de saída do hook | `{"decision": "allow"}` | `hookSpecificOutput.permissionDecision` |

A última linha importa: versões recentes do Claude Code rejeitam o formato antigo com
`Hook JSON output validation failed`, e um hook que falha na validação **não bloqueia nada**.

## Por que a versão 0.2.0 existe

A 0.1.0 era um port fiel, com os hooks originais quase intactos. Nos logs de uso real ela
disparou **uma única vez em duas sessões**, e esse único bloqueio foi contornado: o modelo
releu o mesmo arquivo em quatro fatias com `sed -n '30,120p'`, `'121,200p'`, `'201,276p'`.
Leu tudo, gastou os mesmos tokens, e o modelo local nunca foi chamado.

A investigação mostrou que quase nenhuma leitura passava por onde os hooks vigiavam:

- `cd projeto && cat AGENTS.md` não casava com a regex `^cat `
- `cat arquivo 2>/dev/null` era descartado pelo filtro de redirecionamento
- `sed -n`, `awk` e `head -150` em loop não eram reconhecidos
- `Read` com `limit: 620` num arquivo de 1200 linhas passava, porque a regra era
  "tem `offset` ou `limit`, então é leitura direcionada"
- leituras feitas por outras ferramentas (MCP de terceiros) ficavam fora do matcher

A 0.2.0 reescreve os hooks em Python para fechar essas passagens.

## Estatísticas

**Replay dos hooks 0.2.0 sobre histórico real.** As chamadas de ferramenta de 105 sessões
gravadas do Claude Code foram reprocessadas pelo parser e pela máquina de decisão, com o
limiar em 100 linhas:

| Métrica | Valor |
|---|---|
| Chamadas de ferramenta analisadas | 12.468 |
| Com leitura de arquivo detectada | 1.354 |
| Bloqueadas | 364 |
| Liberadas na janela de edição (≤ 80 linhas) | 1.027 |
| Liberadas e contabilizadas no acumulado | 66 |
| Bloqueadas pelo acumulado de fatias | 4 |
| Linhas que não teriam entrado no contexto | 165.341 |

As 165 mil linhas correspondem a algo entre 1,6 e 2 milhões de tokens, a 10-12 tokens por
linha de código. O número é conservador: o replay resolve caminhos absolutos, então comandos
com caminho relativo a um diretório de trabalho antigo contaram como não detectados.

Para comparação, a 0.1.0 registrou **1** interceptação no mesmo histórico.

**Delegação real ao modelo local.** Medido com `gemma4:e4b` num Apple Silicon:

| Entrada | Tokens ao Ollama | Tokens ao Claude | Tempo |
|---|---|---|---|
| 1 arquivo de 276 linhas | 3.318 | 2.252 | 53 s |
| 1 arquivo de 575 linhas (2 partes) | 13.383 | 378 | 68 s |

A segunda linha é o caso típico: o arquivo custou 13 mil tokens ao modelo local e 378 ao
Claude. A primeira mostra o risco de perguntas amplas, que fazem o modelo local enumerar tudo
e devolver quase o volume original. **Perguntas específicas comprimem, perguntas vagas não.**

Latência é o custo real: dezenas de segundos por delegação. Modelos menores respondem mais
rápido com perda de precisão nos números de linha.

**Suíte de testes.** 27 casos, montados a partir dos comandos exatos que a 0.1.0 deixou passar.

## Como funciona

Três hooks, um script de delegação e um de métricas.

| Peça | Papel |
|---|---|
| `hooks/check-file-size` | `PreToolUse` em `Read`. Calcula linhas efetivas: `min(limit, total - offset)`. |
| `hooks/check-bash-read` | `PreToolUse` em `Bash` e em ferramentas MCP que executam shell. Analisa `cat`, `head`, `tail`, `sed -n`, `awk`, `nl`, `bat`, `rtk read`. |
| `hooks/session-start` | `SessionStart`. Injeta a regra de roteamento e registra se o Ollama está de pé. |
| `scripts/bulk-read` | Monta a mensagem, chama o Ollama, imprime a resposta. |
| `scripts/shunt-stats` | Resume o log: decisões, arquivos mais bloqueados, tokens delegados. |

O `session-start` é o que faz o plugin ser usado em vez de descoberto por acidente. Sem ele,
o modelo só aprende que o shunt existe quando um bloqueio acontece, e a reação natural a um
bloqueio é tentar contornar.

### Regras de decisão

Em `hooks/lib/shunt_common.py`:

1. Leitura de até `SHUNT_EDIT_WINDOW` (80) linhas passa sempre e **não conta**. É a janela que
   o Claude precisa para editar depois de consultar o modelo local. Sem essa exceção, o fluxo
   de edição quebra.
2. Leitura acima de `SHUNT_MIN_LINES` (350) é negada, com o comando `bulk-read` pronto para
   colar na mensagem de erro.
3. **Anti-fatiamento.** As faixas lidas de cada arquivo são guardadas por sessão como união de
   intervalos. Quando o acumulado passa do limiar, a próxima fatia é negada. Reler a mesma
   faixa não faz o total crescer.
4. Vários arquivos num único comando acima de `SHUNT_MAX_TOTAL_LINES` (3× o limiar) também são
   negados.
5. Se o Ollama não responde, **nenhum bloqueio acontece**. A sondagem é cacheada por 2 minutos.
   Bloquear sem ter para onde delegar só travaria o Claude.

### O que o parser reconhece

Cobre `cd dir && cat arquivo`, `2>/dev/null` e `2>&1`, `&&`/`;`/`||`/nova linha, pipes
(`cat f | head -40` conta 40 linhas; `cat f | grep x` é busca e passa), globs, `head -n150` em
todas as variantes de flag, `tail -n +5`, `sed -n '30,120p'` e listas de faixas,
`awk 'NR>=10 && NR<=50'`, `sed -i` como edição e não leitura, e o prefixo `rtk read` de
proxies de CLI que reescrevem `cat` antes deste hook enxergar.

Não cobre: heredocs (`<<EOF`) e caminhos em variáveis de shell (`cat "$f"`). Os dois viram
`skip` no log, com o motivo, em vez de passarem silenciosamente.

## Dependências

| Requisito | Para quê | Observação |
|---|---|---|
| [Ollama](https://ollama.com) rodando | o modelo leitor | `ollama serve` |
| Um modelo puxado | idem | `ollama pull gemma4:e4b`, ou outro via `SHUNT_MODEL` |
| `python3` ≥ 3.9 | os três hooks | **só biblioteca padrão**, nada de `pip install` |
| `bash` | `bulk-read` | 3.2+, o do macOS serve |
| `curl` e `jq` | falar com a API do Ollama | `brew install jq` |
| Claude Code | o host | versão que aceita `hookSpecificOutput` |

Sem dependências Python externas por escolha: um hook que falha porque um pacote não está no
ambiente é um hook que não protege nada.

## Instalação

```bash
claude plugin marketplace add tarcisiojr/shunt-ollama
claude plugin install shunt-ollama@shunt-ollama
```

Reinicie o Claude Code. Antes de testar, garanta o modelo local:

```bash
ollama serve &            # se ainda não estiver rodando
ollama pull gemma4:e4b
```

Peça ao Claude para ler um arquivo de mais de 350 linhas. Ele deve responder que a leitura foi
bloqueada e chamar o `bulk-read`.

### Sem marketplace

Copie `hooks/hooks.json` para a seção `hooks` do seu `settings.json`, trocando
`${CLAUDE_PLUGIN_ROOT}` pelo caminho absoluto do clone, e ponha
`skills/bulk-reader/SKILL.md` em `.claude/skills/bulk-reader/SKILL.md`.

### Ajustando o limiar

O padrão de 350 linhas é o do plugin original. Para uso agressivo, algo entre 100 e 150 pega
muito mais leitura. No `~/.claude/settings.json`:

```json
{
  "env": {
    "SHUNT_MIN_LINES": "150",
    "SHUNT_MODEL": "gemma4:e4b",
    "SHUNT_NUM_CTX": "32768"
  }
}
```

## Uso do bulk-read

```bash
scripts/bulk-read --question "Quais métodos públicos existem e o que cada um faz?" --paths src/Grande.java
scripts/bulk-read --question "Como o token é renovado?" --paths launcher.sh lib/
scripts/bulk-read --question "Onde ficam os handlers?" --glob 'src/**/*.rs'
scripts/bulk-read --question "O que mudou e onde?" --cmd "git diff main"
git diff | scripts/bulk-read --question "Resuma por arquivo" --stdin
```

Diretórios em `--paths` são expandidos, ignorando `.git` e `node_modules`. Sem `--question`, a
pergunta padrão pede símbolos públicos, responsabilidades, dependências e pontos de entrada.

A resposta sai no stdout como `- Nome (path:Lini-Lfim): descrição`. No stderr aparece
`[shunt: N tokens entrada | M saída | Xs | modelo]`. Arquivos maiores que `SHUNT_NUM_CTX` são
fatiados automaticamente, preservando a numeração original, e o script avisa quando o prompt
chega perto de truncar.

Fluxo em duas fases, e a segunda é obrigatória antes de editar: **pergunte** ao modelo local,
depois **leia cirurgicamente** com `offset`/`limit` no trecho apontado. O modelo local pode
errar alguns números de linha, então confira valores exatos antes de um `Edit`.

## Variáveis de ambiente

| Variável | Default | Função |
|---|---|---|
| `SHUNT_MODEL` | `gemma4:e4b` | modelo do Ollama |
| `SHUNT_TEMPERATURE` | `0.2` | mesma do original |
| `SHUNT_NUM_CTX` | `32768` | janela de contexto; o Ollama sobe com 4096 se você não setar, e aí trunca em silêncio |
| `SHUNT_KEEP_ALIVE` | `30m` | mantém o modelo carregado entre chamadas |
| `SHUNT_MIN_LINES` | `350` | limiar de bloqueio |
| `SHUNT_EDIT_WINDOW` | `80` | leituras até este tamanho passam sempre e não contam |
| `SHUNT_MAX_TOTAL_LINES` | `3 × MIN_LINES` | soma de vários arquivos num só comando |
| `SHUNT_TIMEOUT_SECONDS` | `180` | timeout do `curl` |
| `SHUNT_HOOK_LOG` | `~/.claude/shunt.log` | log TSV de decisões |
| `SHUNT_ASSUME_OLLAMA` | vazio | `1` pula a sondagem e bloqueia sempre (testes/CI) |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint |

## Métricas e depuração

```bash
scripts/shunt-stats
scripts/shunt-stats --since 2026-09-01
```

Mostra decisões por ferramenta, arquivos mais bloqueados, tokens delegados ao Ollama contra
tokens devolvidos ao Claude, e a taxa de conversão de bloqueio em delegação.

O log é TSV com oito colunas:

| # | Coluna | Conteúdo |
|---|---|---|
| 1 | timestamp | ISO 8601, hora local |
| 2 | sessão | id da sessão do Claude Code, ou `-` fora de sessão |
| 3 | ferramenta | `Read`, `Bash`, nome da ferramenta MCP, `bulk-read`, `SessionStart` |
| 4 | decisão | `allow`, `deny`, `skip`, `ok`, `error`, `info` |
| 5 | motivo | ver abaixo |
| 6 | arquivo | caminho absoluto, ou `-` |
| 7 | total | linhas do arquivo |
| 8 | efetivo | linhas que entrariam no contexto |

Motivos: `edit-window` (≤ 80 linhas, liberada), `counted` (liberada e somada),
`single-read` (negada por tamanho), `cumulative` (negada pelo acumulado de fatias),
`multi-file` (negada pela soma), `ollama-off` (liberada por falta do modelo),
`heredoc` e `unresolved:$VAR` (não analisável).

O estado por sessão fica em `$TMPDIR/shunt-state-<session_id>.json`. Apagar reseta o acumulado.

## Testes

```bash
python3 -m unittest discover -s tests
flake8 --max-line-length=100 hooks/lib tests scripts/shunt-stats
shellcheck scripts/bulk-read scripts/lib/ollama.sh
```

Os casos vieram de comandos reais de sessões em que a 0.1.0 não interceptou nada. Ao adicionar
suporte a um comando novo, escreva primeiro o caso que hoje escapa.

## Limitações conhecidas

- **Latência.** Dezenas de segundos por delegação. É o preço de não gastar contexto.
- **Precisão dos números de linha.** Um modelo de 4B erra por algumas linhas. Daí a regra de
  reler o trecho antes de editar.
- **Heredocs e variáveis de shell** não são analisados.
- **Pergunta vaga comprime pouco.** O modelo local enumera tudo e devolve quase o volume
  original.
- **Só leitura.** O mode `code-writer` do original não foi portado. O script seria análogo:
  mensagem no formato `Spec: ...\n\nReference:\n<arquivo>` e um system prompt terminando em
  "Output only the code. No markdown fences, no explanation."

## Changelog

- **0.2.0** — hooks em Python, linhas efetivas, anti-fatiamento por sessão, cobertura de
  ferramentas MCP que executam shell, hook de `SessionStart`, `--cmd`/`--stdin`/`--glob`,
  fatiamento automático, `keep_alive`, log TSV e `shunt-stats`.
- **0.1.0** — port inicial: hooks do original com o transporte trocado para o Ollama.

## Licença

Apache 2.0, a mesma do repositório original da Spotify. Veja [LICENSE](LICENSE) e
[NOTICE](NOTICE).
