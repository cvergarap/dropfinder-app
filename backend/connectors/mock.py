"""Proveedor simulado: genera un catálogo sintético determinista y simula
variaciones de precio/stock para probar el monitor."""
from __future__ import annotations

import random

from .base import NormalizedProduct, PriceStock, SupplierConnector

SUPPLIERS = {
    # nombre: (warehouses, rango días envío, factor de costo)
    "AliExpress": (["CN", "CN", "CN", "US", "ES"], (7, 25), 1.0),
    "CJ Dropshipping": (["CN", "US", "US", "DE"], (5, 15), 1.1),
    "Amazon": (["US", "US", "MX"], (2, 6), 1.6),
    "Almacén Local": (["MX", "AR", "CL", "CO", "ES"], (1, 5), 1.4),
}

CATALOG = {
    "Hogar y cocina": (["Organizador", "Lámpara LED", "Dispensador", "Molinillo", "Cortador de verduras",
                        "Humidificador", "Set de cuchillos", "Tabla de picar", "Botella térmica"],
                       ["magnético", "plegable", "recargable", "inteligente", "de bambú", "minimalista"], (2, 25)),
    "Mascotas": (["Cepillo para mascotas", "Comedero automático", "Juguete interactivo", "Cama ortopédica",
                  "Arnés reflectante", "Fuente de agua"], ["autolimpiante", "silencioso", "antideslizante",
                                                            "con sensor", "lavable"], (3, 30)),
    "Belleza": (["Rodillo facial", "Cepillo alisador", "Mascarilla LED", "Organizador de maquillaje",
                 "Limpiador ultrasónico", "Masajeador de cuero cabelludo"], ["de jade", "eléctrico", "portátil",
                                                                          "recargable", "profesional"], (2, 20)),
    "Fitness": (["Bandas de resistencia", "Rodillo abdominal", "Cuerda para saltar", "Pistola de masaje",
                 "Esterilla de yoga", "Mancuernas ajustables"], ["con contador", "antideslizante", "plegable",
                                                                 "inteligente", "profesional"], (3, 40)),
    "Electrónica": (["Auriculares inalámbricos", "Cargador magnético", "Soporte para celular", "Mini proyector",
                     "Tira LED RGB", "Cámara de seguridad", "Power bank"], ["bluetooth 5.3", "con app",
                                                                           "rápido", "solar", "4K", "compacto"],
                    (4, 60)),
    "Bebés y niños": (["Monitor de bebé", "Lámpara proyector", "Mordedor", "Mochila portabebé",
                       "Juguete Montessori"], ["de silicona", "musical", "ergonómico", "de madera"], (3, 35)),
    "Auto": (["Aspiradora para auto", "Organizador de maletero", "Soporte magnético", "Cámara de reversa",
              "Limpiador de tapicería"], ["inalámbrica", "plegable", "universal", "HD"], (3, 35)),
    "Moda y accesorios": (["Reloj minimalista", "Gafas polarizadas", "Billetera RFID", "Bolso cruzado",
                           "Anillo ajustable"], ["de acero", "unisex", "de cuero", "vintage"], (2, 22)),
}

COLORS = ["e0f2fe", "fce7f3", "dcfce7", "fef9c3", "ede9fe", "ffedd5", "e2e8f0"]


class MockSupplierConnector(SupplierConnector):
    name = "mock"

    def __init__(self, size: int = 1500, seed: int = 42):
        self.size = size
        self.seed = seed
        self._rng = random.Random(seed + 1)

    def iter_catalog(self):
        rng = random.Random(self.seed)
        cats = list(CATALOG)
        for i in range(self.size):
            supplier = rng.choice(list(SUPPLIERS))
            warehouses, (dmin, dmax), factor = SUPPLIERS[supplier]
            category = rng.choice(cats)
            nouns, adjs, (cmin, cmax) = CATALOG[category]
            noun, adj = rng.choice(nouns), rng.choice(adjs)
            cost = round(rng.uniform(cmin, cmax) * factor, 2)
            # Popularidad con cola larga (pocos productos venden mucho)
            orders = int(rng.paretovariate(1.1) * 40) - 40 + rng.randint(0, 30)
            orders = max(0, min(orders, 250_000))
            base_week = max(0, int(orders * rng.uniform(0.005, 0.04)))
            trend = rng.choice([0.4, 0.7, 0.9, 1.0, 1.1, 1.3, 1.8, 2.6])
            initials = "".join(w[0] for w in noun.split()[:2]).upper()
            color = COLORS[i % len(COLORS)]
            yield NormalizedProduct(
                supplier=supplier,
                supplier_sku=f"{supplier[:2].upper()}-{100000 + i}",
                title=f"{noun} {adj}",
                category=category,
                cost=cost,
                shipping_cost=round(0 if rng.random() < 0.35 else rng.uniform(0.5, 6.0), 2),
                shipping_days=rng.randint(dmin, dmax),
                warehouse=rng.choice(warehouses),
                stock=0 if rng.random() < 0.06 else rng.randint(5, 5000),
                orders=orders,
                orders_7d=int(base_week * trend),
                orders_prev_7d=base_week,
                rating=round(min(5.0, max(2.8, rng.gauss(4.5, 0.35))), 1),
                reviews=int(orders * rng.uniform(0.05, 0.3)),
                image=f"https://placehold.co/300x300/{color}/334155?text={initials}",
                tags=[category.lower(), noun.lower(), adj.lower()],
            )

    def get_price_stock(self, supplier_sku: str, current: PriceStock | None = None) -> PriceStock | None:
        """Simula el estado actual del proveedor a partir del último conocido."""
        if current is None:
            return None
        r = self._rng.random()
        cost, stock = current.cost, current.stock
        if r < 0.25:
            cost = round(cost * self._rng.uniform(1.08, 1.6), 2)    # sube el costo
        elif r < 0.35:
            cost = round(cost * self._rng.uniform(0.8, 0.95), 2)    # baja el costo
        r2 = self._rng.random()
        if r2 < 0.15:
            stock = 0
        elif r2 < 0.5:
            stock = max(0, stock + self._rng.randint(-200, 200))
        return PriceStock(cost=cost, shipping_cost=current.shipping_cost, stock=stock)
