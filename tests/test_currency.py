"""Pruebas del módulo de moneda (sin red: la TRM se simula con un `opener` falso).

Ejecutar:  python3 -W error::ResourceWarning -m unittest tests.test_currency
"""
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import currency  # noqa: E402
import authtest  # noqa: E402,F401  (contexto y sesión del propietario)
import db  # noqa: E402


def body_opener(payload, calls=None):
    """Opener falso que devuelve `payload` (str/bytes, o objeto que se serializa a JSON)."""
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    raw = raw.encode() if isinstance(raw, str) else raw

    def opener(req, timeout=None):
        if calls is not None:
            calls.append((req, timeout))
        return io.BytesIO(raw)
    return opener


def raising_opener(exc):
    def opener(req, timeout=None):
        raise exc
    return opener


def trm_row(valor="4123.45", desde="2026-11-21T00:00:00.000", hasta="2026-11-21T00:00:00.000"):
    return [{"valor": valor, "unidad": "COP", "vigenciadesde": desde, "vigenciahasta": hasta}]


def today_iso(days_ago=0):
    return (currency._today_co() - timedelta(days=days_ago)).isoformat()


class CurrencyDBTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.conn.execute("DROP TABLE IF EXISTS exchange_rates")
        self.conn.execute("DROP TABLE IF EXISTS currency_settings")
        self.conn.execute("DROP TABLE IF EXISTS user_currency_settings")
        currency.ensure_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    # ── Ajustes ──
    def test_defaults(self):
        self.assertEqual(currency.get_settings(self.conn), currency.DEFAULTS)
        currency.ensure_schema(self.conn)  # idempotente, no pisa valores
        currency.update_settings(self.conn, {"hedge_pct": 3})
        currency.ensure_schema(self.conn)
        self.assertEqual(currency.get_settings(self.conn)["hedge_pct"], 3.0)
        st = currency.get_state(self.conn)
        self.assertEqual([c["code"] for c in st["currencies"]], ["USD", "COP"])
        self.assertEqual(st["currencies"][1], {"code": "COP", "symbol": "$", "decimals": 0, "locale": "es-CO"})

    def test_update_and_normalize(self):
        s = currency.update_settings(self.conn, {"display_currency": "cop", "rate_mode": "manual",
                                                 "manual_rate": "4100,5", "cop_rounding": 1000,
                                                 "vat_enabled": True, "vat_pct": 19})
        self.assertEqual(s["display_currency"], "COP")
        self.assertEqual(s["manual_rate"], 4100.5)
        self.assertEqual(s["cop_rounding"], "1000")
        self.assertIs(s["vat_enabled"], True)

    def test_validation_errors(self):
        bad = [{"display_currency": "EUR"}, {"display_currency": 1}, {"rate_mode": "auto"},
               {"manual_rate": 0}, {"manual_rate": -5}, {"manual_rate": "abc"}, {"manual_rate": True},
               {"manual_rate": float("nan")}, {"hedge_pct": -1}, {"hedge_pct": 50.1},
               {"cop_rounding": "500"}, {"cop_rounding": True}, {"vat_enabled": "si"}, {"vat_enabled": 1},
               {"vat_pct": 101}, {"vat_pct": -0.1}, {"color": "rojo"}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError) as cm:
                currency.update_settings(self.conn, body)
            self.assertTrue(str(cm.exception))
        with self.assertRaises(ValueError):
            currency.update_settings(self.conn, ["no", "dict"])
        # atómico: si un campo falla no se guarda ninguno
        with self.assertRaises(ValueError):
            currency.update_settings(self.conn, {"hedge_pct": 5, "vat_pct": 500})
        self.assertEqual(currency.get_settings(self.conn), currency.DEFAULTS)
        # límites válidos
        currency.update_settings(self.conn, {"hedge_pct": 50, "vat_pct": 0})

    def test_corrupt_setting_falls_back(self):
        self.conn.execute("UPDATE currency_settings SET value = 'no-json' WHERE key = 'manual_rate'")
        self.conn.execute("UPDATE currency_settings SET value = '\"XYZ\"' WHERE key = 'display_currency'")
        s = currency.get_settings(self.conn)
        self.assertEqual(s["manual_rate"], 4000.0)
        self.assertEqual(s["display_currency"], "USD")

    # ── Tasas ──
    def test_trm_mode_without_data_falls_back_to_manual(self):
        info = currency.get_state(self.conn)["rate_info"]
        self.assertEqual(info["source"], "manual")
        self.assertEqual(info["base_rate"], 4000.0)
        self.assertIn("tasa manual", info["error"])
        self.assertFalse(info["stale"])

    def test_manual_mode_and_hedge(self):
        currency.update_settings(self.conn, {"rate_mode": "manual", "manual_rate": 4000, "hedge_pct": 3})
        st = currency.get_state(self.conn)
        self.assertEqual(st["rates"], {"USD": 1.0, "COP": 4120.0})
        self.assertEqual(st["rate_info"]["source"], "manual")
        self.assertEqual(st["rate_info"]["base_rate"], 4000.0)
        self.assertEqual(st["rate_info"]["hedge_pct"], 3.0)
        self.assertIsNone(st["rate_info"]["error"])

    def test_refresh_success_and_history(self):
        calls = []
        st = currency.refresh_trm(self.conn, opener=body_opener(trm_row(desde=today_iso() + "T00:00:00.000"), calls))
        info = st["rate_info"]
        self.assertEqual((info["source"], info["base_rate"], info["date"]), ("trm", 4123.45, today_iso()))
        self.assertFalse(info["stale"])
        self.assertIsNone(info["error"])
        req, timeout = calls[0]
        self.assertEqual(timeout, 6.0)
        self.assertIn("32sa-8pi3.json?$order=vigenciadesde%20DESC&$limit=1", req.full_url)
        self.assertTrue(req.get_header("User-agent").startswith("DropFinder/1.0"))
        # trm + cobertura
        currency.update_settings(self.conn, {"hedge_pct": 2})
        self.assertAlmostEqual(currency.get_state(self.conn)["rates"]["COP"], 4123.45 * 1.02, places=3)
        # una segunda TRM (otro día) queda en el historial; la misma fecha se actualiza
        currency.refresh_trm(self.conn, opener=body_opener(trm_row("4000", today_iso(1))))
        currency.refresh_trm(self.conn, opener=body_opener(trm_row("4001", today_iso(1))))
        hist = currency.rate_history(self.conn)
        self.assertEqual([(h["date"], h["rate"]) for h in hist], [(today_iso(), 4123.45), (today_iso(1), 4001.0)])
        self.assertEqual(currency.get_state(self.conn)["rate_info"]["base_rate"], 4123.45)  # la más reciente

    def test_refresh_failures_keep_last(self):
        currency.refresh_trm(self.conn, opener=body_opener(trm_row("4200", today_iso())))
        failures = [
            body_opener("{no es json"), body_opener([]), body_opener({"error": "x"}), body_opener(b"\xff\xfe"),
            body_opener(trm_row(valor="abc")), body_opener(trm_row(valor="-1")), body_opener(trm_row(desde="ayer")),
            body_opener([{"valor": "4000", "unidad": "USD", "vigenciadesde": "2026-11-21T00:00:00"}]),
            raising_opener(urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)),
            raising_opener(urllib.error.URLError("sin red")),
            raising_opener(socket.timeout("timed out")), raising_opener(TimeoutError()),
            raising_opener(ConnectionResetError()),
        ]
        for op in failures:
            with self.subTest(op=op):
                self.assertIsNone(currency.fetch_trm(opener=op))
                info = currency.refresh_trm(self.conn, opener=op)["rate_info"]
                self.assertEqual((info["source"], info["base_rate"]), ("trm", 4200.0))
                self.assertIn("datos.gov.co", info["error"])
        self.assertEqual(len(currency.rate_history(self.conn)), 1)

    def test_refresh_failure_without_data(self):
        info = currency.refresh_trm(self.conn, opener=raising_opener(urllib.error.URLError("x")))["rate_info"]
        self.assertEqual(info["source"], "manual")
        self.assertIn("datos.gov.co", info["error"])
        self.assertIn("tasa manual", info["error"])

    def test_fetch_parses_fields(self):
        r = currency.fetch_trm(opener=body_opener(trm_row("3,987.10", "2026-11-22T00:00:00.000",
                                                          "2026-11-24T00:00:00.000")))
        self.assertEqual(r, {"rate": 3987.1, "date": "2026-11-22", "valid_until": "2026-11-24", "source": "trm"})

    def test_stale_flag(self):
        for days, stale in ((0, False), (3, False), (4, True), (30, True)):
            with self.subTest(days=days):
                self.conn.execute("DELETE FROM exchange_rates")
                currency.refresh_trm(self.conn, opener=body_opener(trm_row("4000", today_iso(days))))
                self.assertIs(currency.get_state(self.conn)["rate_info"]["stale"], stale)
        currency.update_settings(self.conn, {"rate_mode": "manual"})
        self.assertFalse(currency.get_state(self.conn)["rate_info"]["stale"])  # manual nunca es "vieja"

    def test_maybe_auto_refresh(self):
        calls = []
        op = body_opener(trm_row("4100", today_iso()), calls)
        currency.maybe_auto_refresh(self.conn, opener=op)  # sin datos → consulta
        self.assertEqual(len(calls), 1)
        currency.maybe_auto_refresh(self.conn, opener=op)  # recién consultada → no
        self.assertEqual(len(calls), 1)
        later = datetime.now(timezone.utc) + timedelta(hours=13)
        with mock.patch.object(currency, "_now", return_value=later):
            currency.maybe_auto_refresh(self.conn, opener=op)  # vieja → consulta
        self.assertEqual(len(calls), 2)
        currency.update_settings(self.conn, {"rate_mode": "manual"})
        with mock.patch.object(currency, "_now", return_value=later + timedelta(days=5)):
            currency.maybe_auto_refresh(self.conn, opener=op)  # modo manual → nunca
        self.assertEqual(len(calls), 2)
        # nunca lanza
        self.conn.execute("DELETE FROM exchange_rates")
        currency.maybe_auto_refresh(self.conn, opener=raising_opener(RuntimeError("boom")))
        currency.maybe_auto_refresh(self.conn, opener=raising_opener(urllib.error.URLError("x")))

    def test_with_block_commits_and_closes(self):
        with db.connect() as conn:
            currency.ensure_schema(conn)
            currency.update_settings(conn, {"vat_pct": 16})
        with db.connect() as conn:
            self.assertEqual(currency.get_settings(conn)["vat_pct"], 16.0)
            currency.update_settings(conn, {"vat_pct": 19})


def state(rate=4000.0, display="COP", rounding="900", vat=False, vat_pct=19.0):
    s = dict(currency.DEFAULTS, display_currency=display, cop_rounding=rounding, vat_enabled=vat, vat_pct=vat_pct)
    return {"settings": s, "rates": {"USD": 1.0, "COP": rate}}


class PureFunctionsTest(unittest.TestCase):
    def test_round_cop(self):
        table = {
            "900": [(87350, 87900), (87900, 87900), (87901, 88900), (88000, 88900), (88899.5, 88900),
                    (1, 900), (900, 900), (901, 1900), (87960.00000000001, 87900 + 1000)],
            "1000": [(87350, 88000), (88000, 88000), (88000.4, 89000), (1, 1000), (999.99, 1000)],
            "100": [(87350, 87400), (87400, 87400), (87401, 87500), (1, 100), (0.2, 100)],
            "none": [(87350.4, 87350), (87350.5, 87351), (0.4, 0), (12, 12)],
        }
        for mode, cases in table.items():
            for value, expected in cases:
                with self.subTest(mode=mode, value=value):
                    self.assertEqual(currency.round_price(value, "COP", mode), expected)
        self.assertEqual(currency.round_price(0, "COP", "900"), 0)

    def test_round_usd(self):
        self.assertEqual(currency.round_price(12.345, "USD", "900"), 12.35)
        self.assertEqual(currency.round_price(12.99, "USD", "none"), 12.99)
        self.assertEqual(currency.round_price(0.004, "USD", "none"), 0.0)
        with self.assertRaises(ValueError):
            currency.round_price(1, "EUR", "none")

    def test_convert(self):
        st = state(rate=4123.45)
        self.assertEqual(currency.convert(10, st), 41235.0)          # redondeo simple, sin ...900
        self.assertEqual(currency.convert(10, st, "USD"), 10.0)
        self.assertEqual(currency.convert(1.005, st, "usd"), 1.01)
        self.assertEqual(currency.convert(0.0001, st), 0.0)
        self.assertEqual(currency.convert(-2, st), -8247.0)          # pérdidas también
        with self.assertRaises(ValueError):
            currency.convert(1, st, "MXN")
        with self.assertRaises(ValueError):
            currency.convert(1, {"settings": st["settings"], "rates": {"USD": 1.0, "COP": None}})

    def test_price_view(self):
        self.assertEqual(currency.price_view(21.99, state()),
                         {"currency": "COP", "amount": 88900.0, "amount_with_vat": None, "vat_pct": None,
                          "usd": 21.99})
        v = currency.price_view(21.99, state(vat=True))
        self.assertEqual((v["amount"], v["amount_with_vat"], v["vat_pct"]), (88900.0, 105791.0, 19.0))
        self.assertEqual(currency.price_view(21.99, state(rounding="none"))["amount"], 87960.0)
        usd = currency.price_view(24.99, state(display="USD", vat=True, vat_pct=10))
        self.assertEqual((usd["currency"], usd["amount"], usd["amount_with_vat"]), ("USD", 24.99, 27.49))


if __name__ == "__main__":
    unittest.main()
