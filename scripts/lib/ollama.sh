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
# Janela de contexto: gemma4 aceita bem mais, mas o Ollama sobe com 4096 por
# padrão. 32768 cobre um arquivo de ~4000 linhas com folga. Ajuste conforme
# a RAM/VRAM da sua máquina. O bulk-read fatia o que não couber.
SHUNT_NUM_CTX="${SHUNT_NUM_CTX:-32768}"
SHUNT_TIMEOUT_SECONDS="${SHUNT_TIMEOUT_SECONDS:-180}"
# Mantém o modelo carregado entre chamadas; sem isso cada delegação paga o
# cold start de vários segundos.
SHUNT_KEEP_ALIVE="${SHUNT_KEEP_ALIVE:-30m}"
SHUNT_HOOK_LOG="${SHUNT_HOOK_LOG:-$HOME/.claude/shunt.log}"

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

# Linha no mesmo TSV dos hooks: ts, sessão, ferramenta, decisão, motivo, path, total, efetivo.
shunt_log_line() {
  local tool="$1" decision="$2" reason="$3" path="${4:--}" total="${5:-0}" eff="${6:-0}"
  printf '%s\t-\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S)" \
    "$tool" "$decision" "$reason" "$path" "$total" "$eff" >> "$SHUNT_HOOK_LOG" 2>/dev/null
}

# Uma rodada de chat, sem histórico, contra o modelo local.
#   $1 arquivo com o system prompt (o "mode")
#   $2 arquivo com a mensagem do usuário
shunt_invoke() {
  local system_file="$1" message_file="$2"
  local payload response text rc err

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
  response=$(printf '%s' "$payload" | curl -sS --max-time "$SHUNT_TIMEOUT_SECONDS" \
    -H 'Content-Type: application/json' \
    --data-binary @- "$OLLAMA_HOST/api/chat")
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "Error: chamada ao Ollama falhou (rc=$rc). Se foi timeout, aumente SHUNT_TIMEOUT_SECONDS ou divida os arquivos." >&2
    shunt_log_line bulk-read error "curl-rc=$rc"
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
  SHUNT_PIN_TOTAL=$((SHUNT_PIN_TOTAL + pin))
  SHUNT_POUT_TOTAL=$((SHUNT_POUT_TOTAL + pout))
  SHUNT_DUR_TOTAL=$((SHUNT_DUR_TOTAL + dur))
  echo "[shunt: $pin tokens entrada | $pout saída | ${dur}s | $SHUNT_MODEL]" >&2
  # O Ollama trunca em silêncio quando o prompt encosta em num_ctx.
  if [ "$pin" -ge $((SHUNT_NUM_CTX - 256)) ]; then
    echo "[shunt: AVISO — prompt ocupou $pin de $SHUNT_NUM_CTX tokens; parte do arquivo pode ter sido truncada. Aumente SHUNT_NUM_CTX.]" >&2
  fi

  printf '%s\n' "$text"
}
