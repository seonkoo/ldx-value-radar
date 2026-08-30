#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄《李大霄投资战略》理念 —— 蓝筹价值选股雷达 · 数据构建脚本

设计原则（严格对应书中四条主线）：
  1. 选股：低 PE + 破净/低 PB + 高股息 + 持续分红 + 大盘蓝筹
  2. 排雷：硬排除"黑五类"——小盘股 / 次新股 / 垃圾股 / 题材股 / 伪成长股
  3. 择时：沪深300 估值历史分位（钻石底 / 婴儿底 / 正常 / 地球顶）
  4. 仓位：按估值分位给出正金字塔建仓建议

数据源（全部已实测可用）：
  - 蓝筹池       ak.index_stock_cons('000300')      沪深300 成分股
  - 行情估值     腾讯 qt.gtimg.cn                    PE(TTM)/PB/总市值/价格（已与百度 PE(TTM) 交叉校验）
  - 分红派息     ak.stock_fhps_em                    近12个月实际派息 + 连续分红年数 + 分红稳定性
  - 大盘估值     ak.stock_index_pe_lg('沪深300')     2005 年至今滚动 PE，用于计算历史分位
  - 产业资本     ak.stock_ggcg_em                    股东增减持（国家队/大股东动向）

口径说明（重要，勿随意改动）：
  - 股息率 = 近12个月内【除权除息日】落在窗口内的每股派息合计 / 当前股价
            而非简单取最近一期年报分红，避免漏记中期分红、或把未实施预案计入。
  - 连续分红年 = 自最新年份向前回溯，年报（12-31 报告期）连续有现金分红的年数。
  - PE 采用 PE(TTM)，PB 采用最新市净率。
"""

import json
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import akshare as ak
except ImportError as e:  # pragma: no cover
    print(f"[FATAL] 缺少依赖: {e}")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------- 配置
CONFIG = {
    "index_code": "000300",
    "index_name": "沪深300",
    # —— 黑五类硬排除阈值 ——
    "min_market_cap_yi": 200,      # 总市值下限(亿)：排除小盘股
    "max_pe": 50,                  # PE 上限：排除伪成长/题材股
    "min_pe": 0,                   # PE 必须为正：排除垃圾股(亏损)
    "min_dividend_years": 5,       # 连续分红年数：排除次新股、无稳定分红能力者
    "min_dividend_yield": 0.0,     # 股息率必须 > 0
    # —— 八步财报深度分析覆盖的标的数（每只约 3 次请求）——
    "deep_top_n": 30,
    # —— 四维打分权重（合计 100）——
    "weights": {
        "dividend": 35,   # 股息率：李大霄最看重的"得好报"
        "pe": 25,         # 低 PE
        "pb": 25,         # 低 PB / 破净
        "stability": 15,  # 分红稳定性：体现"业绩稳定"
    },
    # —— 择时分位阈值（基于 2005 年至今沪深300 滚动 PE）——
    "timing": {
        "diamond_bottom": 20,   # <20%  钻石底区域
        "baby_bottom": 35,      # 20-35% 婴儿底 / 偏低
        "normal": 65,           # 35-65% 正常区间
        "hot": 85,              # 65-85% 偏热
        # >85% 地球顶区域
    },
}

WINDOWS_DAYS = 365          # 股息率统计窗口
# 回溯 5 个完整年份 + 当前年 = 6 个年份，既可判定「连续 5 年分红」，
# 又能区分「恰好 5 年」与「超过 5 年」，再往前抓无意义且拖慢速度。
DIVIDEND_YEARS_BACK = 5


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def market_prefix(code: str) -> str:
    """根据代码推断交易所前缀"""
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("8", "4", "920")):
        return "bj" + code
    return "sz" + code


# ============================================================ 1. 蓝筹池
def fetch_pool():
    """沪深300 成分股"""
    log("拉取蓝筹池：沪深300 成分股...")
    try:
        df = ak.index_stock_cons(symbol=CONFIG["index_code"])
        df = df[["品种代码", "品种名称"]].drop_duplicates(subset=["品种代码"])
        log(f"  蓝筹池 {len(df)} 只")
        return df
    except Exception as e:
        log(f"  [WARN] 成分股获取失败: {e}")
        return pd.DataFrame(columns=["品种代码", "品种名称"])


# ============================================================ 2. 行情估值
def fetch_quote(codes):
    """
    腾讯行情批量拉取。
    字段索引（已实测）：1名称 2代码 3现价 32涨跌幅 39市盈率(TTM) 45总市值(亿) 46市净率
    """
    log(f"拉取行情估值（{len(codes)} 只）...")
    rows = {}
    batch = 100
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(market_prefix(c) for c in chunk)
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if '="' not in line:
                    continue
                f = line.split('="')[1].rstrip('";').split("~")
                if len(f) < 47:
                    continue
                try:
                    rows[f[2]] = {
                        "名称": f[1],
                        "价格": float(f[3] or 0),
                        "涨跌幅": float(f[32] or 0),
                        "PE": float(f[39] or 0),
                        "总市值亿": float(f[45] or 0),
                        "PB": float(f[46] or 0),
                    }
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            log(f"  [WARN] 行情批次 {i // batch} 失败: {e}")
        time.sleep(0.15)

    q = pd.DataFrame(rows).T
    for c in ["价格", "涨跌幅", "PE", "总市值亿", "PB"]:
        if c in q.columns:
            q[c] = pd.to_numeric(q[c], errors="coerce")
    log(f"  行情获取 {len(q)} 只")
    return q


# ============================================================ 3. 分红数据
def fetch_dividend():
    """
    抓取最近若干个报告期的分红送配。
    返回 (明细 DataFrame, 成功抓取的报告期列表)
    """
    log("拉取分红派息数据...")
    year = datetime.now().year
    periods = []
    for y in range(year - DIVIDEND_YEARS_BACK, year + 1):
        periods.append(f"{y}1231")
        if y < year:
            periods.append(f"{y}0630")

    frames = []
    ok_periods = []
    for p in periods:
        try:
            df = ak.stock_fhps_em(date=p)
            df["报告期"] = p
            keep = ["代码", "名称", "现金分红-现金分红比例", "除权除息日", "方案进度", "报告期"]
            frames.append(df[[c for c in keep if c in df.columns]])
            ok_periods.append(p)
        except Exception:
            pass
        time.sleep(0.25)

    if not frames:
        log("  [ERROR] 分红数据全部获取失败")
        return pd.DataFrame(), []

    div = pd.concat(frames, ignore_index=True)
    # 防御：同一报告期因接口翻页/重试被重复抓到时会导致股息率翻倍，此处按主键去重
    div = div.drop_duplicates(subset=["代码", "报告期", "现金分红-现金分红比例", "除权除息日"])
    div["派息比例"] = pd.to_numeric(div.get("现金分红-现金分红比例"), errors="coerce").fillna(0)
    div["除权日"] = pd.to_datetime(div.get("除权除息日"), errors="coerce")
    div["年"] = div["报告期"].str[:4].astype(int)
    log(f"  分红记录 {len(div)} 条，覆盖 {len(ok_periods)} 个报告期")
    return div, ok_periods


def compute_dividend_features(div):
    """
    返回 {代码: {每股派息, 连续分红年, 分红稳定性}}
    """
    if div.empty:
        return {}

    today = pd.Timestamp(datetime.now().date())
    win_start = today - pd.Timedelta(days=WINDOWS_DAYS)

    # —— 近12个月【已实施】派息（按除权除息日，且排除未实施的预案）——
    recent = div[
        (div["除权日"].notna())
        & (div["除权日"] >= win_start)
        & (div["除权日"] <= today)
        & (div["派息比例"] > 0)
    ]
    dps = (recent.groupby("代码")["派息比例"].sum() / 10.0).to_dict()  # 每10股派息 -> 每股

    # —— 连续分红年数（仅年报口径）——
    annual = div[(div["报告期"].str.endswith("1231")) & (div["派息比例"] > 0)]
    years = sorted(annual["年"].unique())
    cont = {}
    for code, g in annual.groupby("代码"):
        ys = set(g["年"])
        n = 0
        for y in sorted(years, reverse=True):
            if y in ys:
                n += 1
            else:
                break
        cont[code] = n

    # —— 分红稳定性：近 N 年年报派息的变异系数，越稳定分越高 ——
    stab = {}
    for code, g in annual.groupby("代码"):
        v = g.sort_values("年")["派息比例"].values
        if len(v) >= 3 and v.mean() > 0:
            cv = v.std(ddof=0) / v.mean()
            stab[code] = float(max(0.0, min(1.0, 1 - cv)) * 100)
        else:
            stab[code] = 0.0

    merged = {}
    for code in set(list(dps.keys()) + list(cont.keys())):
        merged[code] = {
            "每股派息": round(dps.get(code, 0.0), 4),
            "连续分红年": int(cont.get(code, 0)),
            "分红稳定性": round(stab.get(code, 0.0), 1),
        }
    return merged


# ============================================================ 4. 大盘估值分位
VAL_CACHE = DATA_DIR / "valuation_cache.json"


def _load_val_cache():
    """读取上次成功抓取的历史 PE 序列（供乐咕不可用时降级推算）"""
    try:
        with open(VAL_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c.get("series"), list) and len(c["series"]) >= 30:
            return c
    except Exception:
        pass
    return None


def _save_val_cache(series, last_pe, last_index, last_date):
    """主路径成功后写入缓存；缓存写入失败不影响本次构建"""
    try:
        with open(VAL_CACHE, "w", encoding="utf-8") as f:
            json.dump({
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "last_pe": round(float(last_pe), 4),
                "last_index": round(float(last_index), 4) if last_index else None,
                "last_date": str(last_date)[:10],
                "series": series,
            }, f, ensure_ascii=False)
    except Exception as e:
        log(f"  [WARN] 估值缓存写入失败（不影响本次构建）: {str(e)[:60]}")


def fetch_index_spot():
    """沪深300 实时点位（腾讯）。仅用于乐咕挂掉时推算当前 PE。"""
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000300", timeout=10, headers=UA)
        r.encoding = "gbk"
        f = r.text.split("~")
        if len(f) > 3:
            return float(f[3])
    except Exception:
        pass
    return None


def _pct_of(series_pes, cur):
    """在给定历史序列上算当前值的百分位"""
    s = pd.Series([float(x) for x in series_pes]).dropna()
    return float((s < cur).sum() / len(s) * 100)


def fetch_market_valuation():
    """沪深300 历史 PE 分位（乐咕，2005 年至今）

    乐咕是唯一覆盖 2005 年至今的长历史数据源，但偶发被反爬拦截返回非 JSON。
    而它一旦拿不到，整个择时模块（钻石底/地球顶/仓位建议）就全废 —— 这是不可接受的。

    因此把历史序列缓存到 data/valuation_cache.json，两条路径：
      主路径   乐咕可用    → 直接算分位，并顺手刷新缓存
      降级路径 乐咕挂了    → 用缓存历史序列 + 实时指数点位推算当前 PE
    推算依据是 PE 与指数点位成正比（成分股盈利在短期内可视作不变）：
      当前PE ≈ 缓存时PE × (当前点位 / 缓存时点位)
    """
    log("拉取沪深300 历史估值分位...")
    out = {"available": False}

    for attempt in range(1, 4):
      try:
        df = ak.stock_index_pe_lg(symbol="沪深300")
        pe = pd.to_numeric(df["滚动市盈率"], errors="coerce").dropna()
        if len(pe) < 30:
            raise ValueError("历史样本不足")
        cur = float(pe.iloc[-1])
        pct = _pct_of(pe.tolist(), cur)
        # 乐咕自带指数点位，可用于降级时按比例推算，无需额外请求
        try:
            idx = float(pd.to_numeric(df["指数"], errors="coerce").dropna().iloc[-1])
        except Exception:
            idx = None
        series = [
            {"d": str(d)[:10], "p": round(float(v), 2)}
            for d, v in zip(df["日期"], pe)
        ]
        out = {
            "available": True,
            "degraded": False,
            "source_note": "乐咕乐股 沪深300 滚动市盈率（2005 年至今月度样本）",
            "pe": round(cur, 2),
            "percentile": round(pct, 1),
            "history_min": round(float(pe.min()), 2),
            "history_max": round(float(pe.max()), 2),
            "history_median": round(float(pe.median()), 2),
            "sample_days": int(len(pe)),
            "start_date": str(df["日期"].min())[:10],
            "end_date": str(df["日期"].max())[:10],
            "quantiles": {
                "10": round(float(pe.quantile(0.10)), 2),
                "20": round(float(pe.quantile(0.20)), 2),
                "35": round(float(pe.quantile(0.35)), 2),
                "50": round(float(pe.quantile(0.50)), 2),
                "65": round(float(pe.quantile(0.65)), 2),
                "85": round(float(pe.quantile(0.85)), 2),
            },
            "series": series[-250:],
        }
        log(f"  沪深300 PE={cur:.2f} 分位={pct:.1f}% 样本={len(pe)}天")
        _save_val_cache(series, cur, idx, df["日期"].iloc[-1])
        return out
      except Exception as e:
        log(f"  [WARN] 第 {attempt} 次获取失败: {str(e)[:60]}")
        if attempt < 3:
            time.sleep(2 * attempt)

    # ---------- 降级路径：缓存历史序列 + 实时指数点位 ----------
    log("  [WARN] 乐咕连续 3 次失败，尝试用本地缓存 + 实时指数点位推算...")
    cache = _load_val_cache()
    spot = fetch_index_spot()
    if not cache:
        log("  [WARN] 无本地缓存可用，择时信号降级为「数据不足」")
        return out
    if not spot:
        log("  [WARN] 实时指数点位也获取失败，择时信号降级为「数据不足」")
        return out

    hist = [float(x["p"]) for x in cache["series"]]
    base_pe = float(cache.get("last_pe") or hist[-1])
    base_idx = cache.get("last_index")
    if not base_idx:
        log("  [WARN] 缓存缺少基准点位，择时信号降级为「数据不足」")
        return out

    est_pe = base_pe * (spot / float(base_idx))
    pct = _pct_of(hist, est_pe)
    s = pd.Series(hist)
    out = {
        "available": True,
        "degraded": True,
        "source_note": (
            f"乐咕今日不可用，改用 {cache.get('last_date', '上次')} 缓存的历史序列 "
            f"按指数点位推算（基准 PE {base_pe:.2f} @ 点位 {float(base_idx):.2f} → 当前点位 {spot:.2f}）"
        ),
        "pe": round(est_pe, 2),
        "percentile": round(pct, 1),
        "history_min": round(float(s.min()), 2),
        "history_max": round(float(s.max()), 2),
        "history_median": round(float(s.median()), 2),
        "sample_days": int(len(hist)),
        "start_date": str(cache["series"][0]["d"])[:10],
        "end_date": str(cache.get("last_date"))[:10],
        "quantiles": {
            "10": round(float(s.quantile(0.10)), 2),
            "20": round(float(s.quantile(0.20)), 2),
            "35": round(float(s.quantile(0.35)), 2),
            "50": round(float(s.quantile(0.50)), 2),
            "65": round(float(s.quantile(0.65)), 2),
            "85": round(float(s.quantile(0.85)), 2),
        },
        "series": cache["series"][-250:],
    }
    log(f"  [降级] 推算 PE={est_pe:.2f} 分位={pct:.1f}%（缓存更新于 {cache.get('updated')}）")
    return out


# ============================================================ 5. 产业资本动向
def fetch_capital_action(pool_codes):
    """大股东/产业资本增减持统计（李大霄择时三要素之一）"""
    log("拉取产业资本增减持动向...")
    out = {"available": False}
    try:
        df = ak.stock_ggcg_em()
        df = df[df["代码"].astype(str).isin(set(pool_codes))]
        if df.empty:
            raise ValueError("蓝筹池内无记录")
        inc = df[df["持股变动信息-增减"] == "增持"]
        dec = df[df["持股变动信息-增减"] == "减持"]
        # 个股级统计，供八步分析的第 3、4 步使用
        codes = df["代码"].astype(str)
        per_stock = {
            c: {
                "增持次数": int(((codes == c) & (df["持股变动信息-增减"] == "增持")).sum()),
                "减持次数": int(((codes == c) & (df["持股变动信息-增减"] == "减持")).sum()),
            }
            for c in set(codes)
        }
        out = {
            "available": True,
            "per_stock": per_stock,
            "增持家数": int(inc["代码"].nunique()),
            "减持家数": int(dec["代码"].nunique()),
            "增持次数": int(len(inc)),
            "减持次数": int(len(dec)),
            "净家数": int(inc["代码"].nunique() - dec["代码"].nunique()),
            "样本记录": int(len(df)),
            "top_increase": [
                {"代码": r["代码"], "名称": r["名称"], "股东": str(r["股东名称"])[:40]}
                for _, r in inc.head(10).iterrows()
            ],
        }
        log(f"  产业资本：增持 {out['增持家数']} 家 / 减持 {out['减持家数']} 家")
    except Exception as e:
        log(f"  [WARN] 产业资本数据获取失败: {e}")
    return out


# ============================================================ 6. 打分
def percentile_score(s, higher_better=True):
    """分位排名打分 0-100。higher_better=False 表示值越小越好"""
    return s.rank(pct=True, ascending=higher_better) * 100


def build_table(pool, quote, div_feat):
    """合并数据 + 黑五类硬排除 + 四维打分"""
    df = quote.copy()
    df.index.name = "代码"

    df["每股派息"] = df.index.map(lambda c: div_feat.get(c, {}).get("每股派息", 0.0)).fillna(0.0)
    df["连续分红年"] = df.index.map(lambda c: div_feat.get(c, {}).get("连续分红年", 0)).fillna(0).astype(int)
    df["分红稳定性"] = df.index.map(lambda c: div_feat.get(c, {}).get("分红稳定性", 0.0)).fillna(0.0)

    df["股息率"] = (df["每股派息"] / df["价格"] * 100).where(df["价格"] > 0, 0).round(2)

    # 与蓝筹池合并，确保名称齐全
    if not pool.empty:
        name_map = dict(zip(pool["品种代码"], pool["品种名称"]))
        df["名称"] = [name_map.get(c, nm) for c, nm in zip(df.index, df["名称"])]

    total = len(df)

    # —— 黑五类硬排除 ——
    reasons = {}
    def mark(mask, reason):
        for c in df.index[mask]:
            reasons.setdefault(c, []).append(reason)

    mark(df["PE"] <= CONFIG["min_pe"], "亏损/PE为负(垃圾股)")
    mark(df["PE"] > CONFIG["max_pe"], f"PE>{CONFIG['max_pe']}(伪成长/题材股)")
    mark(df["PB"] <= 0, "净资产为负")
    mark(df["总市值亿"] < CONFIG["min_market_cap_yi"], f"市值<{CONFIG['min_market_cap_yi']}亿(小盘股)")
    mark(df["连续分红年"] < CONFIG["min_dividend_years"], f"连续分红<{CONFIG['min_dividend_years']}年(次新/不稳定)")
    mark(df["股息率"] <= CONFIG["min_dividend_yield"], "近12月无现金分红")

    passed = df[~df.index.isin(reasons.keys())].copy()
    log(f"硬排除：{total} -> {len(passed)} 只（排除 {total - len(passed)} 只）")

    if passed.empty:
        return passed, {"total": total, "passed": 0, "reasons": reasons}

    # —— 四维打分 ——
    w = CONFIG["weights"]
    passed["S_股息"] = percentile_score(passed["股息率"], True)
    passed["S_PE"] = percentile_score(passed["PE"], False)      # PE 越低越好
    passed["S_PB"] = percentile_score(passed["PB"], False)      # PB 越低越好（破净最优）
    passed["S_稳定"] = percentile_score(passed["分红稳定性"], True)
    passed["总分"] = sum(
        passed[k] * v for k, v in zip(["S_股息", "S_PE", "S_PB", "S_稳定"],
                                      [w["dividend"], w["pe"], w["pb"], w["stability"]])
    ).div(sum(w.values())).round(1)

    # 破净 / 高股息 标记
    passed["破净"] = passed["PB"] < 1.0
    passed["高股息"] = passed["股息率"] >= 5.0

    passed = passed.sort_values("总分", ascending=False)
    return passed, {"total": total, "passed": len(passed), "reasons": reasons}


# ============================================================ 7. 择时
def build_timing(mkt_val, passed, capital):
    """李大霄择时三要素 -> 信号 + 正金字塔仓位建议"""
    t = CONFIG["timing"]
    sig = {
        "level": "unknown", "label": "数据不足", "desc": "",
        "position": "--", "action": "等待数据", "tone": "neutral",
    }

    if mkt_val.get("available"):
        p = mkt_val["percentile"]
        pe = mkt_val["pe"]
        if p < t["diamond_bottom"]:
            sig = {"level": "diamond", "label": "钻石底区域", "tone": "cold",
                   "desc": f"沪深300 滚动 PE {pe}，处于 2005 年以来 {p:.0f}% 分位，属历史极低区间。",
                   "position": "70-80%", "action": "逆向重仓：低位多买，正金字塔大举建仓"}
        elif p < t["baby_bottom"]:
            sig = {"level": "baby", "label": "婴儿底 / 偏低", "tone": "cool",
                   "desc": f"沪深300 滚动 PE {pe}，处于 {p:.0f}% 分位，估值偏低。",
                   "position": "50-65%", "action": "分批加仓：越跌越买，拉大档距"}
        elif p < t["normal"]:
            sig = {"level": "normal", "label": "正常区间", "tone": "neutral",
                   "desc": f"沪深300 滚动 PE {pe}，处于 {p:.0f}% 分位，估值中性。",
                   "position": "30-45%", "action": "持有为主：只买最优质标的，不满仓"}
        elif p < t["hot"]:
            sig = {"level": "hot", "label": "偏热", "tone": "warm",
                   "desc": f"沪深300 滚动 PE {pe}，处于 {p:.0f}% 分位，估值偏高。",
                   "position": "10-25%", "action": "逐步减仓：停止加仓，兑现部分利润"}
        else:
            sig = {"level": "top", "label": "地球顶区域", "tone": "hot",
                   "desc": f"沪深300 滚动 PE {pe}，处于 {p:.0f}% 分位，属历史极热区间。",
                   "position": "0-10%", "action": "淡泊离场：别人疯狂你减仓，逐步清仓"}

    # —— 情绪指标（自建，不依赖外部源）——
    sentiment = {"available": False}
    if not passed.empty:
        n = len(passed)
        sentiment = {
            "available": True,
            "破净占比": round(float((passed["PB"] < 1).sum()) / n * 100, 1),
            "破净家数": int((passed["PB"] < 1).sum()),
            "高股息占比": round(float((passed["股息率"] >= 5).sum()) / n * 100, 1),
            "高股息家数": int((passed["股息率"] >= 5).sum()),
            "中位股息率": round(float(passed["股息率"].median()), 2),
            "中位PE": round(float(passed["PE"].median()), 2),
            "中位PB": round(float(passed["PB"].median()), 2),
            "样本数": n,
        }

    return {"signal": sig, "sentiment": sentiment, "capital": capital, "valuation": mkt_val}


# ============================================================ 7. 八步深度分析
def build_deep_analysis(scored, cap_map, top_n):
    """
    对排名前 top_n 的标的执行「八步财报分析法」。
    只对头部标的做，是因为每只需要 3 次请求（财务 + PE + PB），
    全量 185 只跑下来要十几分钟，且不符合「只深研值得研究的」这一原则。
    """
    log(f"八步财报深度分析：对 TOP {top_n} 执行...")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import deep_analysis as da
    except Exception as e:
        log(f"  [WARN] 深度分析模块加载失败: {e}")
        return {"available": False, "items": [], "top_n": top_n}

    top = scored.head(top_n)
    try:
        industry_map = da.fetch_industry_map()
        log(f"  行业映射 {len(industry_map)} 只")
    except Exception as e:
        log(f"  [WARN] 行业映射获取失败: {e}")
        industry_map = {}

    def analyze(row):
        code, r = row
        stock = {
            "代码": code, "名称": str(r.get("名称", "")),
            "价格": float(r["价格"]), "PE": float(r["PE"]), "PB": float(r["PB"]),
            "股息率": float(r["股息率"]), "连续分红年": int(r["连续分红年"]),
            "分红稳定性": float(r["分红稳定性"]), "总分": float(r["总分"]),
        }
        try:
            fin = da.fetch_financials(code)
        except Exception:
            fin = None
        try:
            val = da.fetch_self_valuation(code)
        except Exception:
            val = {}
        try:
            steps = da.evaluate_steps(fin, val, stock, industry_map.get(code, "未知"), cap_map)
        except Exception as e:
            steps = []
            log(f"  [WARN] {code} 八步评估失败: {e}")

        # 统计通过项 / 风险项，便于页面概览
        ok_n = warn_n = 0
        for s in steps:
            for it in s.get("items", []):
                if it.get("skip_reason"):
                    continue
                if it.get("ok") is True:
                    ok_n += 1
                elif it.get("ok") is False:
                    warn_n += 1
        risks = []
        for s in steps:
            for rk in s.get("risks", []):
                if rk != "未触发量化风险阈值":
                    risks.append(rk)

        return {
            "代码": code, "名称": stock["名称"], "行业": industry_map.get(code, "未知"),
            "总分": round(stock["总分"], 1), "价格": stock["价格"],
            "PE": round(stock["PE"], 2), "PB": round(stock["PB"], 2),
            "股息率": stock["股息率"],
            "达标项": ok_n, "关注项": warn_n, "风险数": len(risks),
            "风险摘要": risks,
            "steps": steps,
        }

    items = []
    # 并发 4 路：新浪/百度接口较慢，并发能显著压缩总耗时，又不至于触发限流
    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(analyze, top.iterrows()):
            if res:
                items.append(res)

    log(f"  深度分析完成 {len(items)} 只")
    return {
        "available": True,
        "top_n": top_n,
        "count": len(items),
        "items": items,
    }


# ============================================================ 主流程
def main():
    t0 = time.time()
    log("=" * 60)
    log("李大霄价值投资雷达 · 开始构建")

    pool = fetch_pool()
    if pool.empty:
        log("[FATAL] 蓝筹池为空，终止")
        sys.exit(1)

    codes = pool["品种代码"].tolist()
    quote = fetch_quote(codes)
    if quote.empty:
        log("[FATAL] 行情为空，终止")
        sys.exit(1)

    div, ok_periods = fetch_dividend()
    div_feat = compute_dividend_features(div)

    scored, filt = build_table(pool, quote, div_feat)
    if scored.empty:
        log("[FATAL] 无股票通过筛选，终止")
        sys.exit(1)

    mkt_val = fetch_market_valuation()
    capital = fetch_capital_action(codes)
    # per_stock 仅内部使用（约 20KB），不写入 data.json
    cap_per_stock = (capital or {}).pop("per_stock", {})
    timing = build_timing(mkt_val, scored, capital)

    # ---- 八步财报深度分析：仅对排名靠前的标的做，控制请求量与耗时 ----
    deep = build_deep_analysis(scored, cap_per_stock, CONFIG["deep_top_n"])

    # —— 组装输出 ——
    stocks = []
    for rank, (code, r) in enumerate(scored.iterrows(), 1):
        stocks.append({
            "rank": rank,
            "代码": code,
            "名称": str(r.get("名称", "")),
            "价格": round(float(r["价格"]), 2),
            "涨跌幅": round(float(r.get("涨跌幅", 0)), 2),
            "PE": round(float(r["PE"]), 2),
            "PB": round(float(r["PB"]), 2),
            "股息率": round(float(r["股息率"]), 2),
            "每股派息": round(float(r["每股派息"]), 4),
            "连续分红年": int(r["连续分红年"]),
            "分红稳定性": round(float(r["分红稳定性"]), 1),
            "总市值亿": round(float(r["总市值亿"]), 1),
            "总分": round(float(r["总分"]), 1),
            "破净": bool(r["破净"]),
            "高股息": bool(r["高股息"]),
            "S_股息": round(float(r["S_股息"]), 1),
            "S_PE": round(float(r["S_PE"]), 1),
            "S_PB": round(float(r["S_PB"]), 1),
            "S_稳定": round(float(r["S_稳定"]), 1),
        })

    result = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pool": CONFIG["index_name"],
            "pool_size": len(codes),
            "quoted": int(len(quote)),
            "passed": int(len(scored)),
            "filtered_out": int(filt["total"] - filt["passed"]),
            "dividend_periods": ok_periods,
            "weights": CONFIG["weights"],
            "thresholds": {
                "min_market_cap_yi": CONFIG["min_market_cap_yi"],
                "max_pe": CONFIG["max_pe"],
                "min_dividend_years": CONFIG["min_dividend_years"],
            },
            "deep_top_n": CONFIG["deep_top_n"],
            "sources": {
                "蓝筹池": "中证指数(沪深300成分股)",
                "行情估值": "腾讯行情 PE(TTM)/PB/总市值（已与百度估值交叉校验）",
                "分红派息": "东方财富 分红送配（近12个月实际除权派息）",
                "大盘估值分位": (
                    "乐咕乐股 沪深300 历史 PE"
                    if not (mkt_val or {}).get("degraded")
                    else "乐咕乐股 沪深300 历史 PE（今日源不可用，已按指数点位推算）"
                ),
                "产业资本": "东方财富 股东增减持",
                "三张报表": "新浪财经 财务摘要（80 指标，年报口径取结构项、最新期取增速项）",
                "个股历史估值": "百度股市通 近三年 PE/PB",
                "行业归属": "新浪财经 行业板块（证监会行业分类）",
            },
            "cost_sec": round(time.time() - t0, 1),
        },
        "timing": timing,
        "stocks": stocks,
        "deep": deep,
    }

    out_path = DATA_DIR / "data.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    log(f"输出 {out_path}  ({out_path.stat().st_size / 1024:.1f} KB)")
    log(f"TOP10: " + " / ".join(f"{s['名称']}({s['总分']})" for s in stocks[:10]))
    log(f"完成，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
