from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from indicators import Candle


def data_directory() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return root / "BitunixDayTraderV1"
    return Path.home() / ".local" / "share" / "bitunix-daytrader-v1"


@contextmanager
def single_instance(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    f = (directory / "daytrader.lock").open("a+b")
    locked = False
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another Bitunix DayTrader v1 window is already running.") from None
        locked = True
        yield
    finally:
        if locked:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


class DayTraderStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_bars (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                time_ms INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                base_volume REAL NOT NULL,
                quote_volume REAL NOT NULL,
                PRIMARY KEY(symbol, interval, time_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_market_bars_time ON market_bars(interval, time_ms);

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_utc TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                spread_pct REAL NOT NULL,
                btc_regime TEXT NOT NULL,
                breadth_1h REAL NOT NULL,
                breadth_4h REAL NOT NULL,
                rule_long_score REAL NOT NULL,
                rule_short_score REAL NOT NULL,
                best_direction TEXT,
                best_setup TEXT,
                features_json TEXT NOT NULL,
                ai_json TEXT,
                UNIQUE(symbol, candle_time_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(candle_time_ms);

            CREATE TABLE IF NOT EXISTS learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                stop_pct REAL NOT NULL,
                target_r REAL NOT NULL,
                rule_score REAL NOT NULL,
                setup_type TEXT,
                features_json TEXT NOT NULL,
                label_status TEXT NOT NULL DEFAULT 'PENDING',
                target_hit INTEGER,
                stop_hit INTEGER,
                first_hit TEXT,
                resolved_utc TEXT,
                mfe_r REAL,
                mae_r REAL,
                terminal_r REAL,
                outcome_r REAL,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id),
                UNIQUE(snapshot_id, direction)
            );
            CREATE INDEX IF NOT EXISTS idx_learning_pending ON learning_examples(label_status, candle_time_ms);
            CREATE INDEX IF NOT EXISTS idx_learning_resolved ON learning_examples(label_status, candle_time_ms);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_utc TEXT NOT NULL,
                closed_utc TEXT,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                initial_stop_price REAL NOT NULL,
                reference_target_price REAL NOT NULL,
                partial_target_price REAL NOT NULL,
                qty REAL NOT NULL,
                initial_qty REAL NOT NULL,
                remaining_fraction REAL NOT NULL DEFAULT 1.0,
                leverage INTEGER NOT NULL,
                margin_usdt REAL NOT NULL,
                rule_score REAL NOT NULL,
                setup_type TEXT NOT NULL,
                market_regime TEXT NOT NULL,
                features_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                best_price REAL NOT NULL,
                mfe_r REAL NOT NULL DEFAULT 0,
                mae_r REAL NOT NULL DEFAULT 0,
                partial_realized_gross REAL NOT NULL DEFAULT 0,
                partial_exit_price REAL,
                partial_exit_utc TEXT,
                gross_pnl REAL,
                fee REAL,
                slippage REAL,
                net_pnl REAL,
                close_price REAL,
                close_reason TEXT,
                trade_type TEXT NOT NULL DEFAULT 'DAY',
                agent_name TEXT NOT NULL DEFAULT 'DAY_AGENT',
                notional_usdt REAL,
                max_hold_minutes INTEGER,
                partial_take_profit_r REAL,
                partial_fraction REAL,
                runner_lock_r REAL,
                strategy_version TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_utc);

            CREATE TABLE IF NOT EXISTS swing_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_utc TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                spread_pct REAL NOT NULL,
                market_regime TEXT NOT NULL,
                rule_long_score REAL NOT NULL,
                rule_short_score REAL NOT NULL,
                best_direction TEXT,
                best_setup TEXT,
                features_json TEXT NOT NULL,
                ai_json TEXT,
                UNIQUE(symbol, candle_time_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_swing_snapshots_time ON swing_snapshots(candle_time_ms);

            CREATE TABLE IF NOT EXISTS swing_learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                stop_pct REAL NOT NULL,
                target_r REAL NOT NULL,
                rule_score REAL NOT NULL,
                setup_type TEXT,
                features_json TEXT NOT NULL,
                label_status TEXT NOT NULL DEFAULT 'PENDING',
                target_hit INTEGER,
                stop_hit INTEGER,
                first_hit TEXT,
                resolved_utc TEXT,
                mfe_r REAL,
                mae_r REAL,
                terminal_r REAL,
                outcome_r REAL,
                FOREIGN KEY(snapshot_id) REFERENCES swing_snapshots(id),
                UNIQUE(snapshot_id, direction)
            );
            CREATE INDEX IF NOT EXISTS idx_swing_learning_pending ON swing_learning_examples(label_status, candle_time_ms);
            CREATE INDEX IF NOT EXISTS idx_swing_learning_resolved ON swing_learning_examples(label_status, candle_time_ms);

            CREATE TABLE IF NOT EXISTS swing_training_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time_utc TEXT NOT NULL,
                run_time_local TEXT NOT NULL,
                model_name TEXT NOT NULL,
                train_n INTEGER NOT NULL,
                test_n INTEGER NOT NULL,
                positive_rate REAL,
                accuracy REAL,
                auc REAL,
                train_auc REAL,
                brier REAL,
                log_loss REAL,
                top20_precision REAL,
                top20_lift REAL,
                top20_mean_r REAL,
                research_score REAL,
                artifact_path TEXT,
                params_json TEXT NOT NULL,
                report_path TEXT,
                selected_shadow INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_swing_training_runs_time ON swing_training_runs(run_time_utc);

            CREATE TABLE IF NOT EXISTS training_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time_utc TEXT NOT NULL,
                run_time_local TEXT NOT NULL,
                model_name TEXT NOT NULL,
                train_n INTEGER NOT NULL,
                test_n INTEGER NOT NULL,
                positive_rate REAL,
                accuracy REAL,
                auc REAL,
                train_auc REAL,
                brier REAL,
                log_loss REAL,
                top20_precision REAL,
                top20_lift REAL,
                top20_mean_r REAL,
                research_score REAL,
                artifact_path TEXT,
                params_json TEXT NOT NULL,
                report_path TEXT,
                selected_shadow INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_training_runs_time ON training_runs(run_time_utc);

            CREATE TABLE IF NOT EXISTS wallet_accounts (
                account TEXT PRIMARY KEY,
                balance REAL NOT NULL,
                updated_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_utc TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT,
                destination TEXT,
                amount REAL NOT NULL,
                reference TEXT UNIQUE,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_transactions_time ON wallet_transactions(time_utc);
            """
        )
        # Migrate databases created by v1.0-v1.2 without losing existing positions/history.
        self._ensure_column("trades", "trade_type", "TEXT NOT NULL DEFAULT 'DAY'")
        self._ensure_column("trades", "agent_name", "TEXT NOT NULL DEFAULT 'DAY_AGENT'")
        self._ensure_column("trades", "notional_usdt", "REAL")
        self._ensure_column("trades", "max_hold_minutes", "INTEGER")
        self._ensure_column("trades", "partial_take_profit_r", "REAL")
        self._ensure_column("trades", "partial_fraction", "REAL")
        self._ensure_column("trades", "runner_lock_r", "REAL")
        self._ensure_column("trades", "strategy_version", "TEXT")
        self._ensure_column("trades", "manual_exit_type", "TEXT")
        self._ensure_column("trades", "manual_exit_limit_price", "REAL")
        self._ensure_column("trades", "manual_exit_requested_utc", "TEXT")
        self.db.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        cols = {str(r[1]) for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        self.db.close()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def upsert_bars(self, symbol: str, interval: str, candles: Iterable[Candle]) -> int:
        rows = [
            (symbol, interval, int(c.time_ms), c.open, c.high, c.low, c.close, c.base_volume, c.quote_volume)
            for c in candles if int(c.time_ms) > 0
        ]
        if not rows:
            return 0
        with self.db:
            self.db.executemany(
                """INSERT INTO market_bars(symbol,interval,time_ms,open,high,low,close,base_volume,quote_volume)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,interval,time_ms) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                base_volume=excluded.base_volume,quote_volume=excluded.quote_volume""",
                rows,
            )
        return len(rows)

    def export_swing_examples_csv(self, path: Path) -> int:
        import csv
        rows = self.db.execute("SELECT * FROM swing_learning_examples ORDER BY id").fetchall()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "snapshot_id", "symbol", "direction", "created_utc", "candle_time_ms", "entry_price",
                "stop_pct", "target_r", "rule_score", "setup_type", "label_status", "target_hit", "stop_hit",
                "first_hit", "resolved_utc", "mfe_r", "mae_r", "terminal_r", "outcome_r", "features_json",
            ])
            for r in rows:
                writer.writerow([
                    r["id"], r["snapshot_id"], r["symbol"], r["direction"], r["created_utc"], r["candle_time_ms"],
                    r["entry_price"], r["stop_pct"], r["target_r"], r["rule_score"], r["setup_type"],
                    r["label_status"], r["target_hit"], r["stop_hit"], r["first_hit"], r["resolved_utc"],
                    r["mfe_r"], r["mae_r"], r["terminal_r"], r["outcome_r"], r["features_json"],
                ])
        return len(rows)

    def bars_between(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM market_bars WHERE symbol=? AND interval=? AND time_ms>? AND time_ms<=?
            ORDER BY time_ms""",
            (symbol, interval, int(start_ms), int(end_ms)),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_snapshot(self, *, candle_time_ms: int, symbol: str, price: float, spread_pct: float,
                        btc_regime: str, breadth_1h: float, breadth_4h: float,
                        rule_long_score: float, rule_short_score: float,
                        best_direction: str | None, best_setup: str | None,
                        features: dict[str, Any], ai: dict[str, Any] | None = None) -> int:
        time_utc = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute(
                """INSERT INTO snapshots(time_utc,candle_time_ms,symbol,price,spread_pct,btc_regime,
                breadth_1h,breadth_4h,rule_long_score,rule_short_score,best_direction,best_setup,features_json,ai_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,candle_time_ms) DO UPDATE SET
                price=excluded.price,spread_pct=excluded.spread_pct,btc_regime=excluded.btc_regime,
                breadth_1h=excluded.breadth_1h,breadth_4h=excluded.breadth_4h,
                rule_long_score=excluded.rule_long_score,rule_short_score=excluded.rule_short_score,
                best_direction=excluded.best_direction,best_setup=excluded.best_setup,
                features_json=excluded.features_json,ai_json=excluded.ai_json""",
                (
                    time_utc, int(candle_time_ms), symbol, float(price), float(spread_pct), btc_regime,
                    float(breadth_1h), float(breadth_4h), float(rule_long_score), float(rule_short_score),
                    best_direction, best_setup, json.dumps(features, sort_keys=True),
                    json.dumps(ai, sort_keys=True) if ai else None,
                ),
            )
            row = self.db.execute(
                "SELECT id FROM snapshots WHERE symbol=? AND candle_time_ms=?",
                (symbol, int(candle_time_ms)),
            ).fetchone()
        return int(row[0])

    def update_snapshot_ai(self, snapshot_id: int, ai: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("UPDATE snapshots SET ai_json=? WHERE id=?", (json.dumps(ai, sort_keys=True), snapshot_id))

    def insert_learning_example(self, *, snapshot_id: int, symbol: str, direction: str,
                                candle_time_ms: int, entry_price: float, stop_pct: float,
                                target_r: float, rule_score: float, setup_type: str | None,
                                features: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO learning_examples(
                snapshot_id,symbol,direction,created_utc,candle_time_ms,entry_price,stop_pct,target_r,
                rule_score,setup_type,features_json,label_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
                (
                    snapshot_id, symbol, direction, datetime.now(timezone.utc).isoformat(), int(candle_time_ms),
                    float(entry_price), float(stop_pct), float(target_r), float(rule_score), setup_type,
                    json.dumps(features, sort_keys=True),
                ),
            )

    def pending_learning_examples(self, older_than_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM learning_examples WHERE label_status='PENDING' AND candle_time_ms<=?
            ORDER BY candle_time_ms,id LIMIT ?""",
            (int(older_than_ms), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_learning_example(self, example_id: int, *, target_hit: bool, stop_hit: bool,
                                 first_hit: str, mfe_r: float, mae_r: float,
                                 terminal_r: float, outcome_r: float) -> None:
        with self.db:
            self.db.execute(
                """UPDATE learning_examples SET label_status='RESOLVED',target_hit=?,stop_hit=?,first_hit=?,
                resolved_utc=?,mfe_r=?,mae_r=?,terminal_r=?,outcome_r=? WHERE id=?""",
                (
                    int(bool(target_hit)), int(bool(stop_hit)), first_hit,
                    datetime.now(timezone.utc).isoformat(), float(mfe_r), float(mae_r),
                    float(terminal_r), float(outcome_r), int(example_id),
                ),
            )

    def labeled_examples(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM learning_examples WHERE label_status='RESOLVED' ORDER BY candle_time_ms,id"
        ).fetchall()
        return [dict(r) for r in rows]

    def learning_counts(self) -> dict[str, int]:
        row = self.db.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN label_status='PENDING' THEN 1 ELSE 0 END) pending,
            SUM(CASE WHEN label_status='RESOLVED' THEN 1 ELSE 0 END) resolved
            FROM learning_examples"""
        ).fetchone()
        return {k: int(row[k] or 0) for k in ("total", "pending", "resolved")}

    def insert_trade(self, record: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO trades(
                client_id,symbol,direction,status,opened_utc,entry_price,stop_price,initial_stop_price,
                reference_target_price,partial_target_price,qty,initial_qty,remaining_fraction,leverage,
                margin_usdt,rule_score,setup_type,market_regime,features_json,reasons_json,best_price,
                trade_type,agent_name,notional_usdt,max_hold_minutes,partial_take_profit_r,partial_fraction,runner_lock_r,strategy_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["client_id"], record["symbol"], record["direction"], record.get("status", "PAPER_OPEN"),
                    record["opened_utc"], record["entry_price"], record["stop_price"], record["initial_stop_price"],
                    record["reference_target_price"], record["partial_target_price"], record["qty"], record["initial_qty"],
                    record.get("remaining_fraction", 1.0), record["leverage"], record["margin_usdt"], record["rule_score"],
                    record["setup_type"], record["market_regime"], json.dumps(record.get("features", {}), sort_keys=True),
                    json.dumps(record.get("reasons", []), sort_keys=True), record.get("best_price", record["entry_price"]),
                    record.get("trade_type", "DAY"), record.get("agent_name", "DAY_AGENT"), record.get("notional_usdt"),
                    record.get("max_hold_minutes"), record.get("partial_take_profit_r"), record.get("partial_fraction"),
                    record.get("runner_lock_r"), record.get("strategy_version"),
                ),
            )

    def update_trade(self, client_id: str, **fields: Any) -> None:
        allowed = {
            "status", "closed_utc", "stop_price", "remaining_fraction", "best_price", "mfe_r", "mae_r",
            "partial_realized_gross", "partial_exit_price", "partial_exit_utc", "gross_pnl", "fee", "slippage",
            "net_pnl", "close_price", "close_reason", "notional_usdt", "max_hold_minutes",
            "partial_take_profit_r", "partial_fraction", "runner_lock_r", "strategy_version",
            "manual_exit_type", "manual_exit_limit_price", "manual_exit_requested_utc",
        }
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        sql = "UPDATE trades SET " + ",".join(f"{k}=?" for k, _ in items) + " WHERE client_id=?"
        values = [v for _, v in items] + [client_id]
        with self.db:
            self.db.execute(sql, values)

    def ensure_wallet(self, *, day_initial: float = 2500.0, swing_initial: float = 5000.0,
                      reserve_initial: float = 5000.0, copy_initial: float = 3000.0,
                      legacy_flex_target: str = "DAY") -> dict[str, float]:
        """Create/migrate paper wallets. v1.8 adds a capped COPY paper allocation."""
        rows = self.db.execute("SELECT account,balance FROM wallet_accounts").fetchall()
        if rows:
            balances = {str(r[0]).upper(): float(r[1]) for r in rows}
            if "COPY" not in balances:
                now = datetime.now(timezone.utc).isoformat()
                with self.db:
                    self.db.execute("INSERT INTO wallet_accounts(account,balance,updated_utc) VALUES(?,?,?)", ("COPY", float(copy_initial), now))
                    self.db.execute(
                        "INSERT OR IGNORE INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                        (now, "INITIALIZE", None, "COPY", float(copy_initial), "INIT:COPY", "v1.8 COPY paper wallet initialization"),
                    )
                balances["COPY"] = float(copy_initial)
            return balances
        target = str(legacy_flex_target or "DAY").upper()
        base_day=float(day_initial); base_swing=float(swing_initial); base_total=base_day+base_swing
        used=self.wallet_used_margin()
        day_used=float(used.get("DAY",0.0)); swing_used=float(used.get("SWING",0.0))
        if target == "SWING":
            day_alloc=min(base_day, max(0.0, day_used))
            swing_alloc=max(swing_used, base_total-day_alloc)
            day_alloc=max(day_used, base_total-swing_alloc)
        else:
            swing_alloc=max(base_swing, swing_used)
            day_alloc=max(day_used, base_total-swing_alloc)
            if day_alloc+swing_alloc < base_total:
                day_alloc=base_total-swing_alloc
        now = datetime.now(timezone.utc).isoformat()
        initial = {"DAY": float(day_alloc), "SWING": float(swing_alloc), "COPY": float(copy_initial), "RESERVE": float(reserve_initial)}
        with self.db:
            for account, balance in initial.items():
                self.db.execute("INSERT INTO wallet_accounts(account,balance,updated_utc) VALUES(?,?,?)", (account, balance, now))
                self.db.execute(
                    "INSERT OR IGNORE INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                    (now, "INITIALIZE", None, account, balance, f"INIT:{account}", "v1.8 wallet initialization"),
                )
            self.db.execute("INSERT INTO meta(key,value) VALUES('wallet_initialized_utc',?) ON CONFLICT(key) DO NOTHING", (now,))
        return initial

    def retire_day_wallet_to_reserve(self) -> float:
        """Move only free legacy DAY allocation to RESERVE. Safe to call repeatedly.

        v1.8 no longer opens DAY trades. If an old DAY paper position is still open,
        its used allocation stays in DAY until that position closes; a later call moves
        the newly released remainder to RESERVE. No position is force-closed.
        """
        balances = self.wallet_balances()
        if "DAY" not in balances or "RESERVE" not in balances:
            return 0.0
        used = self.wallet_used_margin()
        free = max(0.0, float(balances.get("DAY", 0.0)) - float(used.get("DAY", 0.0)))
        if free <= 1e-9:
            return 0.0
        now = datetime.now(timezone.utc).isoformat()
        ref = f"RETIRE_DAY:{now}"
        with self.db:
            self.db.execute("UPDATE wallet_accounts SET balance=balance-?,updated_utc=? WHERE account='DAY'", (free, now))
            self.db.execute("UPDATE wallet_accounts SET balance=balance+?,updated_utc=? WHERE account='RESERVE'", (free, now))
            self.db.execute(
                "INSERT INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                (now, "MIGRATE", "DAY", "RESERVE", free, ref, "v1.8 DAY retirement: free allocation moved to RESERVE"),
            )
        return free

    def wallet_balances(self) -> dict[str, float]:
        rows = self.db.execute("SELECT account,balance FROM wallet_accounts").fetchall()
        return {str(r[0]).upper(): float(r[1]) for r in rows}

    def wallet_used_margin(self) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT trade_type,COALESCE(SUM(margin_usdt),0) FROM trades WHERE status='PAPER_OPEN' GROUP BY trade_type"
        ).fetchall()
        out = {"DAY": 0.0, "SWING": 0.0, "COPY": 0.0}
        for r in rows:
            t = str(r[0] or "DAY").upper()
            if t in out:
                out[t] = float(r[1] or 0.0)
        return out

    def wallet_state(self) -> dict[str, float]:
        bal = self.wallet_balances(); used = self.wallet_used_margin()
        day = float(bal.get("DAY", 0.0)); swing = float(bal.get("SWING", 0.0)); copy = float(bal.get("COPY", 0.0)); reserve = float(bal.get("RESERVE", 0.0))
        return {
            "day_balance": day, "swing_balance": swing, "copy_balance": copy, "reserve_balance": reserve,
            "day_used": float(used.get("DAY", 0.0)), "swing_used": float(used.get("SWING", 0.0)), "copy_used": float(used.get("COPY", 0.0)),
            "day_free": max(0.0, day - float(used.get("DAY", 0.0))),
            "swing_free": max(0.0, swing - float(used.get("SWING", 0.0))),
            "copy_free": max(0.0, copy - float(used.get("COPY", 0.0))),
            "allocated_total": day + swing + copy, "wallet_total": day + swing + copy + reserve,
        }

    def transfer_wallet(self, source: str, destination: str, amount: float, note: str = "HMI transfer", copy_max_balance: float = 3000.0) -> dict[str, float]:
        source = str(source or "").upper(); destination = str(destination or "").upper(); amount = float(amount)
        allowed = {"DAY", "SWING", "COPY", "RESERVE"}
        if source not in allowed or destination not in allowed or source == destination:
            raise ValueError("Wallet transfer must be between DAY, SWING, COPY and RESERVE.")
        if amount <= 0:
            raise ValueError("Transfer amount must be greater than zero.")
        state = self.wallet_state(); balances = self.wallet_balances()
        available = float(balances.get(source, 0.0))
        if source == "DAY": available = float(state["day_free"])
        elif source == "SWING": available = float(state["swing_free"])
        elif source == "COPY": available = float(state["copy_free"])
        if destination == "COPY":
            room = max(0.0, float(copy_max_balance) - float(balances.get("COPY", 0.0)))
            if amount > room + 1e-9:
                raise ValueError(f"COPY allocation is capped at {float(copy_max_balance):.2f} USDT; only {room:.2f} USDT can be added.")
        if amount > available + 1e-9:
            raise ValueError(f"Only {available:.2f} USDT is currently transferable from {source}.")
        now = datetime.now(timezone.utc).isoformat(); ref = f"TRANSFER:{now}:{source}:{destination}"
        with self.db:
            self.db.execute("UPDATE wallet_accounts SET balance=balance-?,updated_utc=? WHERE account=?", (amount, now, source))
            self.db.execute("UPDATE wallet_accounts SET balance=balance+?,updated_utc=? WHERE account=?", (amount, now, destination))
            self.db.execute(
                "INSERT INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                (now, "TRANSFER", source, destination, amount, ref, note),
            )
        return self.wallet_state()

    def active_trade(self, client_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM trades WHERE client_id=? AND status='PAPER_OPEN' LIMIT 1", (str(client_id),)
        ).fetchone()
        return dict(row) if row else None

    def set_manual_exit(self, client_id: str, exit_type: str, limit_price: float | None = None) -> None:
        exit_type = str(exit_type or "").upper()
        if exit_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Manual exit type must be MARKET or LIMIT.")
        if exit_type == "LIMIT":
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("A positive limit price is required.")
            price = float(limit_price)
        else:
            price = None
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            cur = self.db.execute(
                "UPDATE trades SET manual_exit_type=?,manual_exit_limit_price=?,manual_exit_requested_utc=? "
                "WHERE client_id=? AND status='PAPER_OPEN'",
                (exit_type, price, now, str(client_id)),
            )
        if cur.rowcount != 1:
            raise ValueError("The selected trade is no longer open.")

    def pending_fixed_transfer(self) -> dict[str, Any] | None:
        raw = self.meta_get("fixed_2500_transfer")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            return None

    def request_fixed_transfer(self, source: str, destination: str, amount: float = 2500.0, source_floor: float = 0.0) -> dict[str, Any]:
        source = str(source or "").upper(); destination = str(destination or "").upper(); amount = float(amount)
        if {source, destination} != {"DAY", "SWING"}:
            raise ValueError("The fixed transfer is only available between DAY and SWING.")
        if amount <= 0:
            raise ValueError("Transfer amount must be positive.")
        source_floor=max(0.0,float(source_floor))
        balance=float(self.wallet_balances().get(source,0.0))
        if balance-source_floor+1e-9 < amount:
            raise ValueError(f"{source} must keep {source_floor:,.2f} USDT and does not currently have the full {amount:,.2f} USDT flexible allocation to move.")
        existing = self.pending_fixed_transfer()
        if existing and float(existing.get("remaining") or 0) > 1e-9:
            raise ValueError(
                f"A {existing.get('source')} -> {existing.get('destination')} transfer is already pending "
                f"({float(existing.get('remaining') or 0):,.2f} USDT remaining)."
            )
        req = {
            "source": source, "destination": destination, "requested": amount,
            "transferred": 0.0, "remaining": amount,
            "source_floor": source_floor,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.meta_set("fixed_2500_transfer", json.dumps(req, sort_keys=True))
        return self.process_fixed_transfer() or req

    def process_fixed_transfer(self) -> dict[str, Any] | None:
        req = self.pending_fixed_transfer()
        if not req:
            return None
        remaining = max(0.0, float(req.get("remaining") or 0.0))
        if remaining <= 1e-9:
            return req
        source = str(req.get("source") or "").upper(); destination = str(req.get("destination") or "").upper()
        state = self.wallet_state()
        free = float(state["day_free"] if source == "DAY" else state["swing_free"])
        floor=max(0.0,float(req.get("source_floor") or 0.0))
        balance=float(state["day_balance"] if source=="DAY" else state["swing_balance"])
        transferable=max(0.0,min(free,balance-floor))
        chunk = min(remaining, transferable)
        if chunk > 1e-9:
            self.transfer_wallet(source, destination, chunk, note="Fixed 2,500 DAY/SWING transfer")
            req["transferred"] = float(req.get("transferred") or 0.0) + chunk
            req["remaining"] = max(0.0, remaining - chunk)
            req["updated_utc"] = datetime.now(timezone.utc).isoformat()
            self.meta_set("fixed_2500_transfer", json.dumps(req, sort_keys=True))
        return req

    def settle_trade_to_reserve(self, client_id: str, net_pnl: float) -> bool:
        """Credit/debit final realized net P/L to RESERVE exactly once per fully closed trade."""
        ref = f"TRADE_PNL:{client_id}"; now = datetime.now(timezone.utc).isoformat(); amount = float(net_pnl)
        with self.db:
            exists = self.db.execute("SELECT 1 FROM wallet_transactions WHERE reference=?", (ref,)).fetchone()
            if exists:
                return False
            self.db.execute("UPDATE wallet_accounts SET balance=balance+?,updated_utc=? WHERE account='RESERVE'", (amount, now))
            self.db.execute(
                "INSERT INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                (now, "TRADE_PNL", "TRADE", "RESERVE", amount, ref, "Realized net P/L settlement"),
            )
        return True

    def wallet_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM wallet_transactions ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def active_trades(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM trades WHERE status='PAPER_OPEN' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def active_for_symbol(self, symbol: str) -> bool:
        row = self.db.execute("SELECT 1 FROM trades WHERE status='PAPER_OPEN' AND symbol=? LIMIT 1", (symbol,)).fetchone()
        return row is not None

    def active_by_type(self, trade_type: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE status='PAPER_OPEN' AND trade_type=? ORDER BY id", (trade_type.upper(),)
        ).fetchall()
        return [dict(r) for r in rows]

    def opened_since_by_type(self, since_utc: str, trade_type: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM trades WHERE opened_utc>=? AND trade_type=?", (since_utc, trade_type.upper())
        ).fetchone()
        return int(row[0])

    def closed_since_by_type(self, since_utc: str, trade_type: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE closed_utc IS NOT NULL AND closed_utc>=? AND trade_type=? ORDER BY id",
            (since_utc, trade_type.upper()),
        ).fetchall()
        return [dict(r) for r in rows]

    def closed_all_by_type(self, trade_type: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? ORDER BY id", (trade_type.upper(),)
        ).fetchall()
        return [dict(r) for r in rows]

    def opened_all_by_type(self, trade_type: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM trades WHERE trade_type=?", (trade_type.upper(),)).fetchone()
        return int(row[0])

    def last_closed_time(self, symbol: str) -> str | None:
        row = self.db.execute(
            "SELECT closed_utc FROM trades WHERE symbol=? AND closed_utc IS NOT NULL ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return str(row[0]) if row else None

    def closed_since(self, since_utc: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE closed_utc IS NOT NULL AND closed_utc>=? ORDER BY id", (since_utc,)
        ).fetchall()
        return [dict(r) for r in rows]

    def opened_since(self, since_utc: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM trades WHERE opened_utc>=?", (since_utc,)).fetchone()
        return int(row[0])

    def closed_all(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE closed_utc IS NOT NULL ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def opened_all(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM trades").fetchone()
        return int(row[0])

    def consecutive_losses(self) -> int:
        rows = self.db.execute(
            "SELECT net_pnl FROM trades WHERE closed_utc IS NOT NULL ORDER BY id DESC LIMIT 30"
        ).fetchall()
        n = 0
        for row in rows:
            if float(row[0] or 0.0) < 0:
                n += 1
            else:
                break
        return n

    def latest_closed_time(self) -> str | None:
        row = self.db.execute("SELECT closed_utc FROM trades WHERE closed_utc IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return str(row[0]) if row else None

    def consecutive_losses_by_type(self, trade_type: str) -> int:
        rows = self.db.execute(
            "SELECT net_pnl FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? ORDER BY id DESC LIMIT 30",
            (trade_type.upper(),),
        ).fetchall()
        n = 0
        for row in rows:
            if float(row[0] or 0.0) < 0:
                n += 1
            else:
                break
        return n

    def latest_closed_time_by_type(self, trade_type: str) -> str | None:
        row = self.db.execute(
            "SELECT closed_utc FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? ORDER BY id DESC LIMIT 1",
            (trade_type.upper(),),
        ).fetchone()
        return str(row[0]) if row else None

    def consecutive_losses_by_type_direction(self, trade_type: str, direction: str) -> int:
        rows = self.db.execute(
            # Most recent CLOSES first (trades can close in a different order than they opened).
            "SELECT net_pnl FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? AND direction=? ORDER BY closed_utc DESC, id DESC LIMIT 30",
            (trade_type.upper(), direction.upper()),
        ).fetchall()
        n = 0
        for row in rows:
            if float(row[0] or 0.0) < 0:
                n += 1
            else:
                break
        return n

    def latest_closed_time_by_type_direction(self, trade_type: str, direction: str) -> str | None:
        row = self.db.execute(
            "SELECT closed_utc FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? AND direction=? ORDER BY closed_utc DESC, id DESC LIMIT 1",
            (trade_type.upper(), direction.upper()),
        ).fetchone()
        return str(row[0]) if row else None

    def last_closed_time_by_type(self, symbol: str, trade_type: str) -> str | None:
        row = self.db.execute(
            "SELECT closed_utc FROM trades WHERE symbol=? AND trade_type=? AND closed_utc IS NOT NULL ORDER BY id DESC LIMIT 1",
            (symbol, trade_type.upper()),
        ).fetchone()
        return str(row[0]) if row else None

    def insert_swing_snapshot(self, *, candle_time_ms: int, symbol: str, price: float, spread_pct: float,
                              market_regime: str, rule_long_score: float, rule_short_score: float,
                              best_direction: str | None, best_setup: str | None,
                              features: dict[str, Any], ai: dict[str, Any] | None = None) -> int:
        time_utc = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute(
                """INSERT INTO swing_snapshots(time_utc,candle_time_ms,symbol,price,spread_pct,market_regime,
                rule_long_score,rule_short_score,best_direction,best_setup,features_json,ai_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,candle_time_ms) DO UPDATE SET
                price=excluded.price,spread_pct=excluded.spread_pct,market_regime=excluded.market_regime,
                rule_long_score=excluded.rule_long_score,rule_short_score=excluded.rule_short_score,
                best_direction=excluded.best_direction,best_setup=excluded.best_setup,
                features_json=excluded.features_json,ai_json=excluded.ai_json""",
                (time_utc, int(candle_time_ms), symbol, float(price), float(spread_pct), market_regime,
                 float(rule_long_score), float(rule_short_score), best_direction, best_setup,
                 json.dumps(features, sort_keys=True), json.dumps(ai, sort_keys=True) if ai else None),
            )
            row = self.db.execute(
                "SELECT id FROM swing_snapshots WHERE symbol=? AND candle_time_ms=?", (symbol, int(candle_time_ms))
            ).fetchone()
        return int(row[0])

    def insert_swing_learning_example(self, *, snapshot_id: int, symbol: str, direction: str,
                                      candle_time_ms: int, entry_price: float, stop_pct: float,
                                      target_r: float, rule_score: float, setup_type: str | None,
                                      features: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO swing_learning_examples(
                snapshot_id,symbol,direction,created_utc,candle_time_ms,entry_price,stop_pct,target_r,
                rule_score,setup_type,features_json,label_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
                (snapshot_id, symbol, direction, datetime.now(timezone.utc).isoformat(), int(candle_time_ms),
                 float(entry_price), float(stop_pct), float(target_r), float(rule_score), setup_type,
                 json.dumps(features, sort_keys=True)),
            )

    def pending_swing_learning_examples(self, older_than_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM swing_learning_examples WHERE label_status='PENDING' AND candle_time_ms<=?
            ORDER BY candle_time_ms,id LIMIT ?""", (int(older_than_ms), int(limit))
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_swing_learning_example(self, example_id: int, *, target_hit: bool, stop_hit: bool,
                                       first_hit: str, mfe_r: float, mae_r: float,
                                       terminal_r: float, outcome_r: float) -> None:
        with self.db:
            self.db.execute(
                """UPDATE swing_learning_examples SET label_status='RESOLVED',target_hit=?,stop_hit=?,first_hit=?,
                resolved_utc=?,mfe_r=?,mae_r=?,terminal_r=?,outcome_r=? WHERE id=?""",
                (int(bool(target_hit)), int(bool(stop_hit)), first_hit, datetime.now(timezone.utc).isoformat(),
                 float(mfe_r), float(mae_r), float(terminal_r), float(outcome_r), int(example_id)),
            )

    def labeled_swing_examples(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM swing_learning_examples WHERE label_status='RESOLVED' ORDER BY candle_time_ms,id"
        ).fetchall()
        return [dict(r) for r in rows]

    def swing_learning_counts(self) -> dict[str, int]:
        row = self.db.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN label_status='PENDING' THEN 1 ELSE 0 END) pending,
            SUM(CASE WHEN label_status='RESOLVED' THEN 1 ELSE 0 END) resolved
            FROM swing_learning_examples"""
        ).fetchone()
        return {k: int(row[k] or 0) for k in ("total", "pending", "resolved")}

    def insert_swing_training_run(self, record: dict[str, Any]) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO swing_training_runs(
                run_time_utc,run_time_local,model_name,train_n,test_n,positive_rate,accuracy,auc,train_auc,brier,
                log_loss,top20_precision,top20_lift,top20_mean_r,research_score,artifact_path,params_json,report_path,
                selected_shadow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["run_time_utc"], record["run_time_local"], record["model_name"], record["train_n"],
                 record["test_n"], record.get("positive_rate"), record.get("accuracy"), record.get("auc"),
                 record.get("train_auc"), record.get("brier"), record.get("log_loss"),
                 record.get("top20_precision"), record.get("top20_lift"), record.get("top20_mean_r"),
                 record.get("research_score"), record.get("artifact_path"),
                 json.dumps(record.get("params", {}), sort_keys=True), record.get("report_path"),
                 int(bool(record.get("selected_shadow")))),
            )
        return int(cur.lastrowid)

    def insert_training_run(self, record: dict[str, Any]) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO training_runs(
                run_time_utc,run_time_local,model_name,train_n,test_n,positive_rate,accuracy,auc,train_auc,brier,
                log_loss,top20_precision,top20_lift,top20_mean_r,research_score,artifact_path,params_json,report_path,
                selected_shadow)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["run_time_utc"], record["run_time_local"], record["model_name"], record["train_n"],
                    record["test_n"], record.get("positive_rate"), record.get("accuracy"), record.get("auc"),
                    record.get("train_auc"), record.get("brier"), record.get("log_loss"),
                    record.get("top20_precision"), record.get("top20_lift"), record.get("top20_mean_r"),
                    record.get("research_score"), record.get("artifact_path"),
                    json.dumps(record.get("params", {}), sort_keys=True), record.get("report_path"),
                    int(bool(record.get("selected_shadow"))),
                ),
            )
        return int(cur.lastrowid)

    def latest_training_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM training_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def export_examples_csv(self, path: Path) -> int:
        import csv
        rows = self.db.execute("SELECT * FROM learning_examples ORDER BY id").fetchall()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "snapshot_id", "symbol", "direction", "created_utc", "candle_time_ms", "entry_price",
                "stop_pct", "target_r", "rule_score", "setup_type", "label_status", "target_hit", "stop_hit",
                "first_hit", "resolved_utc", "mfe_r", "mae_r", "terminal_r", "outcome_r", "features_json",
            ])
            for r in rows:
                writer.writerow([
                    r["id"], r["snapshot_id"], r["symbol"], r["direction"], r["created_utc"], r["candle_time_ms"],
                    r["entry_price"], r["stop_pct"], r["target_r"], r["rule_score"], r["setup_type"],
                    r["label_status"], r["target_hit"], r["stop_hit"], r["first_hit"], r["resolved_utc"],
                    r["mfe_r"], r["mae_r"], r["terminal_r"], r["outcome_r"], r["features_json"],
                ])
        return len(rows)
