from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from bitunix_api import ApiError, BitunixClient, Credentials
from config import load_config, save_config
from hmi_data import collect_snapshot
from runtime_control import engine_running, launch_engine, read_control, read_engine_state, request_stop, set_pause
from store import DayTraderStore, data_directory


APP_TITLE = "Bitunix Swing + Copy Trader v1.8.3 HMI"
REFRESH_SECONDS = 2.0
MAX_COPY_WALLETS = 3
BG="#0c1117"; PANEL="#131a23"; PANEL2="#17212b"; FG="#e6edf3"; MUTED="#8b949e"; GREEN="#3fb950"; RED="#f85149"; AMBER="#d29922"; BLUE="#58a6ff"; GREY="#8b949e"; BORDER="#30363d"


def _is_solana_address(addr: str) -> bool:
    b58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    addr=addr.strip()
    if not 32<=len(addr)<=44 or any(c not in b58 for c in addr):
        return False
    value = 0
    for c in addr:
        value = value * 58 + b58.index(c)
    leading = len(addr) - len(addr.lstrip("1"))
    return leading + (value.bit_length() + 7) // 8 == 32


def _fmt_money(v: float) -> str:
    return f"{v:+,.2f} USDT"


def _fmt_num(v: float, digits: int = 4) -> str:
    if abs(v)>=1000: return f"{v:,.2f}"
    if abs(v)>=100: return f"{v:.2f}"
    if abs(v)>=1: return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _time(v: Any) -> str:
    if not v: return "—"
    if isinstance(v, datetime): return v.strftime("%H:%M:%S")
    try: return datetime.fromisoformat(str(v)).astimezone().strftime("%H:%M:%S")
    except Exception: return str(v)


class DataWorker(threading.Thread):
    def __init__(self, app_dir: Path, out: queue.Queue):
        super().__init__(daemon=True); self.app_dir=app_dir; self.out=out; self.stop_event=threading.Event(); self.client=BitunixClient(credentials=Credentials("PUBLIC", "PUBLIC")); self.marks:dict[str,float]={}; self.last_market_ok:datetime|None=None; self.cfg=None; self.settings_mtime=None
    def stop(self): self.stop_event.set()
    def run(self):
        d=data_directory(); db=d/"daytrader.sqlite3"
        while not self.stop_event.is_set():
            started=time.monotonic(); error=None
            try:
                settings=d/"settings.json"
                try: mtime=settings.stat().st_mtime if settings.exists() else None
                except OSError: mtime=None
                if self.cfg is None or mtime != self.settings_mtime:
                    self.cfg=load_config(settings); self.settings_mtime=settings.stat().st_mtime if settings.exists() else None
                cfg=self.cfg; store=DayTraderStore(db)
                try:
                    active=store.active_trades(); symbols=[str(x.get("symbol")) for x in active if x.get("symbol")]
                    poll_symbols=symbols or ["BTCUSDT"]
                    try:
                        rows=self.client.tickers_for(poll_symbols)
                        for row in rows:
                            try:
                                sym=str(row.get("symbol") or ""); m=float(row.get("markPrice") or row.get("lastPrice") or row.get("last") or 0)
                                if sym and m>0:self.marks[sym]=m
                            except Exception: pass
                        self.last_market_ok=datetime.now(timezone.utc)
                    except Exception as e:
                        error=f"Market data: {e}"
                    snap=collect_snapshot(store,cfg,d,self.marks)
                finally: store.close()
                snap["engine_running"]=engine_running(); snap["control"]=read_control(); snap["engine_state"]=read_engine_state(); snap["last_market_ok"]=self.last_market_ok; snap["refresh_utc"]=datetime.now(timezone.utc); snap["error"]=error
                while self.out.qsize()>1:
                    try:self.out.get_nowait()
                    except queue.Empty:break
                self.out.put(snap)
            except Exception as e:
                self.out.put({"fatal_error":f"{type(e).__name__}: {e}","refresh_utc":datetime.now(timezone.utc),"engine_running":engine_running(),"control":read_control(),"last_market_ok":self.last_market_ok})
            wait=max(0.2,REFRESH_SECONDS-(time.monotonic()-started)); self.stop_event.wait(wait)


class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(APP_TITLE); self.geometry("1500x920"); self.minsize(1240,760); self.configure(bg=BG)
        self.app_dir=Path(__file__).resolve().parent; self.q:queue.Queue=queue.Queue(); self.worker=DataWorker(self.app_dir,self.q); self.vars:dict[str,tk.StringVar]={}; self.position_items:dict[str,str]={}; self.last_snapshot:dict[str,Any]={}; self.status_labels:dict[str,ttk.Label]={}
        self._style(); self._build(); self.worker.start(); self.after(250,self._poll); self.protocol("WM_DELETE_WINDOW",self._close)
    def _style(self):
        s=ttk.Style(self); s.theme_use("clam")
        s.configure("TFrame",background=BG); s.configure("Panel.TFrame",background=PANEL); s.configure("Panel2.TFrame",background=PANEL2)
        s.configure("TLabel",background=BG,foreground=FG,font=("Segoe UI",10)); s.configure("Panel.TLabel",background=PANEL,foreground=FG,font=("Segoe UI",10)); s.configure("Muted.TLabel",background=PANEL,foreground=MUTED,font=("Segoe UI",9)); s.configure("Title.TLabel",background=BG,foreground=FG,font=("Segoe UI Semibold",18)); s.configure("Section.TLabel",background=PANEL,foreground=FG,font=("Segoe UI Semibold",11)); s.configure("Metric.TLabel",background=PANEL,foreground=FG,font=("Segoe UI Semibold",16)); s.configure("Green.TLabel",background=BG,foreground=GREEN,font=("Segoe UI Semibold",10)); s.configure("Red.TLabel",background=BG,foreground=RED,font=("Segoe UI Semibold",10)); s.configure("Amber.TLabel",background=BG,foreground=AMBER,font=("Segoe UI Semibold",10)); s.configure("Grey.TLabel",background=BG,foreground=GREY,font=("Segoe UI Semibold",10)); s.configure("Blue.TLabel",background=BG,foreground=BLUE,font=("Segoe UI Semibold",10))
        s.configure("TButton",padding=(10,6),font=("Segoe UI Semibold",9)); s.configure("Start.TButton",background="#238636",foreground="white"); s.map("Start.TButton",background=[("disabled","#4b5563"),("active","#2ea043")],foreground=[("disabled","#c9d1d9")]); s.configure("Stop.TButton",background="#da3633",foreground="white"); s.map("Stop.TButton",background=[("active","#f85149")]); s.configure("Pause.TButton",background="#9e6a03",foreground="white")
        s.configure("TNotebook",background=BG,borderwidth=0); s.configure("TNotebook.Tab",background=PANEL,foreground=MUTED,padding=(12,7)); s.map("TNotebook.Tab",background=[("selected",PANEL2)],foreground=[("selected",FG)])
        s.configure("Treeview",background=PANEL,fieldbackground=PANEL,foreground=FG,rowheight=26,borderwidth=0); s.configure("Treeview.Heading",background=PANEL2,foreground=FG,font=("Segoe UI Semibold",9)); s.map("Treeview",background=[("selected","#264f78")])
    def v(self,key:str,default="—")->tk.StringVar:
        if key not in self.vars:self.vars[key]=tk.StringVar(value=default)
        return self.vars[key]
    def _set_status(self,key:str,text:str,style:str):
        self.v(key).set(text)
        lbl=self.status_labels.get(key)
        if lbl is not None: lbl.configure(style=style)
    def _build(self):
        top=ttk.Frame(self); top.pack(fill="x",padx=14,pady=(12,6))
        ttk.Label(top,text="BITUNIX SWING + COPY TRADER",style="Title.TLabel").pack(side="left")
        ttk.Label(top,text="PAPER MODE",style="Blue.TLabel").pack(side="right",padx=(8,0))
        controls=ttk.Frame(self); controls.pack(fill="x",padx=14,pady=(0,7))
        self.start_btn=ttk.Button(controls,text="▶ START BOT",style="Start.TButton",command=self._start); self.start_btn.pack(side="left")
        self.pause_btn=ttk.Button(controls,text="PAUSE NEW TRADES",style="Pause.TButton",command=self._pause); self.pause_btn.pack(side="left",padx=8)
        ttk.Button(controls,text="■ STOP BOT",style="Stop.TButton",command=self._stop).pack(side="left")
        for key in ("bot_status","data_status","copy_status","swing_status","intel_status","ml_status"):
            lbl=ttk.Label(controls,textvariable=self.v(key),style="Grey.TLabel"); lbl.pack(side="right",padx=8); self.status_labels[key]=lbl
        refresh=ttk.Frame(self); refresh.pack(fill="x",padx=14,pady=(0,7)); ttk.Label(refresh,text="HMI refresh: 2 sec",style="TLabel").pack(side="left"); ttk.Label(refresh,textvariable=self.v("last_refresh","Last refresh: —"),style="TLabel").pack(side="left",padx=18); ttk.Label(refresh,textvariable=self.v("market_data","Market data: —"),style="TLabel").pack(side="left")
        self.nb=ttk.Notebook(self); self.nb.pack(fill="both",expand=True,padx=12,pady=(0,12))
        self.dashboard=ttk.Frame(self.nb); self.copytab=ttk.Frame(self.nb); self.swingtab=ttk.Frame(self.nb); self.inteltab=ttk.Frame(self.nb); self.postab=ttk.Frame(self.nb); self.perftab=ttk.Frame(self.nb); self.learn=ttk.Frame(self.nb); self.logtab=ttk.Frame(self.nb); self.settingstab=ttk.Frame(self.nb)
        for w,name in ((self.dashboard,"Dashboard"),(self.copytab,"Copy Trader"),(self.swingtab,"Swing Trader"),(self.inteltab,"Market Intelligence"),(self.postab,"Positions"),(self.perftab,"Performance"),(self.learn,"AI Learning"),(self.logtab,"Logs"),(self.settingstab,"Settings")):self.nb.add(w,text=name)
        self._dashboard(); self._copy_tab(); self.swing_tree=self._scanner_tab(self.swingtab,"SWING SCANNER"); self._intel_tab(); self.pos_tree2=self._positions_table(self.postab)
        pos_actions=ttk.Frame(self.postab); pos_actions.pack(fill="x",padx=6,pady=(6,6))
        ttk.Button(pos_actions,text="CLOSE SELECTED TRADE",style="Stop.TButton",command=self._manual_close_selected).pack(side="left")
        ttk.Label(pos_actions,text="Select an open position, then choose Market or Limit in the confirmation popup.",style="TLabel").pack(side="left",padx=12)
        self._perf_tab(); self._learning_tab(); self._logs_tab(); self._settings_tab()
    def _panel(self,parent,title):
        f=ttk.Frame(parent,style="Panel.TFrame",padding=12); ttk.Label(f,text=title,style="Section.TLabel").pack(anchor="w",pady=(0,8)); return f
    def _dashboard(self):
        perf=self._panel(self.dashboard,"PORTFOLIO PERFORMANCE"); perf.pack(fill="x",padx=6,pady=6)
        row=ttk.Frame(perf,style="Panel.TFrame"); row.pack(fill="x")
        for label,key in (("TODAY","perf_1"),("2 DAYS","perf_2"),("30 DAYS","perf_30"),("ALL TIME","perf_all")):
            c=ttk.Frame(row,style="Panel.TFrame",padding=(4,0)); c.pack(side="left",expand=True,fill="x"); ttk.Label(c,text=label,style="Muted.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.v(key,"0.00 USDT"),style="Metric.TLabel").pack(anchor="w")
        row2=ttk.Frame(perf,style="Panel.TFrame"); row2.pack(fill="x",pady=(12,0))
        for label,key in (("OPEN P/L","open_pnl"),("REALIZED TODAY","realized_today"),("OPEN RISK","open_risk"),("ALLOCATED MARGIN","allocated")):
            c=ttk.Frame(row2,style="Panel.TFrame",padding=(4,0)); c.pack(side="left",expand=True,fill="x"); ttk.Label(c,text=label,style="Muted.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.v(key),style="Panel.TLabel").pack(anchor="w")

        capital=self._panel(self.dashboard,"PAPER WALLETS"); capital.pack(fill="x",padx=6,pady=(0,6))
        cr=ttk.Frame(capital,style="Panel.TFrame"); cr.pack(fill="x")
        for label,key in (("RESERVE WALLET","wallet_reserve"),("SWING ACCOUNT","wallet_swing"),("COPY ACCOUNT","wallet_copy"),("TOTAL EQUITY","wallet_equity")):
            c=ttk.Frame(cr,style="Panel.TFrame",padding=(4,0)); c.pack(side="left",expand=True,fill="x"); ttk.Label(c,text=label,style="Muted.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.v(key),style="Panel.TLabel").pack(anchor="w")
        tr=ttk.Frame(capital,style="Panel.TFrame"); tr.pack(fill="x",pady=(10,0))
        ttk.Label(tr,text="Transfer amount (USDT)",style="Muted.TLabel").pack(side="left")
        self.transfer_amount=tk.StringVar(value="500")
        ttk.Entry(tr,textvariable=self.transfer_amount,width=12).pack(side="left",padx=(8,14))
        for label,src,dst in (("RESERVE → SWING","RESERVE","SWING"),("SWING → RESERVE","SWING","RESERVE"),("RESERVE → COPY","RESERVE","COPY"),("COPY → RESERVE","COPY","RESERVE"),("SWING → COPY","SWING","COPY"),("COPY → SWING","COPY","SWING")):
            ttk.Button(tr,text=label,command=lambda a=src,b=dst:self._wallet_transfer(a,b)).pack(side="left",padx=3)

        agents=ttk.Frame(self.dashboard); agents.pack(fill="x",padx=6,pady=0); agents.columnconfigure(0,weight=1); agents.columnconfigure(1,weight=1)
        cpy=self._panel(agents,"COPY TRADER — PAPER"); cpy.grid(row=0,column=0,sticky="nsew",padx=(0,3)); self._copy_fields(cpy)
        cb=ttk.Frame(cpy,style="Panel.TFrame"); cb.pack(anchor="w",pady=(10,0)); ttk.Button(cb,text="VIEW COPY TRADER",command=lambda:self.nb.select(self.copytab)).pack(side="left")
        swp=self._panel(agents,"SWING TRADER"); swp.grid(row=0,column=1,sticky="nsew",padx=(3,0)); self._agent_fields(swp,"swing",True)
        sb=ttk.Frame(swp,style="Panel.TFrame"); sb.pack(anchor="w",pady=(10,0)); ttk.Button(sb,text="VIEW SWING SCANNER",command=lambda:self.nb.select(self.swingtab)).pack(side="left")
        intel=self._panel(self.dashboard,"MARKET INTELLIGENCE"); intel.pack(fill="x",padx=6,pady=6)
        ir=ttk.Frame(intel,style="Panel.TFrame"); ir.pack(fill="x")
        for label,key in (("Status","intel_panel_status"),("Overall Bias","intel_bias"),("Impact","intel_impact"),("Confidence","intel_conf"),("Horizon","intel_horizon"),("Critical Risk","intel_risk"),("Last Update","intel_update")):
            c=ttk.Frame(ir,style="Panel.TFrame"); c.pack(side="left",expand=True,fill="x"); ttk.Label(c,text=label,style="Muted.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.v(key),style="Panel.TLabel").pack(anchor="w")
        ttk.Label(intel,textvariable=self.v("intel_assets","—"),style="Panel.TLabel").pack(anchor="w",pady=(10,3)); ttk.Button(intel,text="VIEW INTELLIGENCE",command=lambda:self.nb.select(self.inteltab)).pack(anchor="w")
        p=self._panel(self.dashboard,"OPEN POSITIONS"); p.pack(fill="both",expand=True,padx=6,pady=(0,6)); self.pos_tree=self._positions_table(p,compact=True)
    def _copy_fields(self,p):
        fields=(("Status","copy_panel_status"),("Positions","copy_positions"),("Capital","copy_capital"),("Today P/L","copy_pnl"),("Trade size","copy_trade_size"),("Chain","copy_chain"),("Source wallets","copy_sources"),("GMGN API","copy_api"))
        grid=ttk.Frame(p,style="Panel.TFrame"); grid.pack(fill="x")
        for i,(label,key) in enumerate(fields):
            ttk.Label(grid,text=label,style="Muted.TLabel").grid(row=i,column=0,sticky="w",pady=2); ttk.Label(grid,textvariable=self.v(key),style="Panel.TLabel").grid(row=i,column=1,sticky="w",padx=(18,0),pady=2)
    def _copy_tab(self):
        p=self._panel(self.copytab,"COPY TRADER — PAPER MODE"); p.pack(fill="both",expand=True,padx=6,pady=6)
        ttk.Label(p,text="Paper allocation: 3,000 USDT maximum | 500 USDT per copied trade | up to 6 simultaneous paper positions",style="Panel.TLabel").pack(anchor="w")
        ttk.Label(p,text=f"Solana COPY source wallets are editable below (maximum {MAX_COPY_WALLETS}). GMGN read-only access is detected from the CLI configuration; automatic paper copying will be enabled only after the wallet-activity field mapping is validated.",style="Muted.TLabel",wraplength=1250).pack(anchor="w",pady=(6,14))
        grid=ttk.Frame(p,style="Panel.TFrame"); grid.pack(fill="x")
        for i,(label,key) in enumerate((("Status","copy_panel_status"),("Chain","copy_chain"),("GMGN API","copy_api"),("Source wallets","copy_sources"),("Allocated capital","copy_capital"),("Paper trade size","copy_trade_size"),("Open copy positions","copy_positions"),("Today P/L","copy_pnl"))):
            ttk.Label(grid,text=label,style="Muted.TLabel").grid(row=i,column=0,sticky="w",pady=4); ttk.Label(grid,textvariable=self.v(key),style="Panel.TLabel").grid(row=i,column=1,sticky="w",padx=(18,0),pady=4)
        ttk.Label(p,text=f"SOURCE WALLETS (max {MAX_COPY_WALLETS})",style="Section.TLabel").pack(anchor="w",pady=(18,2))
        ttk.Label(p,textvariable=self.v("copy_wallet_count_label"),style="Muted.TLabel").pack(anchor="w")
        lb=tk.Listbox(p,height=3,bg=PANEL2,fg=FG,borderwidth=0,highlightthickness=1,highlightbackground=BORDER,selectbackground=BLUE,font=("Consolas",10)); lb.pack(fill="x",pady=(4,6)); self.copy_wallet_list=lb
        row=ttk.Frame(p,style="Panel.TFrame"); row.pack(fill="x")
        self.copy_wallet_entry=tk.StringVar()
        ttk.Entry(row,textvariable=self.copy_wallet_entry,width=52).pack(side="left")
        ttk.Button(row,text="ADD WALLET",command=self._copy_add_wallet).pack(side="left",padx=6)
        ttk.Button(row,text="DELETE SELECTED",command=self._copy_delete_wallet).pack(side="left")
        ttk.Label(p,text="Paste a Solana (base58) wallet address and ADD WALLET, or select one and DELETE SELECTED. Changes save to settings.json and apply after an engine restart.",style="Muted.TLabel",wraplength=1250).pack(anchor="w",pady=(6,0))
        ttk.Label(p,text="Security: do not store a seed phrase or blockchain-wallet private key in settings.json. Paper mode only needs read-only GMGN access; live execution will be added separately after paper validation.",style="Muted.TLabel",wraplength=1250).pack(anchor="w",pady=(18,0))
        self._copy_refresh_wallet_list()
    def _copy_wallets_now(self) -> list[str]:
        try:
            cfg=load_config(data_directory()/"settings.json")
            return [str(x).strip() for x in (cfg.copy.source_wallets or []) if str(x).strip()]
        except Exception:
            return []
    def _copy_refresh_wallet_list(self):
        if not hasattr(self,"copy_wallet_list"): return
        self.copy_wallet_list.delete(0,"end")
        wallets=self._copy_wallets_now()
        for w in wallets: self.copy_wallet_list.insert("end",w)
        self.v("copy_wallet_count_label").set(f"{len(wallets)} / {MAX_COPY_WALLETS} source wallets configured")
    def _copy_save_wallets(self,wallets:list[str]):
        path=data_directory()/"settings.json"; cfg=load_config(path); cfg.copy.source_wallets=wallets[:MAX_COPY_WALLETS]; save_config(path,cfg)
    def _copy_add_wallet(self):
        addr=str(self.copy_wallet_entry.get()).strip()
        if not addr:
            messagebox.showerror("Add wallet","Enter a Solana wallet address."); return
        if not _is_solana_address(addr):
            messagebox.showerror("Add wallet","That does not look like a valid Solana (base58) address.\n\nExpected 32–44 base58 characters."); return
        wallets=self._copy_wallets_now()
        if addr in wallets:
            messagebox.showerror("Add wallet","That wallet is already in the list."); return
        if len(wallets)>=MAX_COPY_WALLETS:
            messagebox.showerror("Add wallet",f"A maximum of {MAX_COPY_WALLETS} copy source wallets is allowed.\n\nDelete one before adding another."); return
        wallets.append(addr); self._copy_save_wallets(wallets); self.copy_wallet_entry.set(""); self._copy_refresh_wallet_list()
        messagebox.showinfo("Wallet added",f"Added source wallet:\n{addr}\n\nRestart the trading engine for the change to take effect.")
    def _copy_delete_wallet(self):
        if not hasattr(self,"copy_wallet_list"): return
        sel=self.copy_wallet_list.curselection()
        if not sel:
            messagebox.showerror("Delete wallet","Select a wallet in the list first."); return
        addr=self.copy_wallet_list.get(sel[0])
        if not messagebox.askyesno("Delete wallet",f"Remove this copy source wallet?\n\n{addr}"): return
        wallets=[w for w in self._copy_wallets_now() if w!=addr]; self._copy_save_wallets(wallets); self._copy_refresh_wallet_list()
        messagebox.showinfo("Wallet removed",f"Removed:\n{addr}\n\nRestart the trading engine for the change to take effect.")
    def _agent_fields(self,prefix_frame,prefix,swing):
        fields=[("Status",f"{prefix}_panel_status"),("Positions",f"{prefix}_positions"),("Capital",f"{prefix}_capital"),("Today P/L",f"{prefix}_pnl"),("Macro Market" if swing else "Market",f"{prefix}_market"),("Daily Structure" if swing else "Market Participation",f"{prefix}_participation"),("BTC",f"{prefix}_btc"),("Last scan",f"{prefix}_last"),("Next scan",f"{prefix}_next")]
        grid=ttk.Frame(prefix_frame,style="Panel.TFrame"); grid.pack(fill="x")
        for i,(label,key) in enumerate(fields):
            ttk.Label(grid,text=label,style="Muted.TLabel").grid(row=i,column=0,sticky="w",pady=2); ttk.Label(grid,textvariable=self.v(key),style="Panel.TLabel").grid(row=i,column=1,sticky="w",padx=(18,0),pady=2)
    def _scanner_tab(self,parent,title):
        head=ttk.Frame(parent,style="Panel.TFrame",padding=12); head.pack(fill="x",padx=6,pady=6); ttk.Label(head,text=title,style="Section.TLabel").pack(side="left"); ttk.Label(head,text="Latest completed strategy scan | SHADOW/DEFENSIVE rows do not open trades",style="Muted.TLabel").pack(side="right")
        cols=("coin","dir","long","short","score","pa","regime","stop","margin","notional","risk","status"); tree=ttk.Treeview(parent,columns=cols,show="headings")
        names=("Coin","Direction","Long","Short","Score","Price Action / Setup","Market","Stop %","Margin","Notional","Risk $","Status")
        widths={"coin":95,"dir":80,"long":70,"short":70,"score":70,"pa":190,"regime":105,"stop":75,"margin":85,"notional":90,"risk":75,"status":105}
        for c,n in zip(cols,names):tree.heading(c,text=n); tree.column(c,width=widths.get(c,90),anchor="center")
        tree.pack(fill="both",expand=True,padx=6,pady=(0,6)); return tree
    def _positions_table(self,parent,compact=False):
        cols=("type","coin","dir","entry","now","pnl","r","stop","target","age","status"); tree=ttk.Treeview(parent,columns=cols,show="headings",height=6 if compact else 18)
        names=("Type","Coin","Dir","Entry","Now","P/L USDT","R","Stop","Target","Age","Status")
        for c,n in zip(cols,names):tree.heading(c,text=n); tree.column(c,width=78 if c not in ("entry","now","stop","target") else 105,anchor="center")
        tree.pack(fill="both",expand=True); return tree
    def _intel_tab(self):
        p=self._panel(self.inteltab,"MARKET INTELLIGENCE"); p.pack(fill="both",expand=True,padx=6,pady=6)
        ttk.Label(p,text="FREE MARKET INTELLIGENCE — no X subscription and no API key required",style="Panel.TLabel").pack(anchor="w")
        ttk.Label(p,text="Sources: GDELT, SEC, CFTC, CoinDesk RSS and Binance public announcements. The intelligence layer only adjusts candidate ranking; only a high-confidence critical risk can block a contradictory new entry. SWING continues normally if news sources are unavailable.",style="Muted.TLabel",wraplength=1250).pack(anchor="w",pady=(6,16))
        self.intel_text=tk.Text(p,bg=PANEL2,fg=FG,insertbackground=FG,borderwidth=0,height=22,font=("Consolas",10)); self.intel_text.pack(fill="both",expand=True)
    def _perf_tab(self):
        p=self._panel(self.perftab,"PERFORMANCE — TOTAL / SWING / COPY"); p.pack(fill="both",expand=True,padx=6,pady=6)
        cols=("period","type","opened","closed","wins","losses","win","net","avg","pf","best","worst"); self.perf_tree=ttk.Treeview(p,columns=cols,show="headings")
        names=("Period","Book","Opened","Closed","Wins","Losses","Win %","Net P/L","Avg P/L","PF","Best","Worst")
        for c,n in zip(cols,names):self.perf_tree.heading(c,text=n);self.perf_tree.column(c,width=100,anchor="center")
        self.perf_tree.pack(fill="both",expand=True)
    def _learning_tab(self):
        p=self._panel(self.learn,"AI LEARNING LAB — SWING SHADOW ONLY"); p.pack(fill="both",expand=True,padx=6,pady=6)
        ttk.Label(p,textvariable=self.v("learning_counts"),style="Panel.TLabel").pack(anchor="w",pady=(0,8)); self.learning_text=tk.Text(p,bg=PANEL2,fg=FG,insertbackground=FG,borderwidth=0,font=("Consolas",9)); self.learning_text.pack(fill="both",expand=True)
    def _logs_tab(self):
        p=self._panel(self.logtab,"ENGINE LOG"); p.pack(fill="both",expand=True,padx=6,pady=6); self.log_text=tk.Text(p,bg="#05080c",fg="#c9d1d9",insertbackground=FG,borderwidth=0,font=("Consolas",9)); self.log_text.pack(fill="both",expand=True)
    def _settings_tab(self):
        p=self._panel(self.settingstab,"SETTINGS"); p.pack(fill="both",expand=True,padx=6,pady=6)
        ttk.Label(p,text=f"Data folder: {data_directory()}",style="Panel.TLabel").pack(anchor="w",pady=4)
        row=ttk.Frame(p,style="Panel.TFrame"); row.pack(anchor="w",pady=(14,4))
        ttk.Label(row,text="SWING same-direction cap",style="Panel.TLabel").pack(side="left")
        self.swing_same_cap=tk.StringVar(value="6")
        cb=ttk.Combobox(row,textvariable=self.swing_same_cap,values=("3","4","5","6","7"),state="readonly",width=5); cb.pack(side="left",padx=10)
        ttk.Button(row,text="SAVE CAP",command=self._save_swing_cap).pack(side="left")
        ttk.Label(p,text="Choose 3–7. This is the maximum number of SWING positions allowed in the same direction. Risk/correlation limits and the correction logic is shadow-only and the active strategy waits through transition states.",style="Muted.TLabel",wraplength=1100).pack(anchor="w",pady=(0,10))
        ttk.Label(p,text="COPY paper account: Solana only in v1.8.2, maximum 3,000 USDT, 500 USDT per trade, maximum 6 copy positions. Source wallets are editable on the Copy Trader tab (maximum 3); the GMGN API key remains in the GMGN CLI config, not settings.json.",style="Muted.TLabel",wraplength=1100).pack(anchor="w",pady=(0,10))
        ttk.Label(p,text="Other trading settings remain in settings.json. Strategy-setting changes apply after engine restart.",style="Muted.TLabel").pack(anchor="w",pady=4)
        ttk.Button(p,text="OPEN SETTINGS.JSON",command=self._open_settings).pack(anchor="w",pady=12)
    def _open_settings(self):
        path=data_directory()/"settings.json"
        if os.name=="nt": os.startfile(path)  # type: ignore[attr-defined]
        else: messagebox.showinfo("Settings",str(path))
    def _start(self):
        if engine_running():
            self.start_btn.state(["disabled"])
            return
        self.start_btn.state(["disabled"])
        try:
            pid=launch_engine(self.app_dir); messagebox.showinfo("Bitunix Trader",f"Trading engine started.\nPID: {pid}")
        except Exception as e:
            self.start_btn.state(["!disabled"])
            messagebox.showerror("Start failed",str(e))
    def _pause(self):
        c=read_control(); new=not bool(c.get("pause_new_trades")); set_pause(new); self.pause_btn.configure(text="RESUME NEW TRADES" if new else "PAUSE NEW TRADES")
    def _wallet_transfer(self, source: str, destination: str):
        try:
            amount=float(str(self.transfer_amount.get()).replace(",","").strip())
            if amount<=0: raise ValueError
        except Exception:
            messagebox.showerror("Transfer","Enter a positive USDT amount."); return
        if not messagebox.askyesno("Paper wallet transfer",f"Transfer {amount:,.2f} USDT from {source} to {destination}?\n\nOpen positions are never force-closed. Transfers out of SWING/COPY are limited to currently free, unused allocation. COPY cannot exceed 3,000 USDT."):
            return
        store=DayTraderStore(data_directory()/"daytrader.sqlite3")
        try:
            cfg=load_config(data_directory()/"settings.json")
            store.ensure_wallet(day_initial=cfg.capital.day_initial_usdt,swing_initial=cfg.capital.swing_initial_usdt,reserve_initial=cfg.capital.reserve_initial_usdt,copy_initial=cfg.capital.copy_initial_usdt,legacy_flex_target=str(read_control().get("flex_fund_target") or "DAY"))
            store.retire_day_wallet_to_reserve()
            state=store.transfer_wallet(source,destination,amount,copy_max_balance=cfg.capital.copy_max_allocation_usdt)
            messagebox.showinfo("Transfer complete",f"Transferred {amount:,.2f} USDT.\n\nRESERVE: {state['reserve_balance']:,.2f}\nSWING: {state['swing_balance']:,.2f}\nCOPY: {state['copy_balance']:,.2f}")
        except Exception as e:
            messagebox.showerror("Transfer failed",str(e))
        finally:
            store.close()

    def _fixed_transfer(self, source: str, destination: str):
        if not messagebox.askyesno(
            "Move 2,500 USDT",
            f"Move 2,500 USDT from {source} to {destination}?\n\n"
            "Any free amount moves immediately. If some capital is still tied to open positions, "
            "the remaining amount moves automatically as those positions close normally. No position is force-closed.",
        ):
            return
        store=DayTraderStore(data_directory()/"daytrader.sqlite3")
        try:
            cfg=load_config(data_directory()/"settings.json")
            store.ensure_wallet(day_initial=cfg.capital.day_initial_usdt,swing_initial=cfg.capital.swing_initial_usdt,reserve_initial=cfg.capital.reserve_initial_usdt,legacy_flex_target=str(read_control().get("flex_fund_target") or "DAY"))
            floor=5000.0 if source=="SWING" else 0.0
            req=store.request_fixed_transfer(source,destination,2500.0,source_floor=floor)
            moved=float(req.get("transferred") or 0.0); remaining=float(req.get("remaining") or 0.0)
            if remaining<=1e-9:
                msg=f"2,500.00 USDT moved from {source} to {destination}."
            else:
                msg=(f"Transfer started.\n\nMoved now: {moved:,.2f} USDT\n"
                     f"Pending: {remaining:,.2f} USDT\n\nThe pending amount will move as {source} positions release capital.")
            messagebox.showinfo("2,500 transfer",msg)
        except Exception as e:
            messagebox.showerror("Transfer failed",str(e))
        finally:
            store.close()

    def _selected_position(self) -> dict[str, Any] | None:
        sel=self.pos_tree2.selection() if hasattr(self,"pos_tree2") else ()
        if not sel:
            messagebox.showinfo("Manual close","Select an open position first.")
            return None
        cid=str(sel[0])
        for row in self.last_snapshot.get("positions",[]) if isinstance(self.last_snapshot,dict) else []:
            if str(row.get("client_id"))==cid:
                return row
        messagebox.showerror("Manual close","The selected position is no longer open.")
        return None

    def _manual_close_selected(self):
        row=self._selected_position()
        if not row:
            return
        cid=str(row["client_id"]); symbol=str(row.get("symbol") or ""); direction=str(row.get("direction") or "")
        current=float(row.get("now") or row.get("entry") or 0.0); pnl=float(row.get("pnl") or 0.0)
        pop=tk.Toplevel(self); pop.title(f"Close {symbol}"); pop.configure(bg=PANEL); pop.resizable(False,False); pop.transient(self); pop.grab_set()
        body=ttk.Frame(pop,style="Panel.TFrame",padding=16); body.pack(fill="both",expand=True)
        ttk.Label(body,text=f"{row.get('type','')}  {symbol}  {direction}",style="Section.TLabel").grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,10))
        info=(("Entry",_fmt_num(float(row.get("entry") or 0))), ("Current market",_fmt_num(current)), ("Present P/L",_fmt_money(pnl)), ("Current R",f"{float(row.get('r') or 0):+.2f}R"))
        for i,(lab,val) in enumerate(info,1):
            ttk.Label(body,text=lab,style="Muted.TLabel").grid(row=i,column=0,sticky="w",pady=2); ttk.Label(body,text=val,style="Panel.TLabel").grid(row=i,column=1,sticky="w",padx=(18,0),pady=2)
        mode=tk.StringVar(value="LIMIT" if str(row.get("manual_exit_type") or "").upper()=="LIMIT" else "MARKET")
        ttk.Separator(body,orient="horizontal").grid(row=5,column=0,columnspan=2,sticky="ew",pady=12)
        ttk.Radiobutton(body,text="Close at MARKET price",variable=mode,value="MARKET").grid(row=6,column=0,columnspan=2,sticky="w",pady=3)
        lr=ttk.Frame(body,style="Panel.TFrame"); lr.grid(row=7,column=0,columnspan=2,sticky="w",pady=3)
        ttk.Radiobutton(lr,text="Close at LIMIT price",variable=mode,value="LIMIT").pack(side="left")
        limit_var=tk.StringVar(value=_fmt_num(float(row.get("manual_exit_limit_price") or 0)) if row.get("manual_exit_limit_price") else "")
        limit_entry=ttk.Entry(lr,textvariable=limit_var,width=16); limit_entry.pack(side="left",padx=(10,0))
        def sync_limit(*_):
            limit_entry.configure(state="normal" if mode.get()=="LIMIT" else "disabled")
        mode.trace_add("write",sync_limit); sync_limit()
        ttk.Label(body,text=("For LONG, a close-limit must be above the current market. For SHORT, it must be below. "
                            "Market requests execute on the next position-monitor cycle."),style="Muted.TLabel",wraplength=470).grid(row=8,column=0,columnspan=2,sticky="w",pady=(10,8))
        buttons=ttk.Frame(body,style="Panel.TFrame"); buttons.grid(row=9,column=0,columnspan=2,sticky="e",pady=(8,0))
        ttk.Button(buttons,text="CANCEL",command=pop.destroy).pack(side="right")
        def confirm():
            typ=mode.get()
            limit=None
            if typ=="LIMIT":
                try: limit=float(str(limit_var.get()).replace(",","").strip())
                except Exception:
                    messagebox.showerror("Limit price","Enter a valid positive limit price.",parent=pop); return
                if limit<=0:
                    messagebox.showerror("Limit price","Enter a valid positive limit price.",parent=pop); return
                if direction=="LONG" and limit<=current:
                    messagebox.showerror("Limit price","For a LONG position, enter a close-limit above the current market price. Use MARKET for an immediate close.",parent=pop); return
                if direction=="SHORT" and limit>=current:
                    messagebox.showerror("Limit price","For a SHORT position, enter a close-limit below the current market price. Use MARKET for an immediate close.",parent=pop); return
            store=DayTraderStore(data_directory()/"daytrader.sqlite3")
            try:
                store.set_manual_exit(cid,typ,limit)
            except Exception as e:
                messagebox.showerror("Manual close failed",str(e),parent=pop); store.close(); return
            store.close(); pop.destroy()
            if typ=="MARKET":
                messagebox.showinfo("Manual close requested",f"{symbol} will close at market on the next position-monitor cycle.")
            else:
                messagebox.showinfo("Limit close placed",f"{symbol} close-limit set at {_fmt_num(float(limit))}.\nThe normal SL/TP protection remains active while waiting.")
        ttk.Button(buttons,text="CONFIRM CLOSE",style="Stop.TButton",command=confirm).pack(side="right",padx=(0,8))
        pop.update_idletasks(); pop.geometry(f"+{self.winfo_rootx()+220}+{self.winfo_rooty()+160}")

    def _save_swing_cap(self):
        try: cap=int(self.swing_same_cap.get())
        except Exception: cap=6
        if cap not in (3,4,5,6,7):
            messagebox.showerror("SWING cap","Choose a value from 3 to 7."); return
        path=data_directory()/"settings.json"; cfg=load_config(path); cfg.swing.max_same_direction_positions=cap; save_config(path,cfg)
        messagebox.showinfo("SWING cap saved",f"SWING same-direction cap set to {cap}.\nRestart the trading engine for the strategy change to take effect.")

    def _stop(self):
        if not engine_running(): messagebox.showinfo("Bitunix Trader","Trading engine is already stopped."); return
        if messagebox.askyesno("Stop bot","Stop the trading engine?\n\nOpen paper positions remain in the database and will resume monitoring when the bot is started again."):
            request_stop()
    def _poll(self):
        try:
            while True:
                snap=self.q.get_nowait(); self._apply(snap)
        except queue.Empty: pass
        self.after(250,self._poll)
    def _apply(self,s:dict[str,Any]):
        self.last_snapshot=s
        now=datetime.now(timezone.utc); running=bool(s.get("engine_running")); control=s.get("control",{}) or {}; paused=bool(control.get("pause_new_trades"))
        engine_state=s.get("engine_state",{}) or {}; engine_status=str(engine_state.get("status") or "").upper(); fatal=bool(s.get("fatal_error"))
        if running: self.start_btn.state(["disabled"])
        else: self.start_btn.state(["!disabled"])
        if fatal or engine_status=="ERROR": self._set_status("bot_status","BOT ● ERROR","Red.TLabel")
        elif running and paused: self._set_status("bot_status","BOT ◐ PAUSED","Amber.TLabel")
        elif running: self._set_status("bot_status","BOT ● RUNNING","Green.TLabel")
        else: self._set_status("bot_status","BOT ○ IDLE","Grey.TLabel")
        self.pause_btn.configure(text="RESUME NEW TRADES" if paused else "PAUSE NEW TRADES")

        last_market=s.get("last_market_ok"); age=(now-last_market).total_seconds() if isinstance(last_market,datetime) else 9999
        if not running and not fatal: self._set_status("data_status","DATA ○ IDLE","Grey.TLabel")
        elif age<10: self._set_status("data_status","DATA ● LIVE","Green.TLabel")
        elif age<30: self._set_status("data_status","DATA ◐ DELAYED","Amber.TLabel")
        else: self._set_status("data_status","DATA ● STALE","Red.TLabel")

        cap=s.get("capital") or {}; swing_balance=float(cap.get("swing_balance") or 0.0); copy_balance=float(cap.get("copy_balance") or 0.0)
        if not running: self._set_status("swing_status","SWING ○ IDLE","Grey.TLabel")
        elif swing_balance<1: self._set_status("swing_status","SWING ● NO FUNDS","Red.TLabel")
        else: self._set_status("swing_status","SWING ● ACTIVE","Green.TLabel")

        copy=s.get("copy") or {}; cst=str(copy.get("status") or "SETUP REQUIRED").upper(); copy_trade=float(copy.get("paper_trade_usdt") or 500.0)
        if not running: self._set_status("copy_status","COPY ○ IDLE","Grey.TLabel")
        elif cst=="DISABLED": self._set_status("copy_status","COPY ○ OFF","Grey.TLabel")
        elif copy_balance + 1e-9 < copy_trade: self._set_status("copy_status","COPY ● NO FUNDS","Red.TLabel")
        elif cst=="CONFIG READY": self._set_status("copy_status","COPY ◐ CONFIG READY","Amber.TLabel")
        else: self._set_status("copy_status",f"COPY ● {cst}","Red.TLabel")

        intel=(s.get("intelligence") or {}); ist=str(intel.get("status") or "NOT CONNECTED").upper()
        if not running: self._set_status("intel_status","INTEL ○ IDLE","Grey.TLabel")
        elif ist=="ACTIVE": self._set_status("intel_status","INTEL ● ACTIVE","Green.TLabel")
        elif ist=="PARTIAL": self._set_status("intel_status","INTEL ◐ PARTIAL","Amber.TLabel")
        elif ist=="DISABLED": self._set_status("intel_status","INTEL ○ OFF","Grey.TLabel")
        else: self._set_status("intel_status","INTEL ● UNAVAILABLE","Red.TLabel")
        if fatal or engine_status=="ERROR": self._set_status("ml_status","ML ● ERROR","Red.TLabel")
        elif running: self._set_status("ml_status","ML ● SHADOW","Green.TLabel")
        else: self._set_status("ml_status","ML ○ IDLE","Grey.TLabel")

        self.v("last_refresh").set("Last refresh: "+_time(s.get("refresh_utc"))); self.v("market_data").set("Market data: "+(_time(last_market) if last_market else "—"))
        if "performance" not in s: return
        perf=s["performance"]; self.v("perf_1").set(_fmt_money(perf["1 DAY"]["TOTAL"]["net"])); self.v("perf_2").set(_fmt_money(perf["2 DAYS"]["TOTAL"]["net"])); self.v("perf_30").set(_fmt_money(perf["30 DAYS"]["TOTAL"]["net"])); self.v("perf_all").set(_fmt_money(perf["ALL TIME"]["TOTAL"]["net"])); self.v("realized_today").set(_fmt_money(perf["1 DAY"]["TOTAL"]["net"])); self.v("open_risk").set(f"{s['open_risk']:.1f} / 1000 USDT"); self.v("allocated").set(f"{s['allocated_margin']:,.0f} / {float(cap.get('allocated_total') or 0):,.0f} USDT")
        openp=sum(float(x.get("pnl") or 0) for x in s["positions"]); self.v("open_pnl").set(_fmt_money(openp))

        self.v("wallet_reserve").set(f"{float(cap.get('reserve_balance') or 0):,.2f} USDT")
        self.v("wallet_swing").set(f"{swing_balance:,.2f} | free {float(cap.get('swing_new_available') or 0):,.2f}")
        self.v("wallet_copy").set(f"{copy_balance:,.2f} | free {float(cap.get('copy_new_available') or 0):,.2f}")
        self.v("wallet_equity").set(f"{float(cap.get('equity') or cap.get('wallet_total') or 0):,.2f} USDT")

        sw=s["swing"]; swstatus="IDLE" if not running else ("NO FUNDS FOR NEW TRADE" if swing_balance<=0 else f"ACTIVE | same-dir cap {int(sw.get('same_direction_cap') or 6)}")
        self.v("swing_panel_status").set(swstatus); self.v("swing_positions").set(f"{sw['positions']} / {int(sw.get('max_positions') or 7)}"); self.v("swing_capital").set(f"{swing_balance:,.0f} allocated | {float(cap.get('swing_used') or 0):,.0f} used | {float(cap.get('swing_new_available') or 0):,.0f} free"); self.v("swing_pnl").set(_fmt_money(sw['pnl_today'])); self.v("swing_market").set(str(sw['market'])); self.v("swing_participation").set(str(sw['daily_structure'])); self.v("swing_btc").set(str(sw['btc'])); self.v("swing_last").set(_time(sw['last_scan'])); self.v("swing_next").set(_time(sw['next_scan']))
        if hasattr(self,"swing_same_cap"): self.swing_same_cap.set(str(int(sw.get('same_direction_cap') or 6)))

        cpstatus="IDLE" if not running else cst
        self.v("copy_panel_status").set(cpstatus); self.v("copy_positions").set(f"{int(copy.get('positions') or 0)} / {int(copy.get('max_positions') or 6)}"); self.v("copy_capital").set(f"{copy_balance:,.0f} allocated | {float(cap.get('copy_used') or 0):,.0f} used | {float(cap.get('copy_new_available') or 0):,.0f} free"); self.v("copy_pnl").set(_fmt_money(float(copy.get('pnl_today') or 0))); self.v("copy_trade_size").set(f"{float(copy.get('paper_trade_usdt') or 500):,.0f} USDT"); self.v("copy_chain").set(str(copy.get('chain') or 'ETH')); self.v("copy_sources").set(str(int(copy.get('source_wallet_count') or 0))); self.v("copy_api").set("CONFIGURED" if copy.get('api_key_present') else "REQUIRED")

        self.v("intel_panel_status").set(ist); self.v("intel_bias").set(str(intel.get("bias") or "—")); self.v("intel_impact").set(f"{float(intel.get('impact') or 0):.0f} / 100" if ist in ("ACTIVE","PARTIAL") else "—"); self.v("intel_conf").set(f"{float(intel.get('confidence') or 0):.0f}%" if ist in ("ACTIVE","PARTIAL") else "—"); self.v("intel_horizon").set(str(intel.get("horizon") or "—")); self.v("intel_risk").set(str(intel.get("critical_risk") or "—")); self.v("intel_update").set(_time(intel.get("last_update"))); assets=intel.get("assets") or []; self.v("intel_assets").set("   ".join(f"{a.get('symbol','')} {float(a.get('score',0)):+.0f}" for a in assets[:6]) or "No intelligence feed connected")
        self._update_positions(self.pos_tree,s["positions"]); self._update_positions(self.pos_tree2,s["positions"]); self._update_scanner(self.swing_tree,sw["scanner"]); self._update_perf(perf); self._update_learning(s.get("learning") or {}); self._update_intel_text(intel); self._update_log()
    def _update_positions(self,tree,rows):
        ids=set()
        for r in rows:
            iid=r["client_id"]; ids.add(iid); vals=(r["type"],r["symbol"],r["direction"],_fmt_num(r["entry"]),_fmt_num(r["now"]),f"{r['pnl']:+.2f}",f"{r['r']:+.2f}R",_fmt_num(r["stop"]),_fmt_num(r["target"]),r["age"],r["status"])
            if tree.exists(iid):tree.item(iid,values=vals)
            else:tree.insert("","end",iid=iid,values=vals)
        for iid in tree.get_children():
            if iid not in ids:tree.delete(iid)
    def _update_scanner(self,tree,rows):
        ids=set()
        for r in rows:
            iid=r["symbol"]; ids.add(iid); vals=(
                r["symbol"],r["direction"],f"{r['long']:.1f}",f"{r['short']:.1f}",f"{r['score']:.1f}",r["pa"],r.get("regime","—"),
                f"{r.get('stop_pct',0.0):.2f}" if r.get("stop_pct") else "—",
                f"{r.get('margin',0.0):.0f}" if r.get("margin") else "—",
                f"{r.get('notional',0.0):.0f}" if r.get("notional") else "—",
                f"{r.get('risk',0.0):.0f}" if r.get("risk") else "—",r["status"]
            )
            if tree.exists(iid):tree.item(iid,values=vals)
            else:tree.insert("","end",iid=iid,values=vals)
        for iid in tree.get_children():
            if iid not in ids:tree.delete(iid)
    def _update_perf(self,perf):
        wanted=set()
        for period in ("1 DAY","2 DAYS","30 DAYS","ALL TIME"):
            for book in ("TOTAL","SWING","COPY"):
                p=perf[period][book]; pf="INF" if p["profit_factor"]==float("inf") else f"{p['profit_factor']:.2f}"; iid=(period+"_"+book).replace(" ","_"); wanted.add(iid); vals=(period,book,p["opened"],p["closed"],p["wins"],p["losses"],f"{p['win_rate']:.1f}%",f"{p['net']:+.2f}",f"{p['avg']:+.2f}",pf,f"{p['best']:+.2f}",f"{p['worst']:+.2f}")
                if self.perf_tree.exists(iid): self.perf_tree.item(iid,values=vals)
                else: self.perf_tree.insert("","end",iid=iid,values=vals)
        for iid in self.perf_tree.get_children():
            if iid not in wanted:self.perf_tree.delete(iid)
    def _update_learning(self,learning):
        sw=learning.get("swing") or {}; self.v("learning_counts").set(f"SWING: {sw.get('resolved',0)} resolved / {sw.get('pending',0)} pending / {sw.get('total',0)} total")
        p=learning.get("swing_report"); parts=["===== SWING LATEST REPORT ====="]
        if p and Path(p).exists():
            try: parts.append(Path(p).read_text(encoding="utf-8")[-12000:])
            except Exception as e: parts.append(str(e))
        else: parts.append("No SWING learning report yet.")
        txt="\n".join(parts)
        if self.learning_text.get("1.0","end-1c")!=txt:
            self.learning_text.delete("1.0","end"); self.learning_text.insert("1.0",txt)
    def _update_intel_text(self,intel):
        lines=[f"Status: {intel.get('status','NOT CONNECTED')}",f"Bias: {intel.get('bias','—')}",f"Impact: {intel.get('impact','—')}",f"Confidence: {intel.get('confidence','—')}",f"Horizon: {intel.get('horizon','—')}",f"Critical risk: {intel.get('critical_risk','—')}",f"Last update: {_time(intel.get('last_update'))}",f"Relevant events: {intel.get('event_count',0)}","","SOURCE HEALTH:"]
        for name,status in (intel.get("sources") or {}).items(): lines.append(f"  {name:<12} {status}")
        lines.append(""); lines.append("ASSET INTELLIGENCE:")
        for a in intel.get("assets") or []:
            lines.append(f"  {a.get('symbol',''):<7} {a.get('bias','NEUTRAL'):<8} score {float(a.get('score',0)):+5.0f}  impact {float(a.get('impact',0)):3.0f}  conf {float(a.get('confidence',0)):3.0f}%  {a.get('horizon','—'):<5}  {a.get('event','')}")
        lines.append(""); lines.append("RECENT RELEVANT NEWS:")
        for e in (intel.get("events") or [])[:12]:
            assets=",".join(e.get("assets") or [])
            lines.append(f"  [{e.get('source','')}] {assets:<12} {e.get('category',''):<18} {e.get('title','')}")
        if intel.get("note"):
            lines.append(""); lines.append(str(intel.get("note")))
        txt="\n".join(lines)
        if self.intel_text.get("1.0","end-1c")!=txt:
            self.intel_text.delete("1.0","end");self.intel_text.insert("1.0",txt)
    def _update_log(self):
        path=data_directory()/"logs"/"engine.log"
        if not path.exists():return
        try:
            lines=path.read_text(encoding="utf-8",errors="replace").splitlines()[-220:]; text="\n".join(lines)
            if self.log_text.get("1.0","end-1c")!=text:
                self.log_text.delete("1.0","end");self.log_text.insert("1.0",text);self.log_text.see("end")
        except Exception:pass
    def _close(self):
        self.worker.stop(); self.destroy()


def main():
    log=data_directory()/"logs"; log.mkdir(parents=True,exist_ok=True)
    try: App().mainloop()
    except Exception as e:
        (log/"hmi-crash.log").write_text(f"{datetime.now().isoformat()} {type(e).__name__}: {e}\n",encoding="utf-8")
        raise

if __name__=="__main__":main()
