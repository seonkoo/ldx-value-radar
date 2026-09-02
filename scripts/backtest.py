#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄价值雷达 —— 点位回溯（point-in-time）组合回测

设计要点
--------
1. 无前视偏差：每个再平衡日 T 只用 T 日及之前可得的数据
   - 价格：新浪前复权日K（含分红再投）
   - PE/PB：百度估值历史，取 <= T 的最近值
   - 股息率：(T-365, T] 窗口内【已除权除息】的每股派息 / T 日价格
   - 连续分红年 / 分红稳定性：只用 T 年及之前的年报派息
2. 成熟度守卫（借鉴 daily_stock_analysis 的 min_age_days）：
   最后一期若持有期未走完，标记 insufficient，不计入已完成统计。
3. 三方案对比：
   A 纯全池（旧算法，银行霸榜）
   B 混合 0.7（现网算法）
   C 纯行业内（去霸榜最激进）
4. 已知偏差（必须披露）：
   - 幸存者偏差：用【当前】沪深300 成分股回溯历史，无法获取历史成分股
     （akshare 无 index_stock_hist_csindex），会高估收益。
   - 基准为沪深300【价格指数】，而策略用前复权价（含分红再投），
     策略相对优势被高估约等于指数股息率（约 2-3%/年）。

用法
----
python scripts/backtest.py                    # 默认 2021-09~2026-09，季度再平衡，Top20
python scripts/backtest.py --top 10 --freq M  # 月度再平衡、Top10
python scripts/backtest.py --no-cache         # 强制重新抓取
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CACHE = "/tmp/ldx_bt_cache.pkl"
DEFAULT_START = "2021-09-30"
MIN_DIV_YEARS = 5      # 与主工具一致：连续分红 >= 5 年
MAX_PE = 50


def log(m=""):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


# ============================================================ 数据抓取（带缓存）
def _sina_code(code: str) -> str:
    c = str(code).zfill(6)
    return ("sh" if c[0] == "6" else "sz") + c


def fetch_all(codes, start, force=False):
    """抓取历史行情 / 估值 / 分红 / 基准，结果缓存到 CACHE。"""
    if not force and os.path.exists(CACHE):
        try:
            d = pickle.load(open(CACHE, "rb"))
            if len(d.get("px", {})) >= len(codes) * 0.9:
                log(f"命中缓存 {CACHE}（{len(d['px'])} 只），跳过抓取")
                return d
        except Exception as e:
            log(f"  缓存读取失败，重新抓取: {e}")

    import akshare as ak
    px, pe, pb = {}, {}, {}
    t0 = time.time()

    def one(code):
        out = {"code": code, "px": None, "pe": None, "pb": None, "err": None}
        try:
            p = ak.stock_zh_a_daily(symbol=_sina_code(code), adjust="qfq")
            p["date"] = pd.to_datetime(p["date"])
            out["px"] = p[p["date"] >= pd.Timestamp(start) - pd.Timedelta(days=400)][
                ["date", "close"]].reset_index(drop=True)
        except Exception as e:
            out["err"] = f"px:{type(e).__name__}"
        for key, ind in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
            try:
                v = ak.stock_zh_valuation_baidu(symbol=str(code).zfill(6),
                                                indicator=ind, period="近五年")
                v["date"] = pd.to_datetime(v["date"])
                v["value"] = pd.to_numeric(v["value"], errors="coerce")
                out[key] = v[["date", "value"]].dropna().reset_index(drop=True)
            except Exception as e:
                out["err"] = (out["err"] or "") + f" {key}:{type(e).__name__}"
        return out

    log(f"抓取 {len(codes)} 只：行情(新浪qfq) + PE/PB(百度近五年)...")
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(one, c): c for c in codes}
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if r["px"] is not None and not r["px"].empty:
                px[r["code"]] = r["px"]
            if r["pe"] is not None:
                pe[r["code"]] = r["pe"]
            if r["pb"] is not None:
                pb[r["code"]] = r["pb"]
            if done % 40 == 0:
                log(f"  {done}/{len(codes)}  ({time.time()-t0:.0f}s)")

    # 分红（按报告期，含半年报）
    # 必须抓够深：规则要求"连续分红>=5年"，若只抓 7 年，则回测起点那几年
    # 永远凑不满 5 年，会把全部候选杀光（2021-2023 期候选归零就是这个原因）。
    log("抓取分红历史...")
    y_now = datetime.now().year
    periods = []
    for y in range(y_now - 12, y_now + 1):
        periods.append(f"{y}1231")
        if y < y_now:
            periods.append(f"{y}0630")
    frames = []
    for p in periods:
        try:
            df = ak.stock_fhps_em(date=p)
            df["报告期"] = p
            keep = [c for c in ["代码", "现金分红-现金分红比例", "除权除息日", "报告期"]
                    if c in df.columns]
            frames.append(df[keep])
        except Exception:
            pass
        time.sleep(0.2)
    div = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not div.empty:
        div = div.drop_duplicates(subset=["代码", "报告期", "现金分红-现金分红比例", "除权除息日"])
        div["派息比例"] = pd.to_numeric(div.get("现金分红-现金分红比例"), errors="coerce").fillna(0)
        div["除权日"] = pd.to_datetime(div.get("除权除息日"), errors="coerce")
        div["年"] = div["报告期"].str[:4].astype(int)
    log(f"  分红 {len(div)} 条")

    bench = pd.DataFrame()
    try:
        b = ak.stock_zh_index_daily(symbol="sh000300")
        b["date"] = pd.to_datetime(b["date"])
        bench = b[["date", "close"]].reset_index(drop=True)
        log(f"  沪深300基准 {len(bench)} 根")
    except Exception as e:
        log(f"  [WARN] 基准获取失败: {e}")

    data = {"px": px, "pe": pe, "pb": pb, "div": div, "bench": bench,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        pickle.dump(data, open(CACHE, "wb"))
        log(f"缓存已写入 {CACHE}")
    except Exception as e:
        log(f"  [WARN] 缓存写入失败: {e}")
    return data


# ============================================================ 点位回溯特征
def _asof(series: pd.DataFrame, when: pd.Timestamp, tol_days=30):
    """取 <= when 的最近一条；超过 tol_days 视为缺失（避免用过于陈旧的数据）"""
    if series is None or series.empty:
        return np.nan
    s = series[series["date"] <= when]
    if s.empty:
        return np.nan
    if (when - s["date"].iloc[-1]).days > tol_days:
        return np.nan
    return float(s["value"].iloc[-1])


def _price_at(px: pd.DataFrame, when: pd.Timestamp, tol_days=15):
    if px is None or px.empty:
        return np.nan
    s = px[px["date"] <= when]
    if s.empty:
        return np.nan
    if (when - s["date"].iloc[-1]).days > tol_days:
        return np.nan
    return float(s["close"].iloc[-1])


def pt_dividend_features(div: pd.DataFrame, when: pd.Timestamp):
    """点位回溯版：只用 when 及之前的信息算 每股派息(TTM)/连续分红年/稳定性"""
    if div is None or div.empty:
        return {}, {}
    win_start = when - pd.Timedelta(days=365)
    recent = div[(div["除权日"].notna()) & (div["除权日"] > win_start)
                 & (div["除权日"] <= when) & (div["派息比例"] > 0)]
    dps = (recent.groupby("代码")["派息比例"].sum() / 10.0).to_dict()

    cur_year = when.year
    annual = div[(div["报告期"].astype(str).str.endswith("1231"))
                 & (div["派息比例"] > 0) & (div["年"] <= cur_year)]
    years = sorted(annual["年"].unique())
    cont, stab = {}, {}
    for code, g in annual.groupby("代码"):
        ys = set(g["年"])
        n = 0
        for y in sorted(years, reverse=True):
            if y in ys:
                n += 1
            else:
                break
        cont[code] = n
        v = g.sort_values("年")["派息比例"].values
        if len(v) >= 3 and v.mean() > 0:
            cv = v.std(ddof=0) / v.mean()
            stab[code] = float(max(0.0, min(1.0, 1 - cv)) * 100)
        else:
            stab[code] = 0.0
    return dps, {"cont": cont, "stab": stab}


# ============================================================ 打分（三方案）
def _pct(s: pd.Series, higher_better=True):
    return s.rank(pct=True, ascending=higher_better) * 100


def _within(s: pd.Series, grp: pd.Series, higher_better=True):
    df = pd.DataFrame({"v": s, "g": grp})
    pooled = df["v"].rank(pct=True, ascending=higher_better) * 100
    out = pd.Series(index=s.index, dtype=float)
    for _g, gg in df.groupby("g")["v"]:
        if len(gg) >= 2:
            out.loc[gg.index] = gg.rank(pct=True, ascending=higher_better) * 100
        else:
            out.loc[gg.index] = pooled.loc[gg.index]
    return out


def _blend(s, grp, higher_better, w):
    if w >= 1.0:
        return _within(s, grp, higher_better)
    if w <= 0.0:
        return _pct(s, higher_better)
    return w * _within(s, grp, higher_better) + (1 - w) * _pct(s, higher_better)


def score_frame(df: pd.DataFrame, w: float):
    """df 需含 股息率/PE/PB/稳定性/行业"""
    return (0.35 * _blend(df["股息率"], df["行业"], True, w)
            + 0.25 * _blend(df["PE"], df["行业"], False, w)
            + 0.25 * _blend(df["PB"], df["行业"], False, w)
            + 0.15 * _blend(df["稳定性"], df["行业"], True, w))


# ============================================================ 指标
def metrics(nav: pd.Series, bench: pd.Series = None):
    nav = nav.dropna()
    if len(nav) < 2:
        return {}
    r = nav.pct_change().dropna()
    total = nav.iloc[-1] / nav.iloc[0] - 1
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = nav / nav.cummax() - 1
    mdd = dd.min()
    vol = r.std() * np.sqrt(4)          # 季度频率 -> 年化
    sharpe = (r.mean() * 4 - 0.02) / (r.std() * np.sqrt(4)) if r.std() > 0 else np.nan
    out = {"总收益%": round(total * 100, 1), "年化%": round(cagr * 100, 1),
           "最大回撤%": round(mdd * 100, 1), "年化波动%": round(vol * 100, 1),
           "夏普": round(float(sharpe), 2), "期数": len(r),
           "胜率%": round(float((r > 0).mean() * 100), 1)}
    if bench is not None and len(bench) > 1:
        b = bench.dropna()
        bt = b.iloc[-1] / b.iloc[0] - 1
        out["基准总收益%"] = round(bt * 100, 1)
        out["超额%"] = round((total - bt) * 100, 1)
    return out


# ============================================================ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--freq", default="QE", help="QE=季末, ME=月末, YE=年末")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest.json"))
    ap.add_argument("--blends", default="0,0.7,1",
                    help="混合系数列表：0=纯全池(旧) 1=纯行业内，逗号分隔，可扫参")
    a = ap.parse_args()

    blends = [float(x) for x in a.blends.split(",") if x.strip() != ""]
    variants = {}
    for w in blends:
        tag = "纯全池(旧)" if w == 0 else ("纯行业内" if w == 1 else f"混合{w}")
        variants[f"w={w:g} {tag}"] = w

    start, end = pd.Timestamp(a.start), pd.Timestamp(a.end)

    # 1) universe
    import build_data as bd
    log("拉取沪深300 成分股...")
    pool = bd.fetch_pool()
    codes = pool["品种代码"].tolist()
    log(f"  成分股 {len(codes)} 只")

    # 行业映射
    try:
        import deep_analysis as da
        im = da.fetch_industry_map()
        log(f"  行业映射 {len(im)} 只")
    except Exception as e:
        log(f"  [WARN] 行业映射失败: {e}")
        im = {}

    # 2) 数据
    data = fetch_all(codes, a.start, force=a.no_cache)
    px, pe, pb, div, bench = data["px"], data["pe"], data["pb"], data["div"], data["bench"]

    # 3) 再平衡日
    reb = pd.date_range(start, end, freq=a.freq)
    log(f"\n再平衡日 {len(reb)} 个：{reb[0].date()} ~ {reb[-1].date()}  持仓 Top{a.top}")

    picks = {k: [] for k in variants}
    # 延迟初始化：以【第一个有效期】为基准点，避免被跳过的期造成时间轴错位
    navs = {k: [] for k in variants}
    dates, bench_nav, rows = [], [], []
    skipped = 0

    for i, T in enumerate(reb[:-1]):
        Tn = reb[i + 1]
        dps, extra = pt_dividend_features(div, T)
        recs = []
        for c in codes:
            p = _price_at(px.get(c), T)
            e = _asof(pe.get(c), T)
            b = _asof(pb.get(c), T)
            if not (np.isfinite(p) and p > 0 and np.isfinite(e) and np.isfinite(b)):
                continue
            if not (0 < e <= MAX_PE) or b <= 0:
                continue
            dy = dps.get(c, 0.0)
            if dy <= 0:
                continue
            if extra.get("cont", {}).get(c, 0) < MIN_DIV_YEARS:
                continue
            recs.append({"代码": c, "价格": p, "PE": e, "PB": b,
                         "股息率": dy / p * 100, "稳定性": extra.get("stab", {}).get(c, 0.0),
                         "行业": im.get(str(c).zfill(6), "未知")})
        if len(recs) < a.top * 2:
            log(f"  {T.date()} 候选仅 {len(recs)} 只，跳过")
            skipped += 1
            continue

        # 首个有效期为基准点（nav=1.0），其后每期追加，保证时间轴与收益严格对齐
        if not dates:
            dates.append(T)
            for k in variants:
                navs[k].append(1.0)
            bench_nav.append(1.0)

        df = pd.DataFrame(recs).set_index("代码")
        # 持有期收益
        ret = {}
        for c in df.index:
            p0 = _price_at(px.get(c), T)
            p1 = _price_at(px.get(c), Tn)
            ret[c] = (p1 / p0 - 1) if (np.isfinite(p0) and np.isfinite(p1) and p0 > 0) else np.nan

        row = {"日期": str(T.date()), "候选": len(df)}
        for name, w in variants.items():
            sc = score_frame(df, w)
            top = sc.sort_values(ascending=False).head(a.top).index
            r = pd.Series(ret).reindex(top).dropna()
            picks[name].append(list(top))
            if len(r) > 0:
                pr = float(r.mean())
                navs[name].append(navs[name][-1] * (1 + pr))
                row[name] = round(pr * 100, 2)
            else:
                navs[name].append(navs[name][-1])
                row[name] = None
        # 基准
        b0, b1 = _price_at(bench, T, 20), _price_at(bench, Tn, 20)
        if np.isfinite(b0) and np.isfinite(b1) and b0 > 0:
            bench_nav.append(bench_nav[-1] * (b1 / b0))
            row["基准%"] = round((b1 / b0 - 1) * 100, 2)
        else:
            bench_nav.append(bench_nav[-1])
        dates.append(Tn)
        rows.append(row)
        log(f"  {T.date()} 候选{len(df):>3} -> " +
            " | ".join(f"{k.split('_')[0]} {v:+.1f}%" for k, v in row.items()
                       if k in variants and v is not None) +
            f" | 基准 {row.get('基准%')}%")

    # 4) 汇总
    idx = pd.DatetimeIndex(dates)
    bn = pd.Series(bench_nav, index=idx)
    print("\n" + "=" * 78)
    print(f"回测区间 {start.date()} ~ {end.date()}   再平衡 {a.freq}   等权 Top{a.top}")
    print("=" * 78)
    res = {}
    for name in variants:
        nv = pd.Series(navs[name], index=idx)
        m = metrics(nv, bn)
        res[name] = m
        print(f"\n【{name}】")
        print("  " + "  ".join(f"{k}={v}" for k, v in m.items()))
    bm = metrics(bn)
    print(f"\n【基准 沪深300(价格指数)】")
    print("  " + "  ".join(f"{k}={v}" for k, v in bm.items()))
    print("\n⚠️ 偏差提示：①幸存者偏差（用当前成分股回溯）会高估收益；"
          "②基准为价格指数、策略为前复权（含分红），优势被高估约 2-3%/年")

    out = {"meta": {"start": str(start.date()), "end": str(end.date()),
                    "freq": a.freq, "top": a.top,
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "note": "幸存者偏差+基准为价格指数，收益被高估"},
           "variants": res, "benchmark": bm, "periods": rows}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"\n结果已写入 {a.out}")


if __name__ == "__main__":
    main()
