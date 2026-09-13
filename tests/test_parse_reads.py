"""Testes do parser de leituras e da decisão dos hooks.

Rode com: python3 -m unittest discover -s tests
Os comandos abaixo vieram de sessões reais em que os hooks falharam.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks", "lib"))

from parse_reads import parse_reads  # noqa: E402
import shunt_common as sc  # noqa: E402


def make_file(directory: str, name: str, lines: int) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(f"linha {i}\n" for i in range(1, lines + 1))
    return path


class ParseReadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.big = make_file(self.dir, "big.md", 500)
        self.other = make_file(self.dir, "other.md", 300)
        os.makedirs(os.path.join(self.dir, "sub"))
        self.sub = make_file(os.path.join(self.dir, "sub"), "rel.md", 200)

    def tearDown(self):
        self.tmp.cleanup()

    def reads(self, cmd, cwd=None):
        return parse_reads(cmd, cwd or self.dir)[0]

    def test_plain_cat(self):
        self.assertEqual(self.reads(f"cat {self.big}"), {self.big: [(1, None)]})

    def test_cd_then_relative_cat(self):
        """O `cd X && cat f` que escapava da regex original."""
        reads = self.reads(f"cd {self.dir}/sub && cat rel.md", cwd="/")
        self.assertEqual(reads, {self.sub: [(1, None)]})

    def test_stderr_redirect_does_not_hide(self):
        """O `2>/dev/null` fazia o hook antigo desistir da análise."""
        self.assertIn(self.big, self.reads(f"cat {self.big} 2>/dev/null"))
        self.assertIn(self.big, self.reads(f"cat {self.big} 2>&1"))

    def test_stdout_to_file_is_ignored(self):
        """Redirecionar para arquivo não é leitura para o contexto."""
        self.assertEqual(self.reads(f"cat {self.big} > /tmp/x"), {})

    def test_filtering_pipe_is_ignored(self):
        self.assertEqual(self.reads(f"cat {self.big} | grep linha"), {})
        self.assertEqual(self.reads(f"grep -n foo {self.big} | head -5"), {})
        self.assertEqual(self.reads(f"wc -l < {self.big}"), {})

    def test_head_in_pipe_caps_lines(self):
        self.assertEqual(self.reads(f"cat {self.big} | head -40"),
                         {self.big: [(1, 40)]})

    def test_sed_ranges(self):
        self.assertEqual(self.reads(f"sed -n '30,120p' {self.big}"),
                         {self.big: [(30, 120)]})
        self.assertEqual(self.reads(f"sed -n 1,50p < {self.big}"),
                         {self.big: [(1, 50)]})
        self.assertEqual(self.reads(f"sed -n '1,5p;10,20p' {self.big}"),
                         {self.big: [(1, 5), (10, 20)]})
        self.assertEqual(self.reads(f"sed -n '/^## A/,/^## B/p' {self.big}"),
                         {self.big: [(1, 80)]})
        self.assertEqual(self.reads(f"sed -i 's/a/b/' {self.big}"), {})
        self.assertEqual(self.reads(f"sed 's/a/b/' {self.big}"),
                         {self.big: [(1, None)]})

    def test_head_and_tail_flag_variants(self):
        for cmd in (f"head -150 {self.big}", f"head -n 150 {self.big}",
                    f"head -n150 {self.big}", f"head --lines=150 {self.big}"):
            self.assertEqual(self.reads(cmd), {self.big: [(1, 150)]}, cmd)
        self.assertEqual(self.reads(f"tail -20 {self.big}"),
                         {self.big: [(-20, None)]})
        self.assertEqual(self.reads(f"tail -n +5 {self.big}"),
                         {self.big: [(5, None)]})
        self.assertEqual(self.reads(f"head -c 5000 {self.big}"),
                         {self.big: [(1, 100)]})

    def test_awk_programs(self):
        self.assertEqual(self.reads(f"awk 'NR>=10 && NR<=50' {self.big}"),
                         {self.big: [(10, 50)]})
        self.assertEqual(self.reads(f"awk '{{print}}' {self.big}"),
                         {self.big: [(1, None)]})
        self.assertEqual(self.reads(f"awk '{{print $1}}' {self.big}"), {})
        self.assertEqual(self.reads(f"awk '/^## X/,0' {self.big} | head -220"), {})

    def test_cli_proxy_and_wrappers(self):
        """Proxies de CLI reescrevem `cat` antes do hook enxergar."""
        self.assertEqual(self.reads(f"rtk read {self.big}"),
                         {self.big: [(1, None)]})
        self.assertEqual(self.reads(f"FOO=1 sudo cat -n {self.big}"),
                         {self.big: [(1, None)]})

    def test_command_chaining(self):
        reads = self.reads(f"echo x && cat {self.big}; ls")
        self.assertEqual(reads, {self.big: [(1, None)]})
        reads = self.reads(f"ls\ncat {self.other}")
        self.assertEqual(reads, {self.other: [(1, None)]})
        reads = self.reads(f"cat {self.big} {self.other}")
        self.assertEqual(set(reads), {self.big, self.other})

    def test_glob_expansion(self):
        reads = self.reads(f"cat {self.dir}/*.md")
        self.assertEqual(set(reads), {self.big, self.other})

    def test_variables_and_loops_are_skips(self):
        """Ponto cego conhecido: vira nota no log, não aprovação silenciosa."""
        reads, notes = parse_reads('for f in a b; do cat "$f"; done', self.dir)
        self.assertEqual(reads, {})
        self.assertTrue(any(n.startswith("unresolved") for n in notes))
        reads, notes = parse_reads("python3 - <<'EOF'\nprint(1)\nEOF", self.dir)
        self.assertEqual((reads, notes), ({}, ["heredoc"]))

    def test_missing_file(self):
        self.assertEqual(self.reads("cat /nao/existe.md"), {})


class RangeMathTest(unittest.TestCase):
    def test_merge_and_coverage(self):
        self.assertEqual(sc.merge_ranges([(1, 5), (4, 10), (12, 12)]),
                         [(1, 10), (12, 12)])
        self.assertEqual(sc.coverage([(1, 90), (1, 90)]), 90)

    def test_resolve_range(self):
        self.assertEqual(sc.resolve_range((1, None), 500), (1, 500))
        self.assertEqual(sc.resolve_range((-20, None), 500), (481, 500))
        self.assertEqual(sc.resolve_range((30, 9999), 500), (30, 500))
        self.assertIsNone(sc.resolve_range((600, None), 500))


class ThresholdTest(unittest.TestCase):
    """O limiar é em bytes e tem piso de 2× a janela de edição."""

    LIMPAR = ("SHUNT_MIN_BYTES", "SHUNT_EDIT_BYTES", "SHUNT_MIN_LINES",
              "SHUNT_EDIT_WINDOW")

    def run_probe(self, env_extra, expr="(sc.MIN_BYTES, sc.MIN_BYTES_ADJUSTED)"):
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(%s)" % (os.path.join(ROOT, "hooks", "lib"), expr))
        env = {k: v for k, v in os.environ.items() if k not in self.LIMPAR}
        env.update(env_extra)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_low_threshold_is_raised_to_floor(self):
        self.assertEqual(self.run_probe({"SHUNT_MIN_BYTES": "1000",
                                         "SHUNT_EDIT_BYTES": "900"}),
                         "(1800, True)")

    def test_high_threshold_is_kept(self):
        self.assertEqual(self.run_probe({"SHUNT_MIN_BYTES": "9000",
                                         "SHUNT_EDIT_BYTES": "900"}),
                         "(9000, False)")

    def test_default_threshold(self):
        self.assertEqual(self.run_probe({}, "sc.MIN_BYTES"), "6480")

    def test_legacy_line_threshold_is_converted(self):
        """Quem configurou o limiar antigo em linhas não fica sem proteção."""
        self.assertEqual(self.run_probe({"SHUNT_MIN_LINES": "300"},
                                        "sc.MIN_BYTES"), "10800")


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.big = make_file(self.tmp.name, "big.md", 4000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimate_returns_parts_time_and_tokens(self):
        parts, seconds, tokens = sc.estimate_delegation([self.big])
        self.assertGreaterEqual(parts, 1)
        self.assertGreater(seconds, 0)
        self.assertGreater(tokens, 0)

    def test_estimate_survives_missing_file(self):
        self.assertEqual(sc.estimate_delegation(["/nao/existe"]), (1, 0, 0))


class HookEndToEndTest(unittest.TestCase):
    """Executa os hooks como o Claude Code faria: JSON no stdin, JSON no stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.big = make_file(self.dir, "big.md", 900)
        self.small = make_file(self.dir, "small.md", 50)
        # make_file gera linhas de ~10 bytes, então estes limiares em bytes
        # equivalem aos de ~180 e ~80 linhas usados antes.
        self.env = {**os.environ, "SHUNT_MIN_BYTES": "1900",
                    "SHUNT_EDIT_BYTES": "850", "SHUNT_ESCAPE_BYTES": "850",
                    "SHUNT_ASSUME_OLLAMA": "1", "TMPDIR": self.dir,
                    "SHUNT_HOOK_LOG": os.path.join(self.dir, "log.tsv"),
                    "CLAUDE_PLUGIN_ROOT": ROOT}
        self.session = "sess-test"

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, hook, tool_name, tool_input, env=None):
        payload = {"session_id": self.session, "cwd": self.dir,
                   "tool_name": tool_name, "tool_input": tool_input}
        proc = subprocess.run([os.path.join(ROOT, "hooks", hook)],
                              input=json.dumps(payload), capture_output=True,
                              text=True, env=env or self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def decision(self, out):
        return out["hookSpecificOutput"]["permissionDecision"] if out else "allow"

    def reason(self, out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def reasons_logged(self):
        with open(self.env["SHUNT_HOOK_LOG"], encoding="utf-8") as fh:
            return [ln.split("\t")[4] for ln in fh
                    if len(ln.rstrip("\n").split("\t")) in (8, 9, 11, 13)]

    # -- Read ---------------------------------------------------------------
    def test_full_read_is_denied(self):
        out = self.run_hook("check-file-size", "Read", {"file_path": self.big})
        self.assertEqual(self.decision(out), "deny")

    def test_read_with_large_limit_is_denied(self):
        """A regra antiga liberava qualquer leitura que tivesse offset ou limit."""
        out = self.run_hook("check-file-size", "Read",
                            {"file_path": self.big, "limit": 620})
        self.assertEqual(self.decision(out), "deny")

    def test_dense_single_line_file_is_denied(self):
        """O caso que o critério por linha não via: 1 linha, muitos bytes."""
        denso = os.path.join(self.dir, "min.json")
        with open(denso, "w", encoding="utf-8") as fh:
            fh.write('{"k":' + '"' + "x" * 40000 + '"}')
        out = self.run_hook("check-file-size", "Read", {"file_path": denso})
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("tokens", self.reason(out))

    def test_many_short_lines_stay_cheap(self):
        """E o inverso: muitas linhas curtas não custam contexto."""
        curto = os.path.join(self.dir, "curto.txt")
        with open(curto, "w", encoding="utf-8") as fh:
            fh.writelines("a\n" for _ in range(600))
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cat {curto}"})
        self.assertEqual(self.decision(out), "allow")
        self.assertIn("small-file", self.reasons_logged())

    def test_binary_is_left_alone(self):
        """Contar linhas em binário produz números sem sentido."""
        bin_path = os.path.join(self.dir, "img.png")
        with open(bin_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 5000)
        out = self.run_hook("check-file-size", "Read", {"file_path": bin_path})
        self.assertEqual(self.decision(out), "allow")
        self.assertIn("binary", self.reasons_logged())

    # -- Bash ---------------------------------------------------------------
    def test_bash_cat_is_denied(self):
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cd {self.dir} && cat big.md 2>/dev/null"})
        self.assertEqual(self.decision(out), "deny")

    def test_bash_small_file_passes(self):
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cat {self.small}"})
        self.assertEqual(self.decision(out), "allow")

    # -- Trecho mínimo sempre livre ----------------------------------------

    # -- Janela de edição: primeira livre, seguintes contam -----------------

    # -- Acumulado ----------------------------------------------------------

    # -- Mensagem de deny ---------------------------------------------------

    def test_small_file_is_out_of_scope(self):
        """Abaixo do limiar o plugin nao se aplica: nao vale delegar."""
        small = make_file(self.dir, "mid.md", 120)
        for _ in range(6):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"cat {small}"})
            self.assertEqual(self.decision(out), "allow")
        self.assertIn("small-file", self.reasons_logged())

    def test_per_file_budget_blocks_slicing(self):
        """O contorno que devolvia arquivos inteiros em pedacos de 80 linhas."""
        decisions = []
        for start in range(1, 900, 100):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '{start},{start + 79}p' {self.big}"})
            decisions.append(self.decision(out))
        self.assertIn("deny", decisions)
        self.assertLessEqual(decisions.count("allow"), 4)

    def test_tiny_slices_also_drain_budget(self):
        """Leituras de 20 linhas nao sao mais isentas: somam no orcamento."""
        decisions = []
        for start in range(1, 500, 20):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '{start},{start + 19}p' {self.big}"})
            decisions.append(self.decision(out))
        self.assertIn("deny", decisions)

    def test_escape_balance_allows_editing(self):
        """Editar o trecho apontado pelo modelo local tem de continuar possível."""
        self.run_hook("check-bash-read", "Bash",
                      {"command": f"sed -n '1,185p' {self.big}"})
        out = self.run_hook("check-file-size", "Read",
                            {"file_path": self.big, "offset": 300, "limit": 40})
        self.assertEqual(self.decision(out), "allow")
        self.assertIn("escape", self.reasons_logged())

    def test_escape_balance_is_finite(self):
        """Uma isenção sem cota é uma isenção total."""
        self.run_hook("check-bash-read", "Bash",
                      {"command": f"sed -n '1,185p' {self.big}"})
        decisions = []
        for start in (300, 400, 500, 600):
            out = self.run_hook("check-file-size", "Read",
                                {"file_path": self.big, "offset": start, "limit": 40})
            decisions.append(self.decision(out))
        self.assertEqual(decisions[-1], "deny")
        self.assertIn("escape-exhausted", self.reasons_logged())

    def test_rereading_same_range_is_free(self):
        for _ in range(5):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '1,150p' {self.big}"})
            self.assertEqual(self.decision(out), "allow")

    def test_log_carries_ranges_and_coverage(self):
        """É o que dispensa reconstruir fatiamento pelos transcripts."""
        self.run_hook("check-bash-read", "Bash",
                      {"command": f"sed -n '10,60p' {self.big}"})
        with open(self.env["SHUNT_HOOK_LOG"], encoding="utf-8") as fh:
            cols = fh.readline().rstrip("\n").split("\t")
        self.assertEqual(len(cols), 13)
        self.assertEqual(cols[9], "10-60")
        self.assertGreater(int(cols[10]), 0)   # bytes cobertos
        self.assertGreater(int(cols[11]), 0)   # bytes totais
        self.assertGreater(int(cols[12]), 0)   # bytes desta leitura

    def test_coverage_accumulates_across_reads(self):
        self.run_hook("check-bash-read", "Bash",
                      {"command": f"sed -n '1,50p' {self.big}"})
        self.run_hook("check-bash-read", "Bash",
                      {"command": f"sed -n '51,100p' {self.big}"})
        with open(self.env["SHUNT_HOOK_LOG"], encoding="utf-8") as fh:
            cobertos = [int(ln.split("\t")[10]) for ln in fh
                        if len(ln.rstrip("\n").split("\t")) == 13]
        self.assertGreater(cobertos[-1], cobertos[0])

    def test_denial_compares_costs_without_publishing_limits(self):
        """Um limite publicado é um mapa de contorno."""
        out = self.run_hook("check-file-size", "Read", {"file_path": self.big})
        reason = self.reason(out)
        self.assertIn("scripts/bulk-read", reason)
        self.assertIn("tokens do seu contexto", reason)
        self.assertIn("Custo de delegar", reason)
        self.assertIn("grep", reason)
        self.assertIn("somadas por sessão", reason)
        self.assertNotIn("25 linhas", reason)

    # -- Ferramentas MCP ----------------------------------------------------
    def test_mcp_batch_is_denied(self):
        out = self.run_hook("check-bash-read",
                            "mcp__context-mode__ctx_batch_execute",
                            {"commands": [{"label": "x",
                                           "command": f"cat {self.big}"}],
                             "queries": ["a"]})
        self.assertEqual(self.decision(out), "deny")

    def test_mcp_execute_python_passes(self):
        out = self.run_hook("check-bash-read",
                            "mcp__context-mode__ctx_execute",
                            {"language": "python",
                             "code": f"open('{self.big}').read()"})
        self.assertEqual(self.decision(out), "allow")

    # -- Degradação segura --------------------------------------------------
    def test_no_ollama_means_no_blocking(self):
        """Bloquear sem ter para onde delegar só travaria o Claude."""
        env = {**self.env, "OLLAMA_HOST": "http://127.0.0.1:9"}
        env.pop("SHUNT_ASSUME_OLLAMA")
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cat {self.big}"}, env=env)
        self.assertEqual(self.decision(out), "allow")

    def test_log_records_decision_with_version(self):
        self.run_hook("check-bash-read", "Bash", {"command": f"cat {self.big}"})
        with open(self.env["SHUNT_HOOK_LOG"], encoding="utf-8") as fh:
            cols = fh.readline().rstrip("\n").split("\t")
        self.assertEqual(len(cols), 13)
        self.assertEqual(cols[3], "deny")
        self.assertEqual(cols[8], sc.VERSION)

    def test_version_comes_from_manifest(self):
        import json as _json
        manifest = os.path.join(ROOT, ".claude-plugin", "plugin.json")
        with open(manifest, encoding="utf-8") as fh:
            self.assertEqual(sc.VERSION, _json.load(fh)["version"])

    def test_version_falls_back_to_dev(self):
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(sc.VERSION)" % os.path.join(ROOT, "hooks", "lib"))
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": self.dir}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.stdout.strip(), "dev")

    def test_version_comes_from_cache_directory(self):
        cache = os.path.join(self.dir, "0.9.1")
        os.makedirs(cache)
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(sc.VERSION)" % os.path.join(ROOT, "hooks", "lib"))
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": cache}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.stdout.strip(), "0.9.1")


class StatsTest(unittest.TestCase):
    """O agregador precisa ler o formato antigo de 8 colunas junto do novo de
    9, senão a comparação entre versões perde a linha de base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "shunt.log")
        rows = [
            # legado, 8 colunas
            "2026-09-01T10:00:00\ts1\tBash\tdeny\tsingle-read\t/a.py\t900\t900",
            "2026-09-01T10:01:00\ts1\tRead\tallow\tcounted\t/a.py\t900\t120",
            # versionado, 9 colunas
            "2026-09-02T10:00:00\ts2\tBash\tdeny\tsingle-read\t/b.py\t900\t900\t0.4.0",
            "2026-09-02T10:02:00\t-\tbulk-read\tok\t"
            "files=1;pin=9000;pout=300;dur=40;ratio=8\t/b.py\t900\t0\t0.4.0",
            "2026-09-02T10:30:00\t-\tbulk-read\tok\t"
            "files=1;pin=700;pout=120;dur=3;ratio=144\t/c.py\t20\t0\t0.4.0",
            "2026-09-02T10:03:00\ts2\tRead\tallow\talways-free\t/b.py\t900\t20\t0.4.0",
            # linha corrompida, deve ser ignorada
            "lixo",
        ]
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_stats(self, *extra):
        proc = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-stats"), "--log", self.log,
             *extra], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_compares_legacy_with_versioned(self):
        """Sem ler as 8 colunas antigas, a linha de base desaparece."""
        out = self.run_stats()
        self.assertIn("Comparação por versão", out)
        self.assertIn("<=0.3.0", out)
        self.assertIn("0.4.0", out)
        self.assertIn("-> 0.4.0", out)

    def test_conversion_per_version(self):
        out = self.run_stats()
        linha = next(ln for ln in out.splitlines() if ln.strip().startswith("0.4.0"))
        self.assertIn("100%", linha)   # o único deny virou delegação
        legado = next(ln for ln in out.splitlines()
                      if ln.strip().startswith("<=0.3.0"))
        self.assertIn("0%", legado)

    def test_version_filter(self):
        out = self.run_stats("--version", "0.4.0")
        self.assertIn("0.4.0", out)
        self.assertNotIn("<=0.3.0", out)

    def test_delegation_is_counted(self):
        out = self.run_stats()
        self.assertIn("2 delegação(ões)", out)
        self.assertIn("9700", out)   # tokens enviados ao Ollama, somando as duas

    def test_small_sample_warning(self):
        self.assertIn("amostra pequena", self.run_stats())

    def test_delegation_efficiency(self):
        """A razão resposta/conteúdo é o que diz se a delegação valeu."""
        out = self.run_stats()
        self.assertIn("resposta em % do conteúdo lido", out)
        self.assertIn("144%", out)
        self.assertIn("renderam pouco", out)

    def test_efficient_delegation_has_no_warning(self):
        log = os.path.join(self.tmp.name, "bom.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("2026-09-02T10:00:00\ts1\tBash\tdeny\tsingle-read"
                     "\t/b.py\t900\t900\t0.6.0\t1-900\t0\n")
            fh.write("2026-09-02T10:02:00\t-\tbulk-read\tok\t"
                     "files=1;pin=9000;pout=300;dur=40;ratio=8"
                     "\t/b.py\t900\t0\t0.6.0\t-\t0\n")
        proc = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-stats"), "--log", log],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mediana 8%", proc.stdout)
        self.assertNotIn("renderam pouco", proc.stdout)


    def test_model_table_groups_delegations(self):
        """Sem o nome do modelo no log, trocar SHUNT_MODEL não era mensurável."""
        log = os.path.join(self.tmp.name, "modelos.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("2026-09-12T10:00:00\t-\tbulk-read\tok\t"
                     "model=gemma4:e4b;files=1;pin=10000;pout=500;dur=40;ratio=6"
                     "\t/a.py\t900\t0\t0.11.0\t-\t0\n")
            fh.write("2026-09-12T10:05:00\t-\tbulk-read\tok\t"
                     "model=qwen3.5:4b;files=1;pin=10000;pout=250;dur=30;ratio=3"
                     "\t/a.py\t900\t0\t0.11.0\t-\t0\n")
            fh.write("2026-09-12T10:06:00\t-\tbulk-read\terror\t"
                     "model=qwen3.5:4b;curl-rc=28;timeout=60"
                     "\t/a.py\t900\t0\t0.11.0\t-\t0\n")
            fh.write("2026-09-11T10:00:00\t-\tbulk-read\tok\t"
                     "files=1;pin=9000;pout=300;dur=40;ratio=8"
                     "\t/b.py\t900\t0\t0.10.1\t-\t0\n")
        out = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-stats"), "--log", log],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("Comparação por modelo", out)
        linhas = {ln.split()[0]: ln.split() for ln in out.splitlines()
                  if ln.strip().startswith(("gemma4:e4b", "qwen3.5:4b", "(sem"))}
        self.assertEqual(linhas["gemma4:e4b"][1:6], ["1", "0", "10000", "500", "6%"])
        self.assertEqual(linhas["qwen3.5:4b"][1:6], ["1", "1", "10000", "250", "3%"])
        self.assertIn("(sem", linhas)   # linha anterior à 0.11.0 não some
        filtrado = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-stats"), "--log", log,
             "--model", "qwen3.5:4b"], capture_output=True, text=True,
            check=True).stdout
        self.assertNotIn("gemma4:e4b", filtrado)
        self.assertIn("qwen3.5:4b", filtrado)


class ShuntModelTest(unittest.TestCase):
    """O script edita só a chave SHUNT_MODEL do settings e preserva o resto."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = os.path.join(self.tmp.name, "settings.json")
        with open(self.settings, "w", encoding="utf-8") as fh:
            json.dump({"env": {"OUTRA": "1"}, "permissions": {"allow": ["Bash(ls:*)"]}}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_model(self, *args, env_extra=None):
        env = {**os.environ, **(env_extra or {})}
        proc = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-model"), "--settings", self.settings,
             *args], capture_output=True, text=True, env=env)
        return proc

    def read(self):
        with open(self.settings, encoding="utf-8") as fh:
            return json.load(fh)

    def test_set_force_preserves_other_keys(self):
        proc = self.run_model("set", "modelo:teste", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reinicie", proc.stdout)
        dados = self.read()
        self.assertEqual(dados["env"]["SHUNT_MODEL"], "modelo:teste")
        self.assertEqual(dados["env"]["OUTRA"], "1")
        self.assertEqual(dados["permissions"]["allow"], ["Bash(ls:*)"])

    def test_unset_removes_only_the_key(self):
        self.run_model("set", "modelo:teste", "--force")
        proc = self.run_model("unset")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["env"], {"OUTRA": "1"})
        self.assertIn("nada a fazer", self.run_model("unset").stdout)

    def test_current_follows_environment(self):
        proc = self.run_model("current", env_extra={"SHUNT_MODEL": "x:y"})
        self.assertEqual(proc.stdout.strip(), "x:y")

    def test_set_creates_missing_settings(self):
        novo = os.path.join(self.tmp.name, "sub", "settings.json")
        proc = subprocess.run(
            [os.path.join(ROOT, "scripts", "shunt-model"), "--settings", novo,
             "set", "m:1", "--force"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(novo, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"env": {"SHUNT_MODEL": "m:1"}})


class PathRestoreTest(unittest.TestCase):
    """O cabeçalho encurtado pelo modelo volta a ser o caminho que o Claude abre."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.labels = os.path.join(self.tmp.name, "labels")

    def tearDown(self):
        self.tmp.cleanup()

    def restore(self, labels, text):
        with open(self.labels, "w", encoding="utf-8") as fh:
            fh.write("\n".join(labels) + "\n")
        script = (f'. {os.path.join(ROOT, "scripts", "lib", "ollama.sh")}\n'
                  f'shunt_restore_paths {self.labels}')
        proc = subprocess.run(["bash", "-c", script], input=text,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_suffix_becomes_full_label(self):
        full = "/Users/x/proj/backend/app/services/import_service.py"
        out = self.restore([full], "/app/services/import_service.py\n"
                                   "  212-213 _read: rejeita\n")
        self.assertEqual(out, f"{full}\n  212-213 _read: rejeita\n")

    def test_relative_suffix_and_trailing_colon(self):
        full = "/Users/x/proj/backend/app/services/import_service.py"
        out = self.restore([full], "services/import_service.py:\n  1 a: b\n")
        self.assertTrue(out.startswith(full + "\n"))

    def test_partial_word_is_not_a_boundary(self):
        """`_service.py` é sufixo de string, não de caminho: fica como veio."""
        full = "/Users/x/proj/import_service.py"
        out = self.restore([full], "_service.py\n")
        self.assertEqual(out, "_service.py\n")

    def test_ambiguous_suffix_is_left_alone(self):
        labels = ["/p/a/models.py", "/p/b/models.py"]
        self.assertEqual(self.restore(labels, "models.py\n"), "models.py\n")

    def test_untouched_lines(self):
        labels = ["/p/a.py"]
        text = ("- Parte 1/2 (/p/a.py:L1-L10):\n/p/a.py\n  3 f: g\n"
                "not found: saldo\n\nfrase solta do modelo\n")
        self.assertEqual(self.restore(labels, text), text)


class ByteMeasurementTest(unittest.TestCase):
    """Linha e byte só coincidem em arquivo homogêneo, e é o byte que custa."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_range_bytes_follows_the_content(self):
        """As primeiras linhas são curtas, as últimas longas: mesma contagem
        de linhas, custo muito diferente."""
        path = os.path.join(self.dir, "misto.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines("a\n" for _ in range(50))
            fh.writelines("x" * 500 + "\n" for _ in range(50))
        lines, size, binary = sc.file_stats(path)
        self.assertEqual(lines, 100)
        self.assertFalse(binary)
        curtas = sc.ranges_bytes(path, [(1, 50)], lines, size)
        longas = sc.ranges_bytes(path, [(51, 100)], lines, size)
        self.assertEqual(curtas, 100)
        self.assertEqual(longas, 25050)
        self.assertGreater(longas, curtas * 100)

    def test_full_range_shortcuts_to_file_size(self):
        path = make_file(self.dir, "a.txt", 200)
        lines, size, _ = sc.file_stats(path)
        self.assertEqual(sc.ranges_bytes(path, [(1, lines)], lines, size), size)

    def test_file_without_trailing_newline_counts_one_line(self):
        """JSON minificado cai aqui; zero linhas o tornaria invisível."""
        path = os.path.join(self.dir, "min.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"a":1}' * 1000)
        lines, size, _ = sc.file_stats(path)
        self.assertEqual(lines, 1)
        self.assertEqual(size, 7000)

    def test_binary_is_flagged(self):
        path = os.path.join(self.dir, "x.bin")
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\x00\x01" * 100)
        self.assertTrue(sc.file_stats(path)[2])

    def test_missing_file_is_zeroed(self):
        self.assertEqual(sc.file_stats("/nao/existe"), (0, 0, False))


class BytesPerTokenTest(unittest.TestCase):
    """A razão bytes/token varia de 2,1 em JSON denso a 3,7 em Python gerado,
    e muda com o modelo: medir em vez de estimar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "calib.json")

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, samples, expr="sc.bytes_per_token()"):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1,
                       "models": {"gemma4:e4b": {"samples": samples}}}, fh)
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(%s)" % (os.path.join(ROOT, "hooks", "lib"), expr))
        env = {**os.environ, "SHUNT_CALIBRATION": self.path,
               "SHUNT_MODEL": "gemma4:e4b"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return float(out.stdout.strip())

    def test_learns_the_ratio(self):
        self.assertAlmostEqual(
            self.probe([{"tokens": 1000, "bytes": 3000, "seconds": 5},
                        {"tokens": 1000, "bytes": 3000, "seconds": 5}]),
            3.0, places=2)

    def test_uses_median_against_outliers(self):
        ratio = self.probe([{"tokens": 1000, "bytes": 2000, "seconds": 5},
                            {"tokens": 1000, "bytes": 3000, "seconds": 5},
                            {"tokens": 1000, "bytes": 90000, "seconds": 5}])
        self.assertAlmostEqual(ratio, 3.0, places=2)

    def test_falls_back_without_bytes(self):
        """Amostras anteriores à 0.9.0 não têm bytes."""
        self.assertEqual(self.probe([{"tokens": 1000, "seconds": 5}]), 3.0)

    def test_token_conversion_uses_learned_ratio(self):
        tokens = self.probe([{"tokens": 1000, "bytes": 2000, "seconds": 5}],
                            expr="sc.as_tokens(10000)")
        self.assertEqual(int(tokens), 5000)


class CalibrationTest(unittest.TestCase):
    """A velocidade do modelo é da máquina, não do plugin: numa GPU passa de
    1000 tok/s, numa CPU fica abaixo de 30. O plugin aprende a sua."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "calib.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, samples, model="gemma4:e4b"):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "models": {model: {"samples": samples}}}, fh)

    def rate(self, percentile=50, model="gemma4:e4b"):
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(sc.learned_rate(%d))"
                % (os.path.join(ROOT, "hooks", "lib"), percentile))
        env = {**os.environ, "SHUNT_CALIBRATION": self.path,
               "SHUNT_MODEL": model}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout.strip())

    def test_no_history_uses_fallback(self):
        """Sem medição, uma taxa baixa dá timeout generoso na primeira chamada."""
        self.assertEqual(self.rate(), 40)

    def test_learns_from_samples(self):
        self.write([{"tokens": 1000, "seconds": 10},    # 100 tok/s
                    {"tokens": 1000, "seconds": 5},     # 200
                    {"tokens": 1000, "seconds": 2}])    # 500
        self.assertEqual(self.rate(50), 200)

    def test_conservative_percentile_is_lower(self):
        self.write([{"tokens": 1000, "seconds": 10},
                    {"tokens": 1000, "seconds": 5},
                    {"tokens": 1000, "seconds": 2}])
        self.assertLess(self.rate(20), self.rate(50))

    def test_other_model_falls_back(self):
        """Trocar de modelo invalida a medição anterior."""
        self.write([{"tokens": 1000, "seconds": 1}], model="gemma4:e4b")
        self.assertEqual(self.rate(50, model="llama3.1:8b"), 40)

    def test_corrupt_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("isso nao e json")
        self.assertEqual(self.rate(), 40)

    def test_zero_seconds_is_ignored(self):
        self.write([{"tokens": 1000, "seconds": 0},
                    {"tokens": 1000, "seconds": 10}])
        self.assertEqual(self.rate(50), 100)


class ShellCalibrationTest(unittest.TestCase):
    """O lado shell: registro da amostra e timeout derivado."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "calib.json")

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, body, **env_extra):
        script = (f'. {os.path.join(ROOT, "scripts", "lib", "ollama.sh")}\n'
                  + body)
        # O modelo vem fixo: quem tem SHUNT_MODEL no ambiente gravaria a
        # amostra sob outro nome e o teste leria a chave errada.
        env = {**os.environ, "SHUNT_CALIBRATION": self.path,
               "SHUNT_MODEL": "gemma4:e4b"}
        env.pop("SHUNT_TIMEOUT_SECONDS", None)
        env.update(env_extra)
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_records_sample(self):
        self.sh('shunt_record_sample 6000 30000000000')   # 200 tok/s
        with open(self.path, encoding="utf-8") as fh:
            amostras = json.load(fh)["models"]["gemma4:e4b"]["samples"]
        self.assertEqual(amostras[0]["tokens"], 6000)
        self.assertAlmostEqual(amostras[0]["seconds"], 30.0, places=2)

    def test_rejects_cached_prompt_sample(self):
        """O Ollama reaproveita prompt em cache e o tempo cai para quase zero;
        aprender isso derrubaria o timeout antes de um prompt novo."""
        self.sh('shunt_record_sample 6000 100000000')     # 60000 tok/s
        self.assertFalse(os.path.exists(self.path))

    def test_rejects_tiny_sample(self):
        self.sh('shunt_record_sample 12 173839000')
        self.assertFalse(os.path.exists(self.path))

    def test_timeout_grows_when_machine_is_slow(self):
        rapido = int(self.sh('shunt_record_sample 6000 12000000000;'
                             ' shunt_timeout_for 24000'))   # 500 tok/s
        os.remove(self.path)
        lento = int(self.sh('shunt_record_sample 6000 120000000000;'
                            ' shunt_timeout_for 24000'))    # 50 tok/s
        self.assertGreater(lento, rapido)

    def test_timeout_respects_floor_and_ceiling(self):
        self.assertEqual(int(self.sh('shunt_timeout_for 10')), 60)
        self.assertEqual(int(self.sh('shunt_timeout_for 99999999')), 600)

    def test_explicit_timeout_wins(self):
        valor = self.sh('shunt_timeout_for 24000',
                        SHUNT_TIMEOUT_SECONDS="45")
        self.assertEqual(int(valor), 45)

    def test_locale_with_decimal_comma_does_not_break(self):
        """pt_BR faz o awk emitir "37,000", que não é JSON: a divisão ficou no jq."""
        self.sh('shunt_record_sample 6000 30000000000',
                LC_ALL="pt_BR.UTF-8", LC_NUMERIC="pt_BR.UTF-8")
        with open(self.path, encoding="utf-8") as fh:
            self.assertTrue(json.load(fh)["models"]["gemma4:e4b"]["samples"])


class PromptBudgetTest(unittest.TestCase):
    """O Ollama descarta metade do contexto quando ele estoura, sem erro. As
    partes têm de caber com folga, e o que não couber precisa ser detectado."""

    def sh(self, body, **env_extra):
        script = (f'. {os.path.join(ROOT, "scripts", "lib", "ollama.sh")}\n'
                  + body)
        env = {**os.environ}
        for k in ("SHUNT_NUM_CTX", "SHUNT_PROMPT_FRACTION", "SHUNT_PROMPT_RESERVE",
                  "SHUNT_BYTES_PER_TOKEN_FLOOR"):
            env.pop(k, None)
        env.update(env_extra)
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_prompt_limit_stays_under_the_window(self):
        """Medido: até ~27900 tokens passa, de ~30000 em diante corta."""
        limite = int(self.sh("shunt_prompt_limit", SHUNT_NUM_CTX="32768"))
        self.assertEqual(limite, 26214)
        self.assertLess(limite, 27900)

    def test_budget_uses_worst_case_ratio(self):
        """Dimensionar pelo caso médio estoura em JSON denso."""
        orcamento = int(self.sh("shunt_chunk_budget", SHUNT_NUM_CTX="32768"))
        # a 1,6 bytes/token, o pior caso medido, cabe no limite
        self.assertLessEqual(orcamento / 1.6, 26214)
        # e não é tão pequeno a ponto de fatiar demais
        self.assertGreater(orcamento, 30000)

    def test_budget_follows_the_window(self):
        pequeno = int(self.sh("shunt_chunk_budget", SHUNT_NUM_CTX="8192"))
        grande = int(self.sh("shunt_chunk_budget", SHUNT_NUM_CTX="65536"))
        self.assertLess(pequeno, grande)

    def test_tiny_window_still_yields_a_budget(self):
        self.assertGreater(int(self.sh("shunt_chunk_budget",
                                       SHUNT_NUM_CTX="1024")), 0)

    def test_tokens_for_bytes_overestimates(self):
        """Superestimar tokens é o lado seguro: alonga o timeout."""
        tokens = int(self.sh("shunt_tokens_for_bytes 16000"))
        self.assertEqual(tokens, 10000)      # 16000 bytes a 1,6 b/token

    def test_min_tokens_uses_the_other_end(self):
        """Para acusar corte é preciso o mínimo plausível: usar o máximo
        acusaria truncamento em todo arquivo de código."""
        maximo = int(self.sh("shunt_tokens_for_bytes 17453"))
        minimo = int(self.sh("shunt_min_tokens_for_bytes 17453"))
        self.assertLess(minimo, maximo)
        # um shell real de 17453 bytes rendeu 7060 tokens: entre os dois,
        # e acima do gatilho de 70% do mínimo
        self.assertLess(minimo * 0.7, 7060)
        self.assertGreater(maximo, 7060)


class TruncationDetectionTest(unittest.TestCase):
    """Uma resposta sobre metade do conteúdo tem a mesma cara de uma resposta
    completa: se o modelo não viu tudo, a resposta é descartada."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_invoke(self, pin, expected):
        """Chama shunt_invoke contra um Ollama de mentira que relata `pin`."""
        # Precisa se chamar `curl`: é o nome que o PATH intercepta.
        fake = os.path.join(self.tmp.name, "curl")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\ncat >/dev/null\n"
                     'printf \'{"message":{"content":"resposta"},'
                     '"prompt_eval_count":%d,"eval_count":10,'
                     '"total_duration":1000000000,"prompt_eval_duration":500000000}\''
                     f" {pin}\n")
        os.chmod(fake, 0o755)
        sys_file = os.path.join(self.tmp.name, "sys.md")
        msg_file = os.path.join(self.tmp.name, "msg.txt")
        open(sys_file, "w", encoding="utf-8").write("system")
        open(msg_file, "w", encoding="utf-8").write("x" * 1000)
        script = (f'. {os.path.join(ROOT, "scripts", "lib", "ollama.sh")}\n'
                  f'shunt_invoke {sys_file} {msg_file} {expected} 60')
        # O PATH entra pelo ambiente do processo, não por atribuição dentro do
        # script: o bash já teria resolvido `curl` pelo cache.
        env = {**os.environ,
               "PATH": self.tmp.name + os.pathsep + os.environ["PATH"],
               "SHUNT_CALIBRATION": os.path.join(self.tmp.name, "c.json"),
               "SHUNT_HOOK_LOG": os.path.join(self.tmp.name, "log")}
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=env)

    def test_full_prompt_is_accepted(self):
        out = self.run_invoke(pin=9500, expected=10000)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("resposta", out.stdout)

    def test_truncated_prompt_is_rejected(self):
        out = self.run_invoke(pin=4000, expected=10000)
        self.assertEqual(out.returncode, 1)
        self.assertIn("descartado em silêncio", out.stderr + out.stdout)
        self.assertNotIn("resposta", out.stdout)

    def test_no_expectation_skips_the_check(self):
        """Chamadas sem tamanho esperado não podem quebrar."""
        out = self.run_invoke(pin=10, expected=0)
        self.assertEqual(out.returncode, 0, out.stderr)


class SessionStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "SHUNT_ASSUME_OLLAMA": "1",
                    "TMPDIR": self.tmp.name,
                    "SHUNT_HOOK_LOG": os.path.join(self.tmp.name, "log.tsv"),
                    "CLAUDE_PLUGIN_ROOT": ROOT}

    def tearDown(self):
        self.tmp.cleanup()

    def test_injects_rule_without_publishing_limits(self):
        proc = subprocess.run([os.path.join(ROOT, "hooks", "session-start")],
                              input=json.dumps({"session_id": "s1"}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bulk-read", text)
        self.assertIn("orçamento", text)
        self.assertIn("somadas", text)
        self.assertIn("grep/rg", text)
        # Publicar os limites transforma a regra num mapa de contorno.
        for numero in (str(sc.MIN_BYTES), str(sc.EDIT_BYTES),
                       str(sc.ESCAPE_BYTES)):
            self.assertNotIn(numero, text)


if __name__ == "__main__":
    unittest.main()
