import json

import pytest

import loop.core.config as config
from engines.jeff.client import JeffClient, JeffUnavailable, _http_error
from loop.tabs.inference import logic


class FakeClient(JeffClient):
    """jeff-serve 대신 정해진 답을 돌려주는 가짜. 보낸 요청을 기록한다."""

    def __init__(self, choice="ask", probs=None, model=""):
        super().__init__("http://fake", model)
        self.choice, self.probs, self.sent = choice, probs, []

    def _send(self, method, path, body=None):
        self.sent.append((method, path, body))
        if path == "/health":
            return {"status": "ready", "model": "jeff-from-server", "max_options": 16}
        options = list(body["questions"]["choice"]["criteria"])
        probs = self.probs or {key: (0.7 if key == self.choice else 0.3 / (len(options) - 1)) for key in options}
        return {"answers": {"choice": {"type": "choice", "choice": self.choice, "probabilities": probs,
                                       "confidence": 0.5}}, "usage": {}}


ROWS = [["auto", "자동 처리", "clear"], ["ask", "사용자에게 질문", "unclear"]]


def test_bad_json_is_reported_without_calling_jeff():
    client = FakeClient()
    outcome = logic.run("{not json", ROWS, "", False, client)
    assert outcome.status.startswith("JSON 형식 오류")
    assert client.sent == []


def profile_rows():
    """지금 프로필의 라벨로 만든 선택지 표 [[id, 이름, 설명]]."""
    names = config.action_names()
    return [[key, names[key], f"desc {key}"] for key in config.action_labels()]


def test_judge_sends_options_and_marks_expected_match():
    example = config.load_examples()[0]
    client = FakeClient(choice=example["expect"])
    outcome = logic.run(logic.example_json(example["name"]), profile_rows(), "decide", False, client)
    _, _, body = client.sent[-1]
    assert body["model"] == "jeff-from-server"  # 설정이 비면 서버 이름 사용
    assert list(body["questions"]["choice"]["criteria"]) == config.action_labels()
    assert "orders" not in body
    assert next(iter(outcome.label)).startswith(example["expect"])  # 확률 높은 순
    assert outcome.row[5:7] == [example["expect"], "✅"]


def test_mismatch_with_example_expectation_is_shown():
    example = config.load_examples()[0]
    wrong = next(key for key in config.action_labels() if key != example["expect"])
    outcome = logic.run(logic.example_json(example["name"]), profile_rows(), "", True, FakeClient(choice=wrong))
    assert outcome.row[6] == "❌"
    assert f"기대값은 `{example['expect']}`" in outcome.status


def test_edited_input_has_no_expectation():
    example = config.load_examples()[0]
    state = json.loads(logic.example_json(example["name"])) | {"edited": True}
    outcome = logic.run(json.dumps(state), ROWS, "", False, FakeClient(choice="auto"))
    assert outcome.row[5:7] == ["", ""]


def test_double_check_sends_orders_2():
    client = FakeClient()
    logic.run('{"merchant": "x"}', ROWS, "", True, client)
    assert client.sent[-1][2]["orders"] == 2


def test_server_down_gives_korean_hint():
    client = JeffClient("http://127.0.0.1:9", timeout=2)  # 아무도 안 듣는 포트
    outcome = logic.run('{"merchant": "x"}', ROWS, "", False, client)
    assert "연결 못 했어" in outcome.status and outcome.row is None


def test_malformed_answer_is_rejected():
    client = FakeClient(choice="ask", probs={"auto": 1.0})  # ask 확률 빠짐
    with pytest.raises(JeffUnavailable, match="모양이 예상과 달라"):
        client.choose({"x": 1}, {"auto": "a", "ask": "b"})


@pytest.mark.parametrize("status, text", [
    (503, "loading"),
    (529, "busy"),
    (422, '{"detail": [{"loc": ["body", "model"], "msg": "unknown"}]}'),
])
def test_http_errors_become_korean(status, text):
    message = str(_http_error(status, text, {"retry-after": "1"}))
    assert any(word in message for word in ["불러오는 중", "처리 중", "모델 이름"])


def test_history_is_newest_first_and_capped():
    history = []
    for i in range(logic.HISTORY_LIMIT + 5):
        history = logic.add_history(history, [i])
    assert len(history) == logic.HISTORY_LIMIT and history[0] == [logic.HISTORY_LIMIT + 4]


def test_prompt_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setitem(config.PROMPT_FILES, "en", tmp_path / "prompt.en.yaml")
    first, *_, last = config.action_labels()
    config.save_prompt("decide well", {first: "한글도 됨"})
    prompt = config.load_prompt()
    assert prompt["instructions"] == "decide well"
    assert prompt["options"][first] == "한글도 됨"
    assert prompt["options"][last] == ""  # labels.yaml 에만 있는 라벨은 빈 설명


def test_every_example_expectation_is_a_known_label():
    labels = set(config.action_labels())
    for example in config.load_examples():
        assert example["expect"] in labels, example["name"]


def test_each_language_has_its_own_prompt_file(tmp_path, monkeypatch):
    monkeypatch.setitem(config.PROMPT_FILES, "en", tmp_path / "en.yaml")
    monkeypatch.setitem(config.PROMPT_FILES, "ko", tmp_path / "ko.yaml")
    first = config.action_labels()[0]
    config.save_prompt("decide", {first: "clear"}, "en")
    config.save_prompt("정해", {first: "분명함"}, "ko")
    assert config.load_prompt("en")["options"][first] == "clear"
    assert config.load_prompt("ko")["instructions"] == "정해"
    assert "(한국어)" in (tmp_path / "ko.yaml").read_text(encoding="utf-8")


def test_missing_language_file_gives_empty_fields(tmp_path, monkeypatch):
    monkeypatch.setitem(config.PROMPT_FILES, "ko", tmp_path / "none.yaml")
    prompt = config.load_prompt("ko")
    assert prompt["instructions"] == "" and set(prompt["options"].values()) == {""}


def test_shipped_korean_prompt_covers_every_label():
    assert all(config.load_prompt("ko")["options"].values())


def test_switch_keeps_unsaved_edits_per_language():
    from loop.tabs.inference import ui
    edited = [["auto", "자동 처리", "my edit"]]
    _, _, cur, drafts, _ = ui._switch("ko", "en", "my instr", edited, {})
    assert cur == "ko" and drafts["en"] == ("my instr", edited)
    instr, rows, cur, drafts, _ = ui._switch("en", "ko", "ko instr", [], drafts)
    assert (instr, rows, cur) == ("my instr", edited, "en")  # 영어 수정이 그대로 돌아옴


def test_history_records_prompt_language():
    outcome = logic.run('{"merchant": "x"}', ROWS, "", False, FakeClient(), lang="ko")
    assert outcome.row[logic.HISTORY_COLUMNS.index("설명")] == "한국어"
