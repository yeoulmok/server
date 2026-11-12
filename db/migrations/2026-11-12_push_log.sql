-- 푸시 발송 기록. 발송 조건 판단(복귀 알림 중복 방지)의 기준이 된다.
CREATE TABLE IF NOT EXISTS push_log (
  id         SERIAL PRIMARY KEY,
  valley_id  INT NOT NULL REFERENCES valleys(id),
  kind       TEXT NOT NULL,            -- 'alert' | 'recovery'
  level      TEXT NOT NULL,
  devices    INT NOT NULL,             -- 보낸(보내려 한) 기기 수
  dry_run    BOOLEAN NOT NULL,         -- APNs 키가 없어 실제로는 안 보낸 경우 true
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS push_log_valley_idx ON push_log (valley_id, created_at DESC);
