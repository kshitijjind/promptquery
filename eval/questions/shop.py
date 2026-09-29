"""Hand-authored eval questions for the `shop` fixture.

Each Question carries:
- text: the natural-language question (what the user types)
- must_retrieve: tables the retriever MUST surface to have any chance of
  producing correct SQL. Used to score retrieval recall.
- nice_to_retrieve: tables that are useful but not strictly required.
  Not counted against recall, but tracked for precision diagnostics.
- reference_sql: a correct SQL answer (used by the future end-to-end eval).
- difficulty: easy / medium / hard — calibrated by hand.
- tags: free-form categorization for failure-mode analysis.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    text: str
    must_retrieve: tuple[str, ...]
    reference_sql: str
    difficulty: str = "medium"
    nice_to_retrieve: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


QUESTIONS: list[Question] = [
    # --- easy: single-table lookups ----------------------------------------
    Question(
        text="list all products",
        must_retrieve=("products",),
        reference_sql="SELECT * FROM products",
        difficulty="easy",
        tags=("single-table", "lookup"),
    ),
    Question(
        text="how many users do we have",
        must_retrieve=("users",),
        reference_sql="SELECT COUNT(*) FROM users",
        difficulty="easy",
        tags=("single-table", "aggregation"),
    ),
    Question(
        text="show me all countries",
        must_retrieve=("countries",),
        reference_sql="SELECT * FROM countries",
        difficulty="easy",
        tags=("single-table", "lookup"),
    ),
    Question(
        text="list the available subscription plans",
        must_retrieve=("plans",),
        reference_sql="SELECT * FROM plans",
        difficulty="easy",
        tags=("single-table", "lookup"),
    ),
    Question(
        text="what are the product categories",
        must_retrieve=("categories",),
        reference_sql="SELECT * FROM categories",
        difficulty="easy",
        tags=("single-table", "lookup"),
    ),

    # --- easy: aggregations ------------------------------------------------
    Question(
        text="how many orders were placed this month",
        must_retrieve=("orders",),
        reference_sql=(
            "SELECT COUNT(*) FROM orders "
            "WHERE created_at >= date_trunc('month', CURRENT_DATE)"
        ),
        difficulty="easy",
        tags=("single-table", "aggregation", "time-range"),
    ),
    Question(
        text="what is the average product price",
        must_retrieve=("products",),
        reference_sql="SELECT AVG(price) FROM products",
        difficulty="easy",
        tags=("single-table", "aggregation"),
    ),

    # --- medium: one-hop joins --------------------------------------------
    Question(
        text="how many users signed up from Italy last month",
        must_retrieve=("users", "countries"),
        reference_sql=(
            "SELECT COUNT(*) FROM users u "
            "JOIN countries c ON c.code = u.country_code "
            "WHERE c.name = 'Italy' "
            "AND u.signup_date >= (date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') "
            "AND u.signup_date <  date_trunc('month', CURRENT_DATE)"
        ),
        difficulty="medium",
        tags=("join", "time-range"),
    ),
    Question(
        text="list orders with their customer's email",
        must_retrieve=("orders", "users"),
        reference_sql=(
            "SELECT o.id, o.total_amount, u.email "
            "FROM orders o JOIN users u ON u.id = o.user_id"
        ),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="which products have never been reviewed",
        must_retrieve=("products", "reviews"),
        reference_sql=(
            "SELECT p.* FROM products p "
            "LEFT JOIN reviews r ON r.product_id = p.id "
            "WHERE r.id IS NULL"
        ),
        difficulty="medium",
        tags=("join", "anti-join"),
    ),
    Question(
        text="show product names and their category names",
        must_retrieve=("products", "categories"),
        reference_sql=(
            "SELECT p.name AS product, c.name AS category "
            "FROM products p JOIN categories c ON c.id = p.category_id"
        ),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="users who have a default shipping address",
        must_retrieve=("users", "addresses"),
        reference_sql=(
            "SELECT u.* FROM users u "
            "JOIN addresses a ON a.user_id = u.id "
            "WHERE a.is_default = TRUE"
        ),
        difficulty="medium",
        tags=("join", "filter"),
    ),

    # --- medium: subqueries / having ---------------------------------------
    Question(
        text="users who placed more than 5 orders",
        must_retrieve=("users", "orders"),
        reference_sql=(
            "SELECT u.id, u.email, COUNT(o.id) AS order_count "
            "FROM users u JOIN orders o ON o.user_id = u.id "
            "GROUP BY u.id, u.email HAVING COUNT(o.id) > 5"
        ),
        difficulty="medium",
        tags=("join", "group-by", "having"),
    ),
    Question(
        text="products with an average rating above 4",
        must_retrieve=("products", "reviews"),
        reference_sql=(
            "SELECT p.id, p.name, AVG(r.rating) AS avg_rating "
            "FROM products p JOIN reviews r ON r.product_id = p.id "
            "GROUP BY p.id, p.name HAVING AVG(r.rating) > 4"
        ),
        difficulty="medium",
        tags=("join", "group-by", "having"),
    ),

    # --- hard: multi-hop joins --------------------------------------------
    Question(
        text="total revenue per country last quarter",
        must_retrieve=("orders", "users", "countries"),
        nice_to_retrieve=("payments",),
        reference_sql=(
            "SELECT c.name AS country, SUM(o.total_amount) AS revenue "
            "FROM orders o "
            "JOIN users u ON u.id = o.user_id "
            "JOIN countries c ON c.code = u.country_code "
            "WHERE o.created_at >= (date_trunc('quarter', CURRENT_DATE) - INTERVAL '3 months') "
            "AND o.created_at <  date_trunc('quarter', CURRENT_DATE) "
            "GROUP BY c.name ORDER BY revenue DESC"
        ),
        difficulty="hard",
        tags=("multi-join", "group-by", "time-range"),
    ),
    Question(
        text="top 3 best-selling products by quantity",
        must_retrieve=("products", "order_items"),
        reference_sql=(
            "SELECT p.id, p.name, SUM(oi.quantity) AS units_sold "
            "FROM products p JOIN order_items oi ON oi.product_id = p.id "
            "GROUP BY p.id, p.name ORDER BY units_sold DESC LIMIT 3"
        ),
        difficulty="medium",
        tags=("join", "group-by", "order-by"),
    ),
    Question(
        text="average order value per subscription plan",
        must_retrieve=("orders", "users", "plans"),
        reference_sql=(
            "SELECT pl.name AS plan, AVG(o.total_amount) AS avg_order_value "
            "FROM orders o "
            "JOIN users u ON u.id = o.user_id "
            "JOIN plans pl ON pl.id = u.plan_id "
            "GROUP BY pl.name"
        ),
        difficulty="hard",
        tags=("multi-join", "group-by"),
    ),
    Question(
        text="orders that were paid but never shipped",
        must_retrieve=("orders", "payments", "shipments"),
        reference_sql=(
            "SELECT o.id FROM orders o "
            "JOIN payments p ON p.order_id = o.id "
            "LEFT JOIN shipments s ON s.order_id = o.id "
            "WHERE p.status = 'paid' AND s.id IS NULL"
        ),
        difficulty="hard",
        tags=("multi-join", "anti-join"),
    ),
    Question(
        text="customers who have requested a refund",
        must_retrieve=("users", "orders", "payments", "refunds"),
        reference_sql=(
            "SELECT DISTINCT u.id, u.email FROM users u "
            "JOIN orders o ON o.user_id = u.id "
            "JOIN payments p ON p.order_id = o.id "
            "JOIN refunds r ON r.payment_id = p.id"
        ),
        difficulty="hard",
        tags=("multi-join", "distinct"),
    ),

    # --- hard: window functions / advanced --------------------------------
    Question(
        text="for each category, the top 2 products by sales",
        must_retrieve=("products", "categories", "order_items"),
        reference_sql=(
            "WITH ranked AS ("
            "  SELECT p.id, p.name, c.name AS category, SUM(oi.quantity) AS units, "
            "         ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY SUM(oi.quantity) DESC) AS rn "
            "  FROM products p "
            "  JOIN categories c ON c.id = p.category_id "
            "  JOIN order_items oi ON oi.product_id = p.id "
            "  GROUP BY p.id, p.name, c.id, c.name"
            ") SELECT * FROM ranked WHERE rn <= 2"
        ),
        difficulty="hard",
        tags=("multi-join", "window-function", "cte"),
    ),
    Question(
        text="month-over-month order count for the last year",
        must_retrieve=("orders",),
        reference_sql=(
            "SELECT date_trunc('month', created_at) AS month, COUNT(*) AS orders_n "
            "FROM orders "
            "WHERE created_at >= CURRENT_DATE - INTERVAL '1 year' "
            "GROUP BY 1 ORDER BY 1"
        ),
        difficulty="medium",
        tags=("single-table", "time-bucket"),
    ),

    # --- medium: time-range / activity ------------------------------------
    Question(
        text="users who logged in this week",
        must_retrieve=("users", "sessions"),
        reference_sql=(
            "SELECT DISTINCT u.id, u.email FROM users u "
            "JOIN sessions s ON s.user_id = u.id "
            "WHERE s.started_at >= date_trunc('week', CURRENT_DATE)"
        ),
        difficulty="medium",
        tags=("join", "time-range", "distinct"),
    ),

    # --- audit / governance flavour ---------------------------------------
    Question(
        text="recent activity by admin users",
        must_retrieve=("audit_log", "users"),
        reference_sql=(
            "SELECT al.* FROM audit_log al "
            "JOIN users u ON u.id = al.actor_user_id "
            "ORDER BY al.occurred_at DESC LIMIT 100"
        ),
        difficulty="medium",
        tags=("join", "audit"),
    ),

    # --- "negative" / discriminator cases ---------------------------------
    # These exercise that the retriever doesn't latch onto distractors.
    # Ground truth = the table needed; we mark obvious distractors as
    # nice_to_retrieve so they don't poison precision diagnostics either way.
    Question(
        text="total inventory available for sale",
        must_retrieve=("products",),
        reference_sql="SELECT COUNT(*) FROM products WHERE is_available = TRUE",
        difficulty="easy",
        tags=("single-table", "filter"),
    ),
    Question(
        text="payment methods used by our customers",
        must_retrieve=("payments",),
        reference_sql="SELECT DISTINCT method FROM payments",
        difficulty="easy",
        tags=("single-table", "distinct"),
    ),
]
