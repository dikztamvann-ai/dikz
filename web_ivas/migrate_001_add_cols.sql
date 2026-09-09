-- Migrasi kolom yang kurang di D1 (tabel lama sudah ada, jadi CREATE TABLE
-- IF NOT EXISTS di schema.sql tidak akan menambahnya).
--
-- Jalankan:
--   npx wrangler d1 execute ivas_db --remote --file=migrate_001_add_cols.sql
--
-- Kalau kolom sudah ada, D1 akan error "duplicate column name" — aman,
-- jalankan satu-satu dan lewati yang sudah ada.

ALTER TABLE premium_access ADD COLUMN invoice_id TEXT;
ALTER TABLE premium_access ADD COLUMN granted_by INTEGER;
ALTER TABLE premium_access ADD COLUMN created_at TEXT;
ALTER TABLE premium_invoices ADD COLUMN expired_at TEXT;
