-- AI 확인 단계 (app/estimate.py): 강수 기반 수위 상승 추정값과 2단 알람 결과.
ALTER TABLE valley_status ADD COLUMN IF NOT EXISTS est_rise_m  NUMERIC(5,2);              -- AI 추정 상승폭 (수위 없음·결측일 때만)
ALTER TABLE valley_status ADD COLUMN IF NOT EXISTS rain_24h_mm NUMERIC(6,1);              -- 규칙용 24시간 강수
ALTER TABLE valley_status ADD COLUMN IF NOT EXISTS estimated   BOOLEAN NOT NULL DEFAULT FALSE; -- 단계가 AI 추정으로 정해졌는지
