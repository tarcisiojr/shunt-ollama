"""Utilitários compartilhados pelos hooks do shunt-ollama.

Concentra leitura do input do hook, limiares, log de decisões, estado por
sessão (faixas já lidas de cada arquivo) e a emissão do JSON de deny no
formato atual do Claude Code.

Desenho da decisão: o limiar responde duas perguntas. Pelo tamanho total do
arquivo ele diz se o plugin se aplica (não vale delegar um arquivo pequeno);
quando se aplica, ele vira o orçamento daquele arquivo na sessão. Toda leitura
entra no orçamento. Esgotado o orçamento, resta um pequeno saldo para leituras
de edição.

A unidade é BYTE, não linha, desde a 0.9.0. Linha parecia equivalente a custo
e não é: medido em 11.692 arquivos, 3,7% deles passavam pelo limiar de linhas
sendo caros em tokens, somando 1,2 milhão de tokens. O pior caso era um JSON
de uma única linha com 131 mil tokens, que o critério por linha nem olhava. O
erro é assimétrico: bloquear um arquivo barato desperdiça uma rodada, deixar
passar um denso desperdiça a conversa.

Linha continua sendo a unidade de apresentação, porque é o que `offset`/`limit`
e `sed -n` usam, e é nela que a resposta do modelo local ancora.

A proteção é proporcionalmente mais fraca em arquivos pouco acima do limiar. O
ganho real está nos grandes, onde o orçamento é uma fração pequena do total.
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


# Bytes por linha na mediana do corpus medido. Serve só para converter um
# limiar herdado em linhas, quando o usuário configurou o antigo.
BYTES_PER_LINE = 36
# Maior leitura de edição, e teto de cada leitura coberta pelo saldo de escape.
EDIT_BYTES = _env_int("SHUNT_EDIT_BYTES",
                      _env_int("SHUNT_EDIT_WINDOW", 80) * BYTES_PER_LINE)
# Limiar: arquivo com até tantos bytes fica fora do alcance do plugin; acima
# disso, este é o orçamento por sessão. O piso de 2× a janela de edição evita
# uma faixa contável estreita demais.
_MIN_BYTES_RAW = _env_int("SHUNT_MIN_BYTES",
                          _env_int("SHUNT_MIN_LINES", 180) * BYTES_PER_LINE)
MIN_BYTES = max(_MIN_BYTES_RAW, EDIT_BYTES * 2)
MIN_BYTES_ADJUSTED = MIN_BYTES != _MIN_BYTES_RAW
# Saldo liberado depois de o orçamento estourar, para que editar um trecho
# apontado pelo modelo local continue possível. É medido em bytes, e não em
# número de leituras: contar leituras permitia três cheias e devolvia arquivos
# inteiros ao contexto.
ESCAPE_BYTES = _env_int("SHUNT_ESCAPE_BYTES", EDIT_BYTES)
# Soma de bytes efetivos de vários arquivos num único comando.
MAX_TOTAL_BYTES = _env_int("SHUNT_MAX_TOTAL_BYTES", MIN_BYTES * 3)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_CHECK_TTL = 120
NUM_CTX = _env_int("SHUNT_NUM_CTX", 32768)
# Bytes por token. 3,0 é a média medida no tokenizador do gemma sobre shell,
# Go, markdown em português e JSON; a estimativa anterior de 4,0 subestimava
# o custo em 30%, e em JSON denso (2,1 b/token) em quase 90%. Também é
# aprendido por medição: ver bytes_per_token().
FALLBACK_BYTES_PER_TOKEN = 3.0
# Velocidade do modelo depende da máquina, não do plugin, então não há número
# a cravar aqui: o bulk-read registra tokens e duração de cada chamada e esta
# é a mesma fonte que o timeout usa. Sem histórico, assume-se uma taxa baixa.
CALIBRATION_PATH = os.environ.get("SHUNT_CALIBRATION") or os.path.expanduser(
    "~/.claude/shunt-calibration.json")
FALLBACK_RATE = _env_int("SHUNT_FALLBACK_RATE", 40)
MODEL = os.environ.get("SHUNT_MODEL", "gemma4:e4b")


def _samples() -> List[dict]:
    try:
        with open(CALIBRATION_PATH, encoding="utf-8") as fh:
            return json.load(fh)["models"][MODEL]["samples"]
    except (OSError, ValueError, KeyError):
        return []


def learned_rate(percentile: int = 50) -> int:
    """Tokens por segundo medidos neste hardware para o modelo em uso."""
    rates = sorted(s["tokens"] / s["seconds"] for s in _samples()
                   if s.get("seconds", 0) > 0 and s.get("tokens"))
    if not rates:
        return FALLBACK_RATE
    return max(1, int(rates[int((len(rates) - 1) * percentile / 100)]))


def bytes_per_token() -> float:
    """Bytes por token do tokenizador em uso, medido em vez de estimado.

    A razão varia de 2,1 em JSON denso a 3,7 em Python gerado, e muda com o
    modelo. Usa a mediana das amostras para não deixar um arquivo atípico
    distorcer o limiar."""
    ratios = sorted(s["bytes"] / s["tokens"] for s in _samples()
                    if s.get("tokens") and s.get("bytes"))
    if not ratios:
        return FALLBACK_BYTES_PER_TOKEN
    return max(1.0, ratios[len(ratios) // 2])


def as_tokens(size_bytes: int) -> int:
    """Custo de contexto de um trecho, na moeda que importa."""
    return int(size_bytes / bytes_per_token())


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
# Log de decisões, TSV com 13 colunas:
#   ts, sessão, ferramenta, decisão, motivo, path, linhas totais,
#   linhas da leitura, versão, faixas, bytes cobertos na sessão,
#   bytes totais do arquivo, bytes da leitura
# Linha fica para leitura humana e para casar com offset/limit; byte é o que
# a decisão usa. Formatos de 8, 9 e 11 colunas vêm de versões anteriores e o
# shunt-stats ainda os lê.
# --------------------------------------------------------------------------
def format_ranges(ranges: Iterable[Range]) -> str:
    out = ",".join(f"{a}-{b}" for a, b in merge_ranges(list(ranges)))
    return out or "-"


def log(session: str, tool: str, decision: str, reason: str,
        path: str = "", total: int = 0, effective: int = 0,
        ranges: Optional[Iterable[Range]] = None, covered: int = 0,
        total_bytes: int = 0, read_bytes: int = 0) -> None:
    line = "\t".join([
        time.strftime("%Y-%m-%dT%H:%M:%S"), session or "-", tool, decision,
        reason, path or "-", str(total), str(effective), VERSION,
        format_ranges(ranges or []), str(covered),
        str(total_bytes), str(read_bytes),
    ])
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Arquivos
# --------------------------------------------------------------------------
_stats_cache: Dict[str, Tuple[int, int, bool]] = {}


def file_stats(path: str) -> Tuple[int, int, bool]:
    """(linhas, bytes, é binário) num único passe, com cache por processo."""
    if path in _stats_cache:
        return _stats_cache[path]
    lines = 0
    size = 0
    binary = False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            first = True
            for block in iter(lambda: fh.read(1 << 20), b""):
                if first:
                    # NUL nos primeiros bytes: dados binários. Contar "linhas"
                    # neles produz números sem sentido (um PNG do corpus dava
                    # 405 bytes por linha).
                    binary = b"\x00" in block[:8192]
                    first = False
                lines += block.count(b"\n")
    except OSError:
        return (0, 0, False)
    # Um arquivo sem quebra de linha no fim tem uma linha, não zero. JSON
    # minificado cai exatamente aqui, e zero linhas o tornaria invisível.
    if size > 0 and lines == 0:
        lines = 1
    _stats_cache[path] = (lines, size, binary)
    return _stats_cache[path]


def count_lines(path: str) -> int:
    return file_stats(path)[0]


def ranges_bytes(path: str, ranges: List[Range], total_lines: int,
                 total_bytes: int) -> int:
    """Bytes que as faixas de linha pedidas realmente trazem.

    Uma faixa é dada em linhas porque é o que `offset`/`limit` e `sed -n`
    aceitam, mas o custo de contexto está nos bytes, e as duas coisas só
    coincidem em arquivo homogêneo."""
    if not ranges:
        return 0
    if coverage(ranges) >= total_lines:
        return total_bytes
    wanted = merge_ranges(ranges)
    last = wanted[-1][1]
    total = 0
    try:
        with open(path, "rb") as fh:
            for number, raw in enumerate(fh, start=1):
                if number > last:
                    break
                for start, end in wanted:
                    if start <= number <= end:
                        total += len(raw)
                        break
    except OSError:
        # Sem conseguir medir, assume o pior: a média do arquivo.
        per_line = (total_bytes / total_lines) if total_lines else 0
        return int(coverage(ranges) * per_line)
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
    tokens = as_tokens(chars)
    parts = max(1, math.ceil(chars / budget_chars))
    seconds = max(1, round(tokens / learned_rate(50)))
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
        self.data: dict = {"files": {}, "escapes": {}, "ollama": {}}
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
            self.data.setdefault("escapes", {})
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

    def add_ranges(self, path: str, ranges: Iterable[Range]) -> List[Range]:
        merged = merge_ranges(list(self.ranges(path)) + list(ranges))
        self.data["files"][path] = [list(r) for r in merged]
        return merged

    def escape_bytes(self, path: str) -> int:
        return int(self.data["escapes"].get(path, 0))

    def add_escape_bytes(self, path: str, size: int) -> None:
        self.data["escapes"][path] = self.escape_bytes(path) + size

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
    """Aplica o orçamento por arquivo, o saldo de escape e o total por comando.
    Tudo em bytes. Nunca retorna sem encerrar o processo (allow/deny)."""
    if not requests:
        allow()

    with State(session) as state:
        if not state.ollama_ok():
            for path in requests:
                log(session, tool, "allow", "ollama-off", path)
            allow()

        denials: List[str] = []
        denied_paths: List[str] = []
        # path -> (faixas, bytes da leitura, motivo, consome saldo de escape)
        pending: Dict[str, Tuple[List[Range], int, str, bool]] = {}
        grand_total = 0

        for path, reqs in requests.items():
            total_lines, total_bytes, binary = file_stats(path)
            if binary:
                log(session, tool, "allow", "binary", path, total_lines, 0)
                continue
            ranges = [r for r in (resolve_range(q, total_lines) for q in reqs) if r]
            lines = coverage(ranges)
            if lines == 0:
                continue
            size = ranges_bytes(path, ranges, total_lines, total_bytes)
            if size == 0:
                continue

            # Arquivo pequeno: delegar custa mais do que ler. O plugin não se
            # aplica, e por isso nem entra no orçamento.
            if total_bytes <= MIN_BYTES:
                log(session, tool, "allow", "small-file", path, total_lines,
                    lines, ranges, size, total_bytes, size)
                continue

            grand_total += size
            already = state.ranges(path)
            spent = ranges_bytes(path, already, total_lines, total_bytes)
            after_ranges = merge_ranges(already + ranges)
            after = ranges_bytes(path, after_ranges, total_lines, total_bytes)

            if after <= MIN_BYTES:
                pending[path] = (ranges, size, "counted", False)
                continue

            # Orçamento estourado. Uma leitura de edição ainda passa, mas
            # apenas enquanto houver saldo: é o que separa editar um trecho de
            # reconstruir o arquivo inteiro em pedaços.
            escaped = state.escape_bytes(path)
            if size <= EDIT_BYTES and escaped + size <= ESCAPE_BYTES:
                pending[path] = (ranges, size, "escape", True)
                continue

            if size > MIN_BYTES:
                reason, detail = "single-read", (
                    f"{path}: a leitura pedida traz {as_tokens(size)} tokens "
                    f"({lines} de {total_lines} linhas), acima do orçamento de "
                    f"{as_tokens(MIN_BYTES)} tokens deste arquivo."
                )
            elif size <= EDIT_BYTES:
                reason, detail = "escape-exhausted", (
                    f"{path}: o arquivo já rendeu {as_tokens(spent)} tokens "
                    "nesta sessão e o saldo para leituras de edição também já "
                    "foi usado."
                )
            else:
                reason, detail = "cumulative", (
                    f"{path}: você já leu {as_tokens(spent)} tokens deste "
                    f"arquivo nesta sessão; com estes {as_tokens(size)} "
                    f"passaria do orçamento de {as_tokens(MIN_BYTES)} tokens."
                )
            denials.append(detail)
            denied_paths.append(path)
            log(session, tool, "deny", reason, path, total_lines, lines,
                ranges, spent, total_bytes, size)

        if not denials and grand_total > MAX_TOTAL_BYTES:
            denials.append(
                f"{as_tokens(grand_total)} tokens somados em {len(pending)} "
                f"arquivos num só comando, acima do limite de "
                f"{as_tokens(MAX_TOTAL_BYTES)}."
            )
            denied_paths.extend(pending)
            for path, (ranges, size, _, _) in pending.items():
                total_lines, total_bytes, _ = file_stats(path)
                log(session, tool, "deny", "multi-file", path, total_lines,
                    coverage(ranges), ranges, 0, total_bytes, size)

        if denials:
            deny(build_deny_message(denials, denied_paths))

        for path, (ranges, size, reason, consumes) in pending.items():
            if consumes:
                state.add_escape_bytes(path, size)
            total_lines, total_bytes, _ = file_stats(path)
            merged = state.add_ranges(path, ranges)
            covered = ranges_bytes(path, merged, total_lines, total_bytes)
            log(session, tool, "allow", reason, path, total_lines,
                coverage(ranges), ranges, covered, total_bytes, size)
    allow()


def build_deny_message(denials: List[str], paths: List[str]) -> str:
    """A negativa precisa competir com a saída mais fácil, que é desistir da
    informação. Por isso mostra o custo dos dois caminhos, não só a regra.

    Deliberadamente não lista os tamanhos que passariam: publicar os limites
    transforma a regra num mapa de contorno."""
    uniq = list(dict.fromkeys(paths))
    read_cost = as_tokens(sum(file_stats(p)[1] for p in uniq))
    parts, seconds, sent = estimate_delegation(uniq)
    chunk_note = f", em {parts} partes" if parts > 1 else ""
    return (
        "shunt: leitura grande bloqueada. "
        + " ".join(denials)
        + f"\n\nCusto de ler o arquivo inteiro: ~{read_cost} tokens do seu "
        "contexto, que ficam gastos até o fim da conversa."
        + f"\nCusto de delegar: 0 tokens do seu contexto e ~{seconds}s de "
        f"espera{chunk_note}. O modelo local processa ~{sent} tokens fora da "
        "sua janela e devolve linhas ancoradas em path:Lini-Lfim:\n  "
        + bulk_read_command(uniq)
        + "\n\nMais barato ainda, quando serve: `grep -n` ou `rg` para "
        "localizar um símbolo. Ler o arquivo em pedaços não é alternativa: as "
        "faixas são somadas por sessão e o orçamento é do arquivo, não da "
        "leitura. E o que conta é o tamanho em bytes, não o número de linhas: "
        "poucas linhas densas custam mais que muitas linhas curtas."
    )
