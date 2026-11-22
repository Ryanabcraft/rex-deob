"""Limpeza do output do luadec (rota MoonSec V3).

O luadec entrega a estrutura certa com tres vicios que atrapalham a leitura:

1. `local l_7_9, l_7_10 = nil` com a atribuicao nos dois comandos de baixo
   (`l_7_10 = 1; l_7_2 = #l_7_0`): vira `local l_7_9, l_7_10 = 1, #l_7_0`;
2. comentarios `-- DECOMPILER ERROR at PCx: ...` e `-- WARNING: undefined
   locals...` espalhados no meio do codigo: viram UM resumo no topo (a
   informacao fica, o ruido sai);
3. chamada entre parenteses em nome solto: `(task.wait)()` -> `task.wait()`.

Nada aqui reordena ou apaga comando de verdade: so' decl/atribuicao, comentario
de aviso e parenteses de nome.
"""
from __future__ import annotations

import re

_DECL_NIL = re.compile(r"^(\s*)local\s+([\w., ]+?)\s*=\s*nil\s*;?\s*$")
_ATRIB = re.compile(r"^(\s*)(\w+)\s*=\s*(.+?)\s*;?\s*$")
_AVISO = re.compile(r"^\s*--\s*(DECOMPILER ERROR at PC\d+:[^\n]*|WARNING:[^\n]*?)\s*$")
_RUIDO = re.compile(r"^\s*--\s*(Decompiled using luadec|Command line:)[^\n]*$")
_PAR_CHAMADA = re.compile(r"\(([A-Za-z_]\w*(?:[.:]\w+)*)\)\(")


def _nome(n: str) -> str:
    return rf"\b{re.escape(n)}\b"


def _merge_decl(linhas: list[str], i: int, decl: re.Match) -> tuple[str | None, int]:
    """`local a, b = nil` + atribuicoes imediatas -> um local unico.

    Só funde quando TODOS os nomes recebem valor nos comandos de baixo, na
    sequencia e sem o valor de um citar o outro (evita ciclo). Sem fundir,
    devolve o que fazer com a declaracao (None = linha sai, str = linha nova)
    e o indice onde continuar.
    """
    nomes = [n.strip() for n in decl.group(2).split(",") if n.strip()]
    j = i + 1
    valor: dict[str, str] = {}
    while j < len(linhas):
        m = _ATRIB.match(linhas[j])
        if not m or m.group(1) != decl.group(1):
            break
        nome, expr = m.group(2), m.group(3)
        if nome not in nomes or nome in valor:
            break
        valor[nome] = expr
        j += 1
    if len(valor) == len(nomes) and not any(
            re.search(_nome(n), v) for n in nomes for v in valor.values()):
        return (f"{decl.group(1)}local {', '.join(nomes)} = "
                + ", ".join(valor[n] for n in nomes)), j
    resto = "\n".join(linhas[j:])
    vivos = [n for n in nomes if re.search(_nome(n), resto)]
    if not vivos:
        return None, i + 1
    if len(vivos) != len(nomes):
        return f"{decl.group(1)}local {', '.join(vivos)} = nil", i + 1
    return linhas[i].rstrip(), i + 1


_ATRIB_SIMPLES = re.compile(r"^(\s*)(\w+)\s*=\s*([\w.]+)\s*;?$")
_ATRIB_CAMPO = re.compile(r"^(\s*)(\w+)\s*=\s*(\w+)\.([\w.]+)\s*;?$")


def _funde_campo(saida: list[str], linha: str) -> bool:
    """`X = A` seguido de `X = A.campo` -> `X = A.campo`.

    So' vale em comandos CONSECUTIVOS com o mesmo recuo (entre os dois nao pode
    haver nada que leia X): e' o `l_7_6 = table / l_7_6 = l_7_6.insert` que o
    luadec deixa antes de cada chamada.
    """
    if not saida:
        return False
    m2 = _ATRIB_CAMPO.match(linha)
    if not m2:
        return False
    m1 = _ATRIB_SIMPLES.match(saida[-1])
    if not m1 or m1.group(1) != m2.group(1) or m2.group(2) != m1.group(2):
        return False
    if m2.group(3) != m1.group(2):
        return False
    saida[-1] = f"{m2.group(1)}{m2.group(2)} = {m1.group(3)}.{m2.group(4)}"
    return True


def limpar(texto: str) -> str:
    linhas = texto.splitlines()
    avisos: list[str] = []
    saida: list[str] = []
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        if (m := _AVISO.match(linha)):
            avisos.append(m.group(1))
            i += 1
            continue
        if _RUIDO.match(linha):
            i += 1
            continue
        if _funde_campo(saida, linha):
            i += 1
            continue
        if (d := _DECL_NIL.match(linha)):
            nova, i2 = _merge_decl(linhas, i, d)
            if nova:
                saida.append(_PAR_CHAMADA.sub(r"\1(", nova))
            i = i2
            continue
        saida.append(_PAR_CHAMADA.sub(r"\1(", linha.rstrip()))
        i += 1
    while saida and not saida[0].strip():
        saida.pop(0)
    cabeca = []
    if avisos:
        pcs = [a for a in avisos if a.startswith("DECOMPILER ERROR")]
        warnings = [a for a in avisos if a.startswith("WARNING")]
        linha = f"--[[ luadec avisou {len(pcs)} ponto(s) com duvida"
        if warnings:
            linha += f" e {len(warnings)} trecho(s) com atribuicao perdida"
        linha += " — o fluxo pode estar errado nesses trechos"
        if pcs:
            trechos = ", ".join(re.search(r"PC(\d+)", a).group(1) for a in pcs)
            linha += f". PCs: {trechos}"
        linha += " ]]"
        cabeca = [linha, ""]
    return "\n".join(cabeca + saida).rstrip() + "\n"
