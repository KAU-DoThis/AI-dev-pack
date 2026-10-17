"""데이터셋 탭: 빈 칸 채우기 · 검사 · 분할 · 누수 · 잠금 · 학습 탭 연결. 엔진은 가짜 (jeff-kit 안 부름).

가짜 도구의 출력은 진짜 jeff-kit 과 같은 문구를 쓴다: 깃발이 없으면 '- none: …', 누수면 'leak check FAILED'.
"""

import json
import os
import stat

import pytest

from engines.base import EngineUnavailable, KitResult, can_prepare
from engines.jeff.dataset import JeffKit
from loop.core.config import action_labels, load_prompt
from loop.tabs.dataset import logic
from loop.tabs.train import logic as train


class FakeKit:
    """데이터 도구가 있는 엔진 흉내. split 은 줄 순서대로 4개 파일에 나눠 쓴다."""

    name = "가짜"

    def __init__(self, check_ok=True, split_ok=True, leak="pass", flags=0, report_ok=True):
        self.check_ok, self.split_ok, self.leak, self.flags, self.report_ok = check_ok, split_ok, leak, flags, report_ok
        self.calls = []

    def check_rows(self, file):
        self.calls.append(("check", file))
        return KitResult(self.check_ok, "ok" if self.check_ok else "line 1: 'question' is missing")

    def split(self, file, out_dir, test, development, calibration, seed):
        self.calls.append(("split", file, out_dir, test, development, calibration, seed))
        if not self.split_ok:
            return KitResult(False, "too few families")
        lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, name in enumerate(logic.SPLIT_FILES):
            (out_dir / f"{name}.jsonl").write_text("".join(lines[i::4]), encoding="utf-8")
        return KitResult(True, "split table")

    def leak_check(self, split_dir):
        return {"pass": KitResult(True, "leak check passed: no training row overlaps an evaluation row"),
                "leak": KitResult(False, "leak check FAILED. Training id -> evaluation id (rules):\n  a -> b (state)"),
                "crash": KitResult(False, "jeff-kit leak-check: train.jsonl: file has no rows")}[self.leak]

    def shortcut_report(self, split_dir):
        if not self.report_ok:
            return KitResult(False, "Traceback: boom")
        flags = "".join(f"- flag {i}\n" for i in range(self.flags)) or "- none: no check beat chance clearly.\n"
        return KitResult(True, "wrote", f"# Shortcut report\n\n## Flags\n\n{flags}\n## 1. Label balance\n\n- not a flag\n")


class NoKit:
    name = "도구없음"


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(logic, "data_root", lambda: root)
    yield root
    logic.remove_tree(root)  # 잠근(읽기 전용) 시험셋도 지울 수 있게


def make_file(tmp_path, rows, name="rows.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def basic_rows(n=8):
    labels = action_labels()
    return [{"state": {"company": f"Shop {i % 4}", "channel": "email", "message": f"text {i}"},
             "label": labels[i % len(labels)]} for i in range(n)]


# ---- 빈 칸 채우기 ----
def test_fill_adds_every_required_field_from_profile():
    filled, problems, notes = logic.fill(basic_rows(2), "src")
    assert problems == []
    row = filled[0]
    assert set(row) == {"id", "suite", "family", "state", "question", "label", "target", "source"}
    prompt = load_prompt("en")
    assert row["question"] == {"type": "choice", "instructions": prompt["instructions"], "criteria": prompt["options"]}
    assert row["target"] == row["label"] and row["id"] == "src-0001" and row["source"] == {"dataset": "src"}
    assert row["family"] == "shop 0"
    assert any("id 2줄" in n for n in notes)


def test_fill_keeps_given_fields_and_evens_out_family():
    given = {"id": "a1", "family": "  Fam  X ", "state": {"x": "y"}, "label": "zzz", "target": "zzz", "suite": "s",
             "question": {"type": "choice", "criteria": {"zzz": "z", "yyy": "y"}}, "source": {"dataset": "d"}}
    filled, problems, _ = logic.fill([dict(given)], "src")
    assert problems == [] and filled[0] == {**given, "family": "fam x"}  # 직접 적은 family 도 같은 규칙으로


@pytest.mark.parametrize("row, words", [
    ({"state": {"company": "A"}}, "정답(label)이 없어"),
    ({"state": {"company": "A"}, "label": "없는라벨"}, "프로필 라벨"),
    ({"label": "refund"}, "state 가 없어"),
    ({"state": {"x": 1}, "label": "b", "question": {"criteria": {"a": "a"}}}, "이 줄의 선택지에 없어"),
    ({"state": {"x": 1}, "label": "a", "question": "a 아니면 b"}, "question 은"),
    ({"state": {"x": 1}, "label": "a", "question": {"criteria": ["a", "b"]}}, "question 은"),
])
def test_fill_reports_problems(row, words):
    filled, problems, _ = logic.fill([row], "src")
    assert filled == [] and len(problems) == 1 and words in problems[0]


def test_fill_rejects_duplicate_ids():
    rows = basic_rows(2)
    rows[0]["id"] = rows[1]["id"] = "same"
    _, problems, _ = logic.fill(rows, "src")
    assert any("겹쳐" in p for p in problems)


@pytest.mark.parametrize("text, family", [
    ("Bramble Teas (UK)", "bramble teas"),
    ("스타벅스 강남역점", "스타벅스"),
    ("명동칼국수 2호점", "명동칼국수"),
    ("명동칼국수2호점", "명동칼국수"),
    ("(주)우아한형제들", "우아한형제들"),
    ("APPLE.COM/BILL", "apple.com/bill"),
    ("이마트 24", "이마트24"),
    ("STARBUCKS", "starbucks"),
])
def test_family_drops_brackets_and_branch(text, family):
    assert logic.family_of({"company": text}) == family


def test_auto_and_given_family_match():
    rows = [{"state": {"company": "Starbucks"}, "label": action_labels()[0]},
            {"state": {"company": "x"}, "label": action_labels()[0], "family": "STARBUCKS"}]
    filled, _, _ = logic.fill(rows, "src")
    assert filled[0]["family"] == filled[1]["family"] == "starbucks"


# ---- ① 불러오기 ----
def test_load_passes_and_stages(tmp_path, data):
    kit = FakeKit()
    message, preview, token = logic.load(str(make_file(tmp_path, basic_rows())), kit)
    assert message.startswith("✅") and "8줄" in message
    assert (data / logic.STAGING).exists() and len(preview) == 8
    assert token == logic.sha256(data / logic.STAGING)
    assert kit.calls[0][0] == "check" and not list(data.glob("*.checking"))


def test_load_stops_on_problems_without_saving(tmp_path, data):
    rows = basic_rows(3) + [{"state": {"company": "A"}}]
    message, _, token = logic.load(str(make_file(tmp_path, rows)), FakeKit())
    assert message.startswith("❌") and "정답(label)" in message and token == ""
    assert not (data / logic.STAGING).exists()


def test_load_reports_bad_json_line_and_encoding(tmp_path, data):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"state": {"a": "b"}, "label": "refund"}\nnot json\n', encoding="utf-8")
    assert "2번째 줄이 JSON 이 아니야" in logic.load(str(path), FakeKit())[0]
    path.write_bytes('{"state": {"a": "한글"}, "label": "refund"}\n'.encode("cp949"))
    assert "UTF-8 이 아니야" in logic.load(str(path), FakeKit())[0]


def test_failed_check_keeps_previous_staging(tmp_path, data):
    kit = FakeKit()
    _, _, token = logic.load(str(make_file(tmp_path, basic_rows())), kit)
    message, _, bad = logic.load(str(make_file(tmp_path, basic_rows(4), "b.jsonl")), FakeKit(check_ok=False))
    assert message.startswith("❌ 형식 검사 실패") and "'question' is missing" in message and bad == ""
    assert logic.sha256(data / logic.STAGING) == token  # 실패한 파일이 통과한 파일을 덮지 않음


def test_load_without_kit_or_file(tmp_path, data):
    assert logic.NOT_SUPPORTED in logic.load("x", NoKit())[0]
    assert "파일을 먼저" in logic.load(None, FakeKit())[0]
    assert not can_prepare(NoKit()) and can_prepare(FakeKit())


# ---- ② 분할 · 잠금 ----
def staged(tmp_path, kit) -> str:
    return logic.load(str(make_file(tmp_path, basic_rows())), kit)[2]


def test_split_makes_locked_dataset(tmp_path, data):
    kit = FakeKit(flags=2)
    token = staged(tmp_path, kit)
    message, report = logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=token)
    folder = data / "set-1"
    assert message.startswith("✅") and "누수 검사 통과" in message and "깃발 2개" in message
    assert "## Flags" in report
    assert all((folder / f"{n}.jsonl").exists() for n in logic.SPLIT_FILES) and (folder / logic.ROWS_FILE).exists()
    assert kit.calls[-1][3:] == (0.15, 0.15, 0.15, 7)
    assert not os.access(folder / "test.jsonl", os.W_OK)          # 읽기 전용
    assert logic.lock_state(folder) == "🔒 그대로" and logic.ready_problem(folder) == ""
    assert not (data / logic.STAGING).exists()                      # 다음 데이터는 다시 ① 부터
    assert not [p for p in data.iterdir() if p.name.startswith(".making")]
    info = logic.read_lock(folder)
    assert info["leak_ok"] is True and info["rows"]["train"] == 2 and len(info["sha256"]) == 5


def test_no_flags_means_zero_flags(tmp_path, data):
    kit = FakeKit(flags=0)
    message, _ = logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    assert "깃발 없음" in message


def test_failed_report_is_not_shown_as_clean(tmp_path, data):
    kit = FakeKit(report_ok=False)
    message, _ = logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    assert "못 만들었어" in message and "깃발 없음" not in message and "boom" in message


@pytest.mark.parametrize("changed", ["test.jsonl", "train.jsonl", "rows.jsonl"])
def test_any_changed_file_blocks_training(tmp_path, data, changed):
    kit = FakeKit()
    logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    path = data / "set-1" / changed
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    assert logic.lock_state(data / "set-1") == f"⚠️ 바뀜: {changed}"
    assert "바뀌었어" in logic.ready_problem(data / "set-1")
    assert "바뀌었어" in train.start("run-1", str(data / "set-1"), 1, 1e-4, 1, 1, 4, engine=TrainEngine())


def test_leaky_dataset_is_kept_but_never_trained(tmp_path, data):
    kit = FakeKit(leak="leak")
    message, _ = logic.split("leaky", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    assert "❌ 누수 검사 실패" in message and "학습 탭에서 이 데이터는 막혀" in message
    assert logic.table().iloc[0]["누수 검사"] == "❌"
    assert not any(c.endswith("/leaky") for c in logic.data_choices())       # 드롭다운에 안 나옴
    engine = TrainEngine()
    assert "누수 검사를 통과 못 했어" in train.start("run-1", str(data / "leaky"), 1, 1e-4, 1, 1, 4, engine=engine)
    assert engine.started == []                                                # 직접 적어도 막힘


def test_leak_tool_crash_is_not_a_leak(tmp_path, data):
    kit = FakeKit(leak="crash")
    message, _ = logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    assert "도구 오류" in message and not (data / "set-1").exists()


def test_split_failure_leaves_nothing(tmp_path, data):
    kit = FakeKit(split_ok=False)
    token = staged(tmp_path, kit)
    message, _ = logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=token)
    assert message.startswith("❌ 분할 실패") and not (data / "set-1").exists()
    assert not [p for p in data.iterdir() if p.name.startswith(".making")]
    assert (data / logic.STAGING).exists()  # 고쳐서 다시 시도할 수 있게 남김


@pytest.mark.parametrize("name, ratios, seed, words", [
    ("bad name!", (0.15, 0.15, 0.15), 7, "이름은"),
    ("NUL", (0.15, 0.15, 0.15), 7, "이름은"),
    ("ends.", (0.15, 0.15, 0.15), 7, "이름은"),
    ("ok", (0.3, 0.3, 0.3), 7, "비율"),
    ("ok", (0, 0.15, 0.15), 7, "비율"),
    ("ok", (0.15, 0.15, 0.15), None, "숫자"),
])
def test_split_rejects_bad_input(tmp_path, data, name, ratios, seed, words):
    kit = FakeKit()
    token = staged(tmp_path, kit)
    assert words in logic.split(name, *ratios, seed, kit, token=token)[0]
    assert name not in [p.name for p in data.iterdir()]  # Path('NUL').exists() 는 Windows 에서 항상 True


def test_split_needs_matching_load_and_new_name(tmp_path, data):
    kit = FakeKit()
    assert "먼저 **불러오기" in logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token="")[0]
    token = staged(tmp_path, kit)
    assert "먼저 **불러오기" in logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token="다른파일")[0]
    logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=token)
    token = staged(tmp_path, kit)
    assert "이미 있어" in logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=token)[0]


# ---- 목록 · 학습 탭 ----
class TrainEngine:
    """학습만 되는 가짜 엔진 (ready_problem 이 막으면 start_training 까지 안 와야 함)."""

    name = "가짜"

    def __init__(self):
        self.started = []

    def start_training(self, params):
        self.started.append(params)

    def training_status(self):
        return None

    def stop_training(self):
        pass


def test_data_choices_lists_made_sets_before_sample(tmp_path, data):
    kit = FakeKit()
    logic.split("set-1", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    choices = logic.data_choices()
    assert choices[0].endswith("data/set-1")
    if (logic.ROOT / logic.SAMPLE_DATA).exists():
        assert choices[-1] == logic.SAMPLE_DATA
    assert list(logic.table()["이름"]) == ["set-1"] and logic.table().iloc[0]["잠금"] == "🔒 그대로"


def test_unlocked_folder_still_trains(tmp_path):
    """견본·직접 만든 폴더(lock.json 없음)는 막지 않는다."""
    engine = TrainEngine()
    assert train.start("run-1", str(tmp_path), 1, 1e-4, 1, 1, 4, engine=engine).startswith("⏳")
    assert len(engine.started) == 1


# ---- Jeff 쪽 (jeff-kit 실행 전 막는 것) ----
def test_jeff_kit_missing_tool_is_explained(tmp_path):
    kit = JeffKit({"kit": str(tmp_path / "없음.exe")})
    with pytest.raises(EngineUnavailable, match="jeff-kit 을 못 찾았어"):
        kit.check_rows(tmp_path / "a.jsonl")


def test_jeff_kit_relative_path_is_from_webui_folder():
    assert JeffKit({"kit": "vendor/x.exe"}).kit == logic.ROOT / "vendor" / "x.exe"


def test_jeff_kit_split_never_overwrites(tmp_path):
    (tmp_path / "test.jsonl").write_text("{}\n", encoding="utf-8")
    result = JeffKit({"kit": str(tmp_path / "없음.exe")}).split(tmp_path / "rows.jsonl", tmp_path, 0.1, 0.1, 0.1, 7)
    assert not result.ok and "이미 나눈 파일" in result.message


# ---- 평가 탭이 데이터셋·학습 결과를 바로 고르게 ----
def test_evaluate_lists_made_test_sets_but_not_leaky(tmp_path, data):
    from loop.tabs.evaluate import logic as evaluate
    kit = FakeKit()
    logic.split("good", 0.15, 0.15, 0.15, 7, kit, token=staged(tmp_path, kit))
    leaky = FakeKit(leak="leak")
    logic.split("leaky", 0.15, 0.15, 0.15, 7, leaky, token=staged(tmp_path, leaky))
    choices = evaluate.test_choices()
    assert choices[0].endswith("data/good/test.jsonl") and choices[-1] == evaluate.SAMPLE_TEST
    assert not any("leaky" in c for c in choices)


class LastTrained(TrainEngine):
    def __init__(self, name):
        super().__init__()
        self.last = name

    def training_status(self):
        from engines.base import TrainStatus
        return TrainStatus("done", name=self.last)


def test_evaluate_defaults_to_base_and_last_trained():
    from loop.tabs.evaluate import logic as evaluate
    names = ["base", "a-old", "z-new"]
    assert evaluate.default_models(names, LastTrained("z-new")) == ["base", "z-new"]
    assert evaluate.default_models(names, LastTrained("")) == names          # 학습 기록 없으면 전부
    assert evaluate.default_models(names, NoKit()) == names                  # 학습 못 하는 엔진
    assert evaluate.default_models([], LastTrained("z-new")) == []           # 서버 꺼짐
