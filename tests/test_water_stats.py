import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import water_stats as W  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2025, 7, 1, 0, 0, tzinfo=KST)


def series(vals, step=10):
    return {T0 + timedelta(minutes=step * i): (None if v is None else D(str(v))) for i, v in enumerate(vals)}


def test_rises_30m():
    r = W.rises_30m(series([1.0, 1.0, 1.0, 1.2, 1.3]))
    assert r[T0 + timedelta(minutes=30)] == D("0.2") and r[T0 + timedelta(minutes=40)] == D("0.3")


def test_is_dry():
    hourly = {T0 + timedelta(hours=h): D("0") for h in range(0, 10)}
    assert W.is_dry(T0 + timedelta(hours=8, minutes=20), hourly)
    hourly[T0 + timedelta(hours=5)] = D("2")
    assert not W.is_dry(T0 + timedelta(hours=8, minutes=20), hourly)
    assert not W.is_dry(T0 + timedelta(hours=2), hourly)              # 앞 시간 자료 없음 → 비 없다고 못 봄


def test_episodes():
    ts = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=10)]
    assert W.episodes(ts) == [T0, T0 + timedelta(hours=10)]


def test_hold_values_matches_slow():
    vals = [1.0, 0.8, 0.9, 1.4, 1.2, 0.7, 1.1]
    s = series(vals, step=60 * 5)                                     # 5시간 간격 → 24h 창에 5칸
    fast = W.hold_values(s)
    from app import backtest as B
    assert fast == B.hold_series(s)


def test_suggest():
    c, why = W.suggest(D("0.02"), [D("0.3"), D("0.25"), D("0.4")])
    assert c["caution"] == D("0.10") and "맞음" in why
    c, why = W.suggest(D("0.08"), [D("0.5"), D("0.6")])               # 평소 출렁임 큼 → 0.20
    assert c["caution"] == D("0.20") and c["evacuate"] == D("0.60") and "올림" in why
    c, why = W.suggest(D("0.01"), [D("0.06"), D("0.07"), D("0.08")])  # 작은 개울 → 내림
    assert c["caution"] == D("0.05") and "내림" in why


def test_analyze_smoke():
    vals = [1.0] * 60 + [1.0, 1.1, 1.25, 1.4, 1.5, 1.5] + [1.5] * 30
    water = series(vals)
    hourly = {T0 + timedelta(hours=h): D("0") for h in range(-7, 17)}
    hourly[T0 + timedelta(hours=10)] = D("30")
    st = W.analyze("시험", "X", water, hourly)
    assert st.storm_max30 and st.storm_max30[0] >= D("0.4")
    assert st.fires["caution"][0] == 1 and st.fires["caution"][1] == 0


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
