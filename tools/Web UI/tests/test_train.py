"""학습 탭·WSL 학습기: 실제 WSL 없이 runs 폴더를 임시 폴더로 바꿔 끼워 확인."""

import json

import pytest

from engines.base import EngineUnavailable, TrainParams, TrainStatus, can_train
from engines.jeff.trainer import SCRIPT, WslTrainer, to_wsl
from loop.tabs.train import logic


def make_data(folder, with_test=True):
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("train", "development", "calibration") + (("test",) if with_test else ()):
        rows = 31 if name == "train" else 7
        (folder / f"{name}.jsonl").write_text("{}\n" * rows, encoding="utf-8")
    return folder


def write_run(runs, name, state, steps=(), evals=(), log=""):
    run = runs / name
    (run / "run").mkdir(parents=True)
    (run / "data").mkdir()
    (run / "data" / "train.jsonl").write_text("{}\n" * 31)
    (run / "state").write_text(state + "\n")
    (run / "run" / "config.json").write_text(json.dumps({"epochs": 2, "effective_batch_size": 8}))
    (run / "run" / "training.jsonl").write_text(
        "".join(json.dumps({"kind": "training_step", "step": s, "loss": l}) + "\n" for s, l in steps) + '{"broken')
    (run / "run" / "evaluations.jsonl").write_text("".join(
        json.dumps({"step": s, "raw": {"accuracy": a, "nll": n}, "selected_step": s}) + "\n" for s, a, n in evals))
    (run / "train.log").write_text(log)
    return run


def test_to_wsl_path():
    assert to_wsl("C:\\Users\\user\\데이터") == "/mnt/c/Users/user/데이터"
    assert to_wsl(SCRIPT).endswith("/engines/jeff/wsl/train.sh")


def test_script_has_unix_line_endings():
    assert b"\r\n" not in SCRIPT.read_bytes()
    assert b"\r\n" not in (SCRIPT.parent / "serve.sh").read_bytes()


def test_script_never_reads_test_split():
    text = SCRIPT.read_text(encoding="utf-8")
    copy_line = next(line for line in text.splitlines() if line.strip().startswith("for f in"))
    assert "test" not in copy_line and "--temperature" in text


def test_idle_when_no_runs(tmp_path):
    assert WslTrainer(runs_dir=tmp_path / "runs").training_status().state == "idle"


def test_running_status_reads_progress(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", "running", steps=[(1, 1.9), (2, 0.7)], evals=[(0, 0.14, 1.6), (2, 0.71, 0.78)],
              log="Loading\r50%\r100%\nstep line\n")
    s = WslTrainer(runs_dir=runs).training_status()
    assert s.state == "running" and s.name == "a"
    assert s.steps == [(1, 1.9), (2, 0.7)]          # 쓰는 중인 깨진 줄은 건너뜀
    assert s.planned_steps == 8 and "step 2/8" in s.message
    assert s.log.splitlines() == ["100%", "step line", "step    0 · 개발셋 정확도 0.14 nll 1.600",
                                 "step    1 · loss 1.9000", "step    2 · loss 0.7000 · 개발셋 정확도 0.71 nll 0.780"]


def test_done_reloads_adapters_once(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", "done", evals=[(8, 0.86, 0.47)])
    calls = []
    trainer = WslTrainer(runs_dir=runs, reload_adapters=lambda: calls.append(1))
    assert trainer.training_status().state == "done"
    assert trainer.training_status().selected_step == 8
    assert calls == [1]


@pytest.mark.parametrize("state, expected", [("stopped", "stopped"), ("failed 1", "failed")])
def test_stopped_and_failed(tmp_path, state, expected):
    write_run(tmp_path / "runs", "a", state)
    assert WslTrainer(runs_dir=tmp_path / "runs").training_status().state == expected


def test_latest_run_is_shown_after_restart(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "old", "done")
    import os, time
    later = time.time() + 10
    write_run(runs, "new", "running")
    os.utime(runs / "new" / "state", (later, later))
    assert WslTrainer(runs_dir=runs).training_status().name == "new"


def test_check_blocks_bad_input(tmp_path):
    runs = tmp_path / "runs"
    data = make_data(tmp_path / "data")
    trainer = WslTrainer(runs_dir=runs)
    ok = TrainParams("good-1", str(data))
    assert trainer.check(ok) == data
    for params, words in [
        (TrainParams("나쁜 이름", str(data)), "이름"),
        (TrainParams("x", str(tmp_path / "없음")), "train.jsonl"),
        (TrainParams("x", str(data), lr=5), "학습률"),
        (TrainParams("x", str(data), batch_size=8, effective_batch_size=4), "배치"),
    ]:
        with pytest.raises(EngineUnavailable, match=words):
            trainer.check(params)
    write_run(runs, "used", "done")
    with pytest.raises(EngineUnavailable, match="이미 쓴 이름"):
        trainer.check(TrainParams("used", str(data)))
    write_run(runs, "busy", "running")
    with pytest.raises(EngineUnavailable, match="이미 학습이 돌고"):
        trainer.check(TrainParams("other", str(data)))


def test_jeff_engine_can_train():
    import engines
    assert can_train(engines.load("jeff", {}))


class NoTrainEngine:
    name = "학습못함"


def test_tab_explains_when_engine_cannot_train():
    assert logic.start("a", "x", 1, 1e-4, 1, 1, 4, NoTrainEngine()).startswith("❌")
    assert logic.status(NoTrainEngine()).message == logic.NOT_SUPPORTED


def test_view_makes_plot_tables():
    s = TrainStatus("running", "학습 중", "a", [(1, 1.9), (2, 0.7)], [(0, 0.14, 1.6), (2, 0.71, 0.78)], 8, 2, "log")
    message, loss, accuracy, nll, log = logic.view(s)
    assert list(loss.columns) == ["step", "loss"] and len(loss) == 2
    assert list(accuracy["accuracy"]) == [0.14, 0.71] and list(nll["nll"]) == [1.6, 0.78]
    assert "0.14 → **0.71**" in message and log == "log"


def test_default_name_is_valid():
    from engines.jeff.trainer import NAME_RULE
    assert NAME_RULE.match(logic.default_name())


def test_axes_are_fixed_from_the_start():
    s = TrainStatus("running", "", "a", [(1, 1.9)], [(0, 0.14, 1.6)], planned_steps=8)
    x, loss_y, eval_y = logic.axes(s)
    assert x == [0, 8]                       # 끝 step 까지 미리 잡아 둠 → 선만 늘어남
    assert loss_y == [0, 2.09] and eval_y == [0, 1.76]
    assert logic.axes(TrainStatus("idle"))[0] == [0, 1]


def test_signature_skips_redraw_when_nothing_changed():
    a = TrainStatus("running", "m", "a", [(1, 1.9)], [], 8, None, "log")
    b = TrainStatus("running", "m", "a", [(1, 1.9)], [], 8, None, "log")
    assert logic.signature(a) == logic.signature(b)
    b.steps.append((2, 0.7))
    assert logic.signature(a) != logic.signature(b)


def test_planned_steps_known_before_model_loads(tmp_path):
    run = tmp_path / "runs" / "a"
    (run / "data").mkdir(parents=True)
    (run / "data" / "train.jsonl").write_text("{}\n" * 31)
    (run / "params.json").write_text('{"epochs": 6, "effective_batch_size": 8}')
    (run / "state").write_text("running\n")
    s = WslTrainer(runs_dir=tmp_path / "runs").training_status()
    assert s.planned_steps == 24 and logic.axes(s)[0] == [0, 24]
