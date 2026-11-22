"""Benchmark de la búsqueda: crea una base temporal con N productos de demostración y mide las consultas
típicas de /api/products a través del servidor HTTP real (mediana de varias corridas).

Uso:  python3 bench.py [--sizes 1500 50000 100000] [--runs 7]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import auth_context  # noqa: E402
import db  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402

QUERIES = [
    ("sin filtro", "/api/products"),
    ("q=cargador", "/api/products?q=cargador"),
    ("q=lamparas", "/api/products?q=lamparas"),
    ("q=cargdor (corrige)", "/api/products?q=cargdor"),
    ("min_margin=40", "/api/products?min_margin=40"),
    ("categoría+proveedor+orders", "/api/products?category=" + quote("Electrónica") + "&supplier=Amazon&sort=orders"),
    ("página 50", "/api/products?page=50"),
    ("q=cargador + filtros", "/api/products?q=cargador&min_margin=40&in_stock=1&sort=orders"),
    ("facetas", "/api/facets"),
    ("autocompletar 'carg'", "/api/suggest?q=carg"),
    ("autocompletar 'l'", "/api/suggest?q=l"),
]


_cookie: dict = {}


def _get(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, headers={"Cookie": _cookie["value"]} if _cookie else {})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def bench(size: int, runs: int) -> list[tuple[str, float, int]]:
    tmp = tempfile.mkdtemp(prefix="dropfinder-bench-")
    old_path = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "bench.db")
    srv = None
    try:
        t0 = time.perf_counter()
        db.init_db(seed_size=size)
        seed_ms = (time.perf_counter() - t0) * 1000
        # Las rutas exigen sesión: se crea un propietario y una sesión temporales solo para medir
        with db.connect() as conn:
            uid = auth.create_first_owner(conn, "bench@local.test", "Bench", auth.hash_password("clave-bench-12345"))
            token, _csrf = auth.create_session(conn, uid)
        _cookie["value"] = f"{auth.COOKIE_NAME}={token}"
        auth_context.set_current(uid, "owner")      # server.update_settings se llama directo, sin HTTP
        srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        # Cambio de reglas de precio: recalcula las métricas de todo el catálogo
        t0 = time.perf_counter()
        server.update_settings({"multiplier": 2.6})
        settings_ms = (time.perf_counter() - t0) * 1000
        server.update_settings({"multiplier": 2.5})

        out = [("carga inicial (seed)", seed_ms, size), ("PUT /api/settings (recalcula)", settings_ms, size)]
        for label, path in QUERIES:
            _get(base, path)                       # calienta cachés (facetas, vocabulario, títulos)
            times, total = [], 0
            for _ in range(runs):
                t0 = time.perf_counter()
                d = _get(base, path)
                times.append((time.perf_counter() - t0) * 1000)
            total = d.get("total", len(d.get("items", [])))
            out.append((label, statistics.median(times), total))
        return out
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        auth_context.clear()
        _cookie.clear()
        db.DB_PATH = old_path
        search.invalidate()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1500, 50_000, 100_000])
    ap.add_argument("--runs", type=int, default=7)
    args = ap.parse_args()
    results = {n: bench(n, args.runs) for n in args.sizes}
    labels = [r[0] for r in next(iter(results.values()))]
    head = f"{'consulta':<32}" + "".join(f"{f'{n:,} prod.':>22}" for n in args.sizes)
    print(head)
    print("-" * len(head))
    for i, label in enumerate(labels):
        cells = "".join(f"{f'{results[n][i][1]:,.1f} ms ({results[n][i][2]:,})':>22}" for n in args.sizes)
        print(f"{label:<32}{cells}")
    print("\n(ms = mediana de", args.runs, "corridas por HTTP; entre paréntesis, total de resultados)")


if __name__ == "__main__":
    main()
