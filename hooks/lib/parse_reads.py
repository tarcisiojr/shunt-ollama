"""Extrai, de um comando shell, quais arquivos seriam despejados no contexto
e quantas linhas de cada um.

Substitui a regex `^(cat|head|tail|less|more) ` do hook original, que deixava
passar `cd X && cat f`, `sed -n`, `awk`, `2>/dev/null`, loops e o prefixo
`rtk`. A análise é léxica (shlex com pontuação), não executa nada.

Retorna um dicionário path -> lista de pedidos (inicio, fim) no formato de
shunt_common.Request, mais uma lista de notas explicando o que foi ignorado.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from typing import Dict, List, Optional, Tuple

Request = Tuple[int, Optional[int]]

FULL: Request = (1, None)
# Leitores que despejam o arquivo inteiro no stdout.
FULL_READERS = {"cat", "less", "more", "bat", "nl", "tac", "batcat"}
# Comandos que, depois de um pipe, deixam a saída passar quase inteira.
PASSTHROUGH = {"cat", "less", "more", "bat", "nl", "column", "fold", "fmt",
               "pr", "tee", "expand", "unexpand", "batcat"}
# Prefixos que não mudam o que é executado, incluindo palavras-chave de
# controle (`do cat $f`, `then cat f`) que abrem o corpo de loops e ifs.
WRAPPERS = {"sudo", "command", "builtin", "time", "nice", "nohup", "exec",
            "do", "then", "else", "{", "}", "!"}
SEPARATORS = {";", "&&", "||", "&", "\n"}
PIPES = {"|", "|&"}
UNKNOWN_RANGE_LINES = 80  # faixa por regex (/a/,/b/p): tamanho desconhecido

_SED_RANGE = re.compile(r"^(\d+),(\d+)p$")
_SED_LINE = re.compile(r"^(\d+)p$")
_SED_TO_END = re.compile(r"^(\d+),\$p$")
_SED_PLUS = re.compile(r"^(\d+),\+(\d+)p$")
_AWK_BETWEEN = re.compile(
    r"NR\s*>=?\s*(\d+)\s*&&\s*NR\s*<=?\s*(\d+)")
_AWK_EQ = re.compile(r"^\s*NR\s*==\s*(\d+)\s*$")
_AWK_LE = re.compile(r"^\s*NR\s*<=?\s*(\d+)\s*$")
_AWK_GE = re.compile(r"^\s*NR\s*>=?\s*(\d+)\s*$")
_AWK_PRINT_ALL = re.compile(r"^\s*(1|\{\s*print(\s*\$0)?\s*;?\s*\})\s*$")


def parse_reads(command: str, cwd: str) -> Tuple[Dict[str, List[Request]], List[str]]:
    notes: List[str] = []
    if "<<" in command:
        return {}, ["heredoc"]
    cleaned = _strip_stderr_redirects(command).replace("\n", " ; ")
    try:
        lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return {}, ["unbalanced-quotes"]

    reads: Dict[str, List[Request]] = {}
    for segment in _split(tokens, SEPARATORS):
        cwd, seg_reads = _analyze_segment(segment, cwd, notes)
        for path, reqs in seg_reads.items():
            reads.setdefault(path, []).extend(reqs)
    return reads, notes


# --------------------------------------------------------------------------
def _strip_stderr_redirects(command: str) -> str:
    command = re.sub(r"(?<!\S)2>&1", " ", command)
    command = re.sub(r"(?<!\S)2>>?\s*\S+", " ", command)
    command = re.sub(r"(?<!\S)&>>?\s*\S+", " > /dev/null ", command)
    return command


def _split(tokens: List[str], seps: set) -> List[List[str]]:
    out: List[List[str]] = [[]]
    for tok in tokens:
        if tok in seps:
            out.append([])
        elif tok in ("(", ")"):
            continue
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _analyze_segment(tokens: List[str], cwd: str,
                     notes: List[str]) -> Tuple[str, Dict[str, List[Request]]]:
    stages = _split(tokens, PIPES)
    if not stages:
        return cwd, {}

    first = _strip_wrappers(stages[0])
    if not first:
        return cwd, {}
    if first[0] in ("cd", "pushd"):
        return _change_dir(first, cwd), {}

    first, out_to_file, in_file = _extract_redirects(first)
    if out_to_file:
        return cwd, {}
    if in_file:
        first = first + [in_file]

    reads = _reader_requests(first, cwd, notes)
    if not reads:
        return cwd, {}

    # Estágios seguintes: filtro zera, limitador recorta, passthrough mantém.
    for stage in stages[1:]:
        stage = _strip_wrappers(stage)
        stage, out_to_file, _ = _extract_redirects(stage)
        if out_to_file or not stage:
            return cwd, {}
        name = os.path.basename(stage[0])
        if name in PASSTHROUGH:
            continue
        limit = _stage_limit(name, stage)
        if limit is None:
            return cwd, {}  # grep/wc/jq/awk/...: filtro, nada inteiro entra
        reads = {p: [_clip(r, limit) for r in reqs] for p, reqs in reads.items()}
    return cwd, reads


def _strip_wrappers(tokens: List[str]) -> List[str]:
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
        elif tok == "env":
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-")
                                       or "=" in tokens[i]):
                i += 1
        elif tok == "rtk":
            # O hook do RTK reescreve `cat f` para `rtk read f`.
            i += 1
            if i < len(tokens) and tokens[i] == "read":
                return ["cat"] + tokens[i + 1:]
        else:
            break
    return tokens[i:]


def _change_dir(tokens: List[str], cwd: str) -> str:
    if len(tokens) < 2 or tokens[1] == "-":
        return os.path.expanduser("~") if len(tokens) < 2 else cwd
    target = _expand(tokens[1])
    if target is None:
        return cwd
    return os.path.normpath(os.path.join(cwd, target))


def _extract_redirects(tokens: List[str]) -> Tuple[List[str], bool, Optional[str]]:
    out: List[str] = []
    out_to_file = False
    in_file: Optional[str] = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (">", ">>", ">&", ">|"):
            out_to_file = True
            i += 2
        elif tok == "<":
            in_file = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 2
        else:
            out.append(tok)
            i += 1
    return out, out_to_file, in_file


def _expand(tok: str) -> Optional[str]:
    """~ e $VAR do ambiente do hook; o que sobrar com $ é irresolúvel."""
    if tok.startswith("$(") or tok.startswith("`"):
        return None
    tok = os.path.expandvars(os.path.expanduser(tok))
    return None if "$" in tok else tok


def _files(args: List[str], cwd: str, notes: List[str]) -> List[str]:
    paths: List[str] = []
    for arg in args:
        if arg == "-" or arg.startswith("-"):
            continue
        expanded = _expand(arg)
        if expanded is None:
            notes.append(f"unresolved:{arg}")
            continue
        full = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        candidates = glob.glob(full) if any(c in full for c in "*?[") else [full]
        for cand in candidates:
            cand = os.path.normpath(cand)
            if os.path.isfile(cand):
                paths.append(cand)
    return paths


def _int_or_none(text: str) -> Optional[int]:
    return int(text) if text.isdigit() else None


def _head_tail_count(tokens: List[str], default: int = 10
                     ) -> Tuple[Optional[Request], List[str]]:
    """Interpreta flags de head/tail. Devolve (pedido, args restantes)."""
    name = os.path.basename(tokens[0])
    count: Optional[int] = default
    from_line: Optional[int] = None
    full = False
    rest: List[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-n", "--lines"):
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
        elif tok.startswith("--lines="):
            val = tok.split("=", 1)[1]
            i += 1
        elif re.match(r"^-n\S+$", tok):
            val = tok[2:]
            i += 1
        elif re.match(r"^-\d+$", tok):
            val = tok[1:]
            i += 1
        elif tok in ("-c", "--bytes") or tok.startswith("-c"):
            raw = tok[2:] or (tokens[i + 1] if i + 1 < len(tokens) else "")
            i += 1 if tok[2:] else 2
            n = _int_or_none(raw.lstrip("+-"))
            count = max(1, (n or 0) // 50)
            continue
        elif tok.startswith("-"):
            i += 1
            continue
        else:
            rest.append(tok)
            i += 1
            continue
        if val.startswith("+") and name == "tail":
            from_line = _int_or_none(val[1:])
        elif val.startswith("-") and name == "head":
            full = True
        else:
            count = _int_or_none(val.lstrip("+"))
    if full or count is None:
        return FULL, rest
    if name == "head":
        return (1, count), rest
    if from_line is not None:
        return (from_line, None), rest
    return (-count, None), rest


def _sed_requests(tokens: List[str]) -> Tuple[List[Request], List[str]]:
    quiet = False
    scripts: List[str] = []
    rest: List[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-n", "--quiet", "--silent"):
            quiet = True
        elif tok.startswith("-i") or tok == "--in-place":
            return [], []  # edição, não leitura
        elif tok in ("-e", "--expression"):
            scripts.append(tokens[i + 1] if i + 1 < len(tokens) else "")
            i += 1
        elif tok in ("-f", "--file"):
            i += 1
        elif re.match(r"^-[nErsz]+$", tok):
            quiet = quiet or "n" in tok
        elif tok.startswith("-"):
            pass
        elif not scripts:
            scripts.append(tok)
        else:
            rest.append(tok)
        i += 1
    if not quiet:
        return [FULL], rest
    reqs: List[Request] = []
    for cmd in re.split(r"[;\n]", " ".join(scripts)):
        cmd = cmd.strip()
        if not cmd.endswith("p"):
            continue
        if m := _SED_RANGE.match(cmd):
            reqs.append((int(m.group(1)), int(m.group(2))))
        elif m := _SED_LINE.match(cmd):
            reqs.append((int(m.group(1)), int(m.group(1))))
        elif m := _SED_TO_END.match(cmd):
            reqs.append((int(m.group(1)), None))
        elif m := _SED_PLUS.match(cmd):
            start = int(m.group(1))
            reqs.append((start, start + int(m.group(2))))
        elif cmd == "$p":
            reqs.append((-1, None))
        else:
            reqs.append((1, UNKNOWN_RANGE_LINES))
    return reqs, rest


def _awk_requests(tokens: List[str]) -> Tuple[List[Request], List[str]]:
    program: Optional[str] = None
    rest: List[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-F", "-v", "-f"):
            if tok == "-f":
                return [], []
            i += 1
        elif tok.startswith("-"):
            pass
        elif program is None:
            program = tok
        else:
            rest.append(tok)
        i += 1
    if program is None:
        return [], []
    if _AWK_PRINT_ALL.match(program):
        return [FULL], rest
    cond = program.split("{")[0]
    if m := _AWK_BETWEEN.search(cond):
        return [(int(m.group(1)), int(m.group(2)))], rest
    if m := _AWK_EQ.match(cond):
        return [(int(m.group(1)), int(m.group(1)))], rest
    if m := _AWK_LE.match(cond):
        return [(1, int(m.group(1)))], rest
    if m := _AWK_GE.match(cond):
        return [(int(m.group(1)), None)], rest
    if re.match(r"^\s*/.*/\s*,\s*/.*/\s*$", cond):
        return [(1, UNKNOWN_RANGE_LINES)], rest
    return [], []  # awk como processador (campos, somas): não é leitura bruta


def _reader_requests(tokens: List[str], cwd: str,
                     notes: List[str]) -> Dict[str, List[Request]]:
    name = os.path.basename(tokens[0])
    if name in FULL_READERS:
        reqs, args = [FULL], tokens[1:]
    elif name in ("head", "tail"):
        req, args = _head_tail_count(tokens)
        reqs = [req]
    elif name == "sed":
        reqs, args = _sed_requests(tokens)
    elif name in ("awk", "gawk", "mawk"):
        reqs, args = _awk_requests(tokens)
    else:
        return {}
    if not reqs:
        return {}
    files = _files(args, cwd, notes)
    return {path: list(reqs) for path in files}


def _stage_limit(name: str, stage: List[str]) -> Optional[int]:
    """Quantas linhas um estágio de pipe deixa passar; None = filtro."""
    if name in ("head", "tail"):
        req, _ = _head_tail_count(stage)
        if req == FULL:
            return 10 ** 9
        start, end = req
        if start < 0:
            return -start
        return (end - start + 1) if end else 10 ** 9
    if name == "sed":
        reqs, _ = _sed_requests(stage)
        if reqs == [FULL]:
            return 10 ** 9
        return sum(_length(r) for r in reqs) or None
    return None


def _length(req: Request) -> int:
    start, end = req
    if start < 0:
        return -start
    return (end - start + 1) if end else 10 ** 9


def _clip(req: Request, limit: int) -> Request:
    start, end = req
    if start < 0:
        return (-min(-start, limit), None)
    stop = start + limit - 1
    return (start, stop if end is None else min(end, stop))
