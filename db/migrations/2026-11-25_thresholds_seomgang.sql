-- 섬강유원지 수위 기준 (#20, water_stats 11/25)
-- 전천교 비 없을 때 30분 출렁임 99.9% = 0.11m → 0.10m면 2024~25 여름 비 없이 16번 울림. 큰비 날 최대 1.00~1.67m 오름.
UPDATE valley_thresholds t
SET water_rise_m = v.m
FROM (VALUES ('caution', 0.25), ('danger', 0.50), ('evacuate', 0.75)) AS v(level, m), valleys va
WHERE t.valley_id = va.id AND va.name = '섬강유원지' AND t.level = v.level;
SELECT va.name, t.level, t.rain_1h_mm, t.water_rise_m FROM valley_thresholds t JOIN valleys va ON va.id = t.valley_id
WHERE va.name = '섬강유원지' ORDER BY t.water_rise_m;
