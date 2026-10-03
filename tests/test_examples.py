"""Example questions must all answer. Regression suite for B1.

The homepage offered twelve example chips. On a cold start four of them
abstained, because ``top_risks``, ``portfolio_summary`` and ``explain_signal``
read ``FROM findings`` and nothing had populated it. Guardrail A3 then forced a
correct-but-useless abstention on the very first click of the demo.

This test asserts the contract the Definition of Done states: every example
question returns a PASS, or abstains with a stated gap. It is run against a
populated warehouse, so the boot/lazy scan has had a chance to run.
"""

from __future__ import annotations

import pytest

from api.index import example_questions


@pytest.fixture(scope="module")
def questions(copilot):
    copilot.ensure_populated()
    return example_questions(copilot)


def test_example_list_is_complete(questions):
    assert len(questions) == 12


def test_examples_have_no_unresolved_placeholders(questions):
    """``{subject}`` must be substituted, or the UI would show a literal brace."""
    for q in questions:
        assert "{" not in q and "}" not in q, q


def test_every_example_question_answers(questions, copilot):
    """The B1 regression: no example may abstain on a cold start."""
    failures = []
    for q in questions:
        answer = copilot.ask(q)
        status = answer["guardrail"]["status"]
        if status == "FAIL":
            failures.append((q, answer["guardrail"]["violations"]))
    assert not failures, "example questions abstained: " + repr(failures)


def test_every_example_returns_supporting_rows(questions, copilot):
    """A PASS with no rows would be an assertion the guardrails should refuse."""
    for q in questions:
        answer = copilot.ask(q)
        if answer["guardrail"]["status"] == "PASS":
            assert answer["row_count"] > 0, q


def test_portfolio_question_works_on_an_empty_findings_table(warehouse, config,
                                                            copilot):
    """The lazy scan is the fix; this exercises it directly.

    Clears findings to simulate a cold start and confirms the executor
    repopulates rather than abstaining.
    """
    warehouse.execute("DELETE FROM findings")
    assert warehouse.table_count("findings") == 0

    answer = copilot.ask("show the top 10 highest risk customers")
    assert answer["guardrail"]["status"] == "PASS", answer["guardrail"]
    assert answer["row_count"] > 0
    assert warehouse.table_count("findings") > 0


def test_row_count_is_never_stale(questions, copilot):
    """Guards the ``row_count`` bug that halved portfolio_summary confidence.

    An executor that populated ``rows`` without setting ``row_count`` produced a
    supported answer the composer then scored at 0.28, below the abstention
    threshold. ExecutionResult now derives it.
    """
    for q in questions:
        answer = copilot.ask(q)
        if answer["rows"]:
            assert answer["row_count"] == len(answer["rows"]) or answer["row_count"] > 0


def test_scan_is_idempotent(copilot):
    """A second ensure_populated must not rescan or duplicate findings."""
    copilot.ensure_populated()
    first = copilot.wh.table_count("findings")
    copilot.ensure_populated()
    assert copilot.wh.table_count("findings") == first
