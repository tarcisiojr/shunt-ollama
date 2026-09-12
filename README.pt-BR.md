# shunt-ollama

**Português (Brasil)** · [English](README.md)

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

## Estatísticas

**Efeito de cada versão, direto do log.** É o que o `shunt-stats` imprime, e é a única tabela
aqui que você consegue reproduzir no seu próprio uso:

| Versão | Eventos | Taxa de bloqueio | Negativas convertidas em delegação |
|---|---|---|---|
| ≤ 0.3.0 | 589 | 26% | 0% |
| 0.4.0 | 1.238 | 5% | 0% |
| 0.5.0 | 45 | 48% | 71% |

A linha da 0.4.0 parece um plugin em repouso, e por um tempo foi lida assim. Medir cobertura em
vez de negativas mostrou o contrário: 29 arquivos tiveram 60% ou mais do conteúdo no contexto,
vários a 100%, montados em leituras pequenas. Nove estavam acima do limiar, 2.150 linhas que
deveriam ter sido barradas, e 13.603 das 17.281 linhas que entraram passaram pela faixa sempre
livre. Foi isso que a 0.5.0 reparou, e é por isso que a conversão é a linha que importa: ela
conta as negativas que viraram delegação em vez de desistência.
**Um replay pontual, mantido como referência.** As chamadas de ferramenta de 105 sessões
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
| 1 arquivo de 276 linhas, pergunta ampla | 3.318 | 2.252 | 53 s |
| 1 arquivo de 575 linhas (2 partes) | 13.383 | 378 | 68 s |

A segunda linha é o caso típico: o arquivo custou 13 mil tokens ao modelo local e 378 ao
Claude. A primeira mostra o risco de perguntas amplas, que fazem o modelo local enumerar tudo
e devolver quase o volume original. **Perguntas específicas comprimem, perguntas vagas não.**

Latência é o custo real: dezenas de segundos por delegação. Modelos menores respondem mais
rápido com perda de precisão nos números de linha.

**Como conferir isso você mesmo.** Cada linha do log carrega a versão do plugin, as faixas
pedidas e a cobertura já alcançada, e o `shunt-stats` transforma isso numa comparação entre
versões e num relatório de fatiamento. Os números acima saíram do próprio log, não da leitura
de transcripts.

**Suíte de testes.** 62 casos, montados a partir dos comandos exatos que versões anteriores
deixaram passar.

## Como funciona

Três hooks, um script de delegação e um de métricas.

| Peça | Papel |
|---|---|
| `hooks/check-file-size` | `PreToolUse` em `Read`. Calcula linhas efetivas: `min(limit, total - offset)`. |
| `hooks/check-bash-read` | `PreToolUse` em `Bash` e em ferramentas MCP que executam shell. Analisa `cat`, `head`, `tail`, `sed -n`, `awk`, `nl`, `bat`, `rtk read`. |
| `hooks/session-start` | `SessionStart`. Injeta a regra de roteamento e registra se o Ollama está de pé. |
| `scripts/bulk-read` | Monta a mensagem, chama o Ollama, imprime a resposta. |
| `scripts/shunt-stats` | Resume o log: comparação por versão, cobertura, fatiamento, tokens delegados. |
| `scripts/release` | Valida, cria a tag e publica a release da versão do manifesto. |

O `session-start` é o que faz o plugin ser usado em vez de descoberto por acidente. Sem ele,
o modelo só aprende que o shunt existe quando um bloqueio acontece, e a reação natural a um
bloqueio é tentar contornar.

### Regras de decisão

Em `hooks/lib/shunt_common.py`. O limiar responde duas perguntas: pelo tamanho total do arquivo
ele decide se o plugin se aplica, e quando se aplica, vira o orçamento de leitura daquele
arquivo na sessão.

1. Arquivo com até `SHUNT_MIN_LINES` (180) linhas está **fora de alcance**. Delegar custa mais
   do que ler, então ele nunca entra em orçamento algum. Registrado como `small-file`.
2. Acima disso, toda leitura do arquivo consome o orçamento. Não existe tamanho isento nem
   primeira leitura livre. As faixas são guardadas por sessão como união de intervalos, então
   reler a mesma faixa não faz o total crescer, e dividir a leitura em pedaços não compra mais
   linhas.
3. Esgotado o orçamento, restam `SHUNT_ESCAPE_BUDGET` (80) linhas extras em leituras de até
   `SHUNT_EDIT_WINDOW` (80) linhas cada, para que editar o trecho apontado pelo modelo local
   continue possível. Medido em linhas, e não em número de leituras: contar leituras permitia
   três de 80, o que devolvia arquivos de 300 linhas inteiros.
4. Vários arquivos num único comando acima de `SHUNT_MAX_TOTAL_LINES` (3× o limiar) também são
   negados.
5. Se o Ollama não responde, **nenhum bloqueio acontece**. A sondagem é cacheada por 2 minutos.
   Bloquear sem ter para onde delegar só travaria o Claude.

O limiar tem piso de duas vezes a janela de edição, registrado como `threshold-floor` no início
da sessão.

A proteção é proporcionalmente mais fraca em arquivos pouco acima do limiar: um arquivo de 300
linhas com orçamento 180 e escape 80 ainda pode chegar a 87% de cobertura. O ganho real está
nos arquivos grandes, onde 260 linhas de 4.000 são 6%.

O hook de início de sessão enuncia a regra sem publicar os números. A versão anterior listava
os tamanhos isentos, e o log mostrou o resultado: 41% das leituras couberam exatamente na faixa
isenta, e arquivos acima do limiar chegaram ao contexto inteiros, montados em fatias. Um limite
publicado é um mapa de contorno.

### Por que as regras são assim

As regras acima não são precauções, são reparos. A primeira versão era um port
fiel, com uma regex sobre a linha de comando, e nos logs de uso real ela disparou
**uma única vez em duas sessões**. Esse único bloqueio foi contornado: o modelo releu
o mesmo arquivo em quatro fatias com `sed -n`. Leu tudo, gastou os mesmos tokens, e o
modelo local nunca foi chamado.

Quase nenhuma leitura passava por onde os hooks vigiavam:

- `cd projeto && cat AGENTS.md` não casava com a regex `^cat `
- `cat arquivo 2>/dev/null` era descartado pelo filtro de redirecionamento
- `sed -n`, `awk` e `head -150` em loop não eram reconhecidos
- `Read` com `limit: 620` num arquivo de 1200 linhas passava, porque a regra era
  "tem `offset` ou `limit`, então é leitura direcionada"
- leituras feitas por outras ferramentas (MCP de terceiros) ficavam fora do matcher

É para isso que existe o parser léxico. Cada um desses cinco formatos é um caso de teste.

### Adaptação à máquina

A velocidade do modelo é propriedade do hardware, não do plugin. O mesmo `gemma4:e4b` passa de
1.000 tokens por segundo numa GPU dedicada e fica abaixo de 30 numa CPU de notebook. Um número
fixado aqui estaria errado para quase todo mundo, então nada é fixado: cada chamada registra
quantos tokens processou e quanto tempo levou, por modelo, em `SHUNT_CALIBRATION`.

As últimas 20 amostras dão duas taxas. O percentil 20 é a pessimista, usada para dimensionar o
timeout, e a mediana é a típica, usada no tempo que a mensagem de bloqueio mostra. Máquina lenta
ganha timeout maior sozinha, e trocar `SHUNT_MODEL` começa um histórico separado em vez de
reaproveitar o antigo.

Dois filtros mantêm o histórico honesto. Chamadas abaixo de 500 tokens são ignoradas, porque
medem ruído. Amostras que implicam mais de `SHUNT_MAX_PLAUSIBLE_RATE` (3.000 tokens/s) também
são descartadas: o Ollama reaproveita o prompt em cache quando a chamada repete o mesmo prefixo,
e o tempo medido desaba. Aprender isso encolheria o timeout justamente antes de um prompt novo e
lento.

Enquanto não há histórico, a taxa assumida é baixa de propósito, o que compra um timeout
generoso na primeira chamada. A segunda já é medida.

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

O padrão é 250 linhas. Por causa do piso de duas vezes a janela de edição, valores abaixo de
160 não têm efeito a menos que você reduza `SHUNT_EDIT_WINDOW` também. Para uso agressivo,
baixe os dois. No `~/.claude/settings.json`:

```json
{
  "env": {
    "SHUNT_MIN_LINES": "160",
    "SHUNT_EDIT_WINDOW": "60",
    "SHUNT_MODEL": "gemma4:e4b",
    "SHUNT_NUM_CTX": "32768"
  }
}
```

Limiar mais apertado bloqueia mais, e cada bloqueio custa uma delegação de dezenas de segundos
ou uma informação que o Claude vai dispensar. Medido em logs reais, cair de 250 para 160 subiu
a taxa de bloqueio de 32% para 41% e as negativas de 78 para 97.

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

A resposta sai no stdout agrupada por arquivo, com o caminho escrito uma vez e os achados
indentados abaixo:

```text
/caminho/para/install.sh
  186 conferir_checksum: aborta quando o sha256 não casa
  220-245 acrescenta_ao_path: escreve o bloco gerenciado no rc do shell
```

O agrupamento existe porque o caminho era a maior string repetida da resposta. Com 63 caracteres
em 35 achados, custava mais que os próprios achados; medido em três amostras, o formato agrupado
reduziu a resposta em 26%.

**A pergunta define o custo.** A resposta entra no seu contexto, o arquivo não, então o que
importa é o tamanho da resposta, e ele depende inteiramente do que foi perguntado. "Em que linha
o checksum é conferido?" volta com 1% do arquivo. "Explique o que o script faz" volta com 10% ou
mais, e num arquivo pequeno pode passar de 100%, ponto em que ler direto teria custado o mesmo.
O script avisa no stderr quando a resposta passa de `SHUNT_WARN_RATIO` (50%) do conteúdo lido, e
o `shunt-stats` informa a mediana e o pior caso. Duas perguntas estreitas saem mais baratas que
um panorama, e um follow-up nos mesmos paths é gratuito.

No stderr também aparece `[shunt: N tokens entrada | M saída | Xs | modelo]`. Arquivos maiores
que `SHUNT_NUM_CTX` são fatiados automaticamente, preservando a numeração original, e o script
avisa quando o prompt chega perto de truncar.

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
| `SHUNT_MIN_LINES` | `180` | tamanho fora de alcance e orçamento de leitura por arquivo, com piso de `2 × EDIT_WINDOW` |
| `SHUNT_EDIT_WINDOW` | `80` | maior leitura de edição individual |
| `SHUNT_ESCAPE_BUDGET` | `80` | linhas extras para leituras de edição após o orçamento acabar |
| `SHUNT_MAX_TOTAL_LINES` | `3 × MIN_LINES` | soma de vários arquivos num só comando |
| `SHUNT_WARN_RATIO` | `50` | avisa quando a resposta passa desta fração do conteúdo lido |
| `SHUNT_TIMEOUT_SECONDS` | vazio | timeout fixo em segundos; sobrepõe o calculado |
| `SHUNT_TIMEOUT_SLACK` | `200` | % do tempo previsto admitido antes de desistir |
| `SHUNT_TIMEOUT_MIN` / `_MAX` | `60` / `600` | piso e teto do timeout calculado |
| `SHUNT_CALIBRATION` | `~/.claude/shunt-calibration.json` | velocidade aprendida, por modelo |
| `SHUNT_FALLBACK_RATE` | `40` | tokens/s assumidos antes da primeira medição |
| `SHUNT_HOOK_LOG` | `~/.claude/shunt.log` | log TSV de decisões |
| `SHUNT_ASSUME_OLLAMA` | vazio | `1` pula a sondagem e bloqueia sempre (testes/CI) |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint |

## Métricas e depuração

Duas skills expõem isso ao Claude: a `bulk-reader` delega uma leitura, e a `shunt-stats` lê e
interpreta estes números, então perguntar "como o plugin está se comportando?" já basta.

```bash
scripts/shunt-stats
scripts/shunt-stats --since 2026-09-01
scripts/shunt-stats --version 0.5.0
scripts/shunt-stats --file install.sh --top 20
```

A primeira seção compara versões do plugin, para você saber se uma mudança realmente funcionou
em vez de adivinhar pelo timestamp:

```text
Comparação por versão
  versão      eventos  negativas    entrou  bloqueado   taxa  delegações  conversão
  <=0.3.0         589         50     17949       6297    26%        0+9!         0%
  0.4.0          1238          5     23977       1324     5%           4         0%
  0.5.0            45          7      1457       1366    48%           1        71%

  0.4.0 -> 0.5.0: taxa de bloqueio 5% -> 48%, conversão 0% -> 71%
```

`delegações` conta as execuções bem-sucedidas do `bulk-read`, com `+N!` marcando as que
falharam. `conversão` é a fração de negativas seguidas de uma delegação em até dez minutos, e é
o número que diz se o plugin está sendo usado como desvio ou apenas como freio. Amostra abaixo
de 30 eventos recebe aviso explícito, e conversão abaixo de 20% também.

Uma segunda seção mostra quanto de cada arquivo chegou ao contexto, e sinaliza fatiamento:

```text
Cobertura por arquivo e sessão (top 10 por percentual)
   coberto   total     %  leituras  fatias  arquivo
       240     299   80%         4       3  mia-cli/install.sh
       266    4289    6%        11      11  claude-local/claude-local.sh

  Fatiamento: 1 arquivo(s) com 50%+ de cobertura montada em 3+ leituras de até 80 linhas
```

`fatias` conta as leituras de até 80 linhas. Um arquivo com cobertura alta montada quase toda em
fatias é a assinatura de leitura em volta do orçamento, e `--file` restringe qualquer das seções
a um caminho.

O resto mostra decisões por ferramenta, arquivos mais bloqueados e tokens delegados ao Ollama
contra tokens devolvidos ao Claude.

O log é TSV com onze colunas:

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
| 9 | versão | versão do plugin que tomou a decisão |
| 10 | faixas | as faixas de linha que esta leitura pediu, ex. `1-80` ou `10-25,60-90` |
| 11 | coberto | total de linhas do arquivo já lidas nesta sessão |

As colunas 10 e 11 existem para que diagnosticar fatiamento seja uma consulta ao log, e não uma
reconstrução a partir dos transcripts das sessões. Linhas gravadas antes da 0.4.0 têm oito
colunas e as da 0.4.0 têm nove; o `shunt-stats` lê os três formatos e agrupa os mais antigos
como `<=0.3.0`, o que preserva a linha de base para comparação. Durante uma atualização as duas
versões aparecem no mesmo log: sessões já abertas seguem com os hooks antigos até serem
reiniciadas.

Motivos: `small-file` (arquivo abaixo do limiar, fora de alcance), `counted` (debitada do
orçamento), `escape` (debitada do saldo de escape depois de o orçamento acabar), `single-read`
(negada por tamanho), `cumulative` (negada porque o orçamento acabou), `escape-exhausted`
(negada porque o saldo de escape também acabou), `multi-file` (negada pela soma), `ollama-off`
(liberada por falta do modelo), `threshold-floor` (o limiar configurado foi elevado ao piso),
`heredoc` e `unresolved:$VAR` (não analisável).

Versões anteriores também gravavam `always-free`, `edit-window` e `window-counted`, das faixas
isentas que a 0.5.0 removeu.

O estado por sessão fica em `$TMPDIR/shunt-state-<session_id>.json`. Apagar reseta o acumulado.

## Testes

```bash
python3 -m unittest discover -s tests
flake8 --max-line-length=100 hooks/lib tests scripts/shunt-stats scripts/release
shellcheck scripts/bulk-read scripts/lib/ollama.sh
```

### Publicando uma versão

Suba o `version` nos dois arquivos de `.claude-plugin/`, comite, faça push e então:

```bash
scripts/release --dry-run   # mostra a tag, o título e as notas que publicaria
scripts/release
```

Ele se recusa a rodar com a árvore suja, com a tag já existente ou com commits não enviados, e
roda os testes, o flake8 e o shellcheck antes de criar a tag, porque uma tag aponta para um
commit para sempre. As notas saem da entrada daquela versão no changelog deste README. Publicar
a release exige o `gh` autenticado; sem ele a tag é criada e enviada de todo modo, e o comando
da release fica impresso.

Os casos vieram de comandos reais de sessões em que a 0.1.0 não interceptou nada. Ao adicionar
suporte a um comando novo, escreva primeiro o caso que hoje escapa.

**Convenção de idioma:** identificadores sempre em inglês, inclusive nomes de teste.
Comentários, docstrings e as mensagens impressas para o usuário em português brasileiro, que é
também o idioma do texto de roteamento que os hooks injetam.

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

- **0.8.0** — o timeout passa a sair da velocidade medida na própria máquina em vez de uma
  constante: cada chamada registra tokens e tempo de parede por modelo, o percentil 20
  dimensiona o timeout e a mediana alimenta a estimativa da negativa. Amostras de prompt em
  cache são descartadas, e a conversão de nanossegundos saiu do `awk` para o `jq`, onde um
  locale de vírgula decimal não a corrompe.
- **0.7.0** — uma skill `shunt-stats` deixa o Claude relatar e interpretar as métricas quando
  perguntado, e o `scripts/release` valida, cria a tag e publica a release da versão do
  manifesto. As versões 0.2.0 a 0.6.0 foram tagueadas retroativamente.
- **0.6.0** — a resposta passa a ser agrupada por arquivo, com o caminho escrito uma vez em vez
  de repetido em cada achado, o que a reduziu em 26% medido em três amostras. O `bulk-read`
  avisa quando a resposta passa de `SHUNT_WARN_RATIO` do conteúdo lido, a razão entra no log, e
  o `shunt-stats` informa mediana e pior caso, porque uma pergunta ampla pode custar mais que o
  próprio arquivo.
- **0.5.0** — o limiar virou orçamento de leitura por arquivo e todas as faixas isentas
  desapareceram, fechando o caminho de fatiamento que deixava arquivos inteiros chegarem ao
  contexto. O saldo de escape depois de o orçamento acabar é medido em linhas. O texto de
  roteamento deixa de publicar os limites. O log ganha as faixas pedidas e a cobertura
  acumulada, e o `shunt-stats` relata cobertura e sinaliza fatiamento, então diagnosticar isso
  não exige mais ler transcripts. O limiar padrão cai de 250 para 180.
- **0.4.0** — cada linha do log carrega a versão do plugin como nona coluna, e o `shunt-stats`
  compara versões lado a lado, então o efeito de uma mudança passa a ser medido em vez de
  inferido. Linhas de oito colunas das versões anteriores continuam sendo lidas e agrupadas
  como `<=0.3.0`.
- **0.3.0** — a janela de edição passa a contar da segunda leitura de cada arquivo em diante,
  fechando o contorno de fatiar em pedaços de 80 linhas; uma faixa sempre livre de 25 linhas
  mantém a edição cirúrgica possível; as negativas comparam o custo de ler com o de delegar; o
  limiar ganha piso de duas vezes a janela e seu padrão cai de 350 para 250.
- **0.2.0** — hooks em Python, linhas efetivas, anti-fatiamento por sessão, cobertura de
  ferramentas MCP que executam shell, hook de `SessionStart`, `--cmd`/`--stdin`/`--glob`,
  fatiamento automático, `keep_alive`, log TSV e `shunt-stats`.
- **0.1.0** — port inicial: hooks do original com o transporte trocado para o Ollama.

Cada entrada acima foi escrita depois de medir a anterior contra logs reais. É o
`scripts/shunt-stats` que torna isso possível.
## Licença

Apache 2.0, a mesma do repositório original da Spotify. Veja [LICENSE](LICENSE) e
[NOTICE](NOTICE).
