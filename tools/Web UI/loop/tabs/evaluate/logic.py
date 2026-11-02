"""평가 탭 로직 (gradio 없이 테스트 가능). 켜져 있는 엔진에 시험셋을 한 줄씩 보내 정답과 비교한다.

시험셋 한 줄 = {"state": {...}, "question": {"instructions": ..., "criteria": {id: 설명}}, "label": 정답 id}
점수는 시험셋마다 문제가 달라 같은 시험셋끼리만 비교한다 → 평가할 때마다 기록(프로필 outputs/evaluations.jsonl)에
쌓고, 화면은 고른 시험셋의 기록만 보여 준다. 같은 시험셋으로 자주 평가하면 오염 경고.

문턱 = "대상 라벨 확률이 t 이상이면 대상 라벨, 아니면 나머지 중 가장 높은 라벨". 문턱을 고르는 비교는 보정셋으로만 한다
(시험셋으로 고르면 시험셋에 맞춘 튜닝이 된다). 고른 문턱은 시험셋 평가에 적용해 최종 점수를 낸다.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from engines.base import can_train
from loop.core.config import action_labels, append_record, load_profile, read_records
from loop.core.engine import Engine, EngineUnavailable, get_engine
from loop.tabs.dataset.logic import folders, ready_problem, shown
from loop.tabs.train.logic import data_path, last_training

SAMPLE_TEST = "logs/smoke-train/split/test.jsonl"
COLUMNS = ["모델", "줄 수", "정확도", "nll", "평균 확신도", "평균 ms"]   # score() 한 줄
HISTORY_COLUMNS = ["시각", "시험셋", *COLUMNS, "문턱", "라벨별 정밀·재현"]  # 화면 표
WRONG_COLUMNS = ["모델", "입력", "정답", "모델의 답", "정답 확률", "답 확률"]
SWEEP_COLUMNS = ["문턱", "정확도", "대상 재현율", "나머지 정밀도", "대상으로 보낸 비율", "합격"]
THRESHOLDS = [round(i / 20, 2) for i in range(1, 20)]  # 0.05 ~ 0.95
CALIBRATION_FILE = "calibration.jsonl"
EVALS_FILE = "evaluations.jsonl"
THRESHOLDS_FILE = "thresholds.jsonl"
WARN_AFTER = 3  # 같은 시험셋으로 이만큼 평가하면 경고 (점수 보고 설정을 고치면 시험셋에 맞춰 튜닝하게 됨)
WRONG_LIMIT = 500  # 모델 하나당 화면 표에 보여 줄 틀린 줄 수
EASY_PASS_SHARE = 0.8  # 문턱이 이 비율 이상 합격하면 보정셋이 문턱을 못 가려낸다고 경고


def test_choices() -> list[str]:
    """시험셋 후보: 데이터셋 탭에서 만든 것(누수 통과·안 바뀐 것만, 최근 것 먼저) + 견본."""
    made = [shown(folder / "test.jsonl") for folder in folders() if not ready_problem(folder)]
    return made + [SAMPLE_TEST]


def default_test(choices: list[str] | None = None) -> str:
    """기본 시험셋: 마지막 학습에 쓴 데이터 폴더의 test.jsonl (후보에 있으면), 아니면 맨 앞."""
    choices = choices if choices is not None else test_choices()
    last = last_training()
    if last:
        test = test_key(str(data_path(last["data_dir"]) / "test.jsonl"))
        if test in choices:
            return test
    return choices[0] if choices else SAMPLE_TEST


def default_models(names: list[str], engine: Engine | None = None) -> list[str]:
    """기본으로 고를 모델: 맨 앞(학습 전) + 마지막으로 학습한 모델. 학습 기록이 없으면 전부."""
    last = (last_training() or {}).get("name", "")
    if not last:
        engine = engine or get_engine()
        last = engine.training_status().name if can_train(engine) else ""
    if names and last in names[1:]:
        return [names[0], last]
    return names


def test_key(test_file: str) -> str:
    """기록에 쓰는 시험셋 이름: 웹 UI 폴더 기준 경로 (같은 파일이면 어떻게 적어도 같게)."""
    return shown(data_path(test_file or ""))


def test_label(key: str) -> str:
    """표에 보일 짧은 이름: .../domo-1/test.jsonl → domo-1."""
    path = Path(key)
    return path.parent.name if path.name == "test.jsonl" and path.parent.name else path.stem


def read_rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{number}번째 줄이 JSON 이 아니야: {error.msg}") from None
        criteria = (row.get("question") or {}).get("criteria") or {}
        if not isinstance(row.get("state"), dict) or row.get("label") not in criteria:
            raise ValueError(f"{number}번째 줄에 state·question.criteria·label(선택지 중 하나)이 없어.")
        rows.append(row)
    if not rows:
        raise ValueError("빈 파일이야.")
    return rows


@dataclass
class Prediction:
    row: dict
    choice: str
    probabilities: dict[str, float]
    confidence: float
    ms: float

    @property
    def label(self) -> str:
        return self.row["label"]


@dataclass
class Report:
    """평가 한 번의 결과. table = 모델별 점수, details = 라벨별 점수·혼동표(마크다운), wrong = 틀린 줄 표."""
    table: list[list] = field(default_factory=list)
    status: str = ""
    details: str = ""
    wrong: list[list] = field(default_factory=list)


def apply_threshold(probabilities: dict[str, float], target: str, threshold: float) -> str:
    """대상 라벨 확률이 문턱 이상이면 대상 라벨, 아니면 나머지 중 확률이 가장 높은 라벨."""
    if probabilities.get(target, 0.0) >= threshold:
        return target
    return max((label for label in probabilities if label != target), key=probabilities.get, default=target)


def predict(engine: Engine, rows: list[dict], model: str, target: str = "", threshold: float | None = None) -> list[Prediction]:
    predictions = []
    for row in rows:
        question = row["question"]
        decision = engine.choose(row["state"], question["criteria"], question.get("instructions") or "", model=model)
        choice = decision.choice if threshold is None else apply_threshold(decision.probabilities, target, threshold)
        predictions.append(Prediction(row, choice, decision.probabilities, decision.confidence, decision.elapsed_ms))
    return predictions


def score(model: str, predictions: list[Prediction]) -> list:
    n = len(predictions)
    hits = sum(p.choice == p.label for p in predictions)
    nll = -sum(math.log(max(p.probabilities.get(p.label, 0.0), 1e-12)) for p in predictions)
    confidence = sum(p.confidence for p in predictions)
    ms = sum(p.ms for p in predictions)
    return [model, n, round(hits / n, 3), round(nll / n, 3), round(confidence / n, 3), round(ms / n)]


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def label_stats(predictions: list[Prediction]) -> dict[str, dict]:
    """라벨별 {정답 수, 고른 수, 맞힌 수, 정밀도, 재현율}. 모델이 선택지 밖 라벨을 내도 빠지지 않게 답까지 모은다."""
    labels = list(dict.fromkeys(label for p in predictions for label in (*p.row["question"]["criteria"], p.choice)))
    stats = {}
    for label in labels:
        actual = sum(p.label == label for p in predictions)
        picked = sum(p.choice == label for p in predictions)
        hits = sum(p.label == label and p.choice == label for p in predictions)
        stats[label] = {"actual": actual, "picked": picked, "hits": hits,
                        "precision": ratio(hits, picked), "recall": ratio(hits, actual)}
    return stats


def target_scores(predictions: list[Prediction], target: str) -> tuple[float | None, float | None]:
    """(대상 라벨 재현율, 나머지 정밀도). 나머지 정밀도 = 대상이 아니라고 답한 줄 중 맞힌 비율."""
    actual = [p for p in predictions if p.label == target]
    others = [p for p in predictions if p.choice != target]
    return (ratio(sum(p.choice == target for p in actual), len(actual)),
            ratio(sum(p.choice == p.label for p in others), len(others)))


def passes(recall: float | None, precision: float | None, goal_recall: float, goal_precision: float) -> bool:
    # 전부 대상으로 보내면 재현율 100% 라도 정밀도가 없으므로 합격이 아니다
    return recall is not None and precision is not None and recall >= goal_recall and precision >= goal_precision


def details_markdown(model: str, predictions: list[Prediction], target: str, goal_recall: float, goal_precision: float) -> str:
    """모델 하나의 라벨별 점수표 + 혼동표 + (대상 라벨이 있으면) 합격선 판정."""
    stats = label_stats(predictions)
    labels = list(stats)
    lines = [f"#### `{model}`", "", "| 라벨 | 정답 수 | 고른 수 | 정밀도 | 재현율 |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {label} | {s['actual']} | {s['picked']} | {percent(s['precision'])} | {percent(s['recall'])} |"
              for label, s in stats.items()]
    if target in stats:
        recall, precision = target_scores(predictions, target)
        mark = "✅ 합격" if passes(recall, precision, goal_recall, goal_precision) else "❌ 미달"
        lines += ["", f"{mark}: `{target}` 재현율 {percent(recall)} (기준 {goal_recall:.0%}) · "
                      f"나머지 정밀도 {percent(precision)} (기준 {goal_precision:.0%})"]
    lines += ["", "| 정답 ↓ / 답 → | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
    for actual in labels:
        counts = [sum(p.label == actual and p.choice == picked for p in predictions) for picked in labels]
        lines.append(f"| {actual} | " + " | ".join(map(str, counts)) + " |")
    return "\n".join(lines)


def input_name(state: dict) -> str:
    """틀린 줄 표에 보일 입력 이름: 프로필 display_keys 중 처음 찾은 칸, 없으면 입력 전체."""
    for key in load_profile()["display_keys"]:
        if state.get(key):
            return str(state[key])
    return json.dumps(state, ensure_ascii=False)


def wrong_rows(model: str, predictions: list[Prediction]) -> list[list]:
    """틀린 줄. 자신 있게 틀린 것(답 확률이 높은 것)이 먼저."""
    wrong = [p for p in predictions if p.choice != p.label]
    wrong.sort(key=lambda p: -p.probabilities.get(p.choice, 0.0))
    return [[model, input_name(p.row["state"]), p.label, p.choice,
             round(p.probabilities.get(p.label, 0.0), 3), round(p.probabilities.get(p.choice, 0.0), 3)] for p in wrong]


SMALL_TEST = 30  # 문제가 이보다 적으면 한두 개 차이는 우연일 수 있다고 덧붙임


def verdict(table: list[list], base: str) -> str:
    """처음 쓰는 사람용 한 문장 판정: 학습 전(base)보다 학습한 모델이 몇 문제 더 맞혔나."""
    if not table:
        return ""
    n = table[0][1]

    def hits(row):
        return round(row[2] * row[1])

    before = next((row for row in table if row[0] == base), None)
    others = [row for row in table if row[0] != base]
    small = f"\n\n문제가 {n}개뿐이라 한두 개 차이는 우연일 수 있어. 데이터가 많아지면 더 믿을 만해져." if n < SMALL_TEST else ""
    if before and others:
        best = max(others, key=lambda row: (row[2], -row[3]))
        if best[2] > before[2]:
            head = "✅ 학습 효과 있음"
        elif best[2] == before[2]:
            head = "➖ 학습 전과 같음"
        else:
            head = "⚠️ 학습 후가 더 못함"
        return (f"### {head}\n처음 보는 문제 {n}개 중 **{hits(before)}개 → {hits(best)}개** 맞힘 "
                f"(정확도 {before[2]:.0%} → {best[2]:.0%}, `{best[0]}`){small}")
    if len(table) > 1:  # 학습 전 모델 없이 여러 모델끼리
        lines = " · ".join(f"`{row[0]}` **{hits(row)}개** ({row[2]:.0%})" for row in table)
        return f"### 처음 보는 문제 {n}개 중 맞힌 수\n{lines}{small}"
    row = table[0]
    return f"### `{row[0]}`: 처음 보는 문제 {n}개 중 **{hits(row)}개** 맞힘 (정확도 {row[2]:.0%}){small}"


# ---- 기록 ----
def records_for(test_file: str) -> list[dict]:
    key = test_key(test_file)
    return [r for r in read_records(EVALS_FILE) if r.get("test") == key]


def _threshold_text(record: dict) -> str:
    threshold = record.get("threshold")
    return f"{threshold['label']} ≥ {threshold['value']}" if threshold else "-"


def _labels_text(record: dict) -> str:
    labels = record.get("labels") or {}
    return " · ".join(f"{label} {percent(s['precision'])}/{percent(s['recall'])}" for label, s in labels.items()) or "-"


def history(test_file: str) -> list[list]:
    """고른 시험셋의 기록, 최근 것 먼저."""
    rows = [[r["at"][5:16].replace("T", " "), test_label(r["test"]), *r["score"], _threshold_text(r), _labels_text(r)]
            for r in records_for(test_file)]
    return rows[::-1]


def history_status(test_file: str) -> str:
    records = records_for(test_file)
    if not records:
        return f"`{test_label(test_key(test_file))}` 은(는) 아직 평가 기록이 없어. 모델을 고르고 **평가 실행**."
    runs = len({r["at"] for r in records})
    changed = len({r.get("sha256") for r in records}) > 1
    text = f"`{test_label(records[0]['test'])}` 평가 {runs}번 · 같은 시험셋 기록만 보여 줘 (다른 시험셋과는 점수 비교 안 됨)."
    if changed:
        text += "\n\n⚠️ 이 경로의 파일 내용이 기록 중간에 바뀌었어. 예전 점수와 비교하지 마."
    if runs >= WARN_AFTER:
        text += (f"\n\n⚠️ 이 시험셋으로 {runs}번 평가했어. 점수를 보고 설정·데이터를 계속 고치면 시험셋에 맞춰 튜닝하는 셈이라 "
                 "점수가 부풀어. 설정 고르기는 학습 탭의 개발셋으로 하고, 시험셋은 마지막 확인에만.")
    return text


def review_label() -> str:
    """대상 라벨 기본값: 프로필의 review_label, 없으면 첫 라벨."""
    labels = action_labels()
    wanted = load_profile()["review_label"]
    return wanted if wanted in labels else (labels[0] if labels else "")


def goals() -> tuple[float, float]:
    profile = load_profile()
    return float(profile["goal_recall"]), float(profile["goal_precision"])


def _threshold_problem(rows: list[dict], target: str, threshold: float | None) -> str:
    if threshold is None:
        return ""
    if not 0 < threshold < 1:
        return "❌ 문턱은 0과 1 사이 숫자로 적어 줘."
    if not all(target in row["question"]["criteria"] for row in rows):
        return f"❌ 대상 라벨 `{target}` 이(가) 선택지에 없는 줄이 있어."
    return ""


def run(test_file: str, models: list[str], engine: Engine | None = None,
        target: str = "", threshold: float | None = None) -> Report:
    """끝까지 잰 모델의 점수는 기록에 쌓는다. threshold 를 주면 그 문턱으로 답을 정해 채점한다."""
    engine = engine or get_engine()
    if not models:
        return Report(status="❌ 모델을 하나 이상 골라 줘.")
    path = data_path(test_file or "")
    if not path.is_file():
        return Report(status=f"❌ 파일이 없어: `{path}`")
    try:
        rows = read_rows(path)
    except ValueError as error:
        return Report(status=f"❌ {error}")
    problem = _threshold_problem(rows, target, threshold)
    if problem:
        return Report(status=problem)
    if threshold is not None and len(models) > 1:  # 문턱은 모델마다 정하는 값이라 같이 적용하면 비교가 거짓이 된다
        return Report(status="❌ 문턱은 모델마다 달라. 문턱을 쓸 땐 모델을 하나만 골라 줘.")
    at = datetime.now().isoformat(timespec="seconds")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    goal_recall, goal_precision = goals()
    report, stopped, details = Report(), "", []
    for model in models:
        try:
            predictions = predict(engine, rows, model, target, threshold)
        except EngineUnavailable as error:
            stopped = f"❌ `{model}` 평가 중 멈춤: {error}"
            break
        row = score(model, predictions)
        report.table.append(row)
        details.append(details_markdown(model, predictions, target, goal_recall, goal_precision))
        wrong = wrong_rows(model, predictions)
        report.wrong += wrong[:WRONG_LIMIT]
        if len(wrong) > WRONG_LIMIT:
            details[-1] += f"\n\n틀린 줄 {len(wrong)}개 중 앞 {WRONG_LIMIT}개만 표에 보여 줘."
        record = {"at": at, "test": test_key(test_file), "sha256": digest, "score": row, "labels": label_stats(predictions)}
        if threshold is not None:
            record["threshold"] = {"label": target, "value": threshold}
        append_record(EVALS_FILE, record)
    report.details = "\n\n".join(details)
    applied = "" if threshold is None else f" (문턱 `{target}` ≥ {threshold} 적용)"
    report.status = stopped or (f"✅ `{test_label(test_key(test_file))}` {len(rows)}줄 × 모델 {len(models)}개 평가 끝{applied}. "
                                "정확도↑ · nll↓ 이 좋음.")
    return report


# ---- 문턱 정하기 (보정셋 전용) ----
def calibration_choices() -> list[str]:
    """보정셋 후보: 데이터셋 탭에서 만든 것 (누수 통과·안 바뀐 것만, 최근 것 먼저)."""
    return [shown(folder / CALIBRATION_FILE) for folder in folders() if not ready_problem(folder)]


def trained_on(model: str) -> str:
    """이 모델을 학습한 데이터 폴더 (기록에 없으면 빈 글자). 같은 이름이면 최근 기록."""
    for record in reversed(read_records("trainings.jsonl")):
        if record.get("name") == model:
            return record.get("data_dir", "")
    return ""


def default_calibration(model: str, choices: list[str] | None = None) -> str:
    """기본 보정셋: 모델을 학습한 데이터의 보정셋 (후보에 있으면), 아니면 맨 앞."""
    choices = calibration_choices() if choices is None else choices
    folder = trained_on(model)
    if folder:
        wanted = shown(data_path(folder) / CALIBRATION_FILE)
        if wanted in choices:
            return wanted
    return choices[0] if choices else ""


def sweep(predictions: list[Prediction], target: str, goal_recall: float, goal_precision: float) -> list[list]:
    """문턱별 점수 표 (SWEEP_COLUMNS). 엔진은 다시 안 부르고 받아 둔 확률로 계산한다."""
    table = []
    for threshold in THRESHOLDS:
        applied = [Prediction(p.row, apply_threshold(p.probabilities, target, threshold), p.probabilities, p.confidence, p.ms)
                   for p in predictions]
        recall, precision = target_scores(applied, target)
        accuracy = sum(p.choice == p.label for p in applied) / len(applied)
        sent = sum(p.choice == target for p in applied) / len(applied)
        table.append([threshold, round(accuracy, 3), percent(recall), percent(precision), percent(sent),
                      "✅" if passes(recall, precision, goal_recall, goal_precision) else ""])
    return table


def recommend(table: list[list]) -> float | None:
    """합격한 문턱 중 정확도가 가장 높은 것, 같으면 문턱이 높은 것 (쓸데없이 대상으로 덜 보냄)."""
    passed = [row for row in table if row[5]]
    return max(passed, key=lambda row: (row[1], row[0]))[0] if passed else None


def run_sweep(calibration_file: str, model: str, target: str, engine: Engine | None = None) -> tuple[list[list], str]:
    """(문턱별 표, 안내 글). 시험셋으로 문턱을 고르지 못하게 보정셋 파일만 받는다."""
    engine = engine or get_engine()
    if not model:
        return [], "❌ 모델을 골라 줘."
    path = data_path(calibration_file or "")
    if path.name != CALIBRATION_FILE:
        return [], f"❌ 문턱은 보정셋(`{CALIBRATION_FILE}`)으로만 정해. 시험셋으로 고르면 시험 점수가 부풀어."
    if not path.is_file():
        return [], f"❌ 파일이 없어: `{path}`"
    try:
        rows = read_rows(path)
    except ValueError as error:
        return [], f"❌ {error}"
    if not all(target in row["question"]["criteria"] for row in rows):
        return [], f"❌ 대상 라벨 `{target}` 이(가) 선택지에 없는 줄이 있어."
    try:
        predictions = predict(engine, rows, model)
    except EngineUnavailable as error:
        return [], f"❌ `{model}` 계산 중 멈춤: {error}"
    goal_recall, goal_precision = goals()
    table = sweep(predictions, target, goal_recall, goal_precision)
    best = recommend(table)
    append_record(THRESHOLDS_FILE, {"at": datetime.now().isoformat(timespec="seconds"), "model": model,
                                    "calibration": shown(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                    "target": target, "goal_recall": goal_recall, "goal_precision": goal_precision,
                                    "recommended": best})
    head = (f"### 추천 문턱: **{best}**\n`{target}` 확률이 {best} 이상이면 `{target}`. "
            "'시험셋·모델 바꾸기'의 문턱 칸에 넣어 시험셋을 채점해."
            if best is not None else
            f"### 합격하는 문턱 없음\n`{target}` 재현율 {goal_recall:.0%}·나머지 정밀도 {goal_precision:.0%}를 같이 넘는 문턱이 없어. "
            "학습·데이터 쪽을 더 봐야 해.")
    count = sum(row["label"] == target for row in rows)
    notes = [f"보정셋 {len(rows)}줄, `{target}` {count}줄" + (f" (한 줄 차이가 재현율 {1 / count:.1%}p)." if count else ".")]
    if sum(bool(row[5]) for row in table) >= EASY_PASS_SHARE * len(table):
        notes.append("⚠️ 거의 모든 문턱이 합격이야. 이 보정셋이 모델에게 너무 쉬워서 문턱을 가려내지 못해. "
                     "이 추천은 믿지 말고, 실제 데이터에 더 가까운 보정셋으로 다시 봐.")
    folder = trained_on(model)
    if folder and data_path(folder) != path.parent:
        notes.append(f"⚠️ 이 모델은 `{Path(folder).name}` 로 학습했는데 다른 데이터의 보정셋을 골랐어. 학습에 쓴 줄이 섞였을 수 있어.")
    if count < SMALL_TEST:
        notes.append(f"⚠️ `{target}` 줄이 {SMALL_TEST}개보다 적어서 문턱이 우연에 크게 흔들려.")
    return table, head + "\n\n" + "\n\n".join(notes)
