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

    def test_cat_simples(self):
        self.assertEqual(self.reads(f"cat {self.big}"), {self.big: [(1, None)]})

    def test_cd_e_cat_relativo(self):
        reads = self.reads(f"cd {self.dir}/sub && cat rel.md", cwd="/")
        self.assertEqual(reads, {self.sub: [(1, None)]})

    def test_stderr_redirect_nao_esconde(self):
        self.assertIn(self.big, self.reads(f"cat {self.big} 2>/dev/null"))
        self.assertIn(self.big, self.reads(f"cat {self.big} 2>&1"))

    def test_stdout_para_arquivo_ignorado(self):
        self.assertEqual(self.reads(f"cat {self.big} > /tmp/x"), {})

    def test_pipe_filtro_ignorado(self):
        self.assertEqual(self.reads(f"cat {self.big} | grep linha"), {})
        self.assertEqual(self.reads(f"grep -n foo {self.big} | head -5"), {})
        self.assertEqual(self.reads(f"wc -l < {self.big}"), {})

    def test_pipe_head_limita(self):
        self.assertEqual(self.reads(f"cat {self.big} | head -40"),
                         {self.big: [(1, 40)]})

    def test_sed_faixa(self):
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

    def test_head_tail_variantes(self):
        for cmd in (f"head -150 {self.big}", f"head -n 150 {self.big}",
                    f"head -n150 {self.big}", f"head --lines=150 {self.big}"):
            self.assertEqual(self.reads(cmd), {self.big: [(1, 150)]}, cmd)
        self.assertEqual(self.reads(f"tail -20 {self.big}"),
                         {self.big: [(-20, None)]})
        self.assertEqual(self.reads(f"tail -n +5 {self.big}"),
                         {self.big: [(5, None)]})
        self.assertEqual(self.reads(f"head -c 5000 {self.big}"),
                         {self.big: [(1, 100)]})

    def test_awk(self):
        self.assertEqual(self.reads(f"awk 'NR>=10 && NR<=50' {self.big}"),
                         {self.big: [(10, 50)]})
        self.assertEqual(self.reads(f"awk '{{print}}' {self.big}"),
                         {self.big: [(1, None)]})
        self.assertEqual(self.reads(f"awk '{{print $1}}' {self.big}"), {})
        self.assertEqual(self.reads(f"awk '/^## X/,0' {self.big} | head -220"), {})

    def test_rtk_e_wrappers(self):
        self.assertEqual(self.reads(f"rtk read {self.big}"),
                         {self.big: [(1, None)]})
        self.assertEqual(self.reads(f"FOO=1 sudo cat -n {self.big}"),
                         {self.big: [(1, None)]})

    def test_encadeamentos(self):
        reads = self.reads(f"echo x && cat {self.big}; ls")
        self.assertEqual(reads, {self.big: [(1, None)]})
        reads = self.reads(f"ls\ncat {self.other}")
        self.assertEqual(reads, {self.other: [(1, None)]})
        reads = self.reads(f"cat {self.big} {self.other}")
        self.assertEqual(set(reads), {self.big, self.other})

    def test_glob(self):
        reads = self.reads(f"cat {self.dir}/*.md")
        self.assertEqual(set(reads), {self.big, self.other})

    def test_variaveis_e_loops_sao_notas(self):
        reads, notes = parse_reads('for f in a b; do cat "$f"; done', self.dir)
        self.assertEqual(reads, {})
        self.assertTrue(any(n.startswith("unresolved") for n in notes))
        reads, notes = parse_reads("python3 - <<'EOF'\nprint(1)\nEOF", self.dir)
        self.assertEqual((reads, notes), ({}, ["heredoc"]))

    def test_arquivo_inexistente(self):
        self.assertEqual(self.reads("cat /nao/existe.md"), {})


class RangeMathTest(unittest.TestCase):
    def test_merge_e_coverage(self):
        self.assertEqual(sc.merge_ranges([(1, 5), (4, 10), (12, 12)]),
                         [(1, 10), (12, 12)])
        self.assertEqual(sc.coverage([(1, 90), (1, 90)]), 90)

    def test_resolve(self):
        self.assertEqual(sc.resolve_range((1, None), 500), (1, 500))
        self.assertEqual(sc.resolve_range((-20, None), 500), (481, 500))
        self.assertEqual(sc.resolve_range((30, 9999), 500), (30, 500))
        self.assertIsNone(sc.resolve_range((600, None), 500))


class ThresholdFloorTest(unittest.TestCase):
    """O limiar não pode ficar rente à janela de edição: entre 81 e 100 linhas
    a faixa contável era estreita demais e gerava negativas de pouco valor."""

    def run_probe(self, env_extra):
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(sc.MIN_LINES, sc.MIN_LINES_ADJUSTED)"
                % os.path.join(ROOT, "hooks", "lib"))
        env = {**os.environ, **env_extra}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        min_lines, adjusted = out.stdout.split()
        return int(min_lines), adjusted == "True"

    def test_limiar_baixo_e_elevado_ao_piso(self):
        self.assertEqual(self.run_probe({"SHUNT_MIN_LINES": "100",
                                         "SHUNT_EDIT_WINDOW": "80"}), (160, True))

    def test_limiar_alto_e_respeitado(self):
        self.assertEqual(self.run_probe({"SHUNT_MIN_LINES": "400",
                                         "SHUNT_EDIT_WINDOW": "80"}), (400, False))

    def test_default(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SHUNT_MIN_LINES", "SHUNT_EDIT_WINDOW")}
        code = ("import sys; sys.path.insert(0, %r); import shunt_common as sc; "
                "print(sc.MIN_LINES)" % os.path.join(ROOT, "hooks", "lib"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env)
        self.assertEqual(int(out.stdout.strip()), 250)


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.big = make_file(self.tmp.name, "big.md", 4000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimativa_tem_partes_tempo_e_tokens(self):
        parts, seconds, tokens = sc.estimate_delegation([self.big])
        self.assertGreaterEqual(parts, 1)
        self.assertGreater(seconds, 0)
        self.assertGreater(tokens, 0)

    def test_arquivo_inexistente_nao_estoura(self):
        self.assertEqual(sc.estimate_delegation(["/nao/existe"]), (1, 0, 0))


class HookEndToEndTest(unittest.TestCase):
    """Executa os hooks como o Claude Code faria: JSON no stdin, JSON no stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.big = make_file(self.dir, "big.md", 900)
        self.small = make_file(self.dir, "small.md", 50)
        self.env = {**os.environ, "SHUNT_MIN_LINES": "250",
                    "SHUNT_EDIT_WINDOW": "80", "SHUNT_EDIT_FREE": "1",
                    "SHUNT_ALWAYS_FREE": "25",
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
            return [ln.split("\t")[4] for ln in fh if ln.count("\t") == 7]

    # -- Read ---------------------------------------------------------------
    def test_read_completo_nega(self):
        out = self.run_hook("check-file-size", "Read", {"file_path": self.big})
        self.assertEqual(self.decision(out), "deny")

    def test_read_com_limit_grande_nega(self):
        out = self.run_hook("check-file-size", "Read",
                            {"file_path": self.big, "limit": 620})
        self.assertEqual(self.decision(out), "deny")

    def test_read_janela_de_edicao_passa(self):
        out = self.run_hook("check-file-size", "Read",
                            {"file_path": self.big, "offset": 100, "limit": 60})
        self.assertEqual(self.decision(out), "allow")

    # -- Bash ---------------------------------------------------------------
    def test_bash_cat_nega(self):
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cd {self.dir} && cat big.md 2>/dev/null"})
        self.assertEqual(self.decision(out), "deny")

    def test_bash_arquivo_pequeno_passa(self):
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cat {self.small}"})
        self.assertEqual(self.decision(out), "allow")

    # -- Trecho mínimo sempre livre ----------------------------------------
    def test_leitura_minima_passa_sempre(self):
        for _ in range(20):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '1,20p' {self.big}"})
            self.assertEqual(self.decision(out), "allow")
        self.assertIn("always-free", self.reasons_logged())

    def test_minima_continua_livre_apos_acumulado_estourar(self):
        for start in (1, 101, 201, 301):
            self.run_hook("check-bash-read", "Bash",
                          {"command": f"sed -n '{start},{start + 99}p' {self.big}"})
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"sed -n '500,520p' {self.big}"})
        self.assertEqual(self.decision(out), "allow")

    # -- Janela de edição: primeira livre, seguintes contam -----------------
    def test_primeira_janela_livre_segunda_conta(self):
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"sed -n '1,80p' {self.big}"})
        self.assertEqual(self.decision(out), "allow")
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"sed -n '101,180p' {self.big}"})
        self.assertEqual(self.decision(out), "allow")
        logged = self.reasons_logged()
        self.assertIn("edit-window", logged)
        self.assertIn("window-counted", logged)

    def test_fatiamento_em_pedacos_de_janela_acaba_bloqueado(self):
        """O contorno que deixava mais da metade de um arquivo entrar."""
        decisions = []
        for start in range(1, 900, 100):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '{start},{start + 79}p' {self.big}"})
            decisions.append(self.decision(out))
        self.assertIn("deny", decisions)
        self.assertLessEqual(decisions.count("allow"), 5)

    def test_janela_livre_e_por_arquivo(self):
        other = make_file(self.dir, "other.md", 900)
        for path in (self.big, other):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '1,80p' {path}"})
            self.assertEqual(self.decision(out), "allow")
        self.assertEqual(self.reasons_logged().count("edit-window"), 2)

    # -- Acumulado ----------------------------------------------------------
    def test_fatiamento_acumulado_nega(self):
        for start in (1, 101, 201):
            self.run_hook("check-bash-read", "Bash",
                          {"command": f"sed -n '{start},{start + 99}p' {self.big}"})
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"sed -n '301,400p' {self.big}"})
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("fatias", self.reason(out))

    def test_reler_mesma_faixa_nao_acumula(self):
        for _ in range(5):
            out = self.run_hook("check-bash-read", "Bash",
                                {"command": f"sed -n '1,100p' {self.big}"})
            self.assertEqual(self.decision(out), "allow")

    # -- Mensagem de deny ---------------------------------------------------
    def test_deny_mostra_os_dois_custos_e_o_comando(self):
        out = self.run_hook("check-file-size", "Read", {"file_path": self.big})
        reason = self.reason(out)
        self.assertIn("scripts/bulk-read", reason)
        self.assertIn("tokens do seu contexto", reason)
        self.assertIn("Custo de delegar", reason)
        self.assertIn("grep", reason)
        self.assertIn("25 linhas", reason)

    # -- Ferramentas MCP ----------------------------------------------------
    def test_mcp_batch_nega(self):
        out = self.run_hook("check-bash-read",
                            "mcp__context-mode__ctx_batch_execute",
                            {"commands": [{"label": "x",
                                           "command": f"cat {self.big}"}],
                             "queries": ["a"]})
        self.assertEqual(self.decision(out), "deny")

    def test_mcp_execute_python_passa(self):
        out = self.run_hook("check-bash-read",
                            "mcp__context-mode__ctx_execute",
                            {"language": "python",
                             "code": f"open('{self.big}').read()"})
        self.assertEqual(self.decision(out), "allow")

    # -- Degradação segura --------------------------------------------------
    def test_ollama_fora_libera(self):
        env = {**self.env, "OLLAMA_HOST": "http://127.0.0.1:9"}
        env.pop("SHUNT_ASSUME_OLLAMA")
        out = self.run_hook("check-bash-read", "Bash",
                            {"command": f"cat {self.big}"}, env=env)
        self.assertEqual(self.decision(out), "allow")

    def test_log_registra_decisoes(self):
        self.run_hook("check-bash-read", "Bash", {"command": f"cat {self.big}"})
        with open(self.env["SHUNT_HOOK_LOG"], encoding="utf-8") as fh:
            cols = fh.readline().rstrip("\n").split("\t")
        self.assertEqual(len(cols), 8)
        self.assertEqual(cols[3], "deny")


class SessionStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "SHUNT_ASSUME_OLLAMA": "1",
                    "TMPDIR": self.tmp.name,
                    "SHUNT_HOOK_LOG": os.path.join(self.tmp.name, "log.tsv"),
                    "CLAUDE_PLUGIN_ROOT": ROOT}

    def tearDown(self):
        self.tmp.cleanup()

    def test_injeta_regra_com_os_tres_limiares(self):
        proc = subprocess.run([os.path.join(ROOT, "hooks", "session-start")],
                              input=json.dumps({"session_id": "s1"}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bulk-read", text)
        self.assertIn("primeira leitura", text)
        self.assertIn("acumulado", text)
        self.assertIn("grep/rg", text)


if __name__ == "__main__":
    unittest.main()
