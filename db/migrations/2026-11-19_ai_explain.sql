-- AI 설명 (app/explain.py): 마지막으로 설명을 만든(시도한) 시각. 같은 경보 단계가 이어지면 30분마다 새로 만든다.
ALTER TABLE valley_status ADD COLUMN IF NOT EXISTS ai_explained_at TIMESTAMPTZ;
