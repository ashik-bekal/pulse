"""
Unit tests for domain.categorization. Pure function — no database, no
fixtures beyond hand-built VendorRule objects. This is the test class that
was IMPOSSIBLE to write against the original categorizer.py, since it
opened its own sqlite3 connection internally.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from domain.categorization import categorize
from domain.models import VendorRule


def make_rule(id_, pattern, category_id=1, money_type="expense", confidence="high"):
    return VendorRule(id=id_, pattern=pattern, match_type="contains",
                       category_id=category_id, money_type=money_type,
                       owner_id=None, confidence=confidence)


def test_more_specific_pattern_wins_over_generic_one():
    """
    Employer-as-salary vs employer-named-venue clash: a bank credit named
    plain 'Acme Corp' should match the salary rule, while a longer, more
    specific 'ACME CORP CAFETERIA' card-charge description should match the
    venue override rule, REGARDLESS of which rule was defined first.
    """
    rules = [
        make_rule(1, "ACME CORP", category_id=100, money_type="income"),
        make_rule(2, "ACME CORP CAFETERIA", category_id=200, money_type="expense"),
    ]
    salary = categorize("Acme Corp", rules)
    assert salary.category_id == 100
    assert salary.money_type == "income"

    venue = categorize("ACME CORP CAFETERIA LONDON", rules)
    assert venue.category_id == 200
    assert venue.money_type == "expense"


def test_specificity_resolution_independent_of_rule_order():
    """Same test as above but with rules defined in the OPPOSITE order,
    to prove the result doesn't depend on insertion order."""
    rules = [
        make_rule(2, "ACME CORP CAFETERIA", category_id=200, money_type="expense"),
        make_rule(1, "ACME CORP", category_id=100, money_type="income"),
    ]
    venue = categorize("ACME CORP CAFETERIA LONDON", rules)
    assert venue.category_id == 200


def test_no_match_falls_back_to_low_confidence():
    rules = [make_rule(1, "TESCO")]
    result = categorize("SOME UNKNOWN MERCHANT XYZ", rules, fallback_category_id=999)
    assert result.confidence == "low"
    assert result.category_id == 999
    assert result.matched_rule_id is None


def test_inactive_rule_is_ignored():
    rules = [
        VendorRule(id=1, pattern="TESCO", match_type="contains", category_id=1,
                   money_type="expense", owner_id=None, confidence="high", is_active=False),
    ]
    result = categorize("TESCO STORES LONDON", rules)
    assert result.matched_rule_id is None
    assert result.confidence == "low"


def test_case_insensitive_match():
    rules = [make_rule(1, "tesco")]
    result = categorize("TESCO STORES LONDON", rules)
    assert result.matched_rule_id == 1


def test_exact_match_type():
    rules = [
        VendorRule(id=1, pattern="Interest Payment", match_type="exact", category_id=5,
                   money_type="income", owner_id=None, confidence="high"),
    ]
    assert categorize("Interest Payment", rules).matched_rule_id == 1
    assert categorize("Interest Payment Extra Text", rules).matched_rule_id is None
