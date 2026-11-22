# Luraph v15 / v15.1 Deobfuscator (edição consolidada)

Recupera o código Luau legível de scripts protegidos com **Luraph v15 / v15.1**.
Roda o script num sandbox Roblox offline, decifra as constantes ao vivo, faz o
lifting do bytecode da VM de volta para `if/for/while/funções` e limpa o lixo.

## Requisitos

- **Linux** ou **macOS / Windows via WSL** (o `bin/luau` incluído é Linux x64)
- **Node.js 18+** e **Python 3.10+** — sem `npm install`, sem `pip install`

## Uso

```bash
chmod +x bin/luau bin/luau-ast
python3 luraph_deobf.py meu_script.lua                 # -> output/meu_script.deobf.lua
python3 luraph_deobf.py meu_script.lua -o limpo.lua
python3 luraph_deobf.py pasta_de_scripts/               # todos os .lua/.luau/.txt
python3 luraph_deobf.py meu_script.lua --trace          # só o que o script FAZ (~2 s)
python3 luraph_deobf.py meu_script.lua --game-id 1686885941 --place-id 4924922222
```

Saída típica:

```
[*] obfuscator: Luraph v15 (detected, 1.00)
[*] 31 Path2D answers computed by the offline engine model
[*]   candidate python:3: 5 functions, 0 unlifted (19s)
[+] output/meu_script.deobf.lua  (73 linhas, 23s)
```

## Scripts "travados por jogo" (game-locked)

Se a saída tiver só a casca anti-tamper (~40 funções com `Path2D`, `DescendantAdded`)
ou der `size out of range`, o script mistura o `game.GameId` na chave. Passe
`--game-id` (universe id, não o place id). Detalhes: `SETUP.md` seção 3.

Loaders com key-system / autenticação online não podem ser decifrados offline.

## O que esta edição corrige (em relação aos 7 repositórios de base)

Todos os repositórios enviados (ServerDeadd, caomod2077, MrAhmed13, Vermex221,
krglo-code, sikon789, mehCake) são forks ou variações do mesmo motor
(kers / caomod2077). Esta edição parte da versão mais completa (ServerDeadd,
`tests/run_samples.py` 100%) e corrige o que fazia o v15.1 atual falhar:

1. **Fingerprint de teto de pilha (o principal).** O build atual do v15.1
   recursa até a profundidade 19997 e testa `pcall`/`xpcall` exatamente no limite
   do Luau do Roblox. O `luau` patcheado dos repositórios isentava *qualquer*
   função C do limite, então o `pcall` do script ganhava 1 nível a mais que no
   Roblox; o Luraph detectava, corrompia o próprio bytecode e o payload nunca
   carregava (todos os 7 repositórios devolviam só a casca anti-tamper).
   Agora só frames C chamados *pelo sandbox* são isentos
   (`build_luau.py: patch_harness_frames`) — o script vê o limite idêntico ao Roblox.
2. **`Path2DControlPoint.new()` sem argumentos** é válido no Roblox; o sandbox
   rejeitava, o que também disparava o anti-tamper.
3. **Botões liga/desliga**: cada handler de evento roda uma 2ª vez (fora do trace),
   então o segundo ramo de um toggle (`flag = not flag`) executa e as constantes
   criptografadas dele são decifradas.
4. **Rodadas de constantes** continuam quando ainda há constantes pendentes, mesmo
   com todos os blocos recuperados; vence a rodada com menos `constant not decoded`.
5. **`polish.py`**: remove closures internas do Luraph não usadas, reconstrói
   `X.Evento:Connect(function() ... end)`, converte `Color3.new` exato em
   `Color3.fromRGB`, fecha linhas em branco deixadas pela limpeza. Cada passo é
   re-validado com `luau-ast`.
6. **`luraph_deobf.py`**: um comando faz tudo (deob → luaclean → polish), aceita
   pasta inteira e arquivos `.lua.txt`.

Regressão: `python3 tests/run_samples.py` — 6 amostras (5 originais + o arquivo
de teste v15.1) idênticas byte a byte.

## Melhorias da v2 (esta entrega)

Testado em 10 scripts Luraph v15.1: as 5 amostras de referência (continuam
idênticas byte a byte) + 5 scripts reais novos em `samples_v15.1_extra/`.

1. **Crash de recursão infinita no lifter corrigido** (`devirt.py: _resolve_jval`).
   Uma cópia de registrador em ciclo (`a = b; b = a`, ou `r = r`) fazia o Python
   estourar a pilha e a função inteira era perdida. No `ncinside.lua` (gist, 1 MB)
   a saída foi de **30 linhas para 1312 linhas** de código real.
2. **Bloco sem sucessor não derruba mais a closure inteira** (`structure.py:
   thread_empty`): vira um `error("devirt: ...")` só naquele bloco, como o resto da
   ferramenta (fail-loud), em vez de `IndexError` e closure perdida.
3. **`bit32.*` com operandos simbólicos** (resultado de chamada, `nil`, varargs)
   agora é emitido como chamada normal, em vez de `error("devirt: builtin
   bit32.bxor on symbolic values")`.
4. **Loaders não geram mais arquivo vazio** (`src/driver.js`): quando o lift tem 0
   funções (o payload real vem de `HttpGet` + `loadstring`), a saída é o trace de
   comportamento, que mostra a URL do payload. Antes: arquivo de 0 bytes.
5. **Detecção mais robusta** (`src/detect.js`): o header do Luraph é procurado nos
   primeiros 64 KB (scripts republicados com banners `-- skid skid...` ou linhas de
   loader antes do header eram tratados como "obfuscador desconhecido"), e o
   formato sem header `return({...}):X(...)` também é reconhecido.
6. **`polish.py` mais legível** (cada passo re-validado com `luau-ast`):
   - `local x` seguido de `x = valor` vira `local x = valor` (não mexe quando o
     valor cita `x`, p.ex. funções recursivas);
   - cópias de registrador inúteis (`local v12 = Vector3New` usada uma vez logo
     depois, no mesmo bloco) são removidas e o nome original volta ao uso.
   No sample grande (99ba, 666 funções): 758 → 263 cópias, −612 linhas.
7. **Testes novos**: `python3 tests/test_polish.py` e `node tests/test_detect.js`.
8. **Diagnóstico**: `DEVIRT_TB=1` imprime o traceback de cada função que falhar no
   lift (rode `src/devirt_bridge.py pipeline <cfg>` com `DEOB_KEEP_CFG=1`).

Limitações conhecidas: loaders com payload remoto (luaprotect, Luarmor) só mostram
a URL; scripts game-locked precisam de `--game-id`; alguns blocos do anti-tamper do
Luraph continuam como `error("devirt: ...")`.

## Melhorias da v3 (rodada com +10 scripts v15.1)

Testado em 10 scripts **v15.1** novos (todos com o cabeçalho
`Luraph Obfuscator v15.1`), de 5 fontes diferentes: SixZensED/luau-obfuscator-samples
(originais de apiiidj/lua), Dev-Meoz/Luraph-sample, mehCake, YienYun/Luraph,
smurfcatnoob1-boop. Ficam em `samples_v15.1_round2/` (com as saídas em `out/`).

| Script | Antes | Depois | Observação |
| --- | --- | --- | --- |
| r01 apiiidj script_5846cb6c | 12 linhas | 12 | **cabeçalho falso**: é o "Goofyscator", não Luraph |
| r02 sixzens 12 | timeout | 4611 linhas | precisa de ~10 min (use `--timeout`) |
| r03 AutoGatling | 5409 | 5409 | 58 constantes lazy não decodificadas |
| r04 SacarficeHub Aimbot | 2445 | 2445 | limpo, 0 blocos falhos |
| r05 devmeoz 99271388123 | 65 | **12295** | game-locked: `--place-id 2753915549` (Blox Fruits) |
| r06 mehCake gwigren | 266 | 266 | limpo |
| r07 YienYun Lph5 | 332 | 332 | script entra em loop infinito (normal p/ hubs) |
| r08 YienYun Lph4 | 4368 | 4368 | protegido por FlowAuth (precisa de loader autenticado) |
| r09 smurfcat loadstring_10 | 8 | **17011** | chunk estágio-2: 245 funções não-virtualizadas recuperadas |
| r10 devmeoz 0929290930932 | 73 | 73 | anti-tamper Path2D derruba o script (`#number`) — em aberto |

O que mudou:

1. **Funções não-virtualizadas** (`LPH_NO_VIRTUALIZE`): `tools/plain_funcs.py`
   extrai os closures em Luau puro que o Luraph deixa dentro da tabela da VM e
   formata. Quando o lift dá 0 funções, a saída agora inclui esse código
   (r09: 8 → 17011 linhas legíveis do mspaint). Também roda sozinho:
   `python3 tools/plain_funcs.py bin/luau-ast script.lua saida.lua`.
2. **`--place-id` / `--game-id` no `deob.js`** (antes só no wrapper Python), e
   `--cfg-json` agora soma com eles em vez de sobrescrever.
3. **Globais que não existem no Roblox** (`_ENV`, `jit`, `arg`, `package`, `io`,
   `dofile`, `loadfile`, `module`) agora são `nil` no sandbox, em vez de um proxy
   "sempre chamável" (`_ENV or getfenv()` pegava o proxy e quebrava VMs customizadas).
4. **Mensagens de erro legíveis**: `not lifted: <luasym.Multi object at 0x..>`
   virou `KeyError (unexpected Multi value)`.
5. **Diagnóstico**: `DEOB_SAVE_HARNESS=/tmp/h.luau` salva o harness gerado para
   rodar direto com `bin/luau` e bisectar diferenças de ambiente.
6. Teste novo: `python3 tests/test_plain_funcs.py`. Regressão
   (`tests/run_samples.py`): 4 idênticos + 1 tolerado, 0 falhas.

## Recompilar o runtime Luau

```bash
python3 build_luau.py          # precisa de git, cmake e um compilador C++
```

## Uso responsável

Ferramenta de análise defensiva: veja o que um script faz antes de executá-lo.
Use em código seu ou que você tem permissão para analisar.

## Melhorias da v4

**Problema principal encontrado:** quando o Luraph decodifica uma constante muito
grande, o Luau dá `memory allocation error: block too big`. Antes, **uma única**
falha dessas desligava a decodificação "ao vivo" de constantes para o script
inteiro, e todas as outras ficavam como `nil --[[ constant not decoded ]]`.
Agora só aquela constante é pulada; a decodificação ao vivo só é desligada depois
de 8 falhas seguidas (`DEVIRT_FETCH_FAILS=N` muda esse número).

| Script | v3 | v4 |
|---|---|---|
| r03 AutoGatling | 5409 linhas, 895 constantes faltando, 144 funções | **7005 linhas, 67 faltando, 164 funções** |
| r05 Blox Fruits (`--place-id 2753915549`) | 310 constantes faltando | **133 faltando** |
| ff_autoplay (Funky Friday) | 131 linhas (só o anti-tamper) | **5381 linhas** (lógica do autoplay) |
| ncinside | 1311 linhas | 1390 linhas |
| r02, r04, r07, r08 | — | iguais (já não tinham esse problema) |

Outras correções:
- Valor desconhecido no meio de uma expressão era escrito como `--[[?LTable]]`
  sozinho, o que gerava Lua inválido (`f(--[[?LTable]], x)`). Agora sai
  `nil --[[?LTable]]` e o arquivo continua válido (r05 agora passa no parser).
- `DEVIRT_BISECT=1` (dividir o pedido de constantes ao meio quando dá
  "block too big") continua opcional: ligado por padrão ficou lento demais.
- Novo teste: `tests/test_live_fetch.py`.

Regressão: os 5 exemplos públicos continuam 4 iguais byte a byte + 1 tolerado.
