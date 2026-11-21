"""Motor de búsqueda de DropFinder (solo librería estándar + SQLite FTS5).

Piezas:
- Índice de texto completo `products_fts` (FTS5, tabla normal) sobre título, categoría y etiquetas.
  En el índice se guardan las *raíces* de las palabras (ver `stem`), no el texto original, que sigue en
  `products`. El rowid del índice es el id del producto.
- Sincronización: unos triggers marcan en `fts_dirty` los productos insertados o con texto modificado y
  borran del índice los productos eliminados. `sync()` re-indexa los marcados (se llama tras cada carga de
  catálogo y antes de cada búsqueda, así ninguna ruta de escritura queda desincronizada).
- Raíces en español (stemmer ligero y conservador): "lámparas" → "lampar", "cargadores" → "cargador".
- Sinónimos editables en `backend/data/sinonimos.json`.
- Ranking BM25 (el título pesa más que etiquetas y categoría), "¿Quisiste decir?" por distancia de
  edición contra el vocabulario del catálogo, y autocompletado.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections import Counter
from functools import lru_cache

# Cambiar este número obliga a re-indexar al arrancar (p. ej. si se modifica el stemmer).
STEMMER_VERSION = "1"
SYNONYMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sinonimos.json")

# Pesos BM25 por columna del índice: título, categoría, etiquetas.
BM25_WEIGHTS = (10.0, 2.0, 4.0)
# Mezcla ligera con el score ganador: relevancia * (1 + score / SCORE_BOOST). Con 400, un producto de score
# 100 recibe como máximo un +25 % de relevancia: desempata títulos parecidos sin tapar una mejor coincidencia.
SCORE_BOOST = 400.0
FEW_RESULTS = 3          # con menos resultados que esto se calcula una sugerencia "¿Quisiste decir?"

STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "y", "o", "e", "en", "con", "sin",
    "para", "por", "al", "a", "su", "sus", "lo", "que", "the", "for", "and", "of", "with",
}

_WORD = re.compile(r"[^\W_]+")
_VOWELS = set("aeiou")
# Palabras que terminan en -ito/-ita pero no son diminutivos.
_NOT_DIMINUTIVE = {
    "circuito", "gratuito", "fortuito", "favorito", "infinito", "apetito", "requisito", "distrito",
    "exquisito", "mosquito", "bonito", "bonita", "palito", "credito", "debito", "deposito", "proposito",
    "transito", "merito", "limite", "kit", "granito", "cabrito", "hito", "rito", "grito", "mito",
}

FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5("
           "title, category, tags, tokenize='unicode61 remove_diacritics {rd}', prefix='2 3')")

SYNC_DDL = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS products_fts_vocab USING fts5vocab(products_fts, 'row')",
    "CREATE TABLE IF NOT EXISTS fts_dirty (id INTEGER PRIMARY KEY)",
    """CREATE TRIGGER IF NOT EXISTS trg_products_fts_ins AFTER INSERT ON products BEGIN
         INSERT OR IGNORE INTO fts_dirty(id) VALUES (new.id);
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_products_fts_upd AFTER UPDATE OF title, category, tags ON products BEGIN
         INSERT OR IGNORE INTO fts_dirty(id) VALUES (new.id);
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_products_fts_del AFTER DELETE ON products BEGIN
         DELETE FROM products_fts WHERE rowid = old.id;
         DELETE FROM fts_dirty WHERE id = old.id;
       END""",
]


# ---------------------------------------------------------------- normalización y raíces

_FAST = str.maketrans("áéíóúüñàèìòùâêîôûäëïöç", "aeiouunaeiouaeiouaeioc")


def unaccent(text: str | None) -> str:
    """Minúsculas y sin tildes, para búsquedas en español ("magnetico" == "magnético")."""
    if not text:
        return ""
    t = text.lower()
    if t.isascii():
        return t
    t = t.translate(_FAST)            # camino rápido para el español
    if t.isascii():
        return t
    nfkd = unicodedata.normalize("NFKD", t)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokenize(text: str | None) -> list[str]:
    """Palabras sin tildes y en minúsculas (mismo criterio que el tokenizador unicode61 de FTS5)."""
    return _WORD.findall(unaccent(text))


@lru_cache(maxsize=200_000)
def stem(w: str) -> str:
    """Raíz ligera en español. Aplica las mismas reglas al indexar y al buscar, así que basta con que
    singular y plural (y masculino/femenino) terminen en la misma raíz:
      plural:   -ces → -z (luces → luz) · consonante + -es → consonante (cargadores → cargador)
                vocal + -s → vocal (lámparas → lámpara)
      diminutivo: -ito/-ita → raíz (perrito → perr, igual que perro), salvo excepciones
      género:   quita la vocal final a/o/e si la palabra tiene ≥ 4 letras (lámpara → lampar, rápido → rapid)
    Palabras cortas (< 4 letras) o con números no se tocan."""
    if len(w) < 4 or not w.isalpha():
        return w
    if len(w) >= 5 and w.endswith("ces") and w[-4] in _VOWELS:
        w = w[:-3] + "z"
    elif len(w) >= 5 and w.endswith("es") and w[-3] not in _VOWELS:
        w = w[:-2]
    elif w.endswith("s") and w[-2] in _VOWELS:
        w = w[:-1]
    if w.endswith(("ito", "ita")) and len(w) >= 7 and w not in _NOT_DIMINUTIVE:
        w = w[:-3]
    elif len(w) >= 4 and w[-1] in "aoe":
        w = w[:-1]
    return w


@lru_cache(maxsize=100_000)
def stem_text(text: str | None) -> str:
    return " ".join(stem(t) for t in tokenize(text))


def query_tokens(q: str | None) -> list[str]:
    """Palabras significativas de una consulta (sin artículos ni preposiciones)."""
    return [t for t in tokenize(q) if t not in STOPWORDS]


# ---------------------------------------------------------------- cachés en memoria

_lock = threading.RLock()
_gen = 0                      # se incrementa cada vez que cambia el catálogo o las reglas de precio
_cache: dict[str, tuple[int, object]] = {}


def invalidate() -> None:
    """Invalida facetas, vocabulario y títulos de autocompletado (llamar tras cambios en el catálogo)."""
    global _gen
    with _lock:
        _gen += 1
        _cache.clear()


def cached(key: str, build):
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == _gen:
            return hit[1]
        gen = _gen
    value = build()
    with _lock:
        if gen == _gen:
            _cache[key] = (gen, value)
    return value


# ---------------------------------------------------------------- sinónimos

_syn_state = {"mtime": None, "single": {}, "multi": {}}


def _synonyms() -> tuple[dict, dict]:
    """Devuelve (raíz → frases sinónimas, (raíz1, raíz2) → frases). Se recarga si cambia el archivo."""
    try:
        mtime = os.path.getmtime(SYNONYMS_PATH)
    except OSError:
        return {}, {}
    with _lock:
        if _syn_state["mtime"] == mtime:
            return _syn_state["single"], _syn_state["multi"]
        try:
            with open(SYNONYMS_PATH, encoding="utf-8") as f:
                groups = json.load(f).get("grupos", [])
        except (OSError, ValueError):
            groups = []
        single, multi = {}, {}
        for group in groups:
            phrases = sorted({" ".join(stem(t) for t in tokenize(term)) for term in group} - {""})
            for ph in phrases:
                parts = tuple(ph.split())
                target = single if len(parts) == 1 else multi if len(parts) == 2 else None
                if target is not None:
                    key = parts[0] if len(parts) == 1 else parts
                    target.setdefault(key, set()).update(phrases)
        _syn_state.update(mtime=mtime, single=single, multi=multi)
        return single, multi


# ---------------------------------------------------------------- esquema y sincronización

def create_schema(conn) -> bool:
    """Crea el índice FTS5, su vocabulario y los triggers (dentro de la transacción en curso). Devuelve False
    si este SQLite no tiene FTS5 (en ese caso la búsqueda usa LIKE como antes)."""
    for rd in (2, 1):   # remove_diacritics 2 requiere SQLite ≥ 3.27
        try:
            conn.execute(FTS_DDL.format(rd=rd))
            break
        except sqlite3.OperationalError as e:
            if "no such module" in str(e):
                return False
    else:
        return False
    for stmt in SYNC_DDL:          # execute() y no executescript(): no debe cerrar la transacción en curso
        conn.execute(stmt)
    return True


def fts_enabled(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'products_fts'").fetchone() is not None


def _doc(r) -> tuple:
    return r[0], stem_text(r[1]), stem_text(r[2]), stem_text((r[3] or "").replace(",", " "))


def sync(conn) -> int:
    """Indexa los productos marcados como pendientes por los triggers. Devuelve cuántos indexó."""
    if not conn.execute("SELECT 1 FROM fts_dirty LIMIT 1").fetchone():
        return 0
    rows = conn.execute("SELECT p.id, p.title, p.category, p.tags FROM fts_dirty d "
                        "JOIN products p ON p.id = d.id").fetchall()
    conn.executemany("INSERT OR REPLACE INTO products_fts(rowid, title, category, tags) VALUES (?, ?, ?, ?)",
                     map(_doc, rows))
    conn.execute("DELETE FROM fts_dirty")
    invalidate()
    return len(rows)


def reindex_all(conn) -> int:
    """Reconstruye el índice completo (migración o cambio de versión del stemmer)."""
    conn.execute("DELETE FROM products_fts")
    conn.execute("DELETE FROM fts_dirty")
    rows = conn.execute("SELECT id, title, category, tags FROM products")
    n = 0
    for chunk in iter(lambda: rows.fetchmany(5000), []):
        conn.executemany("INSERT INTO products_fts(rowid, title, category, tags) VALUES (?, ?, ?, ?)",
                         map(_doc, chunk))
        n += len(chunk)
    invalidate()
    return n


# ---------------------------------------------------------------- consulta FTS

def _q(term: str, prefix: bool = False) -> str:
    return f'"{term}"*' if prefix else f'"{term}"'


def build_match(tokens: list[str], prefix_last: bool = True) -> str | None:
    """Expresión MATCH de FTS5: cada palabra (o su sinónimo) debe aparecer; la última admite prefijo
    ("carg" encuentra "cargador")."""
    if not tokens:
        return None
    single, multi = _synonyms()
    stems = [stem(t) for t in tokens]
    groups, i, n = [], 0, len(stems)
    while i < n:
        pair = tuple(stems[i:i + 2])
        if len(pair) == 2 and pair in multi:            # "botella térmica" ↔ "termo"
            alts = {_q(p) for p in multi[pair]}
            i += 2
        else:
            s = stems[i]
            alts = {_q(p) for p in single.get(s, ())} | {_q(s)}
            if i == n - 1 and prefix_last and len(s) >= 2:
                alts.discard(_q(s))
                alts.add(_q(s, prefix=True))
            i += 1
        groups.append("(" + " OR ".join(sorted(alts)) + ")")
    return " AND ".join(groups)


def rank_sql() -> str:
    w = ", ".join(str(x) for x in BM25_WEIGHTS)
    return f"bm25(products_fts, {w}) * (1 + p.score / {SCORE_BOOST})"


# ---------------------------------------------------------------- "¿Quisiste decir?"

def _vocab(conn) -> dict:
    def build():
        terms = {r[0]: r[1] for r in conn.execute("SELECT term, doc FROM products_fts_vocab")}
        return {"freq": terms, "sorted": sorted(terms)}
    return cached("vocab", build)


def _has_prefix(sorted_terms: list, p: str) -> bool:
    i = bisect.bisect_left(sorted_terms, p)
    return i < len(sorted_terms) and sorted_terms[i].startswith(p)


def _freq(stem_: str, is_last: bool, vocab: dict) -> int:
    """Frecuencia (documentos) de una palabra de la consulta en el catálogo, contando sus sinónimos."""
    single, _ = _synonyms()
    freq = vocab["freq"]
    n = freq.get(stem_, 0)
    for ph in single.get(stem_, ()):
        n = max(n, min((freq.get(w, 0) for w in ph.split()), default=0))
    if not n and is_last and _has_prefix(vocab["sorted"], stem_):
        n = 1
    return n


def edit_distance(a: str, b: str, limit: int) -> int:
    """Distancia de Damerau-Levenshtein (transposiciones adyacentes) con corte: devuelve limit+1 si se pasa."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = ca != cb
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _closest(s: str, vocab: dict, min_freq: int = 1) -> str | None:
    """Término del vocabulario más cercano (distancia ≤ 1 en raíces de hasta 5 letras, ≤ 2 en el resto).
    Filtro rápido antes de la distancia: largo parecido y que coincida la 1.ª o 2.ª letra (en cualquier
    orden), así no se compara contra todo el vocabulario."""
    limit = 1 if len(s) <= 5 else 2
    best = None
    for term, doc in vocab["freq"].items():
        if doc < min_freq or abs(len(term) - len(s)) > limit or not term.isalpha():
            continue
        if not (term[0] == s[0] or term[:2] == s[1:3] or term[1:3] == s[:2] or term[1:2] == s[1:2]):
            continue
        d = edit_distance(s, term, limit)
        if d <= limit and (best is None or (d, -doc) < best[0]):
            best = ((d, -doc), term)
    return best[1] if best else None


def _display(conn, stem_: str) -> str:
    """Palabra original (con tildes) más frecuente cuya raíz es `stem_`."""
    words = Counter()
    for r in conn.execute("SELECT title, category, tags FROM products WHERE id IN "
                          "(SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT 25)", (_q(stem_),)):
        for w in _WORD.findall(" ".join(x or "" for x in r).lower()):
            if stem(unaccent(w)) == stem_:
                words[w] += 1
    return words.most_common(1)[0][0] if words else stem_


def correct(conn, q: str, current_total: int = 0) -> str | None:
    """Corrige las palabras desconocidas (o muy raras si hay pocos resultados) de la consulta.
    Devuelve la consulta corregida o None si no hay nada que corregir."""
    vocab = _vocab(conn)
    words = tokenize(q)
    meaningful = [i for i, w in enumerate(words) if w not in STOPWORDS]
    changed = False
    for i in meaningful:
        w = words[i]
        if len(w) < 3 or not w.isalpha():
            continue
        s = stem(w)
        f = _freq(s, i == meaningful[-1], vocab)
        if f == 0:
            cand = _closest(s, vocab)
        elif current_total and f < FEW_RESULTS:
            cand = _closest(s, vocab, min_freq=max(10, f * 10))   # solo si la alternativa es mucho más común
        else:
            continue
        if cand and cand != s:
            words[i] = _display(conn, cand)
            changed = True
    return " ".join(words) if changed else None


# ---------------------------------------------------------------- registro de búsquedas y autocompletado

def log_search(conn, q: str, results: int, ts: str) -> None:
    conn.execute("INSERT INTO search_log(q, q_norm, results, created_at) VALUES (?, ?, ?, ?)",
                 (q[:120], " ".join(tokenize(q))[:120], results, ts))
    with _lock:
        _cache.pop("popular", None)


def _titles(conn) -> list:
    """Títulos distintos ordenados por popularidad, con su forma normalizada y en raíces."""
    def build():
        out = []
        for title, pop in conn.execute("SELECT title, SUM(orders) FROM products GROUP BY title ORDER BY 2 DESC"):
            toks = tokenize(title)
            out.append((title, " " + " ".join(toks), " " + " ".join(stem(t) for t in toks)))
        return out
    return cached("titles", build)


def _categories(conn) -> list:
    return cached("categories", lambda: [
        (r[0], " " + " ".join(tokenize(r[0])))
        for r in conn.execute("SELECT category, COUNT(*) c FROM products GROUP BY category ORDER BY c DESC")])


def _matches(norm: str, stemmed: str | None, toks: list[str]) -> bool:
    for t in toks:
        if " " + t not in norm and (stemmed is None or " " + stem(t) not in stemmed):
            return False
    return True


def suggest(conn, prefix: str, limit: int = 8) -> list[dict]:
    """Autocompletado: búsquedas populares, categorías y títulos de productos que coinciden con el prefijo
    (cada palabra escrita debe ser el comienzo de alguna palabra del texto)."""
    toks = tokenize(prefix)
    norm_q = " ".join(toks)
    items, seen = [], set()

    def add(text, type_):
        key = " ".join(tokenize(text))
        if key and key not in seen and len(items) < limit:
            seen.add(key)
            items.append({"text": text, "type": type_})

    # 1) Búsquedas populares que empiezan igual (y que dieron resultados)
    n_pop = max(1, limit // 3) if toks else limit
    sql = ("SELECT q_norm, MAX(q) AS q, COUNT(*) AS n FROM search_log WHERE results > 0 {w} "
           "GROUP BY q_norm ORDER BY n DESC, MAX(id) DESC LIMIT ?")
    if toks:
        rows = conn.execute(sql.format(w="AND q_norm >= ? AND q_norm < ?"), (norm_q, norm_q + "￿", n_pop))
    else:
        rows = conn.execute(sql.format(w=""), (n_pop,))
    for r in rows:
        add(r["q"], "busqueda")
    if not toks:
        return items
    # 2) Categorías
    n_cat = len(items) + 2
    for cat, norm in _categories(conn):
        if len(items) >= n_cat:
            break
        if _matches(norm, None, toks):
            add(cat, "categoria")
    # 3) Productos (títulos distintos, los más vendidos primero)
    for title, norm, stemmed in _titles(conn):
        if len(items) >= limit:
            break
        if _matches(norm, stemmed, toks):
            add(title, "producto")
    return items
