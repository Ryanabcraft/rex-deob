"""Conservative text cleanups applied to engine output before delivery."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from . import moonsec_clean, pdeobf_clean  # noqa: E402


def clean_pdeobf(text: str) -> str:
    try:
        return pdeobf_clean.limpar(text)
    except Exception:
        return text


def clean_luadec(text: str) -> str:
    try:
        return moonsec_clean.limpar(text)
    except Exception:
        return text
