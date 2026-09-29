-- Deterministic synthetic data for the shop schema.
-- Tuned so that all 25 eval questions return non-empty, distinguishable result sets.

INSERT INTO countries (code, name, currency_code) VALUES
    ('IT', 'Italy', 'EUR'),
    ('US', 'United States', 'USD'),
    ('DE', 'Germany', 'EUR'),
    ('FR', 'France', 'EUR'),
    ('UK', 'United Kingdom', 'GBP'),
    ('JP', 'Japan', 'JPY');

INSERT INTO plans (name, monthly_price) VALUES
    ('free', 0.00),
    ('basic', 9.99),
    ('pro', 29.99),
    ('enterprise', 199.00);

INSERT INTO users (email, full_name, country_code, plan_id, signup_date, is_active) VALUES
    ('marco@example.com',  'Marco Rossi',     'IT', 3, CURRENT_DATE - INTERVAL '40 days', TRUE),
    ('lucia@example.com',  'Lucia Bianchi',   'IT', 2, CURRENT_DATE - INTERVAL '20 days', TRUE),
    ('giulia@example.com', 'Giulia Verdi',    'IT', 2, CURRENT_DATE - INTERVAL '5 days',  TRUE),
    ('paolo@example.com',  'Paolo Esposito',  'IT', 4, CURRENT_DATE - INTERVAL '300 days', TRUE),
    ('john@example.com',   'John Smith',      'US', 3, CURRENT_DATE - INTERVAL '50 days', TRUE),
    ('jane@example.com',   'Jane Doe',        'US', 1, CURRENT_DATE - INTERVAL '120 days', FALSE),
    ('hans@example.com',   'Hans Mueller',    'DE', 2, CURRENT_DATE - INTERVAL '15 days', TRUE),
    ('claire@example.com', 'Claire Dubois',   'FR', 3, CURRENT_DATE - INTERVAL '70 days', TRUE),
    ('oliver@example.com', 'Oliver King',     'UK', 4, CURRENT_DATE - INTERVAL '200 days', TRUE),
    ('akira@example.com',  'Akira Tanaka',    'JP', 2, CURRENT_DATE - INTERVAL '10 days', TRUE);

INSERT INTO addresses (user_id, street, city, postal_code, country_code, is_default) VALUES
    (1, 'Via Roma 1',      'Milano',  '20100', 'IT', TRUE),
    (2, 'Via Dante 5',     'Roma',    '00100', 'IT', TRUE),
    (3, 'Corso Italia 9',  'Torino',  '10100', 'IT', FALSE),
    (4, 'Via Garibaldi 3', 'Napoli',  '80100', 'IT', TRUE),
    (5, '1 Main St',       'NYC',     '10001', 'US', TRUE),
    (7, 'Hauptstr. 2',     'Berlin',  '10115', 'DE', TRUE),
    (8, 'Rue de Paris 7',  'Paris',   '75001', 'FR', TRUE),
    (9, '10 Downing St',   'London',  'SW1A',  'UK', TRUE),
    (10,'1-1 Chiyoda',     'Tokyo',   '100-0001','JP', TRUE);

INSERT INTO categories (name, parent_id) VALUES
    ('Electronics', NULL),
    ('Books',       NULL),
    ('Clothing',    NULL),
    ('Laptops',     1),
    ('Phones',      1),
    ('Fiction',     2),
    ('Non-fiction', 2);

INSERT INTO products (sku, name, category_id, price, is_available) VALUES
    ('LAP-001', 'ThinkPad X1',        4, 1899.00, TRUE),
    ('LAP-002', 'MacBook Air',        4, 1299.00, TRUE),
    ('PHN-001', 'iPhone 17',          5,  999.00, TRUE),
    ('PHN-002', 'Pixel 11',           5,  799.00, TRUE),
    ('PHN-003', 'Old Nokia',          5,   49.00, FALSE),
    ('BOK-001', 'Sapiens',            7,   19.99, TRUE),
    ('BOK-002', 'Norwegian Wood',     6,   14.99, TRUE),
    ('CLO-001', 'Plain T-shirt',      3,   19.00, TRUE),
    ('CLO-002', 'Wool Sweater',       3,   89.00, TRUE);

INSERT INTO orders (user_id, shipping_address_id, status, total_amount, created_at) VALUES
    (1, 1, 'delivered', 1899.00, NOW() - INTERVAL '35 days'),
    (1, 1, 'delivered',   19.99, NOW() - INTERVAL '25 days'),
    (1, 1, 'delivered',   89.00, NOW() - INTERVAL '15 days'),
    (1, 1, 'pending',    999.00, NOW() - INTERVAL '2 days'),
    (1, 1, 'delivered',   14.99, NOW() - INTERVAL '50 days'),
    (1, 1, 'delivered',   19.00, NOW() - INTERVAL '8 days'),
    (2, 2, 'delivered',  999.00, NOW() - INTERVAL '18 days'),
    (3, 3, 'pending',    799.00, NOW() - INTERVAL '3 days'),
    (4, 4, 'delivered', 1299.00, NOW() - INTERVAL '250 days'),
    (4, 4, 'delivered',   19.99, NOW() - INTERVAL '100 days'),
    (5, 5, 'delivered', 1299.00, NOW() - INTERVAL '40 days'),
    (5, 5, 'delivered',   89.00, NOW() - INTERVAL '20 days'),
    (7, 6, 'pending',   1899.00, NOW() - INTERVAL '10 days'),
    (8, 7, 'delivered',  999.00, NOW() - INTERVAL '60 days'),
    (9, 8, 'delivered', 1299.00, NOW() - INTERVAL '150 days'),
    (10,9, 'pending',     14.99, NOW() - INTERVAL '4 days');

INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 1, 1899.00),
    (2, 6, 1,   19.99),
    (3, 9, 1,   89.00),
    (4, 3, 1,  999.00),
    (5, 7, 1,   14.99),
    (6, 8, 1,   19.00),
    (7, 3, 1,  999.00),
    (8, 4, 1,  799.00),
    (9, 2, 1, 1299.00),
    (10,6, 1,   19.99),
    (11,2, 1, 1299.00),
    (12,9, 1,   89.00),
    (13,1, 1, 1899.00),
    (14,3, 1,  999.00),
    (15,2, 1, 1299.00),
    (16,7, 1,   14.99);

INSERT INTO payments (order_id, method, amount, processed_at, status) VALUES
    (1, 'card',   1899.00, NOW() - INTERVAL '35 days', 'paid'),
    (2, 'card',     19.99, NOW() - INTERVAL '25 days', 'paid'),
    (3, 'card',     89.00, NOW() - INTERVAL '15 days', 'paid'),
    (4, 'paypal',  999.00, NOW() - INTERVAL '2 days',  'paid'),
    (5, 'card',     14.99, NOW() - INTERVAL '50 days', 'paid'),
    (6, 'card',     19.00, NOW() - INTERVAL '8 days',  'paid'),
    (7, 'card',    999.00, NOW() - INTERVAL '18 days', 'paid'),
    (9, 'sepa',   1299.00, NOW() - INTERVAL '250 days','paid'),
    (10,'card',     19.99, NOW() - INTERVAL '100 days','paid'),
    (11,'card',   1299.00, NOW() - INTERVAL '40 days', 'paid'),
    (12,'card',     89.00, NOW() - INTERVAL '20 days', 'paid'),
    (14,'card',    999.00, NOW() - INTERVAL '60 days', 'paid'),
    (15,'card',   1299.00, NOW() - INTERVAL '150 days','paid');

INSERT INTO reviews (user_id, product_id, rating, body, created_at) VALUES
    (1, 1, 5, 'Excellent laptop',     NOW() - INTERVAL '30 days'),
    (1, 6, 4, 'Insightful',           NOW() - INTERVAL '20 days'),
    (4, 2, 5, 'Lightweight and fast', NOW() - INTERVAL '230 days'),
    (5, 2, 5, 'Great',                NOW() - INTERVAL '35 days'),
    (5, 9, 4, 'Warm',                 NOW() - INTERVAL '18 days'),
    (8, 3, 5, 'Best phone',           NOW() - INTERVAL '55 days'),
    (9, 2, 4, 'Solid',                NOW() - INTERVAL '140 days'),
    (1, 9, 5, 'Nice sweater',         NOW() - INTERVAL '12 days');

INSERT INTO shipments (order_id, carrier, tracking_no, shipped_at, delivered_at) VALUES
    (1,  'DHL', 'DHL001', NOW() - INTERVAL '34 days', NOW() - INTERVAL '32 days'),
    (2,  'UPS', 'UPS001', NOW() - INTERVAL '24 days', NOW() - INTERVAL '22 days'),
    (3,  'DHL', 'DHL002', NOW() - INTERVAL '14 days', NOW() - INTERVAL '12 days'),
    (5,  'UPS', 'UPS002', NOW() - INTERVAL '49 days', NOW() - INTERVAL '47 days'),
    (6,  'DHL', 'DHL003', NOW() - INTERVAL '7 days',  NOW() - INTERVAL '5 days'),
    (7,  'DHL', 'DHL004', NOW() - INTERVAL '17 days', NOW() - INTERVAL '15 days'),
    (9,  'TNT', 'TNT001', NOW() - INTERVAL '249 days',NOW() - INTERVAL '247 days'),
    (10, 'DHL', 'DHL005', NOW() - INTERVAL '99 days', NOW() - INTERVAL '97 days'),
    (11, 'UPS', 'UPS003', NOW() - INTERVAL '39 days', NOW() - INTERVAL '37 days'),
    (12, 'DHL', 'DHL006', NOW() - INTERVAL '19 days', NOW() - INTERVAL '17 days'),
    (14, 'DHL', 'DHL007', NOW() - INTERVAL '59 days', NOW() - INTERVAL '57 days'),
    (15, 'UPS', 'UPS004', NOW() - INTERVAL '149 days',NOW() - INTERVAL '147 days');
-- orders 4 (pending paypal), 8, 13, 16: paid but no shipment, or unshipped

INSERT INTO sessions (user_id, started_at, ended_at, ip_address, user_agent) VALUES
    (1, NOW() - INTERVAL '1 day',  NOW() - INTERVAL '1 day' + INTERVAL '1 hour',  '10.0.0.1', 'Mozilla/5.0'),
    (2, NOW() - INTERVAL '2 days', NOW() - INTERVAL '2 days' + INTERVAL '30 min', '10.0.0.2', 'Mozilla/5.0'),
    (3, NOW() - INTERVAL '3 days', NOW() - INTERVAL '3 days' + INTERVAL '20 min', '10.0.0.3', 'Mozilla/5.0'),
    (4, NOW() - INTERVAL '14 days',NOW() - INTERVAL '14 days' + INTERVAL '15 min','10.0.0.4', 'Mozilla/5.0'),
    (5, NOW() - INTERVAL '6 days', NOW() - INTERVAL '6 days' + INTERVAL '10 min', '10.0.0.5', 'Mozilla/5.0'),
    (1, NOW() - INTERVAL '6 hours',NULL,                                          '10.0.0.1', 'Mozilla/5.0');

INSERT INTO refunds (payment_id, reason, amount, refunded_at) VALUES
    (5, 'damaged',      14.99, NOW() - INTERVAL '45 days'),
    (9, 'changed mind', 100.00, NOW() - INTERVAL '240 days');

INSERT INTO audit_log (table_name, action, row_id, actor_user_id, occurred_at) VALUES
    ('products', 'update', 5, 1, NOW() - INTERVAL '5 days'),
    ('users',    'update', 6, 4, NOW() - INTERVAL '120 days'),
    ('orders',   'update', 4, 1, NOW() - INTERVAL '1 day'),
    ('plans',    'insert', 4, 4, NOW() - INTERVAL '300 days');

ANALYZE;
