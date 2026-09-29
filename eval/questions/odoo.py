"""Hand-authored Odoo questions for the parsing-mode benchmark.

Each `must_retrieve` is the SET of tables that any correct SQL answer MUST
reference. Based on Odoo 18's standard table naming conventions:

  res_partner       — contacts (customers, vendors, employees)
  res_users         — system users
  res_country       — country reference
  product_template  — product master
  product_product   — product variant
  product_category  — product categories
  sale_order        — sales orders
  sale_order_line   — sales order line items
  purchase_order    — purchase orders
  purchase_order_line
  stock_picking     — shipments / receipts
  stock_move        — inventory movements
  stock_quant       — on-hand stock
  stock_warehouse, stock_location
  account_move      — accounting moves (invoices etc.)
  account_move_line
  account_journal, account_account
  hr_employee, hr_department, hr_job
  crm_lead          — leads / opportunities
  crm_team
  project_project, project_task
  mrp_production    — manufacturing orders
  mrp_bom           — bills of materials
  mail_message, mail_followers
  calendar_event
  fleet_vehicle
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    text: str
    must_retrieve: tuple[str, ...]
    difficulty: str = "medium"
    tags: tuple[str, ...] = ()


QUESTIONS: list[Question] = [
    # --- single-table lookups ---------------------------------------------
    Question(
        text="list all customers and vendors",
        must_retrieve=("res_partner",),
        difficulty="easy",
        tags=("single-table",),
    ),
    Question(
        text="show me all system users",
        must_retrieve=("res_users",),
        difficulty="easy",
        tags=("single-table",),
    ),
    Question(
        text="list every product in the catalog",
        must_retrieve=("product_template",),
        difficulty="easy",
        tags=("single-table",),
    ),
    Question(
        text="how many sales orders have been created",
        must_retrieve=("sale_order",),
        difficulty="easy",
        tags=("single-table", "aggregation"),
    ),
    Question(
        text="list all manufacturing orders",
        must_retrieve=("mrp_production",),
        difficulty="easy",
        tags=("single-table",),
    ),
    Question(
        text="show the chart of accounts",
        must_retrieve=("account_account",),
        difficulty="easy",
        tags=("single-table",),
    ),

    # --- one-hop joins ----------------------------------------------------
    Question(
        text="sales orders with their customer name",
        must_retrieve=("sale_order", "res_partner"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="purchase orders with the vendor name",
        must_retrieve=("purchase_order", "res_partner"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="list employees with their department",
        must_retrieve=("hr_employee", "hr_department"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="open opportunities with the salesperson assigned",
        must_retrieve=("crm_lead", "res_users"),
        difficulty="medium",
        tags=("join", "filter"),
    ),
    Question(
        text="show all project tasks with the project name",
        must_retrieve=("project_task", "project_project"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="products grouped by category",
        must_retrieve=("product_template", "product_category"),
        difficulty="medium",
        tags=("join", "group-by"),
    ),
    Question(
        text="employees per job position",
        must_retrieve=("hr_employee", "hr_job"),
        difficulty="medium",
        tags=("join", "group-by"),
    ),

    # --- multi-hop / harder ----------------------------------------------
    Question(
        text="for each sales order, list the line items with product name",
        must_retrieve=("sale_order", "sale_order_line", "product_product"),
        difficulty="hard",
        tags=("multi-join",),
    ),
    Question(
        text="total revenue per customer across all sales orders",
        must_retrieve=("sale_order", "res_partner"),
        difficulty="medium",
        tags=("join", "group-by", "aggregation"),
    ),
    Question(
        text="manufacturing orders with their bill of materials",
        must_retrieve=("mrp_production", "mrp_bom"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="incoming shipments with the source warehouse",
        must_retrieve=("stock_picking", "stock_warehouse"),
        difficulty="medium",
        tags=("join", "filter"),
    ),
    Question(
        text="current stock levels per product",
        must_retrieve=("stock_quant", "product_product"),
        difficulty="medium",
        tags=("join", "aggregation"),
    ),
    Question(
        text="purchase order lines with product and vendor",
        must_retrieve=("purchase_order", "purchase_order_line", "res_partner"),
        difficulty="hard",
        tags=("multi-join",),
    ),
    Question(
        text="invoices that are still unpaid, with the customer",
        must_retrieve=("account_move", "res_partner"),
        difficulty="medium",
        tags=("join", "filter"),
    ),

    # --- governance / messaging flavours ---------------------------------
    Question(
        text="leads with the assigned sales team",
        must_retrieve=("crm_lead", "crm_team"),
        difficulty="medium",
        tags=("join",),
    ),
    Question(
        text="calendar events organized by employee",
        must_retrieve=("calendar_event",),
        difficulty="easy",
        tags=("single-table",),
    ),
    Question(
        text="list vehicles in the company fleet",
        must_retrieve=("fleet_vehicle",),
        difficulty="easy",
        tags=("single-table",),
    ),

    # --- analytic / aggregation across domains ---------------------------
    Question(
        text="top 5 customers by total sales order value",
        must_retrieve=("sale_order", "res_partner"),
        difficulty="hard",
        tags=("join", "group-by", "order-by"),
    ),
    Question(
        text="number of project tasks per project, descending",
        must_retrieve=("project_task", "project_project"),
        difficulty="medium",
        tags=("join", "group-by"),
    ),
]
