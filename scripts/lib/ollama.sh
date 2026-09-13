#!/bin/bash
# Plumbing compartilhado: substitui o `aika.sh` do plugin da Spotify,
# trocando o Portal CLI (aika:invoke-chat) pela API local do Ollama.
#
# Cada delegação é one-shot, como no original: nada fica guardado no servidor.
# Para um follow-up, chame de novo com os mesmos arquivos — eles vão para o
# Ollama, nunca para o contexto do Claude, então reenviar custa zero tokens.

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
SHUNT_MODEL="${SHUNT_MODEL:-gemma4:e4b}"
SHUNT_TEMPERATURE="${SHUNT_TEMPERATURE:-0.2}"
# Janela de contexto do modelo. O Ollama sobe com 4096 se você não pedir
# outro valor, e aí um arquivo grande é cortado sem aviso.
#
# Aumentar este valor não é a saída para arquivo grande: medido nesta
# máquina, um prompt de ~48000 tokens com num_ctx=65536 foi processado
# inteiro, mas levou 1328s contra 78s de uma chamada equivalente em 32768. O
# tempo cresce muito mais que linearmente com a janela, então fatiar é sempre
# mais rápido que alargar.
#
# ATENÇÃO, medido neste plugin com num_ctx=32768: um prompt de até ~27900
# tokens é processado inteiro, e a partir de ~30000 o prompt_eval_count cai
# para exatamente 16387, ou seja num_ctx/2. O llama.cpp descarta metade do
# contexto quando ele estoura, e nada nisso vira erro: o modelo responde
# sobre o pedaço que sobrou, com a mesma confiança. Por isso o orçamento fica
# abaixo do teto e uma resposta truncada é detectada e descartada.
SHUNT_NUM_CTX="${SHUNT_NUM_CTX:-32768}"
# Fração do num_ctx usável pelo prompt com folga. O teto medido fica em torno
# de 85%; 80% deixa margem para a variação da razão bytes/token.
SHUNT_PROMPT_FRACTION="${SHUNT_PROMPT_FRACTION:-80}"
# Tokens reservados para o system prompt do mode e para o template de chat.
SHUNT_PROMPT_RESERVE="${SHUNT_PROMPT_RESERVE:-900}"
# Bytes por token, em décimos, no pior caso plausível. Medido no tokenizador
# do gemma: 1,6 em JSONL denso, 2,1 em JSON minificado, 2,7 a 3,7 em código e
# prosa. As partes são dimensionadas pelo piso de propósito. Estourar o limite
# significa truncar em silêncio; subestimar só gera uma parte a mais, e a
# diferença medida foi de uma parte em cinco num script de 190 KB.
SHUNT_BYTES_PER_TOKEN_FLOOR="${SHUNT_BYTES_PER_TOKEN_FLOOR:-16}"  # décimos
# E o teto da mesma razão. Serve ao outro lado da conta: para saber se o
# prompt foi cortado é preciso o MÍNIMO de tokens que ele poderia ter, e usar
# o piso ali acusaria truncamento em todo arquivo de código.
SHUNT_BYTES_PER_TOKEN_CEIL="${SHUNT_BYTES_PER_TOKEN_CEIL:-40}"  # décimos

# Mantém o modelo carregado entre chamadas; sem isso cada delegação paga o
# cold start de vários segundos.
SHUNT_KEEP_ALIVE="${SHUNT_KEEP_ALIVE:-30m}"
SHUNT_HOOK_LOG="${SHUNT_HOOK_LOG:-$HOME/.claude/shunt.log}"
# Acima desta razão saída/entrada (%), a delegação rendeu pouco e vale avisar.
SHUNT_WARN_RATIO="${SHUNT_WARN_RATIO:-50}"
# Velocidade do modelo é propriedade da máquina, não do plugin: numa GPU
# dedicada passa de 1000 tokens/s, numa CPU fica abaixo de 30. Em vez de
# cravar um número, cada chamada registra tokens e duração aqui, e o timeout
# das chamadas seguintes sai desse histórico.
SHUNT_CALIBRATION="${SHUNT_CALIBRATION:-$HOME/.claude/shunt-calibration.json}"
# Taxa assumida enquanto não há histórico. Pessimista de propósito: errar para
# baixo dá um timeout generoso na primeira chamada, e a segunda já é medida.
SHUNT_FALLBACK_RATE="${SHUNT_FALLBACK_RATE:-40}"
# Amostras guardadas por modelo, e o mínimo de tokens para uma amostra contar
# (chamadas triviais medem ruído, não capacidade).
SHUNT_CALIBRATION_SAMPLES="${SHUNT_CALIBRATION_SAMPLES:-20}"
SHUNT_CALIBRATION_MIN_TOKENS="${SHUNT_CALIBRATION_MIN_TOKENS:-500}"
# Acima desta taxa a amostra não mede a máquina: o Ollama reaproveita o
# prompt em cache quando a chamada repete o mesmo prefixo, e o tempo medido
# cai para quase zero. Uma amostra assim faria o timeout despencar justamente
# antes de um prompt novo e lento.
SHUNT_MAX_PLAUSIBLE_RATE="${SHUNT_MAX_PLAUSIBLE_RATE:-3000}"
# Folga sobre o tempo previsto, e piso/teto do timeout calculado.
SHUNT_TIMEOUT_SLACK="${SHUNT_TIMEOUT_SLACK:-300}"
SHUNT_TIMEOUT_MIN="${SHUNT_TIMEOUT_MIN:-60}"
# Teto: sem histórico a taxa assumida é bem baixa, e sem um teto a primeira
# chamada de uma parte grande esperaria vinte minutos por um Ollama travado.
SHUNT_TIMEOUT_MAX="${SHUNT_TIMEOUT_MAX:-600}"

# Versão gravada em cada linha do log, para comparar o efeito de uma mudança.
# Mesma fonte que os hooks usam: o manifesto do plugin.
shunt_version() {
  local root manifest v
  root="${CLAUDE_PLUGIN_ROOT:-$SHUNT_ROOT}"
  if [ -z "$root" ]; then
    # BASH_SOURCE não existe fora do bash; $0 cobre o caso de ser sourced.
    root=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd) || root=""
  fi
  manifest="$root/.claude-plugin/plugin.json"
  if [ -r "$manifest" ]; then
    v=$(jq -r '.version // empty' "$manifest" 2>/dev/null)
    [ -n "$v" ] && { printf '%s' "$v"; return; }
  fi
  v=$(basename "$root")
  case "$v" in [0-9]*) printf '%s' "$v" ;; *) printf 'dev' ;; esac
}
SHUNT_VERSION="${SHUNT_VERSION:-$(shunt_version)}"

# Totais acumulados pelas chamadas desta execução (para o log/stats).
SHUNT_PIN_TOTAL=0
SHUNT_POUT_TOTAL=0
SHUNT_DUR_TOTAL=0

SHUNT_TMPFILES=()
shunt_tmpfile() {
  local f
  f=$(mktemp) || return 1
  SHUNT_TMPFILES+=("$f")
  # shellcheck disable=SC2064
  trap 'rm -f "${SHUNT_TMPFILES[@]}"' EXIT
  printf -v "$1" '%s' "$f"
}

shunt_preflight() {
  local missing=""
  command -v jq   >/dev/null 2>&1 || missing="$missing jq"
  command -v curl >/dev/null 2>&1 || missing="$missing curl"
  if [ -n "$missing" ]; then
    echo "Error: missing required command(s):$missing" >&2
    return 1
  fi
  if ! curl -sf "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    echo "Error: Ollama não respondeu em $OLLAMA_HOST. Rode 'ollama serve'." >&2
    return 1
  fi
  return 0
}

# Linha no mesmo TSV dos hooks: ts, sessão, ferramenta, decisão, motivo,
# path, total, efetivo, versão, faixas, cobertura acumulada. As duas últimas
# não se aplicam a uma delegação, que lê o arquivo todo fora do contexto.
shunt_log_line() {
  local tool="$1" decision="$2" reason="$3" path="${4:--}" total="${5:-0}" eff="${6:-0}"
  printf '%s\t-\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t-\t0\n' \
    "$(date +%Y-%m-%dT%H:%M:%S)" \
    "$tool" "$decision" "$reason" "$path" "$total" "$eff" "$SHUNT_VERSION" \
    >> "$SHUNT_HOOK_LOG" 2>/dev/null
}

# Tokens que o Ollama aceita de prompt, na prática.
shunt_prompt_limit() {
  printf '%s' $(( SHUNT_NUM_CTX * SHUNT_PROMPT_FRACTION / 100 ))
}

# Bytes de conteúdo por parte, para que o prompt montado caiba no limite real.
shunt_chunk_budget() {
  local tokens bytes
  tokens=$(( $(shunt_prompt_limit) - SHUNT_PROMPT_RESERVE ))
  [ "$tokens" -lt 500 ] && tokens=500
  bytes=$(( tokens * SHUNT_BYTES_PER_TOKEN_FLOOR / 10 ))
  printf '%s' "$bytes"
}

# Tokens que um trecho deve ocupar, no máximo. Pelo piso da razão, então
# superestima em código, que é o lado seguro para dimensionar o timeout.
shunt_tokens_for_bytes() {
  printf '%s' $(( $1 * 10 / SHUNT_BYTES_PER_TOKEN_FLOOR ))
}

# E no mínimo. Pelo teto da razão, que é o lado seguro para acusar corte: só
# dispara quando o Ollama processou menos do que qualquer conteúdo renderia.
shunt_min_tokens_for_bytes() {
  printf '%s' $(( $1 * 10 / SHUNT_BYTES_PER_TOKEN_CEIL ))
}

# Taxa de processamento aprendida para este modelo, em tokens/s.
#   $1 percentil (20 = conservador, para timeout; 50 = típico, para estimar)
# As amostras trazem tokens, bytes e segundos de cada chamada real.
# Sem histórico suficiente, devolve SHUNT_FALLBACK_RATE.
shunt_rate() {
  local pct="${1:-20}"
  if [ ! -r "$SHUNT_CALIBRATION" ]; then
    printf '%s' "$SHUNT_FALLBACK_RATE"; return
  fi
  jq -r --arg m "$SHUNT_MODEL" --argjson pct "$pct" --argjson fb "$SHUNT_FALLBACK_RATE" '
    (.models[$m].samples // [])
    | map(select(.seconds > 0) | .tokens / .seconds) | sort
    | if length == 0 then $fb
      else .[((length - 1) * $pct / 100) | floor] end
    | floor | if . < 1 then 1 else . end
  ' "$SHUNT_CALIBRATION" 2>/dev/null || printf '%s' "$SHUNT_FALLBACK_RATE"
}

# Timeout para uma chamada de N tokens, derivado da taxa conservadora.
shunt_timeout_for() {
  local tokens="$1" rate calculated
  # Um valor explícito do usuário manda; o cálculo é só o padrão.
  if [ -n "${SHUNT_TIMEOUT_SECONDS:-}" ]; then
    printf '%s' "$SHUNT_TIMEOUT_SECONDS"; return
  fi
  rate="$(shunt_rate 20)"
  calculated=$(( tokens * SHUNT_TIMEOUT_SLACK / 100 / rate ))
  [ "$calculated" -lt "$SHUNT_TIMEOUT_MIN" ] && calculated="$SHUNT_TIMEOUT_MIN"
  [ "$calculated" -gt "$SHUNT_TIMEOUT_MAX" ] && calculated="$SHUNT_TIMEOUT_MAX"
  printf '%s' "$calculated"
}

# Registra uma amostra de tempo de parede por token de entrada.
#
# Usa total_duration de propósito, e não prompt_eval_duration: o que interessa
# ao timeout é quanto a chamada demora de fato, incluindo carregar o modelo e
# gerar a resposta. E descarta taxas implausíveis, que denunciam prompt em
# cache em vez de capacidade real da máquina.
shunt_record_sample() {
  local tokens="$1" nanos="$2" bytes="${3:-0}" tmp implied
  [ "$tokens" -lt "$SHUNT_CALIBRATION_MIN_TOKENS" ] && return 0
  [ "$nanos" -le 0 ] && return 0
  implied=$(( tokens * 1000000000 / nanos ))
  [ "$implied" -gt "$SHUNT_MAX_PLAUSIBLE_RATE" ] && return 0
  mkdir -p "$(dirname "$SHUNT_CALIBRATION")" 2>/dev/null || return 0
  [ -r "$SHUNT_CALIBRATION" ] || printf '{"version":1,"models":{}}' > "$SHUNT_CALIBRATION"
  tmp="$(mktemp)" || return 0
  # A conversão de nanossegundos fica no jq, e não no awk: um locale com
  # vírgula decimal (pt-BR entre eles) faz o awk emitir "37,000", que não é
  # JSON válido, e a calibração falharia em silêncio na máquina do usuário.
  if jq --arg m "$SHUNT_MODEL" \
        --argjson t "$tokens" \
        --argjson ns "$nanos" \
        --argjson b "$bytes" \
        --argjson keep "$SHUNT_CALIBRATION_SAMPLES" \
        --arg now "$(date +%Y-%m-%dT%H:%M:%S)" '
        .version = 1
        | .models[$m].samples = (((.models[$m].samples // [])
            + [{tokens: $t, bytes: $b,
                seconds: (($ns / 1000000000 * 1000 | round) / 1000)}])
            | .[-$keep:])
        | .models[$m].updated = $now
      ' "$SHUNT_CALIBRATION" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$SHUNT_CALIBRATION"
  else
    rm -f "$tmp"
  fi
}

# Uma rodada de chat, sem histórico, contra o modelo local.
#   $1 arquivo com o system prompt (o "mode")
#   $2 arquivo com a mensagem do usuário
#   $3 tokens esperados no prompt, para detectar truncamento (0 = não checar)
#   $4 timeout em segundos (opcional; padrão vem da calibração)
shunt_invoke() {
  local system_file="$1" message_file="$2" expected="${3:-0}" timeout="$4"
  local payload response text rc err
  if [ -z "$timeout" ]; then
    timeout="$(shunt_timeout_for "$expected")"
  fi

  payload=$(jq -n \
    --arg model "$SHUNT_MODEL" \
    --rawfile system "$system_file" \
    --rawfile message "$message_file" \
    --argjson temp "$SHUNT_TEMPERATURE" \
    --argjson ctx "$SHUNT_NUM_CTX" \
    --arg keep "$SHUNT_KEEP_ALIVE" \
    '{
      model: $model,
      stream: false,
      think: false,
      keep_alive: $keep,
      options: { temperature: $temp, num_ctx: $ctx },
      messages: [
        { role: "system", content: $system },
        { role: "user",   content: $message }
      ]
    }')

  # Payload via arquivo (--data-binary @-) para não esbarrar em ARG_MAX,
  # que era uma das limitações do plugin original.
  response=$(printf '%s' "$payload" | curl -sS --max-time "$timeout" \
    -H 'Content-Type: application/json' \
    --data-binary @- "$OLLAMA_HOST/api/chat")
  rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 28 ]; then
      echo "Error: Ollama não respondeu em ${timeout}s (taxa aprendida: $(shunt_rate 20) tok/s). Se a máquina estava ocupada, repetir costuma resolver; senão suba SHUNT_TIMEOUT_SLACK ou fixe SHUNT_TIMEOUT_SECONDS." >&2
    else
      echo "Error: chamada ao Ollama falhou (rc=$rc)." >&2
    fi
    shunt_log_line bulk-read error "curl-rc=$rc;timeout=$timeout"
    return 1
  fi

  if ! printf '%s' "$response" | jq -e . >/dev/null 2>&1; then
    echo "Error: resposta do Ollama não é JSON válido:" >&2
    printf '%s\n' "$response" >&2
    return 1
  fi

  err=$(printf '%s' "$response" | jq -r '.error // empty')
  if [ -n "$err" ]; then
    echo "Error: Ollama: $err" >&2
    shunt_log_line bulk-read error "ollama:$err"
    return 1
  fi

  text=$(printf '%s' "$response" | jq -r '.message.content // empty')
  if [ -z "$text" ]; then
    echo "Error: Ollama devolveu resposta vazia." >&2
    return 1
  fi

  # Métricas de uso no stderr, no espírito do "[shunt: ...]" original.
  local pin pout dur
  pin=$(printf '%s' "$response" | jq -r '.prompt_eval_count // 0')
  pout=$(printf '%s' "$response" | jq -r '.eval_count // 0')
  dur=$(printf '%s' "$response" | jq -r '((.total_duration // 0) / 1000000000 | floor)')
  local wall_nanos sent_bytes
  wall_nanos=$(printf '%s' "$response" | jq -r '.total_duration // 0')
  # Bytes enviados junto dos tokens contados: é o que permite medir a razão
  # bytes/token deste tokenizador em vez de estimá-la.
  sent_bytes=$(( $(wc -c < "$system_file") + $(wc -c < "$message_file") ))
  shunt_record_sample "$pin" "$wall_nanos" "$sent_bytes"
  SHUNT_PIN_TOTAL=$((SHUNT_PIN_TOTAL + pin))
  SHUNT_POUT_TOTAL=$((SHUNT_POUT_TOTAL + pout))
  SHUNT_DUR_TOTAL=$((SHUNT_DUR_TOTAL + dur))
  echo "[shunt: $pin tokens entrada | $pout saída | ${dur}s | $SHUNT_MODEL]" >&2
  # Truncamento silencioso: o prompt enviado tinha um tamanho conhecido, e o
  # Ollama relatou ter processado muito menos. A resposta cobriria só parte do
  # conteúdo, com a mesma confiança de sempre, então é descartada.
  if [ "$expected" -gt 0 ] && [ "$pin" -lt $(( expected * 70 / 100 )) ]; then
    echo "Error: o Ollama processou $pin tokens de um prompt de ~$expected; o resto foi descartado em silêncio (context shift do llama.cpp em num_ctx/2). A resposta cobriria só parte do conteúdo e foi rejeitada. Aumente SHUNT_NUM_CTX se houver RAM/VRAM, ou baixe SHUNT_PROMPT_FRACTION para gerar partes menores." >&2
    shunt_log_line bulk-read error "truncated;pin=$pin;expected=$expected"
    return 1
  fi

  printf '%s\n' "$text"
}
