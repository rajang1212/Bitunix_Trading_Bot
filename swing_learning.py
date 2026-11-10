from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
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

from config import AppConfig, SwingLearningConfig
from store import DayTraderStore
from swing_strategy import SWING_FEATURE_NAMES


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def resolve_swing_labels(store: DayTraderStore, cfg: SwingLearningConfig, now_ms: int | None = None, limit: int = 4000) -> dict[str, int]:
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    horizon_ms = cfg.label_horizon_minutes * 60_000
    rows = store.pending_swing_learning_examples(now_ms - horizon_ms, limit=limit)
    resolved = 0; waiting = 0
    for row in rows:
        start = int(row["candle_time_ms"]); end = start + horizon_ms
        bars = store.bars_between(row["symbol"], "1h", start, end)
        if len(bars) < cfg.min_bars_for_label:
            waiting += 1; continue
        entry = float(row["entry_price"]); stop_pct = max(float(row["stop_pct"]), 1e-9)
        target_r = float(row["target_r"]); direction = str(row["direction"])
        if direction == "LONG":
            stop = entry * (1-stop_pct/100); target = entry * (1+target_r*stop_pct/100)
        else:
            stop = entry * (1+stop_pct/100); target = entry * (1-target_r*stop_pct/100)
        target_hit = stop_hit = False; first_hit = "TIMEOUT"; mfe = mae = 0.0
        for b in bars:
            hi=float(b["high"]); lo=float(b["low"])
            if direction == "LONG":
                favorable=(hi-entry)/entry*100/stop_pct; adverse=(entry-lo)/entry*100/stop_pct
                ht=hi>=target; hs=lo<=stop
            else:
                favorable=(entry-lo)/entry*100/stop_pct; adverse=(hi-entry)/entry*100/stop_pct
                ht=lo<=target; hs=hi>=stop
            mfe=max(mfe,favorable); mae=max(mae,adverse)
            if ht and hs: stop_hit=True; first_hit="AMBIGUOUS_STOP"; break
            if hs: stop_hit=True; first_hit="STOP"; break
            if ht: target_hit=True; first_hit="TARGET"; break
        last=float(bars[-1]["close"])
        directional=((last-entry)/entry*100)*(1 if direction=="LONG" else -1)
        terminal=directional/stop_pct
        outcome=target_r if target_hit else (-1.0 if stop_hit else _clip(terminal,-1.0,target_r))
        store.resolve_swing_learning_example(int(row["id"]), target_hit=target_hit, stop_hit=stop_hit,
                                             first_hit=first_hit, mfe_r=mfe, mae_r=mae,
                                             terminal_r=terminal, outcome_r=outcome)
        resolved += 1
    return {"eligible": len(rows), "resolved": resolved, "waiting_for_bars": waiting}


def _decode(rows: list[dict[str, Any]]):
    X=[]; y=[]; r=[]; times=[]
    for row in rows:
        try:
            d=json.loads(row["features_json"])
            X.append([float(d.get(k,0.0) or 0.0) for k in SWING_FEATURE_NAMES])
            y.append(1 if int(row.get("target_hit") or 0)==1 else 0)
            r.append(float(row.get("outcome_r") or 0.0)); times.append(int(row["candle_time_ms"]))
        except (ValueError,TypeError,json.JSONDecodeError):
            continue
    if not X:
        return np.empty((0,len(SWING_FEATURE_NAMES))),np.empty(0,dtype=int),np.empty(0),np.empty(0,dtype=np.int64)
    return np.asarray(X,float),np.asarray(y,int),np.asarray(r,float),np.asarray(times,np.int64)


def _split(times: np.ndarray, cfg: SwingLearningConfig):
    unique=np.unique(times)
    if unique.size<5:
        return np.zeros(times.size,bool),np.zeros(times.size,bool),None
    idx=min(unique.size-2,max(1,int(unique.size*cfg.train_fraction))); cutoff=int(unique[idx]); purge=cfg.purge_minutes*60_000
    return times<=cutoff-purge, times>=cutoff, cutoff


def _models(cfg: SwingLearningConfig):
    return {
        "LOGISTIC": Pipeline([("scale",StandardScaler()),("model",LogisticRegression(C=cfg.logistic_c,max_iter=900,class_weight="balanced",random_state=cfg.random_state))]),
        "NEURAL_MLP": Pipeline([("scale",StandardScaler()),("model",MLPClassifier(hidden_layer_sizes=tuple(cfg.mlp_hidden_layers),alpha=cfg.mlp_alpha,max_iter=cfg.mlp_max_iter,batch_size=cfg.mlp_batch_size,learning_rate_init=cfg.mlp_learning_rate_init,early_stopping=cfg.mlp_early_stopping,validation_fraction=0.15,random_state=cfg.random_state))]),
        "HIST_GRADIENT_BOOST": HistGradientBoostingClassifier(learning_rate=cfg.hgb_learning_rate,max_iter=cfg.hgb_max_iter,max_leaf_nodes=cfg.hgb_max_leaf_nodes,l2_regularization=1.0,random_state=cfg.random_state),
    }


def _score(auc,train_auc,brier,rate,lift):
    baseline=max(rate*(1-rate),1e-9)
    return 100*(.35*_clip((auc-.5)/.25,0,1)+.25*_clip((baseline-brier)/baseline,0,1)+.25*_clip((lift-1),0,1)+.15*_clip(1-abs(train_auc-auc)/.15,0,1))


def _metrics(model,Xtr,ytr,Xte,yte,rte):
    p=np.asarray(model.predict_proba(Xte))[:,1]; ptr=np.asarray(model.predict_proba(Xtr))[:,1]
    pred=(p>=.5).astype(int); rate=float(np.mean(yte)); acc=float(accuracy_score(yte,pred))
    auc=float(roc_auc_score(yte,p)) if np.unique(yte).size==2 else .5
    trauc=float(roc_auc_score(ytr,ptr)) if np.unique(ytr).size==2 else .5
    b=float(brier_score_loss(yte,p)); ll=float(log_loss(yte,np.column_stack([1-p,p]),labels=[0,1]))
    n=max(1,int(math.ceil(len(p)*.2))); idx=np.argsort(p)[-n:]; prec=float(np.mean(yte[idx])); lift=prec/rate if rate>0 else 0; meanr=float(np.mean(rte[idx]))
    return {"positive_rate":rate,"accuracy":acc,"auc":auc,"train_auc":trauc,"brier":b,"log_loss":ll,"top20_precision":prec,"top20_lift":lift,"top20_mean_r":meanr,"research_score":_score(auc,trauc,b,rate,lift)}


def _run_swing_daily_learning_unlocked(data_dir: Path, app_cfg: AppConfig, *, force: bool=False) -> dict[str,Any]:
    cfg=app_cfg.swing_learning; reports=data_dir/"reports"; models=data_dir/"models"/"swing"; reports.mkdir(parents=True,exist_ok=True); models.mkdir(parents=True,exist_ok=True)
    now=datetime.now().astimezone(); day=now.strftime("%Y-%m-%d"); txt=reports/f"swing-learning-{day}.txt"; js=reports/f"swing-learning-{day}.json"
    store=DayTraderStore(data_dir/"daytrader.sqlite3")
    try:
        if store.meta_get("last_swing_learning_day")==day and not force and js.exists():
            return json.loads(js.read_text(encoding="utf-8"))
        resolved=resolve_swing_labels(store,cfg); counts=store.swing_learning_counts(); rows=store.labeled_swing_examples(); X,y,r,times=_decode(rows); tr,te,cut=_split(times,cfg)
        split={"train":int(np.sum(tr)),"test":int(np.sum(te)),"cutoff_ms":int(cut or 0)}
        if split["train"]<cfg.min_train_examples or split["test"]<cfg.min_test_examples or (split["train"] and np.unique(y[tr]).size<2):
            msg=f"Need {cfg.min_train_examples} purged train and {cfg.min_test_examples} purged test swing examples; currently {split['train']} / {split['test']}. Swing labels mature after 7 days."
            payload={"status":"COLLECTING_DATA","message":msg,"counts":counts,"split":split}
            js.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8"); txt.write_text("BITUNIX SWING AGENT - DAILY LEARNING REPORT\n===========================================\nStatus: COLLECTING DATA\n"+msg+f"\n\nExamples: {counts['resolved']} resolved | {counts['pending']} pending\nML authority: SHADOW ONLY.\n",encoding="utf-8")
            store.meta_set("last_swing_learning_day",day); store.meta_set("latest_swing_learning_report",str(txt)); return payload
        Xtr,ytr=X[tr],y[tr]; Xte,yte,rte=X[te],y[te],r[te]; results=[]; stamp=now.strftime("%Y%m%d-%H%M%S")
        for name,model in _models(cfg).items():
            model.fit(Xtr,ytr); m=_metrics(model,Xtr,ytr,Xte,yte,rte); art=models/f"{name.lower()}-{stamp}.joblib"; joblib.dump({"model":model,"feature_names":SWING_FEATURE_NAMES,"trained_at":now.isoformat()},art); results.append({"model_name":name,"artifact_path":str(art),"train_n":len(ytr),"test_n":len(yte),**m})
        results.sort(key=lambda z:(z["research_score"],z["auc"],z["top20_mean_r"]),reverse=True); best=results[0]
        pointer={"model_name":best["model_name"],"artifact_path":best["artifact_path"],"research_score":best["research_score"],"auc":best["auc"],"selected_at":now.isoformat(),"trading_authority":"NONE_SHADOW_ONLY"}
        (models/"shadow_champion.json").write_text(json.dumps(pointer,indent=2)+"\n",encoding="utf-8")
        runutc=datetime.now(timezone.utc).isoformat()
        for row in results:
            store.insert_swing_training_run({"run_time_utc":runutc,"run_time_local":now.isoformat(),"report_path":str(txt),"params":asdict(cfg),"selected_shadow":row["model_name"]==best["model_name"],**row})
        payload={"status":"TESTED","counts":counts,"split":split,"feature_count":len(SWING_FEATURE_NAMES),"models":results,"shadow_champion":pointer,"resolved_this_run":resolved}
        js.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
        lines=["BITUNIX SWING AGENT - DAILY 21:00 LEARNING REPORT","=================================================",f"Dataset: {counts['resolved']} resolved | {counts['pending']} pending",f"Purged chronological test: {len(ytr)} train | {len(yte)} test | 7-day purge",""]
        for row in results:
            tag=" <-- SHADOW CHAMPION" if row["model_name"]==best["model_name"] else ""
            lines += [f"{row['model_name']}{tag}",f"  Research Score : {row['research_score']:.1f}/100",f"  Test ROC-AUC   : {row['auc']:.3f} (train {row['train_auc']:.3f})",f"  Top-20% mean R : {row['top20_mean_r']:+.3f}R",""]
        lines += ["ML authority: SHADOW ONLY. It cannot open, close, resize, or veto swing trades."]
        txt.write_text("\n".join(lines)+"\n",encoding="utf-8"); store.meta_set("last_swing_learning_day",day); store.meta_set("latest_swing_learning_report",str(txt)); return payload
    finally:
        store.close()



def run_swing_daily_learning(data_dir: Path, app_cfg: AppConfig, *, force: bool=False) -> dict[str,Any]:
    """Serialize SWING model testing so Task Scheduler and the running app cannot train twice."""
    import os, time
    data_dir.mkdir(parents=True, exist_ok=True)
    lock=data_dir/"swing-learning-run.lock"
    try:
        if lock.exists() and time.time()-lock.stat().st_mtime>7200:
            lock.unlink(missing_ok=True)
        fd=os.open(str(lock),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        return {"status":"ALREADY_RUNNING","message":"Another SWING learning cycle is already running."}
    try:
        os.write(fd,str(os.getpid()).encode("ascii","ignore")); os.close(fd)
        return _run_swing_daily_learning_unlocked(data_dir,app_cfg,force=force)
    finally:
        try: lock.unlink(missing_ok=True)
        except OSError: pass

class SwingShadowPredictor:
    def __init__(self,data_dir:Path):
        self.pointer=data_dir/"models"/"swing"/"shadow_champion.json"; self._mtime=None; self._model=None; self._name=None
    def _reload(self):
        if not self.pointer.exists(): self._model=None; return
        m=self.pointer.stat().st_mtime
        if self._mtime==m and self._model is not None: return
        ptr=json.loads(self.pointer.read_text(encoding="utf-8")); art=Path(ptr["artifact_path"])
        if not art.exists(): self._model=None; return
        bundle=joblib.load(art)
        if bundle.get("feature_names")!=SWING_FEATURE_NAMES: self._model=None; return
        self._model=bundle["model"]; self._name=str(ptr.get("model_name") or "UNKNOWN"); self._mtime=m
    def predict(self,features:dict[str,float]):
        self._reload()
        if self._model is None: return None
        x=np.asarray([[float(features.get(k,0.0) or 0.0) for k in SWING_FEATURE_NAMES]],float); p=float(self._model.predict_proba(x)[0,1])
        return {"model":self._name,"p_target_before_failure":p,"authority":"SHADOW_ONLY"}
