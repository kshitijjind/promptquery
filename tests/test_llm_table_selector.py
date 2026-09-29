from promptquery.retrieval import llm_select_tables
from promptquery.schema import Column, Table


class _FakeLLM:
    """Records the prompts and returns a queued response."""
    def __init__(self, response: str):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response


def _candidates() -> list[Table]:
    return [
        Table(schema="public", name="res_partner",
              comment="contacts (customers, vendors)",
              columns=[Column("id", "bigint", False, True),
                       Column("name", "text", False, False)]),
        Table(schema="public", name="account_move",
              comment="invoices and other accounting moves",
              columns=[Column("id", "bigint", False, True),
                       Column("move_type", "text", False, False)]),
        Table(schema="public", name="sale_order",
              columns=[Column("id", "bigint", False, True),
                       Column("partner_id", "bigint", False, False)]),
        Table(schema="public", name="random_unrelated",
              columns=[Column("id", "bigint", False, True)]),
        Table(schema="public", name="audit_log",
              columns=[Column("id", "bigint", False, True),
                       Column("note", "text", True, False)]),
    ]


def test_selector_returns_named_tables_in_order():
    llm = _FakeLLM('["account_move", "res_partner"]')
    selected = llm_select_tables("unpaid invoices with customer",
                                  _candidates(), llm, max_select=3)
    names = [t.name for t in selected]
    assert names == ["account_move", "res_partner"]


def test_selector_extracts_json_from_chatty_response():
    llm = _FakeLLM('Sure! Here you go:\n```json\n["sale_order", "res_partner"]\n```\nLet me know if you need more.')
    selected = llm_select_tables("orders with customer",
                                  _candidates(), llm, max_select=3)
    assert [t.name for t in selected] == ["sale_order", "res_partner"]


def test_selector_falls_back_when_response_unparseable():
    llm = _FakeLLM("sorry, I don't know")
    selected = llm_select_tables("anything", _candidates(), llm, max_select=3)
    # Falls back to the first 3 candidates rather than crashing
    assert len(selected) == 3


def test_selector_ignores_invalid_names_in_response():
    llm = _FakeLLM('["account_move", "nonexistent_table", "res_partner"]')
    selected = llm_select_tables("anything", _candidates(), llm, max_select=3)
    assert [t.name for t in selected] == ["account_move", "res_partner"]


def test_selector_skips_when_candidates_already_small_enough():
    # No LLM call when the candidate list already fits.
    llm = _FakeLLM('["res_partner"]')
    cands = _candidates()
    selected = llm_select_tables("anything", cands, llm, max_select=10)
    assert selected == cands
    assert llm.calls == []


def test_selector_handles_llm_exception_gracefully():
    class _BrokenLLM:
        def generate(self, system: str, user: str) -> str:
            raise RuntimeError("api down")

    selected = llm_select_tables("anything", _candidates(), _BrokenLLM(), max_select=2)
    assert len(selected) == 2  # fell back to first N candidates
