================================================================
 DEOBFUSCADOR LURAPH v15 / IRONBREW1 / GENERIC  —  COPIA COMPLETA
================================================================

QUÉ ES
------
Motor de desofuscación de scripts Lua/Luau (Roblox) con:
  - Detección automática del ofuscador (ironbrew1, luraph_v15,
    generic) con nivel de confianza.
  - Devirtualización completa: ejecuta el script en un VM Luau
    instrumentado (envlog), captura las funciones internas y
    reconstruye código fuente legible.
  - Plugin Luraph v15: macros LPH_JIT / LPH_NOVIRTUALIZE /
    LPH_ENCSTR / LPH_ENCBUF / LPH_PRECHECK, strings cifradas,
    plegado de constantes, renombrado de variables IllIlIl.
  - Plugin IronBrew1: VM de dispatch, extracción de bytecode.
  - Plugin genérico: limpieza de anti-tamper, formateo.

REQUISITOS
----------
  - Linux x86_64 (los binarios incluidos son estáticos).
  - Python 3.8+ (SOLO stdlib: no hace falta instalar nada con pip).
  - Los binarios `bin/luau` y `bin/luau-ast` ya vienen compilados
    (Luau 0.739 + parche del vector metatable), así que NO
    necesitas compilar nada para usarlo.

USO
---
  # Detectar el ofuscador:
  python3 deobf/deob.py script.lua --detect

  # Desofuscar completo (detect + devirt + output):
  python3 deobf/deob.py script.lua -o salida.lua

  # Solo devirt (sin reconstrucción completa):
  python3 deobf/deob.py script.lua --no-devirt -o salida.lua

  # Trace del entorno (loguea lo que toca el script):
  python3 deobf/deob.py script.lua --trace

  # Probar que todo funciona (sample incluido):
  python3 deobf/deob.py samples/001_vm_like_dispatch-ib1.lua --detect
  -> debe responder: ironbrew1  1.00

ESTRUCTURA
----------
  deobf/
  ├── deobf/            <- motor (NO mover de sitio, usa rutas relativas)
  │   ├── deob.py       <- punto de entrada
  │   ├── *.py          <- motor: lexer, ir, fold, structure, codegen...
  │   ├── envlog.luau   <- runtime instrumentado que captura las VMs
  │   ├── datatypes.luau / roblox_api.luau / unicode_data.luau
  │   ├── bin/luau      <- VM Luau compilada (estática)
  │   ├── bin/luau-ast  <- impresor de AST (estático)
  │   ├── obfuscators/  <- plugins: luraph_v15, ironbrew1, generic
  │   └── research/     <- herramientas de verificación/regresión
  ├── samples/          <- ejemplos para probar
  ├── CLAUDE.md         <- documentación interna del motor (inglés)
  ├── LURAPH.md         <- notas del formato Luraph v15
  ├── IRONBREW1.md      <- notas del formato IronBrew1
  └── README_DEOB.txt   <- este archivo

  scripts/              <- en la raíz del zip, junto a deobf/
  └── build_luau_full.sh  <- por si algún día hay que recompilar
       luau/luau-ast (ver abajo). La fuente de Luau (131 MB) NO
       va en el zip: los binarios ya vienen compilados.

RECOMPILAR LOS BINARIOS (solo si hace falta)
--------------------------------------------
  1. Descargar el código fuente de Luau 0.739:
     https://github.com/luau-lang/luau (tag 0.739)
  2. Ponerlo en scripts/luau-src/
  3. Ejecutar:  bash scripts/build_luau_full.sh
     El script aplica automáticamente el parche del vector
     metatable en VM/src/lveclib.cpp (deja el metatable
     escribible para que envlog.luau añada los miembros de
     Vector3 de Roblox).
  Nota de la máquina original: 2 núcleos / 3.9 GB RAM → sin LTO
  y --parallel 2 para no morir de OOM. Si tu máquina tiene más
  recursos puedes subir el paralelismo.

LÍMITES CONOCIDOS
-----------------
  - Entrada máx. ~8 MB por script (límite práctico del traceo).
  - El devirt ejecuta el script: solo pasarlo sobre código del
    que eres dueño o hayas revisado (como siempre hemos hecho
    con los dumps de StealAnEgg519 / CokeBoys).
  - Si la detección devuelve conf. baja, probar --detect con
    otro sample o forzar el plugin a mano en deob.py.

COPIA DE SEGURIDAD
------------------
  Este zip es autocontenido: extráelo en cualquier Linux con
  Python 3 y funciona sin instalar nada. Guárdalo en tu PC.

----------------------------------------------------------------
  NUEVO: PLUGIN LURAPH v14 (deob_Luraph_v14Full)
----------------------------------------------------------------

  Soporte para Luraph Obfuscator v14.x (probado con v14.2:
  BloxFruitFreeV3.lua de HohoV2).

  # Detectar:
    python3 deobf/deob.py script.lua --detect
    -> luraph_v14  1.00  Luraph v14

  # Trazado de comportamiento (recomendado para v14 hoy):
    python3 deobf/deob.py script.lua --obfuscator luraph_v14 --no-devirt \
        --strings -o salida.luau

  # Devirtualizacion (parcial: ve LURAPH14.md "What does not work yet"):
    python3 deobf/deobf/obfuscators/luraph_v14/devirt.py script.lua \
        script.lua.protos.json --lift <pid>

  Detalles del formato v14 (dispatch por busqueda binaria con not(...),
  proto = parametro 0 del maker, upvalues de nivel main capturados con
  __SNAP, instrucciones con campos perezosos decodificados por una VM
  decoficadora) estan documentados en LURAPH14.md.
