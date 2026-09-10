"""Utilitários compartilhados pelos hooks do shunt-ollama.

Concentra o que antes estava duplicado nos dois hooks em bash: leitura do
input do hook, limiares, log de decisões, estado por sessão (faixas já lidas
de cada arquivo) e a emissão do JSON de deny no formato atual do Claude Code.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import shlex
import sys
import tempfile
import time
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

# Um pedido de leitura: (inicio, fim). fim None = até o fim do arquivo;
# inicio negativo = "últimas N linhas" (tail). Resolvido em resolve_range().
Request = Tuple[int, Optional[int]]
Range = Tuple[int, int]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() else default


# Leitura de edição: o trecho que o Claude abre para editar depois de
# consultar o modelo local.
EDIT_WINDOW = _env_int("SHUNT_EDIT_WINDOW", 80)
# Quantas leituras de janela por arquivo passam sem entrar no acumulado. A
# partir da seguinte elas contam: fatiar em pedaços de 80 linhas era o
# contorno que deixava mais da metade de um arquivo entrar no contexto.
EDIT_FREE = _env_int("SHUNT_EDIT_FREE", 1)
# Válvula de escape: leitura até este tamanho nunca conta e nunca é negada,
# para que editar um trecho já conhecido continue possível mesmo com o
# acumulado estourado.
ALWAYS_FREE = _env_int("SHUNT_ALWAYS_FREE", 25)
# Limiar de bloqueio. O piso de 2× a janela evita a faixa estreita que
# gerava negativas de 83 a 96 linhas com economia mínima.
_MIN_LINES_RAW = _env_int("SHUNT_MIN_LINES", 250)
MIN_LINES = max(_MIN_LINES_RAW, EDIT_WINDOW * 2)
MIN_LINES_ADJUSTED = MIN_LINES != _MIN_LINES_RAW
# Soma de linhas efetivas de vários arquivos num único comando.
MAX_TOTAL_LINES = _env_int("SHUNT_MAX_TOTAL_LINES", MIN_LINES * 3)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_CHECK_TTL = 120
# Estimativas para a mensagem de deny, medidas com gemma4:e4b em Apple
# Silicon. Servem para comparar custos, não para prometer prazo.
TOKENS_PER_LINE = 12
OLLAMA_TOKENS_PER_SEC = 380
NUM_CTX = _env_int("SHUNT_NUM_CTX", 32768)

LOG_PATH = os.environ.get("SHUNT_HOOK_LOG") or os.path.expanduser(
    "~/.claude/shunt.log"
)
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
BULK_READ = os.path.join(PLUGIN_ROOT, "scripts", "bulk-read")


def plugin_version() -> str:
    """Versão registrada em cada linha do log, para comparar o efeito de uma
    mudança sem depender de adivinhar pelo timestamp. Durante um update as
    duas versões convivem: sessões abertas seguem com os hooks antigos."""
    manifest = os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            version = json.load(fh).get("version")
        if version:
            return str(version)
    except (OSError, ValueError):
        pass
    # Instalado pelo cache de plugins, o diretório já é a versão.
    tail = os.path.basename(PLUGIN_ROOT)
    if tail and tail[0].isdigit():
        return tail
    return "dev"


VERSION = plugin_version()


# --------------------------------------------------------------------------
# Entrada e saída do hook
# --------------------------------------------------------------------------
def read_hook_input() -> dict:
    """Lê o JSON do hook; qualquer falha vira {} para nunca travar a ferramenta."""
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}


def allow() -> None:
    """Sem saída = permissão concedida."""
    sys.exit(0)


def deny(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


# --------------------------------------------------------------------------
# Log de decisões, TSV com 9 colunas:
#   ts, sessão, ferramenta, decisão, motivo, path, total, efetivo, versão
# Linhas de 8 colunas são de versões até a 0.3.0 e ainda são lidas pelo
# shunt-stats, que as agrupa como "<=0.3.0".
# --------------------------------------------------------------------------
def log(session: str, tool: str, decision: str, reason: str,
        path: str = "", total: int = 0, effective: int = 0) -> None:
    line = "\t".join([
        time.strftime("%Y-%m-%dT%H:%M:%S"), session or "-", tool, decision,
        reason, path or "-", str(total), str(effective), VERSION,
    ])
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Arquivos
# --------------------------------------------------------------------------
_line_cache: Dict[str, int] = {}


def count_lines(path: str) -> int:
    if path in _line_cache:
        return _line_cache[path]
    total = 0
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                total += block.count(b"\n")
    except OSError:
        total = 0
    _line_cache[path] = total
    return total


def is_regular_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.R_OK)


def estimate_delegation(paths: List[str]) -> Tuple[int, int, int]:
    """Custo de delegar ao modelo local: (partes, segundos, tokens enviados).

    Replica o orçamento do bulk-read: ~80% da janela menos reserva para
    resposta, e o `cat -n` que acrescenta cerca de 8 caracteres por linha.
    """
    budget_chars = max((NUM_CTX * 80 // 100 - 2000) * 4, 20000)
    chars = 0
    for path in paths:
        try:
            chars += os.path.getsize(path) + count_lines(path) * 8
        except OSError:
            continue
    if chars <= 0:
        return (1, 0, 0)
    tokens = chars // 4
    parts = max(1, math.ceil(chars / budget_chars))
    seconds = max(1, round(tokens / OLLAMA_TOKENS_PER_SEC))
    return (parts, seconds, tokens)


# --------------------------------------------------------------------------
# Estado por sessão: faixas já lidas de cada arquivo
# --------------------------------------------------------------------------
def state_path(session: str) -> str:
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir()
    safe = "".join(c for c in (session or "nosession") if c.isalnum() or c in "-_")
    return os.path.join(tmp, f"shunt-state-{safe}.json")


class State:
    """Estado com lock de arquivo; hooks podem rodar em paralelo."""

    def __init__(self, session: str):
        self.path = state_path(session)
        self.data: dict = {"files": {}, "windows": {}, "ollama": {}}
        self._fh = None

    def __enter__(self) -> "State":
        try:
            self._fh = open(self.path, "a+", encoding="utf-8")
            fcntl.flock(self._fh, fcntl.LOCK_EX)
            self._fh.seek(0)
            raw = self._fh.read()
            if raw:
                self.data = json.loads(raw)
            self.data.setdefault("files", {})
            self.data.setdefault("windows", {})
            self.data.setdefault("ollama", {})
        except (OSError, ValueError):
            self._fh = None
        return self

    def __exit__(self, *_exc) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(json.dumps(self.data))
            self._fh.flush()
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass

    def ranges(self, path: str) -> List[Range]:
        return [tuple(r) for r in self.data["files"].get(path, [])]

    def add_ranges(self, path: str, ranges: Iterable[Range]) -> None:
        merged = merge_ranges(list(self.ranges(path)) + list(ranges))
        self.data["files"][path] = [list(r) for r in merged]

    def windows_used(self, path: str) -> int:
        return int(self.data["windows"].get(path, 0))

    def mark_window(self, path: str) -> None:
        self.data["windows"][path] = self.windows_used(path) + 1

    def ollama_ok(self) -> bool:
        """Se o Ollama está fora, bloquear leitura só travaria o Claude."""
        if os.environ.get("SHUNT_ASSUME_OLLAMA", "") in ("1", "true"):
            return True
        info = self.data["ollama"]
        now = time.time()
        if now - info.get("checked_at", 0) < OLLAMA_CHECK_TTL:
            return bool(info.get("ok", True))
        ok = probe_ollama()
        self.data["ollama"] = {"ok": ok, "checked_at": now}
        return ok


def probe_ollama(timeout: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - qualquer falha = indisponível
        return False


# --------------------------------------------------------------------------
# Aritmética de faixas
# --------------------------------------------------------------------------
def merge_ranges(ranges: List[Range]) -> List[Range]:
    out: List[Range] = []
    for a, b in sorted(r for r in ranges if r[1] >= r[0]):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def coverage(ranges: List[Range]) -> int:
    return sum(b - a + 1 for a, b in merge_ranges(ranges))


def resolve_range(req: Request, total: int) -> Optional[Range]:
    """Converte um pedido (head/tail/faixa/completo) em faixa absoluta."""
    start, end = req
    if total <= 0:
        return None
    if start < 0:  # tail -N
        return (max(1, total + start + 1), total)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if end < start:
        return None
    return (start, end)


# --------------------------------------------------------------------------
# Decisão
# --------------------------------------------------------------------------
def bulk_read_command(paths: List[str]) -> str:
    return (f'{BULK_READ} --question "<sua pergunta específica>" '
            f'--paths {shlex.join(paths)}')


def decide(session: str, tool: str, requests: Dict[str, List[Request]]) -> None:
    """Aplica limiares, acumulado por sessão e total por comando. Nunca retorna
    sem encerrar o processo (allow/deny)."""
    if not requests:
        allow()

    with State(session) as state:
        if not state.ollama_ok():
            for path in requests:
                log(session, tool, "allow", "ollama-off", path)
            allow()

        denials: List[str] = []
        denied_paths: List[str] = []
        pending: Dict[str, List[Range]] = {}
        windows: List[str] = []
        grand_total = 0

        for path, reqs in requests.items():
            total = count_lines(path)
            ranges = [r for r in (resolve_range(q, total) for q in reqs) if r]
            effective = coverage(ranges)
            if effective == 0:
                continue

            # Trecho mínimo: sempre livre, para não travar edição pontual.
            if effective <= ALWAYS_FREE:
                log(session, tool, "allow", "always-free", path, total, effective)
                continue

            # Janela de edição: as primeiras EDIT_FREE leituras por arquivo
            # passam sem contar; as seguintes entram no acumulado.
            if effective <= EDIT_WINDOW and state.windows_used(path) < EDIT_FREE:
                windows.append(path)
                log(session, tool, "allow", "edit-window", path, total, effective)
                continue

            grand_total += effective
            if effective > MIN_LINES:
                denials.append(
                    f"{path}: {effective} de {total} linhas pedidas "
                    f"(limiar {MIN_LINES})."
                )
                denied_paths.append(path)
                log(session, tool, "deny", "single-read", path, total, effective)
                continue

            already = coverage(state.ranges(path))
            after = coverage(state.ranges(path) + ranges)
            if after > MIN_LINES:
                denials.append(
                    f"{path}: você já leu {already} linhas deste arquivo em "
                    f"fatias nesta sessão; com estas {effective} passaria de "
                    f"{MIN_LINES}. Fatiar não é a saída."
                )
                denied_paths.append(path)
                log(session, tool, "deny", "cumulative", path, total, effective)
                continue

            pending[path] = ranges

        if not denials and grand_total > MAX_TOTAL_LINES:
            denials.append(
                f"{grand_total} linhas somadas em {len(pending)} arquivos num só "
                f"comando (limite {MAX_TOTAL_LINES})."
            )
            denied_paths.extend(pending)
            for path in pending:
                log(session, tool, "deny", "multi-file", path,
                    count_lines(path), coverage(pending[path]))

        if denials:
            deny(build_deny_message(denials, denied_paths))

        for path in windows:
            state.mark_window(path)
        for path, ranges in pending.items():
            state.add_ranges(path, ranges)
            reason = ("window-counted" if coverage(ranges) <= EDIT_WINDOW
                      else "counted")
            log(session, tool, "allow", reason, path, count_lines(path),
                coverage(ranges))
    allow()


def build_deny_message(denials: List[str], paths: List[str]) -> str:
    """A negativa precisa competir com a saída mais fácil, que é desistir da
    informação. Por isso mostra o custo dos dois caminhos, não só a regra."""
    uniq = list(dict.fromkeys(paths))
    blocked_lines = sum(count_lines(p) for p in uniq)
    read_cost = blocked_lines * TOKENS_PER_LINE
    parts, seconds, sent = estimate_delegation(uniq)
    chunk_note = f", em {parts} partes" if parts > 1 else ""
    return (
        "shunt: leitura grande bloqueada. "
        + " ".join(denials)
        + f"\n\nCusto de ler direto: ~{read_cost} tokens do seu contexto, "
        "que ficam gastos até o fim da conversa."
        + f"\nCusto de delegar: 0 tokens do seu contexto e ~{seconds}s de "
        f"espera{chunk_note}. O modelo local processa ~{sent} tokens fora da "
        "sua janela e devolve bullets ancorados em path:Lini-Lfim:\n  "
        + bulk_read_command(uniq)
        + "\n\nMais barato ainda, quando serve: `grep -n` ou `rg` para "
        f"localizar um símbolo, ou Read com offset/limit de até {ALWAYS_FREE} "
        "linhas num trecho que você já conhece (esse tamanho nunca é "
        "bloqueado). Reler o arquivo em fatias não funciona: elas são somadas "
        "por sessão."
    )
