"""Mutación manual de backend/profit.py: introduce un error de fórmula a la vez en una COPIA temporal y comprueba que
tests/test_profit.py lo detecta (la prueba debe fallar). Un mutante que sobrevive = hueco en las pruebas.
Ejecutar:  python3 tests/mutation_profit.py        (código de salida 1 si sobrevive alguno)"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MUTANTS = [
    # (nombre, texto original (debe aparecer), reemplazo)
    ("devoluciones: ignora fletes y empaque de la devolución", "- r * loss_returned", "- r * freight"),
    ("devoluciones: intercambia entregados y devueltos", "d, r = (100 - ret_pct) / 100, ret_pct / 100", "r, d = (100 - ret_pct) / 100, ret_pct / 100"),
    ("devoluciones: olvida el flete de devolución", "loss_returned = freight + ret_freight + pack", "loss_returned = freight + pack"),
    ("devoluciones: olvida la pérdida del producto", "+ cost * loss_pct / 100      # una devolución", "+ 0      # una devolución"),
    ("devoluciones: efectividad no se invierte", 'ret_pct = 100 - v["delivery_rate_pct"]', 'ret_pct = v["delivery_rate_pct"]'),
    ("CPA máximo: usa la utilidad con publicidad", "max_cpa = max(before_ads, Decimal(0))", "max_cpa = max(profit_confirmed, Decimal(0))"),
    ("CPA máximo: permite negativos", "max_cpa = max(before_ads, Decimal(0))", "max_cpa = before_ads"),
    ("ROAS de equilibrio: invertido", '_q(price / max_cpa, 2) if max_cpa', '_q(max_cpa / price, 2) if max_cpa'),
    ("ROAS objetivo: multiplica en vez de dividir", "cpa, roas_in = price / v[\"target_roas\"]", "cpa, roas_in = price * v[\"target_roas\"]"),
    ("utilidad: suma el CPA", "profit_confirmed = before_ads - cpa", "profit_confirmed = before_ads + cpa"),
    ("por entregado: no divide por la efectividad", "q(profit_confirmed / d) if d > 0", "q(profit_confirmed) if d > 0"),
    ("por 100: redondea antes de multiplicar", "q(profit_confirmed * 100)", "q(profit_confirmed) * 100"),
    ("margen: sobre el precio y no sobre lo cobrado", "\"margin_pct\": _q(profit_confirmed / revenue * 100", "\"margin_pct\": _q(profit_confirmed / price * 100"),
    ("recaudo: olvida la comisión fija", "price * fee_pct / 100 + fee_fix", "price * fee_pct / 100"),
    ("redondeo bancario en vez de mitad hacia arriba", "rounding=ROUND_HALF_UP)\n    return float(r)", "rounding=ROUND_HALF_EVEN)\n    return float(r)"),
    ("aviso de devoluciones: umbral inclusivo", "if ret_pct > warn_pct:", "if ret_pct >= warn_pct:"),
    ("margen bajo: umbral inclusivo (10,0 exacto)", "profit_confirmed / revenue * 100 < LOW_MARGIN_PCT:\n        result", "profit_confirmed / revenue * 100 <= LOW_MARGIN_PCT:\n        result"),
    ("aviso de margen bajo: umbral inclusivo", "elif revenue > 0 and profit / revenue * 100 < LOW_MARGIN_PCT:", "elif revenue > 0 and profit / revenue * 100 <= LOW_MARGIN_PCT:"),
    ("pérdida: utilidad 0 cuenta como pérdida", "if profit_confirmed < 0:                  # el veredicto", "if profit_confirmed <= 0:                  # el veredicto"),
    ("aviso de pérdida: utilidad 0 cuenta como pérdida", "    if profit < 0:\n        unit", "    if profit <= 0:\n        unit"),
    ("tolerancia de devoluciones+efectividad de 0,01 a 1", '> Decimal("0.01")', '> Decimal("1")'),
    ("roas_used sin exigir precio > 0", "if cpa > 0 and price > 0 else None", "if cpa > 0 else None"),
    ("-0 sin normalizar", "return float(r) if r != 0 else 0.0", "return float(r)"),
    ("money_text sin signo negativo", "{'-' if amount < 0 else ''}", "{''}"),
    ("target_roas sin redondear en inputs", "_q(roas_in, 2) if roas_in", "float(roas_in) if roas_in"),
    ("veredicto con utilidad redondeada", 'elif revenue > 0 and profit_confirmed / revenue * 100 < LOW_MARGIN_PCT:', 'elif revenue > 0 and result["margin_pct"] < 10:'),
    ("validación: acepta porcentajes sobre 100", "if not 0 <= n <= 100:", "if not 0 <= n <= 101:"),
]


def main() -> int:
    work = tempfile.mkdtemp(prefix="mut-profit-")
    try:
        shutil.copytree(os.path.join(ROOT, "backend"), os.path.join(work, "backend"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.db*"))
        os.makedirs(os.path.join(work, "tests"))
        shutil.copy(os.path.join(ROOT, "tests", "test_profit.py"), os.path.join(work, "tests"))
        target = os.path.join(work, "backend", "profit.py")
        original = open(target, encoding="utf-8").read()
        env = {**os.environ, "DROPFINDER_SYNTH_HISTORY": "0"}
        env.pop("DROPFINDER_DB", None)

        def run() -> bool:
            r = subprocess.run([sys.executable, "-m", "unittest", "test_profit"], cwd=os.path.join(work, "tests"),
                               env=env, capture_output=True, text=True)
            return r.returncode == 0

        assert run(), "las pruebas deben pasar sin mutar"
        survivors = []
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"?  {name}: el texto original aparece {original.count(old)} veces (actualiza el mutante)")
                survivors.append(name)
                continue
            open(target, "w", encoding="utf-8").write(original.replace(old, new))
            killed = not run()
            print(f"{'MUERTO    ' if killed else 'SOBREVIVE '} {name}")
            if not killed:
                survivors.append(name)
        print(f"\n{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} mutantes detectados")
        return 1 if survivors else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
