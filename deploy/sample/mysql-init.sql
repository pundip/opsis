-- Sample source database for trying Opsis and for the connector tests.
-- Started by the "sample" Compose profile (COMPOSE_PROFILES=sample in .env).
--   host: sample-mysql   port: 3306   database: sample   user: reader   password: reader-sample-pw

CREATE TABLE donors (
  id INT PRIMARY KEY AUTO_INCREMENT,
  full_name VARCHAR(100) NOT NULL,
  region VARCHAR(10) NOT NULL,
  joined_on DATE NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1
);

CREATE TABLE gifts (
  id INT PRIMARY KEY AUTO_INCREMENT,
  donor_id INT NOT NULL,
  fund_name VARCHAR(60) NOT NULL,
  amount DECIMAL(12, 2) NOT NULL,
  gift_date DATE NOT NULL,
  channel VARCHAR(20),
  notes JSON,
  updated_at DATETIME NOT NULL
);

-- 200 donors and 5000 gifts, generated so the numbers are stable between runs.
INSERT INTO donors (full_name, region, joined_on, is_active)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 200)
SELECT CONCAT('Donor ', LPAD(n, 3, '0')),
       ELT(1 + n % 6, 'VIC', 'NSW', 'QLD', 'WA', 'SA', 'TAS'),
       DATE_ADD('2019-01-01', INTERVAL (n * 9) DAY),
       n % 11 <> 0
FROM seq;

SET SESSION cte_max_recursion_depth = 10000;

INSERT INTO gifts (donor_id, fund_name, amount, gift_date, channel, notes, updated_at)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5000)
SELECT 1 + (n * 7) % 200,
       ELT(1 + n % 5, 'Scholarships', 'Medical Research', 'Library', 'Student Hardship', 'Sport'),
       ROUND(20 + (n * 37 % 4980) + (n % 100) / 100, 2),
       DATE_ADD('2023-07-01', INTERVAL (n * 5) HOUR),
       IF(n % 9 = 0, NULL, ELT(1 + n % 4, 'online', 'mail', 'event', 'phone')),
       IF(n % 50 = 0, JSON_OBJECT('pledge', TRUE, 'n', n), NULL),
       DATE_ADD('2023-07-01 08:00:00', INTERVAL (n * 5) HOUR)
FROM seq;

CREATE USER 'reader'@'%' IDENTIFIED BY 'reader-sample-pw';
GRANT SELECT ON sample.* TO 'reader'@'%';
