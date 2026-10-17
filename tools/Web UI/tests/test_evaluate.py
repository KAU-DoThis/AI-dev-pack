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


# ---- 시험셋별 기록 · 오염 경고 · 기본 선택 ----
def test_scores_are_recorded_per_test_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = str(write(tmp_path / "a" / "test.jsonl", ["a", "b"]))
    b = str(write(tmp_path / "b" / "test.jsonl", ["a"]))
    logic.run(a, ["base", "tuned"], Fixed())
    logic.run(b, ["base"], Fixed())
    rows = logic.history(a)
    assert [r[1:3] for r in rows] == [["a", "tuned"], ["a", "base"]]  # 최근 것 먼저, a 기록만
    assert len(logic.history(b)) == 1
    assert "평가 1번" in logic.history_status(a) and "⚠️" not in logic.history_status(a)


def test_warns_after_many_runs_on_same_test(tmp_path):
    import time
    path = str(write(tmp_path / "t.jsonl", ["a"]))
    for _ in range(logic.WARN_AFTER):
        logic.run(path, ["base"], Fixed())
        time.sleep(1.01)  # 기록은 초 단위 시각으로 평가 횟수를 센다
    assert f"{logic.WARN_AFTER}번 평가했어" in logic.history_status(path)


def test_warns_when_test_file_changed_between_runs(tmp_path):
    path = write(tmp_path / "t.jsonl", ["a"])
    logic.run(str(path), ["base"], Fixed())
    write(path, ["a", "b"])
    logic.run(str(path), ["base"], Fixed())
    assert "내용이 기록 중간에 바뀌었어" in logic.history_status(str(path))


def test_failed_model_is_not_recorded(tmp_path):
    class Down(Fixed):
        def choose(self, *args, model="", **kwargs):
            if model == "tuned":
                raise EngineUnavailable("꺼짐")
            return super().choose(*args, model=model, **kwargs)
    path = str(write(tmp_path / "t.jsonl", ["a"]))
    table, status = logic.run(path, ["base", "tuned"], Down())
    assert "꺼짐" in status and [r[2] for r in logic.history(path)] == ["base"]


def test_defaults_follow_last_training(tmp_path):
    from loop.tabs.train import logic as train

    class Trains(Fixed):
        def start_training(self, params):
            pass

        def training_status(self):
            return None

        def stop_training(self):
            pass

    folder = tmp_path / "set"
    folder.mkdir()
    train.start("run-9", str(folder), 1, 1e-4, 1, 1, 4, engine=Trains())
    choices = [logic.test_key(str(folder / "test.jsonl")), logic.SAMPLE_TEST]
    assert logic.default_test(choices) == choices[0]
    assert logic.default_models(["base", "a", "run-9"]) == ["base", "run-9"]
    assert logic.default_test([logic.SAMPLE_TEST]) == logic.SAMPLE_TEST  # 그 시험셋이 후보에 없으면 맨 앞


def test_label_is_dataset_name():
    assert logic.test_label("profiles/x/data/domo-1/test.jsonl") == "domo-1"
    assert logic.test_label("C:/a/my.jsonl") == "my"
