"""평가 탭: 가짜 엔진으로 채점 계산 확인."""

import json
import math

from engines.base import Decision, EngineUnavailable
from loop.tabs.evaluate import logic


class Fixed:
    """항상 첫 선택지를 0.8 로 고르는 가짜 엔진."""

    name = "가짜"

    def __init__(self):
        self.models_used = []

    def models(self):
        return ["base"]

    def choose(self, state, options, instructions="", orders=1, model=""):
        self.models_used.append(model)
        first = next(iter(options))
        probs = {k: (0.8 if k == first else 0.2 / (len(options) - 1)) for k in options}
        return Decision(first, probs, 0.6, 10.0, {}, {})


def write(path, labels):
    criteria = {"a": "A", "b": "B"}
    path.write_text("".join(json.dumps({"state": {"i": i}, "question": {"criteria": criteria}, "label": label}) + "\n"
                            for i, label in enumerate(labels)), encoding="utf-8")
    return path


def test_scores_per_model(tmp_path):
    engine = Fixed()
    table, status = logic.run(str(write(tmp_path / "t.jsonl", ["a", "a", "b", "a"])), ["base", "tuned"], engine)
    assert status.startswith("✅")
    assert [row[0] for row in table] == ["base", "tuned"]
    assert table[0][1:3] == [4, 0.75]
    assert table[0][3] == round(-(3 * math.log(0.8) + math.log(0.2)) / 4, 3)
    assert engine.models_used == ["base"] * 4 + ["tuned"] * 4


def test_bad_inputs_are_reported(tmp_path):
    assert logic.run(str(tmp_path / "없음.jsonl"), ["base"], Fixed())[1].startswith("❌ 파일이 없어")
    assert logic.run(str(write(tmp_path / "t.jsonl", ["a"])), [], Fixed())[1].startswith("❌ 모델")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"state": {}, "question": {"criteria": {"a": "A"}}, "label": "z"}\n', encoding="utf-8")
    assert "label" in logic.run(str(bad), ["base"], Fixed())[1]


def test_engine_failure_stops_with_message(tmp_path):
    class Down(Fixed):
        def choose(self, *args, **kwargs):
            raise EngineUnavailable("꺼짐")
    table, status = logic.run(str(write(tmp_path / "t.jsonl", ["a"])), ["base"], Down())
    assert table == [] and "꺼짐" in status


def test_sample_test_file_is_readable():
    path = logic.data_path(logic.SAMPLE_TEST)
    if path.exists():  # logs/ 는 커밋 안 함 → 없는 컴퓨터에선 건너뜀
        assert len(logic.read_rows(path)) == 15
