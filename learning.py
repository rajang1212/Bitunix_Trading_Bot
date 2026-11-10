from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from config import AppConfig, LearningConfig
from store import DayTraderStore
from strategy import FEATURE_NAMES


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def resolve_labels(store: DayTraderStore, cfg: LearningConfig, now_ms: int | None = None, limit: int = 5000) -> dict[str, int]:
    """Resolve +2R-before--1R labels from locally persisted 15m bars.

    Both LONG and SHORT theoretical examples are created for every snapshot. If target and
    stop occur within the same 15m candle, the label is conservatively counted as a stop.
    """
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    horizon_ms = cfg.label_horizon_minutes * 60_000
    eligible_ms = now_ms - horizon_ms
    rows = store.pending_learning_examples(eligible_ms, limit=limit)
    resolved = 0
    waiting_for_bars = 0
    for row in rows:
        start = int(row["candle_time_ms"])
        end = start + horizon_ms
        bars = store.bars_between(row["symbol"], "15m", start, end)
        if len(bars) < cfg.min_bars_for_label:
            waiting_for_bars += 1
            continue
        entry = float(row["entry_price"])
        stop_pct = max(float(row["stop_pct"]), 1e-9)
        target_r = float(row["target_r"])
        direction = str(row["direction"])
        if direction == "LONG":
            stop_price = entry * (1.0 - stop_pct / 100.0)
            target_price = entry * (1.0 + target_r * stop_pct / 100.0)
        else:
            stop_price = entry * (1.0 + stop_pct / 100.0)
            target_price = entry * (1.0 - target_r * stop_pct / 100.0)

        target_hit = False
        stop_hit = False
        first_hit = "TIMEOUT"
        mfe_r = 0.0
        mae_r = 0.0
        for b in bars:
            high = float(b["high"])
            low = float(b["low"])
            if direction == "LONG":
                favorable = (high - entry) / entry * 100.0 / stop_pct
                adverse = (entry - low) / entry * 100.0 / stop_pct
                hit_t = high >= target_price
                hit_s = low <= stop_price
            else:
                favorable = (entry - low) / entry * 100.0 / stop_pct
                adverse = (high - entry) / entry * 100.0 / stop_pct
                hit_t = low <= target_price
                hit_s = high >= stop_price
            mfe_r = max(mfe_r, favorable)
            mae_r = max(mae_r, adverse)
            if hit_t and hit_s:
                # Intrabar ordering is unknowable from 15m OHLC; assume the adverse event first.
                stop_hit = True
                first_hit = "AMBIGUOUS_STOP"
                break
            if hit_s:
                stop_hit = True
                first_hit = "STOP"
                break
            if hit_t:
                target_hit = True
                first_hit = "TARGET"
                break

        last_close = float(bars[-1]["close"])
        directional_pct = ((last_close - entry) / entry * 100.0) * (1.0 if direction == "LONG" else -1.0)
        terminal_r = directional_pct / stop_pct
        if target_hit:
            outcome_r = target_r
        elif stop_hit:
            outcome_r = -1.0
        else:
            outcome_r = _clip(terminal_r, -1.0, target_r)
        store.resolve_learning_example(
            int(row["id"]), target_hit=target_hit, stop_hit=stop_hit, first_hit=first_hit,
            mfe_r=mfe_r, mae_r=mae_r, terminal_r=terminal_r, outcome_r=outcome_r,
        )
        resolved += 1
    return {"eligible": len(rows), "resolved": resolved, "waiting_for_bars": waiting_for_bars}



def _decode_examples(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    X: list[list[float]] = []
    y: list[int] = []
    r: list[float] = []
    times: list[int] = []
    for row in rows:
        try:
            data = json.loads(row["features_json"])
            X.append([float(data.get(k, 0.0) or 0.0) for k in FEATURE_NAMES])
            y.append(1 if int(row.get("target_hit") or 0) == 1 else 0)
            r.append(float(row.get("outcome_r") or 0.0))
            times.append(int(row["candle_time_ms"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    if not X:
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0, dtype=int), np.empty(0), np.empty(0, dtype=np.int64)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int), np.asarray(r, dtype=float), np.asarray(times, dtype=np.int64)



def _purged_split(times: np.ndarray, cfg: LearningConfig) -> tuple[np.ndarray, np.ndarray, int | None]:
    unique = np.unique(times)
    if unique.size < 5:
        return np.zeros(times.size, dtype=bool), np.zeros(times.size, dtype=bool), None
    idx = min(unique.size - 2, max(1, int(unique.size * cfg.train_fraction)))
    cutoff = int(unique[idx])
    purge_ms = cfg.purge_minutes * 60_000
    # One-sided purge: training observations must end their 24h label window before the test period begins.
    # Test begins at the cutoff; there is no need to discard the first 24h of test data itself.
    train_mask = times <= cutoff - purge_ms
    test_mask = times >= cutoff
    return train_mask, test_mask, cutoff



def _research_score(auc: float, train_auc: float, brier: float, positive_rate: float, lift: float) -> float:
    baseline_brier = max(positive_rate * (1.0 - positive_rate), 1e-9)
    auc_component = _clip((auc - 0.50) / 0.25, 0.0, 1.0)
    brier_component = _clip((baseline_brier - brier) / baseline_brier, 0.0, 1.0)
    lift_component = _clip((lift - 1.0) / 1.0, 0.0, 1.0)
    stability_component = _clip(1.0 - abs(train_auc - auc) / 0.15, 0.0, 1.0)
    return 100.0 * (0.35 * auc_component + 0.25 * brier_component + 0.25 * lift_component + 0.15 * stability_component)



def _metrics(model, X_train: np.ndarray, y_train: np.ndarray, X_test: np.ndarray, y_test: np.ndarray,
             r_test: np.ndarray) -> dict[str, float]:
    p = np.asarray(model.predict_proba(X_test))[:, 1]
    p_train = np.asarray(model.predict_proba(X_train))[:, 1]
    pred = (p >= 0.5).astype(int)
    positive_rate = float(np.mean(y_test))
    accuracy = float(accuracy_score(y_test, pred))
    auc = float(roc_auc_score(y_test, p)) if np.unique(y_test).size == 2 else 0.5
    train_auc = float(roc_auc_score(y_train, p_train)) if np.unique(y_train).size == 2 else 0.5
    brier = float(brier_score_loss(y_test, p))
    ll = float(log_loss(y_test, np.column_stack([1.0 - p, p]), labels=[0, 1]))
    n_top = max(1, int(math.ceil(len(p) * 0.20)))
    top_idx = np.argsort(p)[-n_top:]
    top_precision = float(np.mean(y_test[top_idx]))
    top_lift = top_precision / positive_rate if positive_rate > 0 else 0.0
    top_mean_r = float(np.mean(r_test[top_idx]))
    score = _research_score(auc, train_auc, brier, positive_rate, top_lift)
    return {
        "positive_rate": positive_rate,
        "accuracy": accuracy,
        "auc": auc,
        "train_auc": train_auc,
        "brier": brier,
        "log_loss": ll,
        "top20_precision": top_precision,
        "top20_lift": top_lift,
        "top20_mean_r": top_mean_r,
        "research_score": score,
    }



def _models(cfg: LearningConfig):
    logistic = Pipeline([
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=cfg.logistic_c, max_iter=900, class_weight="balanced", random_state=cfg.random_state)),
    ])
    mlp = Pipeline([
        ("scale", StandardScaler()),
        ("model", MLPClassifier(
            hidden_layer_sizes=tuple(int(x) for x in cfg.mlp_hidden_layers),
            alpha=cfg.mlp_alpha,
            max_iter=cfg.mlp_max_iter,
            batch_size=cfg.mlp_batch_size,
            learning_rate_init=cfg.mlp_learning_rate_init,
            early_stopping=cfg.mlp_early_stopping,
            validation_fraction=0.15,
            random_state=cfg.random_state,
        )),
    ])
    hgb = HistGradientBoostingClassifier(
        learning_rate=cfg.hgb_learning_rate,
        max_iter=cfg.hgb_max_iter,
        max_leaf_nodes=cfg.hgb_max_leaf_nodes,
        l2_regularization=1.0,
        random_state=cfg.random_state,
    )
    return {"LOGISTIC": logistic, "NEURAL_MLP": mlp, "HIST_GRADIENT_BOOST": hgb}



def _write_insufficient_report(report_txt: Path, report_json: Path, counts: dict[str, int], split: dict[str, int], message: str) -> None:
    obj = {"status": "COLLECTING_DATA", "message": message, "counts": counts, "split": split}
    report_json.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    report_txt.write_text(
        "BITUNIX DAY AGENT v1.3.1 - DAILY LEARNING REPORT\n"
        "===========================================\n"
        f"Status: COLLECTING DATA\n{message}\n\n"
        f"Examples: {counts.get('total',0)} total | {counts.get('resolved',0)} resolved | {counts.get('pending',0)} pending\n"
        f"Purged split: {split.get('train',0)} train | {split.get('test',0)} test\n\n"
        "No DAY model is allowed to affect trading in v1.3. Data collection and shadow evaluation continue automatically.\n",
        encoding="utf-8",
    )



def _run_daily_learning_unlocked(data_dir: Path, app_cfg: AppConfig, *, force: bool = False) -> dict[str, Any]:
    cfg = app_cfg.learning
    data_dir.mkdir(parents=True, exist_ok=True)
    reports = data_dir / "reports"
    models_dir = data_dir / "models"
    reports.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    local_now = datetime.now().astimezone()
    day = local_now.strftime("%Y-%m-%d")
    report_txt = reports / f"learning-{day}.txt"
    report_json = reports / f"learning-{day}.json"

    store = DayTraderStore(data_dir / "daytrader.sqlite3")
    try:
        last_day = store.meta_get("last_learning_day")
        if last_day == day and not force and report_json.exists():
            return json.loads(report_json.read_text(encoding="utf-8"))
        resolved_info = resolve_labels(store, cfg)
        counts = store.learning_counts()
        rows = store.labeled_examples()
        X, y, outcome_r, times = _decode_examples(rows)
        train_mask, test_mask, cutoff = _purged_split(times, cfg)
        split = {"train": int(np.sum(train_mask)), "test": int(np.sum(test_mask)), "cutoff_ms": int(cutoff or 0)}
        if split["train"] < cfg.min_train_examples or split["test"] < cfg.min_test_examples:
            msg = (
                f"Need at least {cfg.min_train_examples} purged train and {cfg.min_test_examples} purged test examples; "
                f"currently {split['train']} / {split['test']}. Labels mature only after the 24h horizon."
            )
            _write_insufficient_report(report_txt, report_json, counts, split, msg)
            store.meta_set("last_learning_day", day)
            store.meta_set("latest_learning_report", str(report_txt))
            return json.loads(report_json.read_text(encoding="utf-8"))

        X_train, y_train = X[train_mask], y[train_mask]
        X_test, y_test, r_test = X[test_mask], y[test_mask], outcome_r[test_mask]
        if np.unique(y_train).size < 2:
            msg = "Training period contains only one label class; waiting for more varied outcomes."
            _write_insufficient_report(report_txt, report_json, counts, split, msg)
            store.meta_set("last_learning_day", day)
            store.meta_set("latest_learning_report", str(report_txt))
            return json.loads(report_json.read_text(encoding="utf-8"))

        results: list[dict[str, Any]] = []
        fitted: dict[str, Any] = {}
        stamp = local_now.strftime("%Y%m%d-%H%M%S")
        for name, model in _models(cfg).items():
            model.fit(X_train, y_train)
            metrics = _metrics(model, X_train, y_train, X_test, y_test, r_test)
            artifact = models_dir / f"{name.lower()}-{stamp}.joblib"
            joblib.dump({"model": model, "feature_names": FEATURE_NAMES, "trained_at": local_now.isoformat()}, artifact)
            record = {
                "model_name": name,
                "artifact_path": str(artifact),
                "train_n": int(len(y_train)),
                "test_n": int(len(y_test)),
                **metrics,
            }
            results.append(record)
            fitted[name] = model

        results.sort(key=lambda x: (float(x["research_score"]), float(x["auc"]), float(x["top20_mean_r"])), reverse=True)
        best = results[0]
        # The selected model is used only for SHADOW predictions. It never changes rule-based trading in v1.3.
        shadow_pointer = {
            "model_name": best["model_name"], "artifact_path": best["artifact_path"],
            "research_score": best["research_score"], "auc": best["auc"], "selected_at": local_now.isoformat(),
            "trading_authority": "NONE_SHADOW_ONLY",
        }
        (models_dir / "shadow_champion.json").write_text(json.dumps(shadow_pointer, indent=2) + "\n", encoding="utf-8")

        run_utc = datetime.now(timezone.utc).isoformat()
        for row in results:
            row["selected_shadow"] = row["model_name"] == best["model_name"]
            store.insert_training_run({
                "run_time_utc": run_utc, "run_time_local": local_now.isoformat(), "report_path": str(report_txt),
                "params": asdict(cfg), **row,
            })

        payload = {
            "status": "TESTED",
            "run_time_local": local_now.isoformat(),
            "resolved_this_run": resolved_info,
            "counts": counts,
            "split": split,
            "feature_count": len(FEATURE_NAMES),
            "models": results,
            "shadow_champion": shadow_pointer,
            "note": "Research metrics are out-of-sample diagnostics, not a profit guarantee. v1.3.1 keeps DAY ML shadow-only.",
        }
        report_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        lines = [
            "BITUNIX DAY AGENT v1.3.1 - DAILY 21:00 LEARNING REPORT",
            "=================================================",
            f"Local run time: {local_now.isoformat()}",
            f"Dataset: {counts['resolved']} resolved | {counts['pending']} pending | {len(FEATURE_NAMES)} features",
            f"Purged chronological test: {len(y_train)} train | {len(y_test)} test | 24h purge",
            "",
            "MODEL TEST RESULTS (UNSEEN / LATER DATA)",
            "----------------------------------------",
        ]
        for row in results:
            selected = "  <-- SHADOW CHAMPION" if row["model_name"] == best["model_name"] else ""
            lines.extend([
                f"{row['model_name']}{selected}",
                f"  Research Score : {row['research_score']:.1f}/100",
                f"  Test ROC-AUC   : {row['auc']:.3f} (train {row['train_auc']:.3f})",
                f"  Accuracy       : {row['accuracy']:.3f}",
                f"  Brier score    : {row['brier']:.4f} (lower is better)",
                f"  Top-20% hit %  : {row['top20_precision']*100:.1f}% | lift {row['top20_lift']:.2f}x",
                f"  Top-20% mean R : {row['top20_mean_r']:+.3f}R",
                "",
            ])
        lines.extend([
            "HOW TO READ THE RESEARCH SCORE",
            "------------------------------",
            "It combines out-of-sample discrimination (AUC), calibration improvement, high-confidence lift,",
            "and train/test stability. It is a tracking index, not an IQ score and not proof of profitability.",
            "",
            "AUTHORITY",
            "---------",
            "Machine learning remains SHADOW ONLY. It records probabilities beside rule trades but cannot open,",
            "close, resize, or veto a trade. We can change that later only after reviewing repeated test reports.",
        ])
        report_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        store.meta_set("last_learning_day", day)
        store.meta_set("latest_learning_report", str(report_txt))
        return payload
    finally:
        store.close()


def run_daily_learning(data_dir: Path, app_cfg: AppConfig, *, force: bool = False) -> dict[str, Any]:
    """Serialize daily model training so the in-app 21:00 fallback and Windows task cannot train twice concurrently."""
    import os
    import time
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / "learning-run.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 7200:
            lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"status": "ALREADY_RUNNING", "message": "Another learning cycle is already running."}
    try:
        os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
        os.close(fd)
        return _run_daily_learning_unlocked(data_dir, app_cfg, force=force)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


class ShadowPredictor:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.pointer_path = data_dir / "models" / "shadow_champion.json"
        self._mtime: float | None = None
        self._name: str | None = None
        self._model = None

    def _reload_if_needed(self) -> None:
        if not self.pointer_path.exists():
            self._model = None
            return
        mtime = self.pointer_path.stat().st_mtime
        if self._mtime == mtime and self._model is not None:
            return
        pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        artifact = Path(pointer["artifact_path"])
        if not artifact.exists():
            self._model = None
            return
        bundle = joblib.load(artifact)
        if bundle.get("feature_names") != FEATURE_NAMES:
            self._model = None
            return
        self._model = bundle["model"]
        self._name = str(pointer.get("model_name") or "UNKNOWN")
        self._mtime = mtime

    def predict(self, features: dict[str, float]) -> dict[str, Any] | None:
        self._reload_if_needed()
        if self._model is None:
            return None
        x = np.asarray([[float(features.get(k, 0.0) or 0.0) for k in FEATURE_NAMES]], dtype=float)
        p = float(self._model.predict_proba(x)[0, 1])
        return {"model": self._name, "p_target_before_failure": p, "authority": "SHADOW_ONLY"}
