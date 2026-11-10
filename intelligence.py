from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from config import AppConfig
from store import data_directory

VERSION = "1.6.0"
REQUEST_TIMEOUT = 8
MAX_BYTES = 1_500_000

SOURCE_QUALITY = {
    "SEC": 100.0,
    "CFTC": 100.0,
    "BINANCE": 92.0,
    "COINDESK": 78.0,
    "GDELT": 58.0,
}

ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc"), "ETH": ("ethereum", "ether", "eth"), "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"), "BNB": ("bnb", "binance coin"), "DOGE": ("dogecoin", "doge"),
    "ADA": ("cardano", "ada"), "LINK": ("chainlink",), "AVAX": ("avalanche", "avax"),
    "SUI": ("sui",), "TON": ("toncoin",), "TRX": ("tron", "trx"), "DOT": ("polkadot",),
    "LTC": ("litecoin", "ltc"), "BCH": ("bitcoin cash", "bch"), "HBAR": ("hedera", "hbar"),
    "XLM": ("stellar", "xlm"), "APT": ("aptos",), "ARB": ("arbitrum",), "OP": ("optimism",),
    "NEAR": ("near protocol",), "TAO": ("bittensor", "tao"), "RENDER": ("render token", "render network"),
    "HYPE": ("hyperliquid", "hype"), "PEPE": ("pepe",), "AAVE": ("aave",), "UNI": ("uniswap",),
    "ATOM": ("cosmos", "atom"), "FIL": ("filecoin",), "ICP": ("internet computer",),
    "ETC": ("ethereum classic",), "POL": ("polygon",), "MATIC": ("polygon", "matic"),
    "SHIB": ("shiba inu", "shib"), "TIA": ("celestia", "tia"), "INJ": ("injective", "inj"),
}

@dataclass
class EventRule:
    category: str
    direction: int
    importance: float
    horizon: str
    critical: bool = False

RULES: list[tuple[tuple[str, ...], EventRule]] = [
    (("hack", "exploit", "drained", "security breach", "compromised", "attack"), EventRule("SECURITY_INCIDENT", -1, 100, "BOTH", True)),
    (("depeg", "de-pegged", "loses peg", "lost peg"), EventRule("DEPEG", -1, 100, "BOTH", True)),
    (("bankruptcy", "insolvent", "insolvency", "withdrawals suspended", "withdrawal suspended"), EventRule("INSOLVENCY", -1, 98, "BOTH", True)),
    (("network halt", "network halted", "outage", "chain halted", "downtime"), EventRule("NETWORK_OUTAGE", -1, 92, "BOTH", True)),
    (("delist", "delisting", "will remove", "removal of spot trading pairs", "removal of margin trading pairs"), EventRule("DELISTING", -1, 86, "BOTH", False)),
    (("charges", "charged", "lawsuit", "sues", "enforcement action", "investigation"), EventRule("ENFORCEMENT", -1, 66, "SWING", False)),
    (("token unlock", "unlock schedule", "vesting unlock"), EventRule("TOKEN_UNLOCK", -1, 45, "SWING", False)),
    (("etf approved", "approves etf", "approval of", "approved spot", "permits in-kind"), EventRule("ETF_APPROVAL", 1, 92, "BOTH", False)),
    (("will list", "new cryptocurrency listing", "lists ", "launch perpetual", "perpetual contract", "new trading pair"), EventRule("LISTING", 1, 72, "DAY", False)),
    (("mainnet", "protocol upgrade", "network upgrade", "hard fork", "successful upgrade"), EventRule("UPGRADE", 1, 58, "SWING", False)),
    (("partnership", "strategic partnership", "integration", "institutional adoption", "adopts "), EventRule("ADOPTION", 1, 52, "SWING", False)),
    (("buyback", "token burn", "burn program"), EventRule("SUPPLY_REDUCTION", 1, 50, "SWING", False)),
]

CRYPTO_TERMS = ("crypto", "cryptocurrency", "bitcoin", "ethereum", "solana", "xrp", "stablecoin", "token", "blockchain", "digital asset")

@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published_utc: str | None = None
    summary: str = ""

    @property
    def key(self) -> str:
        raw=(self.source+"|"+self.title.strip().lower()+"|"+self.url.strip().lower()).encode("utf-8", errors="ignore")
        return hashlib.sha256(raw).hexdigest()[:20]

class _BinanceParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_link=False; self.href=""; self.buf=[]; self.items=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a": return
        href=dict(attrs).get("href") or ""
        if "/support/announcement/detail/" in href:
            self.in_link=True; self.href=href; self.buf=[]
    def handle_data(self, data):
        if self.in_link: self.buf.append(data)
    def handle_endtag(self, tag):
        if tag.lower()=="a" and self.in_link:
            title=" ".join("".join(self.buf).split())
            if len(title)>=12:
                url=self.href if self.href.startswith("http") else "https://www.binance.com"+self.href
                self.items.append((title,url))
            self.in_link=False; self.href=""; self.buf=[]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)+"\n", encoding="utf-8")
    tmp.replace(path)


def _parse_date(text: str | None) -> datetime | None:
    if not text: return None
    s=str(text).strip()
    try:
        d=parsedate_to_datetime(s)
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: pass
    try:
        d=datetime.fromisoformat(s.replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try: return datetime.strptime(s,fmt).replace(tzinfo=timezone.utc)
        except Exception: pass
    m=re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if m:
        try: return datetime(int(m.group(1)),int(m.group(2)),int(m.group(3)),tzinfo=timezone.utc)
        except Exception: pass
    return None


def _age_freshness(published: datetime | None, now: datetime) -> float:
    if published is None: return 45.0
    hours=max(0.0,(now-published).total_seconds()/3600)
    if hours<=1: return 100.0
    if hours<=4: return 88.0
    if hours<=12: return 68.0
    if hours<=24: return 48.0
    if hours<=48: return 25.0
    return 10.0


def _http_get(url: str, *, timeout: int=REQUEST_TIMEOUT, accept: str="*/*") -> bytes:
    ctx=ssl.create_default_context(); ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    req=urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) BitunixDayTrader/1.5 MarketIntelligence",
        "Accept":accept,
        "Accept-Language":"en-US,en;q=0.8",
        "Cache-Control":"no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data=r.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise RuntimeError("response too large")
    return data


def _xml_text(node: ET.Element, names: Iterable[str]) -> str:
    for child in node.iter():
        tag=child.tag.split("}")[-1].lower()
        if tag in names and child.text:
            return " ".join(child.text.split())
    return ""


def parse_rss(data: bytes, source: str) -> list[NewsItem]:
    root=ET.fromstring(data)
    out=[]
    entries=[x for x in root.iter() if x.tag.split("}")[-1].lower() in ("item","entry")]
    for e in entries[:80]:
        title=_xml_text(e,{"title"}); summary=_xml_text(e,{"description","summary","content"}); link=_xml_text(e,{"link"})
        if not link:
            for ch in e.iter():
                if ch.tag.split("}")[-1].lower()=="link" and ch.attrib.get("href"):
                    link=ch.attrib["href"]; break
        date=_xml_text(e,{"pubdate","published","updated","date"})
        if title: out.append(NewsItem(source,title,link,_parse_date(date).isoformat() if _parse_date(date) else None,summary))
    return out


def fetch_gdelt(hours: int=12, maxrecords: int=50) -> list[NewsItem]:
    q='(crypto OR cryptocurrency OR bitcoin OR ethereum OR solana OR XRP OR stablecoin) (hack OR exploit OR ETF OR listing OR delisting OR regulation OR outage OR upgrade OR partnership OR depeg OR bankruptcy)'
    params={"query":q,"mode":"artlist","maxrecords":str(maxrecords),"timespan":f"{max(1,hours)}h","sort":"datedesc","format":"json"}
    url="https://api.gdeltproject.org/api/v2/doc/doc?"+urllib.parse.urlencode(params)
    raw=json.loads(_http_get(url,accept="application/json").decode("utf-8-sig"))
    arts=raw.get("articles",[]) if isinstance(raw,dict) else []
    out=[]
    for a in arts:
        if not isinstance(a,dict): continue
        title=str(a.get("title") or "").strip(); u=str(a.get("url") or "").strip(); seen=str(a.get("seendate") or "")
        if title: out.append(NewsItem("GDELT",title,u,_parse_date(seen).isoformat() if _parse_date(seen) else None,str(a.get("domain") or "")))
    return out


def fetch_rss_sources() -> tuple[list[NewsItem], dict[str,str]]:
    feeds={
        "SEC":"https://www.sec.gov/news/pressreleases.rss",
        "CFTC":"https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        "CFTC_ENF":"https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
        "COINDESK":"https://www.coindesk.com/arc/outboundfeeds/rss/",
    }
    items=[]; status={}
    for label,url in feeds.items():
        source="CFTC" if label.startswith("CFTC") else label
        try:
            rows=parse_rss(_http_get(url,accept="application/rss+xml, application/xml, text/xml, */*"),source)
            items.extend(rows); status[label]=f"CONNECTED ({len(rows)})"
        except Exception as e:
            status[label]=f"UNAVAILABLE: {type(e).__name__}"
    return items,status


def fetch_binance() -> list[NewsItem]:
    data=_http_get("https://www.binance.com/en/support/announcement",accept="text/html,application/xhtml+xml")
    p=_BinanceParser(); p.feed(data.decode("utf-8",errors="ignore"))
    seen=set(); out=[]
    for title,url in p.items:
        k=(title,url)
        if k in seen: continue
        seen.add(k)
        dt=_parse_date(title)
        out.append(NewsItem("BINANCE",unescape(title),url,dt.isoformat() if dt else None,""))
        if len(out)>=50: break
    return out


def _asset_matches(text: str) -> list[str]:
    low=" "+re.sub(r"[^a-z0-9$]+"," ",text.lower())+" "
    found=[]
    for asset,aliases in ASSET_ALIASES.items():
        for alias in aliases:
            a=alias.lower()
            if " " in a:
                if a in low: found.append(asset); break
            elif re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", low):
                found.append(asset); break
    return sorted(set(found))


def classify_item(item: NewsItem, now: datetime | None=None) -> dict[str,Any] | None:
    now=now or _utcnow(); text=(item.title+" "+item.summary).lower()
    if item.source in ("SEC","CFTC") and not any(t in text for t in CRYPTO_TERMS): return None
    rule=None
    for words,r in RULES:
        if any(w in text for w in words): rule=r; break
    if rule is None:
        if not any(t in text for t in CRYPTO_TERMS): return None
        rule=EventRule("GENERAL_CRYPTO",0,24,"BOTH",False)
    published=_parse_date(item.published_utc); freshness=_age_freshness(published,now); assets=_asset_matches(item.title+" "+item.summary)
    if not assets: assets=["MARKET"]
    return {
        "key":item.key,"source":item.source,"title":item.title,"url":item.url,"published_utc":item.published_utc,
        "category":rule.category,"direction":rule.direction,"importance":rule.importance,"horizon":rule.horizon,
        "critical":rule.critical,"source_quality":SOURCE_QUALITY.get(item.source,55.0),"freshness":freshness,"assets":assets,
    }


def _dedupe(events: list[dict[str,Any]]) -> list[dict[str,Any]]:
    seen=set(); out=[]
    for e in events:
        sig=re.sub(r"\W+"," ",str(e["title"]).lower()).strip()
        sig=" ".join(sig.split()[:16])
        key=(e["source"],sig)
        if key in seen: continue
        seen.add(key); out.append(e)
    return out


def build_state(items: list[NewsItem], source_status: dict[str,str], now: datetime | None=None) -> dict[str,Any]:
    now=now or _utcnow(); events=_dedupe([x for x in (classify_item(i,now) for i in items) if x])
    # corroboration is deliberately simple: independent sources reporting same asset+event category.
    groups: dict[tuple[str,str], set[str]]={}
    for e in events:
        for a in e["assets"]: groups.setdefault((a,e["category"]),set()).add(e["source"])
    for e in events:
        counts=[len(groups.get((a,e["category"]),set())) for a in e["assets"]]
        n=max(counts or [1]); corr=45.0 if n<=1 else (72.0 if n==2 else 90.0)
        e["corroboration"]=corr
        e["confidence"]=round(0.35*e["source_quality"]+0.25*e["freshness"]+0.25*corr+0.15*e["importance"],1)
        e["event_score"]=round(e["importance"]*(0.55+0.45*e["freshness"]/100.0),1)

    asset_rows=[]; critical=[]
    assets=sorted({a for e in events for a in e["assets"]})
    for asset in assets:
        ev=[e for e in events if asset in e["assets"]]
        if not ev: continue
        weights=[max(1.0,e["event_score"]*e["confidence"]/100.0) for e in ev]
        signed=sum(w*int(e["direction"]) for w,e in zip(weights,ev)); total=sum(weights)
        direction_score=100.0*signed/total if total else 0.0
        impact=min(100.0,max((float(e["event_score"]) for e in ev),default=0.0)+min(18.0,4.0*(len(ev)-1)))
        confidence=min(100.0,sum(float(e["confidence"])*w for e,w in zip(ev,weights))/max(total,1e-9)+min(10.0,3.0*(len({e["source"] for e in ev})-1)))
        if abs(direction_score)<12: bias="NEUTRAL"
        else: bias="BULLISH" if direction_score>0 else "BEARISH"
        dominant=max(ev,key=lambda x: x["event_score"]*x["confidence"])
        row={"symbol":asset,"score":round(direction_score,1),"bias":bias,"impact":round(impact,1),"confidence":round(confidence,1),"horizon":dominant["horizon"],"event":dominant["category"]}
        asset_rows.append(row)
        for e in ev:
            if e["critical"] and e["direction"]<0 and e["event_score"]>=80 and e["confidence"]>=78:
                critical.append({"symbol":asset,"direction":"BEARISH","category":e["category"],"impact":e["event_score"],"confidence":e["confidence"],"title":e["title"]})
    asset_rows.sort(key=lambda a:(a["impact"]*a["confidence"],abs(a["score"])),reverse=True)
    market=next((a for a in asset_rows if a["symbol"]=="MARKET"),None)
    tradable=[a for a in asset_rows if a["symbol"]!="MARKET"]
    if market:
        overall_score=float(market["score"]); overall_impact=float(market["impact"]); overall_conf=float(market["confidence"]); horizon=str(market["horizon"])
    elif tradable:
        top=tradable[:8]; den=sum(max(10.0,float(a["impact"])) for a in top); overall_score=sum(float(a["score"])*max(10.0,float(a["impact"])) for a in top)/den if den else 0.0; overall_impact=sum(float(a["impact"]) for a in top)/len(top); overall_conf=sum(float(a["confidence"]) for a in top)/len(top); horizon="BOTH"
    else:
        overall_score=overall_impact=overall_conf=0.0; horizon="—"
    overall_bias="NEUTRAL" if abs(overall_score)<12 else ("BULLISH" if overall_score>0 else "BEARISH")
    ok=sum(1 for x in source_status.values() if str(x).startswith("CONNECTED")); total=max(1,len(source_status)); status="ACTIVE" if ok==total else ("PARTIAL" if ok else "UNAVAILABLE")
    critical_text="NONE" if not critical else f"{critical[0]['symbol']} {critical[0]['category']}"
    recent=sorted(events,key=lambda e:_parse_date(e.get("published_utc")) or datetime(1970,1,1,tzinfo=timezone.utc),reverse=True)[:20]
    return {
        "schema":1,"version":VERSION,"status":status,"bias":overall_bias,"score":round(overall_score,1),"impact":round(overall_impact,1),"confidence":round(overall_conf,1),"horizon":horizon,"critical_risk":critical_text,
        "assets":asset_rows[:20],"critical_blocks":critical[:10],"sources":source_status,"event_count":len(events),"last_update":now.isoformat(),"events":recent,
        "note":"Free intelligence only. No X API. News adjusts ranking; only confirmed critical risk can block a contradictory new entry.",
    }


def load_state(out_dir: Path | None=None) -> dict[str,Any]:
    path=(out_dir or data_directory())/"intelligence-state.json"
    try:
        raw=json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw,dict) else {}
    except Exception:
        return {}


def research_once(cfg: AppConfig | None=None, out_dir: Path | None=None) -> dict[str,Any]:
    cfg=cfg or AppConfig(); out_dir=out_dir or data_directory(); source_status={}; items=[]
    if not cfg.intelligence.enabled:
        state={"schema":1,"version":VERSION,"status":"DISABLED","bias":"NEUTRAL","impact":0,"confidence":0,"horizon":"—","critical_risk":"NONE","assets":[],"sources":{},"events":[],"last_update":_utcnow().isoformat()}
        _atomic_json(out_dir/"intelligence-state.json",state); return state
    rss,status=fetch_rss_sources(); items.extend(rss); source_status.update(status)
    try:
        b=fetch_binance(); items.extend(b); source_status["BINANCE"]=f"CONNECTED ({len(b)})"
    except Exception as e: source_status["BINANCE"]=f"UNAVAILABLE: {type(e).__name__}"
    try:
        g=fetch_gdelt(cfg.intelligence.lookback_hours,cfg.intelligence.gdelt_max_records); items.extend(g); source_status["GDELT"]=f"CONNECTED ({len(g)})"
    except Exception as e: source_status["GDELT"]=f"UNAVAILABLE: {type(e).__name__}"
    state=build_state(items,source_status)
    _atomic_json(out_dir/"intelligence-state.json",state)
    return state


class IntelligenceWorker(threading.Thread):
    def __init__(self, cfg: AppConfig, out_dir: Path):
        super().__init__(daemon=True); self.cfg=cfg; self.out_dir=out_dir; self.stop_event=threading.Event()
    def stop(self): self.stop_event.set()
    def run(self):
        interval=max(60,int(self.cfg.intelligence.research_interval_minutes*60))
        while not self.stop_event.is_set():
            try: research_once(self.cfg,self.out_dir)
            except Exception as e:
                _atomic_json(self.out_dir/"intelligence-state.json",{"schema":1,"version":VERSION,"status":"ERROR","bias":"NEUTRAL","impact":0,"confidence":0,"horizon":"—","critical_risk":"NONE","assets":[],"sources":{},"events":[],"last_update":_utcnow().isoformat(),"error":f"{type(e).__name__}: {e}"})
            if self.stop_event.wait(interval): break


def main() -> None:
    from config import load_config
    d=data_directory(); cfg=load_config(d/"settings.json"); state=research_once(cfg,d)
    print(json.dumps({k:state.get(k) for k in ("status","bias","impact","confidence","horizon","critical_risk","last_update")},indent=2))
    print("Sources:")
    for k,v in (state.get("sources") or {}).items(): print(f"  {k}: {v}")
    print(f"Saved: {d/'intelligence-state.json'}")

if __name__=="__main__": main()
