"""평가 탭: 가짜 엔진으로 채점 계산 확인."""

import json
import math

from engines.base import Decision, EngineUnavailable
from loop.core.config import append_record, read_records
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
    report = logic.run(str(write(tmp_path / "t.jsonl", ["a", "a", "b", "a"])), ["base", "tuned"], engine)
    table = report.table
    assert report.status.startswith("✅")
    assert [row[0] for row in table] == ["base", "tuned"]
    assert table[0][1:3] == [4, 0.75]
    assert table[0][3] == round(-(3 * math.log(0.8) + math.log(0.2)) / 4, 3)
    assert engine.models_used == ["base"] * 4 + ["tuned"] * 4


def test_bad_inputs_are_reported(tmp_path):
    assert logic.run(str(tmp_path / "없음.jsonl"), ["base"], Fixed()).status.startswith("❌ 파일이 없어")
    assert logic.run(str(write(tmp_path / "t.jsonl", ["a"])), [], Fixed()).status.startswith("❌ 모델")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"state": {}, "question": {"criteria": {"a": "A"}}, "label": "z"}\n', encoding="utf-8")
    assert "label" in logic.run(str(bad), ["base"], Fixed()).status


def test_engine_failure_stops_with_message(tmp_path):
    class Down(Fixed):
        def choose(self, *args, **kwargs):
            raise EngineUnavailable("꺼짐")
    report = logic.run(str(write(tmp_path / "t.jsonl", ["a"])), ["base"], Down())
    assert report.table == [] and "꺼짐" in report.status


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
    report = logic.run(path, ["base", "tuned"], Down())
    assert "꺼짐" in report.status and [r[2] for r in logic.history(path)] == ["base"]


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


# ---- 라벨별 점수 · 혼동표 · 틀린 줄 · 문턱 ----
class Scripted(Fixed):
    """줄마다 정해 둔 a 확률을 내는 가짜 엔진. b 확률 = 1 - a 확률. state.i 로 줄을 찾는다."""

    def __init__(self, probs):
        super().__init__()
        self.probs = probs

    def choose(self, state, options, instructions="", orders=1, model=""):
        a = self.probs[state["i"]]
        return Decision("a" if a >= 0.5 else "b", {"a": a, "b": 1 - a}, max(a, 1 - a), 1.0, {}, {})


LABELS = ["a", "a", "a", "a", "b", "b", "b", "b"]
PROBS = [0.9, 0.6, 0.4, 0.2, 0.15, 0.1, 0.1, 0.7]  # a 를 놓친 두 줄(0.4, 0.2), b 를 a 로 고른 한 줄(0.7)


def preds(engine=None):
    rows = [{"state": {"i": i}, "question": {"criteria": {"a": "A", "b": "B"}}, "label": label}
            for i, label in enumerate(LABELS)]
    return logic.predict(engine or Scripted(PROBS), rows, "m")


def test_label_stats_precision_and_recall():
    stats = logic.label_stats(preds())
    assert (stats["a"]["hits"], stats["a"]["picked"], stats["a"]["actual"]) == (2, 3, 4)
    assert stats["a"]["precision"] == 2 / 3 and stats["a"]["recall"] == 0.5
    assert stats["b"]["precision"] == 3 / 5 and stats["b"]["recall"] == 0.75


def test_details_show_confusion_and_goal_verdict():
    text = logic.details_markdown("m", preds(), "a", 0.95, 0.9)
    assert "| a | 2 | 2 |" in text and "| b | 1 | 3 |" in text  # 정답 a → a2·b2, 정답 b → a1·b3
    assert "❌ 미달" in text and "재현율 50%" in text and "나머지 정밀도 60%" in text
    clean = Scripted([.9, .9, .9, .9, .1, .1, .1, .1])
    assert "✅ 합격" in logic.details_markdown("m", preds(clean), "a", 0.95, 0.9)


def test_wrong_rows_most_confident_first():
    wrong = logic.wrong_rows("m", preds())
    assert [row[4:] for row in wrong] == [[0.2, 0.8], [0.3, 0.7], [0.4, 0.6]]
    assert all(row[2] != row[3] for row in wrong)


def test_apply_threshold_goes_to_target_at_or_above():
    assert logic.apply_threshold({"a": 0.2, "b": 0.8}, "a", 0.2) == "a"
    assert logic.apply_threshold({"a": 0.19, "b": 0.81}, "a", 0.2) == "b"
    assert logic.apply_threshold({"a": 0.1, "b": 0.3, "c": 0.6}, "a", 0.2) == "c"


def test_sweep_lower_threshold_raises_recall():
    table = {row[0]: row for row in logic.sweep(preds(), "a", 0.95, 0.5)}
    assert table[0.5][2] == "50%"
    assert table[0.2][2] == "100%"  # 놓친 0.4·0.2 를 모두 잡음
    assert table[0.2][3] == "100%" and table[0.2][5] == "✅"
    assert logic.recommend(list(table.values())) == 0.2


def test_sweep_sending_everything_to_target_is_not_a_pass():
    table = logic.sweep(preds(Scripted([0.9] * 8)), "a", 0.95, 0.9)
    assert not any(row[5] for row in table if row[4] == "100%")
    assert logic.recommend([[0.5, 0.5, "100%", "-", "100%", ""]]) is None


def test_threshold_changes_scores_and_is_recorded(tmp_path):
    path = write(tmp_path / "t.jsonl", LABELS)
    report = logic.run(str(path), ["m"], Scripted(PROBS), target="a", threshold=0.2)
    assert report.table[0][2] == 0.875  # a 4줄 + b 3줄 맞힘 / 8
    row = logic.history(str(path))[0]
    assert row[-2] == "a ≥ 0.2" and row[-1].startswith("a ")
    logic.run(str(path), ["m"], Scripted(PROBS))
    assert logic.history(str(path))[0][-2] == "-"


def test_bad_threshold_is_refused(tmp_path):
    path = str(write(tmp_path / "t.jsonl", LABELS))
    assert "0과 1 사이" in logic.run(path, ["m"], Scripted(PROBS), target="a", threshold=1.5).status
    assert "선택지에 없는" in logic.run(path, ["m"], Scripted(PROBS), target="z", threshold=0.2).status


def test_threshold_needs_a_single_model(tmp_path):
    path = str(write(tmp_path / "t.jsonl", LABELS))
    report = logic.run(path, ["base", "m"], Scripted(PROBS), target="a", threshold=0.2)
    assert "하나만" in report.status and report.table == [] and logic.history(path) == []


def test_wrong_rows_are_limited_per_model(tmp_path, monkeypatch):
    monkeypatch.setattr(logic, "WRONG_LIMIT", 2)
    path = str(write(tmp_path / "t.jsonl", LABELS))
    report = logic.run(path, ["base", "m"], Scripted(PROBS))
    assert [row[0] for row in report.wrong] == ["base", "base", "m", "m"]
    assert "틀린 줄 3개 중 앞 2개만" in report.details


def test_old_records_without_details_still_show(tmp_path):
    path = str(write(tmp_path / "t.jsonl", ["a"]))
    append_record(logic.EVALS_FILE, {"at": "2026-11-01T20:11:29", "test": logic.test_key(path), "sha256": "x",
                                     "score": ["old", 1, 1.0, 0.0, 1.0, 5]})
    assert logic.history(path)[0][-2:] == ["-", "-"]


def test_run_sweep_only_accepts_calibration_files(tmp_path):
    test = write(tmp_path / "test.jsonl", LABELS)
    table, message = logic.run_sweep(str(test), "m", "a", Scripted(PROBS))
    assert table == [] and "보정셋" in message
    assert read_records(logic.THRESHOLDS_FILE) == []


def test_run_sweep_recommends_and_keeps_test_history_clean(tmp_path):
    calibration = write(tmp_path / "calibration.jsonl", LABELS)
    table, message = logic.run_sweep(str(calibration), "m", "a", Scripted([.9, .9, .9, .9, .1, .1, .1, .1]))
    assert len(table) == len(logic.THRESHOLDS) and "추천 문턱" in message
    assert read_records(logic.EVALS_FILE) == []  # 보정셋 비교는 시험 횟수에 안 들어감
    assert read_records(logic.THRESHOLDS_FILE)[0]["model"] == "m"
    assert "우연에" in message  # a 4줄뿐
    assert "너무 쉬워서" in message  # 모든 문턱 합격


def test_run_sweep_no_easy_warning_when_thresholds_differ(tmp_path):
    calibration = write(tmp_path / "calibration.jsonl", LABELS)
    assert "너무 쉬워서" not in logic.run_sweep(str(calibration), "m", "a", Scripted(PROBS))[1]


def test_run_sweep_warns_when_calibration_is_from_another_dataset(tmp_path):
    calibration = write(tmp_path / "calibration.jsonl", LABELS)
    append_record("trainings.jsonl", {"name": "m", "data_dir": "profiles/x/data/other"})
    assert "다른 데이터의 보정셋" in logic.run_sweep(str(calibration), "m", "a", Scripted(PROBS))[1]
