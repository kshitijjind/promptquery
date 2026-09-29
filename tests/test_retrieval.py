from promptquery.retrieval import TfIdfRetriever, expand_via_fks, tokenize
from promptquery.schema import Column, ForeignKey, Schema, Table


def test_tokenize_splits_snake_case():
    tokens = tokenize("user_email_address")
    assert "user" in tokens
    assert "email" in tokens
    assert "address" in tokens


def test_tokenize_splits_camel_case():
    tokens = tokenize("createdAtTimestamp")
    assert "created" in tokens
    assert "timestamp" in tokens


def test_tokenize_drops_stopwords():
    tokens = tokenize("how many users")
    assert "how" not in tokens
    assert "many" not in tokens
    # Stemmed: 'users' -> 'user' so plural questions match singular table names.
    assert "user" in tokens


def test_tokenize_stems_common_plurals():
    """Plural→singular normalisation so questions match table names."""
    assert "lead" in tokenize("show me leads")           # crm_lead vs 'leads'
    assert "invoice" in tokenize("unpaid invoices")
    assert "address" in tokenize("default addresses")    # 'addresses' -> 'address'
    assert "company" in tokenize("companies in Italy")   # ies -> y
    assert "tax" in tokenize("which taxes apply")        # xes -> x


def _schema() -> Schema:
    users = Table(
        schema="public", name="users",
        comment="end user accounts",
        columns=[
            Column("id", "int", False, True),
            Column("email", "text", True, False),
            Column("country", "text", True, False),
        ],
    )
    orders = Table(
        schema="public", name="orders",
        comment="customer purchase orders",
        columns=[
            Column("id", "int", False, True),
            Column("user_id", "int", False, False),
            Column("total_amount", "numeric", False, False),
        ],
        foreign_keys=[ForeignKey("user_id", "public", "users", "id")],
    )
    products = Table(
        schema="public", name="products",
        comment="catalog of items for sale",
        columns=[
            Column("id", "int", False, True),
            Column("name", "text", False, False),
        ],
    )
    unrelated = Table(
        schema="public", name="server_logs",
        columns=[
            Column("ts", "timestamptz", False, False),
            Column("msg", "text", True, False),
        ],
    )
    return Schema(tables=[users, orders, products, unrelated])


def test_rank_prefers_topically_relevant_table():
    retriever = TfIdfRetriever(_schema())
    ranked = retriever.rank("show me total order amount per user", top_k=4)
    top_names = [t.name for t, _ in ranked[:2]]
    assert "orders" in top_names
    # server_logs should not be in the top results
    assert "server_logs" not in [t.name for t, _ in ranked[:2]]


def test_fk_expansion_pulls_in_referenced_tables():
    schema = _schema()
    orders = next(t for t in schema.tables if t.name == "orders")
    expanded = expand_via_fks(schema, [orders], max_total=10)
    names = {t.name for t in expanded}
    assert "users" in names  # orders.user_id → users
    assert "orders" in names


def test_fk_expansion_pulls_in_referencing_tables():
    schema = _schema()
    users = next(t for t in schema.tables if t.name == "users")
    expanded = expand_via_fks(schema, [users], max_total=10)
    names = {t.name for t in expanded}
    assert "orders" in names  # orders references users


def test_fk_expansion_respects_max_total():
    schema = _schema()
    seed = [schema.tables[0]]
    expanded = expand_via_fks(schema, seed, max_total=1)
    assert len(expanded) == 1
