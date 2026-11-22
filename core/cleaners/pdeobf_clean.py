"""Limpeza do output do pdeobf (scaffolding que sobra da reconstrucao).

O pdeobf entrega o source com nomes inferidos, mas deixa sobras que o dono do
bot apontou, medidas no paste real (samples/novos/paste_53XHn9rt.lua):

  - blocos `do` / `end` VAZIOS (sobra das dobras de bloco do obfuscador);
  - `local vN` declarado e nunca usado, e um `local` solto no fim de bloco
    (bug do passo 09-fmt do pdeobf: a declaracao perde o nome);
  - `local vN = fN` — alias puro pra outro nome (108 linhas no paste);
  - `local v1 = game:GetService("Workspace")` — o `workspace` nao e' service
    nomeado pela ferramenta;
  - um branco depois de CADA comando (formato do printer dela).

Tudo e' edicao conservadora: sai o que nao pode estar em uso. Parenteses de
chamada (`local x = (f())`) NAO sao tocados — no Lua isso muda semantica de
multi-retorno — e labels/goto ficam como estao.
"""
from __future__ import annotations

import re

_PALAVRAS = {
    "nil", "true", "false", "and", "or", "not", "function", "end", "do",
    "while", "for", "if", "else", "elseif", "then", "repeat", "until",
    "return", "break", "local", "in", "goto", "self", "type",
}
_DECL = re.compile(r"local\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*$")
_DECL_NOMES = re.compile(r"(?:^|\s)local\s+(?!function\b)([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)")
_ATRIB = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=(?!=)")
_ALIAS = re.compile(r"\s*local\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*")


def _sem_literais(texto: str) -> str:
    """Mesmo texto com strings/comentarios trocados por espaco (busca de nome)."""
    out = list(texto)
    i, n = 0, len(texto)
    while i < n:
        c = texto[i]
        if c == "-" and i + 1 < n and texto[i + 1] == "-":
            j = texto.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c in ("'", '"'):
            q = c
            i += 1
            while i < n:
                if texto[i] == "\\":
                    i += 2
                    continue
                if texto[i] == q:
                    i += 1
                    break
                i += 1
        elif c == "[" and texto.startswith("[[", i):
            j = texto.find("]]", i)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _usos(mascara: str, nome: str) -> list[re.Match]:
    """Ocorrencias de `nome` que NAO sao campo (`x.nome`) nem sufixo."""
    return list(re.finditer(r"(?<![\w.:])%s(?![\w])" % re.escape(nome), mascara))


def _declaracoes(codigo: list[str]) -> tuple[dict[str, tuple[int, int]], dict[str, int]]:
    """(primeira declaracao `local` de cada nome, quantas vezes foi declarado)."""
    decl: dict[str, tuple[int, int]] = {}
    conta: dict[str, int] = {}
    for i, linha in enumerate(codigo):
        for m in _DECL_NOMES.finditer(linha):
            for nome in (x.strip() for x in m.group(1).split(",")):
                if not nome:
                    continue
                decl.setdefault(nome, (i, len(linha) - len(linha.lstrip())))
                conta[nome] = conta.get(nome, 0) + 1
    return decl, conta


def _reatribuidos(codigo: list[str], decl: dict[str, tuple[int, int]]) -> set[str]:
    """Nomes que levam `=` fora da propria declaracao (nao sao alias puro)."""
    impuro = set()
    for i, linha in enumerate(codigo):
        for m in _ATRIB.finditer(linha):
            nome = m.group(1)
            if nome in decl and decl[nome][0] != i:
                impuro.add(nome)
    return impuro


def _tira_do_end_vazio(linhas: list[str]) -> list[str]:
    """`do` seguido de `end` (nada no meio) nao faz nada: sai os dois."""
    mudou = True
    while mudou:
        mudou = False
        saida: list[str] = []
        i = 0
        while i < len(linhas):
            if (linhas[i].strip() == "do" and i + 1 < len(linhas)
                    and linhas[i + 1].strip() == "end"):
                i += 2
                mudou = True
                continue
            saida.append(linhas[i])
            i += 1
        linhas = saida
    return linhas


def _tira_locais_mortos(linhas: list[str]) -> list[str]:
    """Nome declarado que nao aparece mais no arquivo inteiro nao esta em uso."""
    codigo = _sem_literais("\n".join(linhas)).splitlines()
    saida: list[str] = []
    for idx, linha in enumerate(linhas):
        s = linha.strip()
        if s == "local":                      # declaracao sem nome (09-fmt)
            continue
        m = _DECL.fullmatch(s)
        if not m:
            saida.append(linha)
            continue
        nomes = [x.strip() for x in m.group(1).split(",")]
        resto = "\n".join(codigo[:idx] + codigo[idx + 1:])
        vivos = [n for n in nomes if _usos(resto, n)]
        if not vivos:
            continue
        if len(vivos) != len(nomes):
            saida.append(re.sub(r"local\s+.*$", "local " + ", ".join(vivos), linha))
            continue
        saida.append(linha)
    return saida


def _inline_aliases(linhas: list[str]) -> list[str]:
    """`local A = B` (B nome/campo) -> todo uso de A vira B e a linha sai.

    Guardas: A declarado uma vez so', nunca reatribuido, nenhum uso de A como
    campo (`x.A`) nem colado a outro nome; B nao pode ser palavra-chave nem
    estar declarado depois (o inlining mudaria semantica). Sem uso nenhum, a
    linha e' so' um dead store de valor puro e sai direto.
    """
    for _ in range(400):                      # cada volta inline-a UM (guarda rechecada)
        codigo = [_sem_literais(l) for l in linhas]
        decl, conta = _declaracoes(codigo)
        impuro = _reatribuidos(codigo, decl)
        saiu = False
        for i, linha in enumerate(linhas):
            m = _ALIAS.fullmatch(linha)
            if not m:
                continue
            a, b = m.group(1), m.group(2)
            if a == b or b in _PALAVRAS or a in impuro or conta.get(a, 0) != 1:
                continue
            if b in decl and not (decl[b][0] < i and decl[b][1] == 0):
                continue
            usos = [(j, mm) for j, l in enumerate(codigo) for mm in _usos(l, a) if j != i]
            if not usos:
                linhas = linhas[:i] + linhas[i + 1:]   # dead store: valor puro, sai
                saiu = True
                break
            novas = []
            for j, l in enumerate(linhas):
                if j == i:
                    continue
                mascara, pos, pedacos = codigo[j], 0, []
                for mm in _usos(mascara, a):
                    pedacos.append(l[pos:mm.start()])
                    pedacos.append(b)
                    pos = mm.end()
                pedacos.append(l[pos:])
                novas.append("".join(pedacos))
            linhas = novas
            saiu = True
            break
        if not saiu:
            break
    return linhas


def _workspace_conhecido(linhas: list[str]) -> list[str]:
    """`local X = game:GetService("Workspace")` -> `local workspace` (global do Luau)."""
    codigo = [_sem_literais(l) for l in linhas]
    decl, conta = _declaracoes(codigo)
    if "workspace" in decl:
        return linhas
    impuro = _reatribuidos(codigo, decl)
    for i, linha in enumerate(linhas):
        m = re.fullmatch(
            r"\s*local\s+([A-Za-z_]\w*)\s*=\s*\(?\s*game:GetService\(\"Workspace\"\)\s*\)?\s*",
            linha)
        if not m or m.group(1) in impuro or conta.get(m.group(1), 0) != 1:
            continue
        a = m.group(1)
        novas = []
        for j, l in enumerate(linhas):
            if j == i:
                novas.append(re.sub(r"local\s+%s\b" % re.escape(a), "local workspace", l))
                continue
            mascara, pos, pedacos = codigo[j], 0, []
            for mm in _usos(mascara, a):
                pedacos.append(l[pos:mm.start()])
                pedacos.append("workspace")
                pos = mm.end()
            pedacos.append(l[pos:])
            novas.append("".join(pedacos))
        return novas
    return linhas


def _compacta_brancos(linhas: list[str]) -> list[str]:
    """Um branco depois de CADA comando e' formato da ferramenta, nao do script.

    Fica branco so' onde separa o que importa: antes de `local function`,
    `function` e comentario.
    """
    def proximo(k: int) -> str:
        for l in linhas[k + 1:]:
            if l.strip():
                return l.strip()
        return ""

    saida: list[str] = []
    for k, linha in enumerate(linhas):
        if linha.strip():
            saida.append(linha.rstrip())
            continue
        if proximo(k).startswith(("local function", "function", "--")):
            if saida and saida[-1].strip():
                saida.append("")
    return saida


_COPIA = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*$")
_ATRIB_LITERAL = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=\s*(?:nil|true|false|[-+]?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*')\s*$")
_GOTO = re.compile(r"^(\s*)goto\s+(L\d+)\s*$")
_ROTULO = re.compile(r"^\s*::(L\d+)::\s*$")
_PONTO_VIRGULA = re.compile(r"^(\s*);\s*(.*)$")


def _indent(l: str) -> int:
    return len(l) - len(l.lstrip())


def _corpo_do_bloco(linhas: list[str], cabeca: int) -> tuple[int, int]:
    """(inicio, fim) do corpo indentado depois da linha `cabeca`."""
    base = _indent(linhas[cabeca])
    i = cabeca + 1
    while i < len(linhas) and (not linhas[i].strip() or _indent(linhas[i]) > base):
        i += 1
    return cabeca + 1, i


def _propaga_copias(linhas: list[str]) -> list[str]:
    """`X = Y` (dois nomes) -> usos de X viram Y e a linha sai.

    So' enquanto ninguem reatribuir/REdeclarar X nem Y: o limite e' a proxima
    atribuicao (ou declaracao) de qualquer um dos dois — depois disso o valor de
    X nao e' mais Y. Sem uso no intervalo, a linha e' dead store de valor puro
    (Y e' nome, nao expressao) e sai igual. O limite por declaracao cobre nomes
    `vN` que aparecem em escopos diferentes (o pdeobf reusa a numeracao).
    """
    for _ in range(300):
        codigo = [_sem_literais(l) for l in linhas]
        decl, _conta = _declaracoes(codigo)
        atrib: dict[str, list[int]] = {}
        for i, linha in enumerate(codigo):
            for m in _ATRIB.finditer(linha):
                atrib.setdefault(m.group(1), []).append(i)
            m2 = _DECL_NOMES.search(linha)
            if m2:
                for nome in (x.strip() for x in m2.group(1).split(",")):
                    atrib.setdefault(nome, []).append(i)
        mudou = False
        for i, linha in enumerate(linhas):
            m = _COPIA.fullmatch(linha)
            m_lit = _ATRIB_LITERAL.fullmatch(linha) if m is None else None
            if m is None and m_lit is None:
                continue
            x = (m or m_lit).group(2)
            if x not in decl:
                # escrita em global (registrador do VM): so' sai se o nome nao
                # aparece em nenhum outro lugar — dead store puro
                outros = [j for j in range(len(codigo)) if j != i and _usos(codigo[j], x)]
                if not outros:
                    linhas = linhas[:i] + linhas[i + 1:]
                    mudou = True
                    break
                continue
            if m is None:
                # `X = literal` com X local: so' sai se X nao for lido em lugar
                # nenhum (as outras ocorrencias seriam so' as declaracoes)
                outros = [j for j in range(len(codigo)) if j != i and _usos(codigo[j], x)]
                decls_x = {
                    j for j in range(len(codigo))
                    if (d := _DECL_NOMES.search(codigo[j]))
                    and x in [n.strip() for n in d.group(1).split(",")]
                }
                if all(j in decls_x for j in outros):
                    linhas = linhas[:i] + linhas[i + 1:]
                    mudou = True
                    break
                continue
            y = m.group(3)
            if x == y or y in _PALAVRAS or y not in decl:
                continue
            limites = [j for j in atrib.get(x, []) + atrib.get(y, []) if j > i]
            limite = min(limites) if limites else len(linhas)
            usos = [j for j in range(i + 1, limite) if _usos(codigo[j], x)]
            if not usos:
                linhas = linhas[:i] + linhas[i + 1:]
                mudou = True
                break
            novas = []
            for j, l in enumerate(linhas):
                if j == i or j >= limite or not _usos(codigo[j], x):
                    novas.append(l)
                    continue
                mascara, pos, pedacos = codigo[j], 0, []
                for mm in _usos(mascara, x):
                    pedacos.append(l[pos:mm.start()])
                    pedacos.append(y)
                    pos = mm.end()
                pedacos.append(l[pos:])
                novas.append("".join(pedacos))
            linhas = novas
            mudou = True
            break
        if not mudou:
            break
    return linhas


def _dedent(linha: str, n: int) -> str:
    """Tira ate' `n` espacos iniciais (usado ao reposicionar um bloco)."""
    corte = 0
    while corte < n and linha.startswith(" ", corte):
        corte += 1
    return linha[n:] if corte >= n else linha


def _junta_if_aninhado(linhas: list[str]) -> list[str]:
    """`if A then / if B then S1 else ::L:: S2 end / else / goto L / end`
    vira `if A and B then S1 else S2 end`.

    Exato nesse formato (e SO' nele): A&&B -> S1; A&&!B -> S2; !A -> goto -> S2.
    O then do if externo tem de ser so' o if interno (que tem de ter `else`, com
    o rotulo logo no comeco), e o else externo so' o goto. Melhor que duplicar:
    nao repete S2.
    """
    i = 0
    while i < len(linhas):
        m = re.match(r"^(\s*)if (.+) then\s*$", linhas[i])
        if not m:
            i += 1
            continue
        ind, cond = len(m.group(1)), m.group(2)
        t_ini, t_fim = _corpo_do_bloco(linhas, i)
        if t_fim >= len(linhas) or linhas[t_fim].strip() != "else":
            i += 1
            continue
        e_ini, e_fim = _corpo_do_bloco(linhas, t_fim)
        corpo_else = [l for l in linhas[e_ini:e_fim] if l.strip()]
        g = _GOTO.fullmatch(corpo_else[0]) if len(corpo_else) == 1 else None
        if g is None or len(corpo_else) != 1:
            i += 1
            continue
        # then: exatamente um bloco `if B then ... end` (bloco, nao linha)
        primeiro = next((x for x in range(t_ini, t_fim) if linhas[x].strip()), None)
        ultimo = next((x for x in range(t_fim - 1, t_ini - 1, -1) if linhas[x].strip()), None)
        m2 = re.match(r"^(\s*)if (.+) then\s*$", linhas[primeiro]) if primeiro is not None else None
        if not m2 or _indent(linhas[primeiro]) != ind + 2:
            i += 1
            continue
        cond_b = m2.group(2)
        b_ini, b_fim = _corpo_do_bloco(linhas, primeiro)      # corpo do then interno
        if b_fim >= len(linhas) or linhas[b_fim].strip() != "else" or _indent(linhas[b_fim]) != ind + 2:
            i += 1
            continue
        eb_ini, eb_fim = _corpo_do_bloco(linhas, b_fim)       # corpo do else interno
        if eb_fim >= len(linhas) or linhas[eb_fim].strip() != "end" or _indent(linhas[eb_fim]) != ind + 2:
            i += 1
            continue
        if eb_fim != ultimo:                                  # nada depois do if interno
            i += 1
            continue
        k = next((x for x in range(eb_ini, eb_fim) if linhas[x].strip()), None)
        mr = _ROTULO.match(linhas[k]) if k is not None else None
        if not mr or mr.group(1) != g.group(2):
            i += 1
            continue
        novo = [f"{' ' * ind}if {cond} and {cond_b} then"]
        novo += [_dedent(l, 2) for l in linhas[b_ini:b_fim]]
        novo += [f"{' ' * ind}else"]
        novo += [_dedent(l, 2) for l in linhas[k + 1:eb_fim]]
        novo += [f"{' ' * ind}end"]
        fim_externo = next((x for x in range(e_fim, len(linhas))
                            if linhas[x].strip() == "end" and _indent(linhas[x]) == ind), None)
        if fim_externo is None:
            i += 1
            continue
        linhas = linhas[:i] + novo + linhas[fim_externo + 1:]
    return linhas


def _corpo_then_apenas(linhas: list[str], then_corpo: list[int]) -> bool:
    """Checa se a unica linha do then e' um `if ... then` de bloco."""
    return len(then_corpo) == 1


def _corpo_apos_rotulo(linhas: list[str], li: int) -> tuple[int, int]:
    """(inicio, fim) do bloco do rotulo: o rotulo e' o 1o statement dele,
    entao o corpo tem a MESMA indentacao do rotulo (nao maior)."""
    ind = _indent(linhas[li])
    i = li + 1
    while i < len(linhas) and (not linhas[i].strip() or _indent(linhas[i]) >= ind):
        i += 1
    return li + 1, i


def _goto_vira_corpo(linhas: list[str]) -> list[str]:
    """`goto L` vira uma COPIA do corpo do rotulo `::L::`, e o rotulo sai.

    E' a saida EXATA pro control flow flattening do WAD: os rotulos do pdeobf
    ficam no comeco de um bloco e os `goto` pulam pra eles de outro ramo. A
    copia so' entra quando, do fim do corpo do rotulo ate' o fechamento do bloco
    do goto, nao existe NENHUM comando (so' `end`/`else`/vazio) — assim o fluxo
    depois da copia e' o mesmo do salto. Nada de adivinhar `if/else`: duplicar
    e' feio, mas nao muda comportamento.
    """
    while True:
        rotulos = {m.group(1): i for i, l in enumerate(linhas) if (m := _ROTULO.match(l))}
        alvo = None
        for g, linha in enumerate(linhas):
            m = _GOTO.fullmatch(linha)
            if not m or m.group(2) not in rotulos:
                continue
            li = rotulos[m.group(2)]
            ini, fim = _corpo_apos_rotulo(linhas, li)
            corpo = linhas[ini:fim]
            if not corpo:
                # rotulo vazio (ultimo statement do bloco): o salto cai no fim do
                # bloco e segue — com o guard abaixo valido, e' no-op puro.
                pass
            elif len(corpo) > 30:
                continue
            ind_g = _indent(linha)
            j, ok = fim, True
            while j < len(linhas):
                s = linhas[j].strip()
                if not s:
                    j += 1
                    continue
                if _indent(linhas[j]) < ind_g:
                    break                        # fechou o bloco do goto: fim limpo
                if s == "end" or s.startswith("until") or s == "else" or s.startswith("elseif"):
                    j += 1
                    continue
                ok = False                        # tem comando no meio: nao duplica
                break
            if not ok:
                continue
            if not corpo:
                linhas = linhas[:g] + linhas[g + 1:]
                alvo = True
                break
            delta = ind_g - _indent(linhas[li])
            novo = [
                ((_dedent(x, -delta) if delta < 0 else " " * delta + x).rstrip() if x.strip() else x)
                for x in corpo
            ]
            linhas = linhas[:g] + novo + linhas[g + 1:]
            alvo = True
            break
        if alvo is None:
            break
    # so' rotulos ORFAOS saem: se algum goto ainda aponta, o rotulo fica
    vivos = {m.group(2) for l in linhas if (m := _GOTO.fullmatch(l))}
    return [l for l in linhas if not (m := _ROTULO.match(l)) or m.group(1) in vivos]


def _tira_ponto_e_virgula(linhas: list[str]) -> list[str]:
    """`;comando` no comeco da linha: o `;` solto nao existe em Luau.

    Comando de UMA linha que comeca com `(` precisa de separacao do anterior e
    de um `local` de apoio (`(f()).X = v` nao pode abrir statement): vira
    `do local _rex = f(); _rex.X = v end`. Multi-linha fica como esta (mexer
    quebraria a continuacao).
    """
    saida: list[str] = []
    for linha in linhas:
        m = _PONTO_VIRGULA.match(linha)
        if not m or not m.group(2):
            saida.append(linha)
            continue
        recuo, resto = m.group(1), m.group(2)
        if not resto.startswith("("):
            saida.append(recuo + resto)
            continue
        prof, fecha = 0, -1
        for pos, ch in enumerate(resto):
            if ch == "(":
                prof += 1
            elif ch == ")":
                prof -= 1
                if prof == 0:
                    fecha = pos
                    break
        # a linha inteira tem de estar balanceada: statement multi-linha nao pode
        # ser embrulhado (o `do`/`end` cairia no meio da continuacao)
        saldo = sum(1 for ch in resto if ch == "(") - sum(1 for ch in resto if ch == ")")
        if fecha < 0 or saldo != 0 or "_rex" in "\n".join(linhas) or resto.rstrip().endswith(("function()", "function(", ",")):
            # multi-linha: separa passando o `;` pro fim da linha anterior
            # (terminador valido em Lua/Luau) e mantem o statement intacto
            j = len(saida) - 1
            while j >= 0 and (not saida[j].strip() or saida[j].lstrip().startswith("--")):
                j -= 1
            if j < 0:
                saida.append(linha)
                continue
            saida[j] = saida[j].rstrip() + ";"
            saida.append(recuo + resto)
            continue
        dentro, sufixo = resto[1:fecha], resto[fecha + 1:]
        if not sufixo or (not sufixo.startswith((".", ":", "["))):
            saida.append(linha)
            continue
        saida.append(f"{recuo}do")
        saida.append(f"{recuo}  local _rex = {dentro}")
        saida.append(f"{recuo}  _rex{sufixo}")
        saida.append(f"{recuo}end")
    return saida


def limpar(texto: str) -> str:
    """Aplica a limpeza inteira e devolve o texto (terminando em \\n)."""
    linhas = _tira_do_end_vazio(texto.splitlines())
    linhas = _tira_locais_mortos(linhas)
    linhas = _inline_aliases(linhas)
    linhas = _propaga_copias(linhas)
    linhas = _workspace_conhecido(linhas)
    linhas = _junta_if_aninhado(linhas)
    linhas = _goto_vira_corpo(linhas)
    linhas = _tira_ponto_e_virgula(linhas)
    linhas = _tira_locais_mortos(linhas)      # limpa decls que as passadas acima deixaram
    linhas = _compacta_brancos(linhas)
    while linhas and not linhas[0].strip():
        linhas.pop(0)
    return "\n".join(linhas).rstrip() + "\n"
