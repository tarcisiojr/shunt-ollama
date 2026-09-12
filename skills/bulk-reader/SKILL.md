---
name: bulk-reader
description: "Delega leitura de arquivos grandes, diffs extensos ou vários arquivos a um modelo Ollama local, que devolve linhas ancoradas em número. Use para entender, resumir ou responder perguntas sobre código/docs acima do limiar do hook, sempre que um hook do shunt negar uma leitura, ao explorar um projeto desconhecido ou revisar um diff grande."
---

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "<pergunta específica>" --paths <arquivo|diretório> [...]
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "..." --glob 'src/**/*.rs'
${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "o que mudou e onde?" --cmd "git diff main"
git diff | ${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read --question "..." --stdin
```

## A pergunta define o custo

A resposta entra no seu contexto; o arquivo não. Então o que importa é o tamanho da resposta, e
ele depende inteiramente da pergunta.

- **Pergunte o que precisa.** "Em que linha o checksum é conferido?" devolve 1% do tamanho do
  arquivo. "Explique o que o script faz" devolve 10% ou mais, e num arquivo pequeno pode passar
  de 100%, caso em que ler direto teria custado o mesmo.
- O script avisa no stderr quando a resposta passa de `SHUNT_WARN_RATIO` (50%) do conteúdo lido.
  Ao ver esse aviso, refaça com uma pergunta mais estreita em vez de usar a resposta inflada.
- Follow-up custa zero: chame de novo com os mesmos paths e outra pergunta. Duas perguntas
  específicas saem mais baratas que um panorama.

## O formato da resposta

Agrupado por arquivo, com o caminho uma vez e as linhas indentadas:

```text
/caminho/para/arquivo.sh
  186 conferir_checksum: aborta quando o sha256 não casa
  220-245 acrescenta_ao_path: escreve o bloco gerenciado no rc do shell
```

Use esses números num `Read` com `offset`/`limit` no trecho que vai editar. Confira linhas e
valores exatos antes de editar: o modelo local pode errar por algumas linhas.

## Limites

Cada arquivo grande tem um orçamento de leitura por sessão, medido em bytes, e dividir a
leitura em pedaços não aumenta o total, porque as faixas são somadas. O que conta é o tamanho, e
não o número de linhas: um JSON minificado de uma linha pode custar mais contexto que um arquivo
de mil linhas curtas. Para **localizar** um símbolo, use `grep -n` ou `rg`, que não consomem
orçamento. Arquivos maiores que a janela do modelo são fatiados automaticamente, preservando a
numeração original.
