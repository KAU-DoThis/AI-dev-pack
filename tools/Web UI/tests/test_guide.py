"""처음 쓰는 사람 길잡이: 학습 이름 자동, 평가 한 문장 판정, 진행 단계, 추론 A·B 비교."""

from engines.base import TrainStatus
from engines.jeff.trainer import NAME_RULE
from loop.core import nav
from loop.core.config import append_record
from loop.tabs.evaluate import logic as evaluate
from loop.tabs.inference import ui as inference_ui
from loop.tabs.inference.logic import Outcome
from loop.tabs.train import logic as train


class Trainer:
    """학습만 되는 가짜 엔진. status 로 지금 학습 상태를 정한다."""

    name = "가짜"

    def __init__(self, status=None):
        self.started, self.status = [], status or TrainStatus("idle", "아직")

    def start_training(self, params):
        self.started.append(params)

    def training_status(self):
        return self.status

    def stop_training(self):
        pass


# ---- 학습: 이름 비우면 자동 ----
def test_blank_name_becomes_dataset_name_and_time(tmp_path):
    folder = tmp_path / "demo-2"
    folder.mkdir()
    engine = Trainer()
    assert train.start("", str(folder), 1, 1e-4, 1, 1, 4, engine=engine).startswith("⏳")
    name = engine.started[0].name
    assert name.startswith("demo-2-") and NAME_RULE.match(name)
    assert train.last_training()["name"] == name


def test_auto_name_is_always_valid():
    for folder in ["profiles/x/data/한글 폴더", "", "logs/smoke-train/split", "a" * 100]:
        assert NAME_RULE.match(train.auto_name(folder))


def test_train_view_guides_next_step():
    loading = train.view(TrainStatus("running", "⏳ 학습 중 · 모델 불러오는 중", "r"))[0]
    assert "30초" in loading
    done = train.view(TrainStatus("done", "✅ 끝", "r"))[0]
    assert "③ 평가" in done


# ---- 평가: 한 문장 판정 ----
def test_verdict_counts_problems_before_and_after():
    table = [["base", 7, 0.143, 1.4, 0.5, 80], ["tuned", 7, 1.0, 0.0, 1.0, 50]]
    text = evaluate.verdict(table, "base")
    assert "학습 효과 있음" in text and "1개 → 7개" in text and "14% → 100%" in text and "우연" in text


def test_verdict_same_or_worse():
    assert "같음" in evaluate.verdict([["base", 40, 0.5, 1, 1, 1], ["t", 40, 0.5, 1, 1, 1]], "base")
    worse = evaluate.verdict([["base", 40, 0.5, 1, 1, 1], ["t", 40, 0.25, 1, 1, 1]], "base")
    assert "더 못함" in worse and "우연" not in worse  # 문제가 충분하면 우연 안내 없음


def test_verdict_single_model_and_empty():
    assert "3개" in evaluate.verdict([["only", 4, 0.75, 1, 1, 1]], "base")
    assert evaluate.verdict([], "base") == ""


# ---- 진행 단계 ----
def test_steps_follow_the_work(tmp_path, monkeypatch):
    from loop.tabs.dataset import logic as dataset
    monkeypatch.setattr(dataset, "folders", lambda: [])
    assert nav.step_states(Trainer()) == ["👉 할 차례", "", "", ""]

    monkeypatch.setattr(dataset, "folders", lambda: [tmp_path / "demo-2"])
    assert nav.step_states(Trainer())[:2] == ["✅ demo-2", "👉 할 차례"]
    assert nav.step_states(Trainer(TrainStatus("running", "", "r1")))[1] == "⏳ 학습 중"

    done = Trainer(TrainStatus("done", "", "r1"))
    assert nav.step_states(done)[1:] == ["👉 할 차례", "", ""]  # 지난 학습이 끝나 있어도 이 데이터셋은 아직
    append_record(train.TRAININGS_FILE, {"at": "t", "name": "r1", "data_dir": str(tmp_path / "demo-2")})
    assert nav.step_states(done)[1:] == ["✅ r1", "👉 할 차례", "가능"]
    append_record(evaluate.EVALS_FILE, {"at": "t", "test": "x", "score": ["r1", 7, 1.0, 0.0, 1.0, 5]})
    assert nav.step_states(done)[2] == "👉 할 차례"  # 다른 시험셋(견본 등)으로 잰 건 안 침
    test = evaluate.test_key(str(tmp_path / "demo-2" / "test.jsonl"))
    append_record(evaluate.EVALS_FILE, {"at": "t", "test": test, "score": ["r1", 7, 1.0, 0.0, 1.0, 5]})
    assert nav.step_states(done)[2:] == ["✅ 함", "👉 해 보기"]


def test_steps_skip_leaky_dataset(tmp_path, monkeypatch):
    """누수 실패한 최신 데이터셋은 학습 탭에 안 나오니 진행 단계도 그 앞 데이터셋 기준."""
    import json
    from loop.tabs.dataset import logic as dataset
    good, leaky = tmp_path / "good", tmp_path / "leaky"
    for folder, ok in ((good, True), (leaky, False)):
        folder.mkdir()
        (folder / dataset.LOCK_FILE).write_text(json.dumps({"leak_ok": ok}), encoding="utf-8")
    monkeypatch.setattr(dataset, "folders", lambda: [leaky, good])
    append_record(train.TRAININGS_FILE, {"at": "t", "name": "r1", "data_dir": str(good)})
    assert nav.step_states(Trainer(TrainStatus("done", "", "r1")))[:2] == ["✅ good", "✅ r1"]


def test_progress_never_breaks_the_page(monkeypatch):
    def boom(engine=None):
        raise RuntimeError("x")
    monkeypatch.setattr(nav, "step_states", boom)
    assert "① 데이터셋" in nav.progress_markdown()


# ---- 추론: A·B 나란히 ----
def outcome(label):
    return Outcome({label: 0.9}, f"✅ {label}", {"m": label}, ["t", "x", label, 0.9, 0.9, "", "", 5, "영어", label])


def test_judge_runs_both_models(monkeypatch):
    calls = []

    def run(raw, rows, instructions, double_check, engine=None, lang="", model=""):
        calls.append(model)
        return outcome(model)
    monkeypatch.setattr(inference_ui.logic, "run", run)
    a, b, status, _, history, _ = inference_ui._judge("{}", [], "", False, [], "en", "base", "tuned")
    assert calls == ["base", "tuned"] and a == {"base": 0.9} and b == {"tuned": 0.9}
    assert "**A**" in status and "**B**" in status and [row[-1] for row in history] == ["tuned", "base"]


def test_judge_without_compare_runs_once(monkeypatch):
    calls = []

    def run(raw, rows, instructions, double_check, engine=None, lang="", model=""):
        calls.append(model)
        return outcome(model)
    monkeypatch.setattr(inference_ui.logic, "run", run)
    _, b, status, _, history, _ = inference_ui._judge("{}", [], "", False, [], "en", "base", inference_ui.NO_COMPARE)
    assert calls == ["base"] and b is None and "**B**" not in status and len(history) == 1


# ---- 리뷰 반영: 학습 탭 데이터셋 고르기 ----
def test_trained_before_counts_any_earlier_training(tmp_path):
    x, y = tmp_path / "x", tmp_path / "y"
    append_record(train.TRAININGS_FILE, {"at": "t", "name": "a", "data_dir": str(x)})
    append_record(train.TRAININGS_FILE, {"at": "t", "name": "b", "data_dir": str(y)})
    assert train.trained_before(str(x)) and train.needs_training(str(x))  # 전에 했지만 마지막은 아님
    assert not train.trained_before(str(tmp_path / "z"))


def test_train_tab_keeps_chosen_dataset_until_a_new_one_appears(monkeypatch):
    from loop.tabs.dataset import logic as dataset
    from loop.tabs.train import ui
    monkeypatch.setattr(dataset, "data_choices", lambda: ["new", "old", "sample"])
    update, seen = ui._on_show("old", "new")          # 새 데이터셋 없음 → 고르던 것
    assert update["value"] == "old" and seen == "new"
    update, seen = ui._on_show("old", "older-newest")  # 그사이 새 데이터셋 생김 → 그걸로
    assert update["value"] == "new" and seen == "new"
    assert ui._data_choices("C:/elsewhere/mine")["value"] == "C:/elsewhere/mine"  # 직접 적은 경로 유지


def test_verdict_without_base_lists_each_model():
    text = evaluate.verdict([["a", 40, 0.5, 1, 1, 1], ["b", 40, 0.75, 1, 1, 1]], "base")
    assert "`a` **20개**" in text and "`b` **30개**" in text
