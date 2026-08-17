"""
tests/test_opportunities.py — unit tests for the products × match-fit opportunity
pivot (`_aggregate_opportunities` in routers/products.py).

Pure function — no DB, no network. Reports are newest-first (as db.get_match_reports
returns). Band score = the match_report [N/10] fit score.
"""

from routers.products import _aggregate_opportunities


def _p(pid, name, is_focus=False, category=None):
    return {"id": pid, "name": name, "is_focus": is_focus, "category": category}


def _section(sym, label, score, product):
    return f"## {sym} {label} [{score}/10]: {product}\nEvidence about {product}.\n"


def _report(client, *sections):
    return {"client_name": client, "content": "\n".join(sections)}


STRONG = ("✓", "Strong Fit")
POT = ("~", "Potential Fit")
NOT = ("✗", "Not a Fit")


def _sec(fit, score, product):
    return _section(fit[0], fit[1], score, product)


# ---------------------------------------------------------------------------
# Band counts + matched-client count
# ---------------------------------------------------------------------------

def test_bands_counts_and_matched():
    products = [_p(1, "IBM Instana", is_focus=True), _p(2, "WatsonX")]
    reports = [  # newest-first
        _report("Globex", _sec(STRONG, 9, "IBM Instana"), _sec(POT, 6, "WatsonX")),
        _report("SAP", _sec(STRONG, 9, "IBM Instana")),
        _report("Adler", _sec(STRONG, 8, "IBM Instana")),
        _report("BBraun", _sec(STRONG, 10, "WatsonX"), _sec(NOT, 4, "IBM Instana")),
    ]
    rows, matched = _aggregate_opportunities(products, reports, min_score=5)

    assert matched == 4  # Globex, SAP, Adler, BBraun

    by_name = {r["name"]: r for r in rows}
    instana = by_name["IBM Instana"]
    assert instana["total"] == 3                       # BBraun's 4/10 dropped (<5)
    assert instana["bands"] == {"9": ["Globex", "SAP"], "8": ["Adler"]}

    watsonx = by_name["WatsonX"]
    assert watsonx["total"] == 2
    assert watsonx["bands"] == {"10": ["BBraun"], "6": ["Globex"]}


# ---------------------------------------------------------------------------
# Newest report wins per (product, client)
# ---------------------------------------------------------------------------

def test_newest_report_wins_per_client():
    products = [_p(1, "IBM Instana")]
    reports = [  # newest-first: 9/10 is the current view, 4/10 is stale
        _report("Globex", _sec(STRONG, 9, "IBM Instana")),
        _report("Globex", _sec(NOT, 4, "IBM Instana")),
    ]
    rows, _ = _aggregate_opportunities(products, reports, min_score=5)
    assert rows[0]["total"] == 1
    assert rows[0]["bands"] == {"9": ["Globex"]}


# ---------------------------------------------------------------------------
# Substring product-name match (catalog name inside the heading)
# ---------------------------------------------------------------------------

def test_substring_product_name_match():
    products = [_p(1, "Instana")]                       # catalog name shorter than heading
    reports = [_report("Globex", _sec(STRONG, 7, "IBM Instana"))]
    rows, _ = _aggregate_opportunities(products, reports, min_score=5)
    assert rows[0]["total"] == 1
    assert rows[0]["bands"] == {"7": ["Globex"]}


# ---------------------------------------------------------------------------
# Threshold: scores below min_score are dropped
# ---------------------------------------------------------------------------

def test_min_score_threshold():
    products = [_p(1, "IBM Instana")]
    reports = [
        _report("X", _sec(STRONG, 5, "IBM Instana")),
        _report("Y", _sec(NOT, 4, "IBM Instana")),
    ]
    rows5, _ = _aggregate_opportunities(products, reports, min_score=5)
    assert rows5[0]["total"] == 1                       # X kept (5), Y dropped (4)

    rows6, _ = _aggregate_opportunities(products, reports, min_score=6)
    assert rows6[0]["total"] == 0                       # both below 6
    assert rows6[0]["bands"] == {}


# ---------------------------------------------------------------------------
# A product with no matching sections has total 0 / empty bands
# ---------------------------------------------------------------------------

def test_product_with_no_matches():
    products = [_p(1, "Nonexistent Product")]
    reports = [_report("Globex", _sec(STRONG, 9, "IBM Instana"))]
    rows, matched = _aggregate_opportunities(products, reports, min_score=5)
    assert rows[0]["total"] == 0
    assert rows[0]["bands"] == {}
    assert matched == 1                                 # Globex still counts as analyzed


# ---------------------------------------------------------------------------
# Ordering: focus products first, then by total desc
# ---------------------------------------------------------------------------

def test_ordering_focus_first_then_total():
    products = [
        _p(1, "A", is_focus=True),      # total 1
        _p(2, "B", is_focus=False),     # total 2
        _p(3, "C", is_focus=True),      # total 2
    ]
    reports = [
        _report("c1", _sec(STRONG, 9, "A"), _sec(STRONG, 9, "B"), _sec(STRONG, 9, "C")),
        _report("c2", _sec(STRONG, 8, "B"), _sec(STRONG, 8, "C")),
    ]
    rows, _ = _aggregate_opportunities(products, reports, min_score=5)
    assert [r["name"] for r in rows] == ["C", "A", "B"]  # focus(C total2) > focus(A total1) > B
