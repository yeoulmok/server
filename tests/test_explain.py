"""AI 설명(app/explain.py) 테스트 — 실제 API 없이 가짜 Claude 서버로.  실행: python tests/test_explain.py"""

import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import explain as X  # noqa: E402
from app.estimate import Estimate  # noqa: E402
from app.judge import Signal, Threshold, decide  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
T = datetime(2026, 8, 9, 14, 0, tzinfo=KST)
TH = [Threshold("caution", D("15.0"), D("0.10")),
      Threshold("danger", D("25.0"), D("0.20")),
      Threshold("evacuate", D("40.0"), D("0.30"))]


def caution_by_rain():
    return decide(Signal(D("18.0"), T), Signal(D("0.06"), T), TH)


def test_facts_rain_caution():
    f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
    text = "\n".join(f.lines)
    assert "지금 단계: 주의" in text
    assert "상류 1시간 강수: 18.0mm (기준 15.0mm) → 기준을 넘음" in text
    assert "하류 30분 수위 상승: 0.06m (기준 0.10m) → 기준 아래" in text
    assert f.numbers == {"18.0", "15.0", "0.06", "0.10"}


def test_facts_danger_uses_danger_threshold():
    v = decide(Signal(D("26.5"), T), Signal(D("0.05"), T), TH)
    f = X.build_facts("금대리 계곡", v, TH)
    assert "상류 1시간 강수: 26.5mm (기준 25.0mm) → 기준을 넘음" in f.lines


def test_facts_ai_estimate():
    est = Estimate(D("18.0"), D("0.0812"), T)
    v = decide(Signal(D("2.0"), T), Signal(None, None, "연결된 관측소 없음"), TH, est, has_water=False)
    f = X.build_facts("갑천면 계곡", v, TH)
    text = "\n".join(f.lines)
    assert "AI 추정 앞으로 3시간 수위 상승: 0.08m (기준 0.07m) → 주의 기준을 넘음" in text
    assert "24시간 강수: 18.0mm (기준 17.0mm)" in text
    assert "이 단계는 실제 수위가 아니라 AI 추정으로 정해짐" in text
    assert "하류 30분 수위 상승" not in text


def test_check_accepts_good_text():
    f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
    good = "상류에 1시간 동안 18mm의 비가 와서 기준 15mm를 넘었어요. 하류 수위는 30분 동안 0.06m 올라 아직 크지 않지만 주의가 필요해요."
    assert X.check(good, f) is None


def test_check_rejects():
    f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
    assert X.check("상류에 비가 23mm 왔어요.", f).startswith("사실에 없는 숫자")
    assert X.check("비가 많이 와서 위험해요.", f) == "다른 단계 이름 '위험'"
    assert X.check("30분 뒤에 물이 불어나요.", f) == "도착 시간 예측"
    assert X.check("가" * 200, f).startswith("너무 김")
    assert X.check("", f) == "빈 응답"


def test_prompt_names_current_level_only():
    system, user = X.prompt(X.build_facts("금대리 계곡", caution_by_rain(), TH))
    assert "'주의'만 쓰고" in system and user.startswith("계곡: 금대리 계곡")


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def json(self):
        return self._data


class _FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def post(self, url, headers, json):
        self.calls.append((url, headers, json))
        return self.replies.pop(0)


def _text(t):
    return _Resp(200, {"content": [{"type": "text", "text": t}]})


def _with_key(fn):
    old = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        fn()
    finally:
        if old is None:
            os.environ.pop("ANTHROPIC_API_KEY")
        else:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_generate_ok_and_request_shape():
    def run():
        f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
        claude = _FakeClaude([_text("상류 1시간 강수가 18.0mm로 기준 15.0mm를 넘어 주의 단계예요.")])
        text, reason = X.generate(f, claude)
        assert reason is None and text.startswith("상류")
        url, headers, body = claude.calls[0]
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "test-key" and headers["anthropic-version"] == "2023-06-01"
        assert body["messages"][0]["role"] == "user" and "지금 단계: 주의" in body["messages"][0]["content"]
        assert "'주의'만 쓰고" in body["system"]
    _with_key(run)


def test_generate_retries_once_then_falls_back():
    def run():
        f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
        claude = _FakeClaude([_text("비가 23mm 왔어요."), _text("상류에 비가 기준보다 많이 와서 주의 단계예요.")])
        text, reason = X.generate(f, claude)
        assert text and len(claude.calls) == 2                  # 두 번째에 통과
        claude = _FakeClaude([_text("위험해요."), _text("30분 뒤 도착해요.")])
        text, reason = X.generate(f, claude)
        assert text is None and reason == "도착 시간 예측"        # 두 번 다 실패 → 고정 문구
    _with_key(run)


def test_generate_api_error_falls_back():
    def run():
        f = X.build_facts("금대리 계곡", caution_by_rain(), TH)
        claude = _FakeClaude([_Resp(529, {"error": {"message": "Overloaded"}})])
        text, reason = X.generate(f, claude)
        assert text is None and "529" in reason and "test-key" not in reason
    _with_key(run)


def test_should_explain():
    assert X.should_explain("caution", True, None, T)
    assert not X.should_explain("caution", False, T - timedelta(minutes=10), T)
    assert X.should_explain("danger", False, T - timedelta(minutes=30), T)          # 30분마다 새로
    assert not X.should_explain("safe", True, None, T)                              # 안전은 고정 문구
    assert not X.should_explain("unknown", True, None, T)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
