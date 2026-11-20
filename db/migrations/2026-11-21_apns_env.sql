-- #45 TestFlight(production)·Xcode(sandbox) 토큰 둘 다 보내기 + Apple 응답 기록
-- 실행: docker exec -i db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/migrations/2026-11-21_apns_env.sql
ALTER TABLE devices  ADD COLUMN IF NOT EXISTS apns_env TEXT
  CHECK (apns_env IN ('production', 'sandbox'));          -- 이 토큰이 성공한 쪽 (모르면 NULL)
ALTER TABLE push_log ADD COLUMN IF NOT EXISTS detail TEXT; -- 토큰마다 Apple 응답 (예: …a1b2c3 production 200)
