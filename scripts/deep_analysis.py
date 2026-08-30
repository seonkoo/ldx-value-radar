#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄八步财报分析法 —— 深度分析模块

八步中，第 1 步（商业模式/护城河）、第 3 步部分（实控人/关联交易/信披）、
第 6 步部分（业绩驱动因素）属于定性判断，机器无法量化，本模块不伪造数据，
仅输出结构化检查清单交由人工研判；其余各步均输出可核验的量化指标。

数据源：
  - 三张报表     新浪财务摘要 stock_financial_abstract（80 指标 × 多报告期）
  - 个股历史估值  百度估值    stock_zh_valuation_baidu（近三年 PE/PB）
  - 行业归属     新浪行业板块 stock_sector_detail（84 个板块，并发拉取建映射）

重要设计：金融业（银行/保险/证券）的高资产负债率与现金流口径与实业不可比，
本模块对这类行业豁免相应指标，避免把银行的正常经营特征误判为风险。
"""

import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("TQDM_DISABLE", "1")   # 屏蔽 akshare 内部进度条，避免污染 CI 日志

try:
    import akshare as ak
except ImportError:
    ak = None

# ------------------------------------------------------------------ 行业例外
# 这些行业的经营特征与实业不同，统一阈值会误判，故对特定指标作豁免
# 同时兼容两种命名：新浪板块名（银行/保险）与证监会行业名（货币金融服务/保险业）
FINANCIAL_INDUSTRIES = (
    "银行", "保险", "证券", "多元金融", "金融",
    "货币金融服务", "资本市场服务", "保险业", "其他金融业",
)

EXEMPT_INDICATORS = {
    "资产负债率": FINANCIAL_INDUSTRIES,   # 金融业高杠杆属正常经营
    "现金流质量": FINANCIAL_INDUSTRIES,   # 银行/保险经营现金流口径不可比
    "商誉占净资产": FINANCIAL_INDUSTRIES,  # 金融业商誉项目含义不同
}


def is_exempt(indicator: str, industry: str) -> bool:
    """该行业是否豁免此指标"""
    exempts = EXEMPT_INDICATORS.get(indicator, ())
    return any(k in (industry or "") for k in exempts)


# ------------------------------------------------------------------ 财务数据
def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def fetch_financials(code: str):
    """
    拉取三张报表核心指标。
    返回 dict，含：报告期、以及按「完整年报」与「最新期」分别取值。
    """
    if ak is None:
        return None
    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception:
        return None
    if df is None or df.empty:
        return None

    periods = [c for c in df.columns if str(c).isdigit()]
    if not periods:
        return None
    periods.sort(reverse=True)

    # 完整年报（1231 结尾）用于结构性指标；最新期用于增长类指标
    annual = [p for p in periods if p.endswith("1231")]

    def val(name, period):
        if period is None:
            return None
        row = df[df["指标"] == name]
        if row.empty:
            return None
        return _num(row.iloc[0].get(period))

    def val_any(name, period_list):
        """按优先级取第一个非空值，用于重复指标名（新浪按分组重复出现）"""
        rows = df[df["指标"] == name]
        for p in period_list:
            for _, r in rows.iterrows():
                v = _num(r.get(p))
                if v is not None:
                    return v, p
        return None, None

    base = annual[0] if annual else periods[0]        # 最近完整年报
    latest = periods[0]                                # 最新报告期
    prev_year = periods[4] if len(periods) > 4 else None  # 约一年前，用于同比

    # —— 利润表 ——
    roe, roe_p = val_any("净资产收益率(ROE)", [base, latest])
    revenue, _ = val_any("营业总收入", [latest, base])
    revenue_prev, _ = val_any("营业总收入", [prev_year]) if prev_year else (None, None)
    net_profit, _ = val_any("归母净利润", [latest, base])
    deduct_profit, _ = val_any("扣非净利润", [latest, base])
    total_profit, _ = val_any("净利润", [latest, base])
    gross_margin, _ = val_any("毛利率", [base, latest])
    net_margin, _ = val_any("销售净利率", [base, latest])
    rev_growth, _ = val_any("营业总收入增长率", [latest])
    profit_growth, _ = val_any("归属母公司净利润增长率", [latest])

    # —— 资产负债表 ——
    equity, _ = val_any("股东权益合计(净资产)", [latest, base])
    goodwill, _ = val_any("商誉", [latest, base])
    debt_ratio, _ = val_any("资产负债率", [latest, base])

    # —— 现金流量表 ——
    # 必须用【年报】口径：建筑等行业中报现金流为负是常态（工程款年底集中回款），
    # 用半年报计算「经营现金流/净利润」会把正常的季节性错判成利润造假。
    ocf, _ = val_any("经营现金流量净额", [base, latest])
    profit_annual, _ = val_any("净利润", [base, latest])

    def ratio(a, b):
        if a is None or b is None or b == 0:
            return None
        return round(a / b, 4)

    return {
        "最新报告期": latest,
        "年报报告期": base,
        "ROE": round(roe, 2) if roe is not None else None,
        "毛利率": round(gross_margin, 2) if gross_margin is not None else None,
        "销售净利率": round(net_margin, 2) if net_margin is not None else None,
        "资产负债率": round(debt_ratio, 2) if debt_ratio is not None else None,
        "营收增速": round(rev_growth, 2) if rev_growth is not None else None,
        "净利增速": round(profit_growth, 2) if profit_growth is not None else None,
        "扣非占比": ratio(deduct_profit, total_profit),   # 越接近1，利润越"干净"
        "商誉占净资产": ratio(goodwill, equity),
        "现金含量": ratio(ocf, profit_annual),            # 年报经营现金流/年报净利润
        "营业收入_亿": round(revenue / 1e8, 1) if revenue else None,
        "归母净利润_亿": round(net_profit / 1e8, 1) if net_profit else None,
        "经营现金流_亿": round(ocf / 1e8, 1) if ocf else None,
        "净资产_亿": round(equity / 1e8, 1) if equity else None,
    }


# ------------------------------------------------------------------ 自身历史估值
def fetch_self_valuation(code: str):
    """个股自身历史 PE/PB 分位（百度，近三年）"""
    out = {"pe_available": False, "pb_available": False}
    for key, indicator in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近三年")
            if df is None or df.empty:
                continue
            s = pd.to_numeric(df["value"], errors="coerce").dropna()
            s = s[s > 0]
            if len(s) < 60:
                continue
            cur = float(s.iloc[-1])
            pct = float((s < cur).sum() / len(s) * 100)
            out.update({
                f"{key}_available": True,
                f"{key}_current": round(cur, 2),
                f"{key}_percentile": round(pct, 1),
                f"{key}_min": round(float(s.min()), 2),
                f"{key}_max": round(float(s.max()), 2),
                f"{key}_median": round(float(s.median()), 2),
                f"{key}_days": int(len(s)),
            })
        except Exception:
            continue
        time.sleep(0.12)
    return out


# ------------------------------------------------------------------ 行业映射
def fetch_industry_map(max_workers=8):
    """
    遍历新浪行业板块，建立 代码 -> 行业 映射。
    84 个板块并发拉取，约 10-20 秒完成。
    """
    mapping = {}
    if ak is None:
        return mapping
    try:
        sectors = ak.stock_sector_spot(indicator="行业")
    except Exception:
        return mapping

    labels = sectors["label"].tolist()

    def pull(label):
        try:
            d = ak.stock_sector_detail(sector=label)
            if d is None or d.empty or "code" not in d.columns:
                return label, []
            return label, d["code"].astype(str).tolist()
        except Exception:
            return label, []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(pull, labels))

    name_by_label = dict(zip(sectors["label"], sectors["板块"]))
    for label, codes in results:
        ind = name_by_label.get(label, label)
        for c in codes:
            mapping.setdefault(c.zfill(6), ind)
    return mapping


# ------------------------------------------------------------------ 八步评估
def evaluate_steps(fin, val, stock, industry, capital_map):
    """
    综合八步，输出每步的量化结果 + 定性检查清单。
    fin: 财务指标  val: 自身历史估值  stock: 主表行  industry: 行业名
    capital_map: {代码: {增持次数, 减持次数}}
    """
    steps = []
    code = stock["代码"]

    # ---------- 第 1 步：业务与商业模式（定性）----------
    steps.append({
        "no": 1, "title": "业务与商业模式", "type": "qualitative",
        "summary": "靠什么赚钱、护城河在哪 —— 机器无法判断，需人工研读年报「经营情况讨论与分析」",
        "checklist": [
            "能否用一句话说清这家公司靠什么赚钱？说不清就不投",
            "护城河属于哪一类：品牌 / 牌照壁垒 / 成本优势 / 网络效应 / 转换成本",
            "主营是否专注？多元化扩张（尤其跨行业并购）通常是价值毁灭信号",
            "客户是谁？是否依赖单一大客户或政府补贴",
            "这门生意 10 年后还在吗？需求是否会被技术或政策颠覆",
        ],
    })

    # ---------- 第 2 步：三张报表（量化）----------
    if fin:
        items = []
        # 利润表
        items.append({"k": "ROE（净资产收益率）", "v": fin.get("ROE"), "unit": "%",
                      "good": "长期 ≥15% 优秀，≥10% 合格", "ok": (fin.get("ROE") or 0) >= 10})
        items.append({"k": "毛利率", "v": fin.get("毛利率"), "unit": "%",
                      "good": "越高越说明有定价权", "ok": (fin.get("毛利率") or 0) >= 20})
        items.append({"k": "销售净利率", "v": fin.get("销售净利率"), "unit": "%",
                      "good": "与同行对比，高于同行说明成本控制好", "ok": (fin.get("销售净利率") or 0) >= 8})
        items.append({"k": "扣非净利/净利润", "v": fin.get("扣非占比"), "unit": "",
                      "good": "≥0.9 说明利润靠主营，非卖资产/补贴",
                      "ok": (fin.get("扣非占比") or 0) >= 0.9,
                      "skip_reason": None})
        # 资产负债表
        dr_ok = None
        dr_skip = None
        if is_exempt("资产负债率", industry):
            dr_ok, dr_skip = None, f"{industry}业高杠杆属正常经营，不适用统一阈值"
        else:
            dr_ok = (fin.get("资产负债率") or 100) <= 60
        items.append({"k": "资产负债率", "v": fin.get("资产负债率"), "unit": "%",
                      "good": "实业 ≤60% 较安全", "ok": dr_ok, "skip_reason": dr_skip})

        gw_skip = f"{industry}业商誉项目含义不同" if is_exempt("商誉占净资产", industry) else None
        items.append({"k": "商誉/净资产", "v": fin.get("商誉占净资产"), "unit": "",
                      "good": "≤10% 安全，>30% 有减值风险",
                      "ok": (fin.get("商誉占净资产") or 0) <= 0.1, "skip_reason": gw_skip})
        # 现金流
        cf_skip = f"{industry}业经营现金流口径不可比" if is_exempt("现金流质量", industry) else None
        items.append({"k": "经营现金流/净利润", "v": fin.get("现金含量"), "unit": "",
                      "good": "≥1 说明利润是真金白银", "ok": (fin.get("现金含量") or 0) >= 1,
                      "skip_reason": cf_skip})
        items.append({"k": "营收增速", "v": fin.get("营收增速"), "unit": "%",
                      "good": "正增长且稳定优于大起大落", "ok": (fin.get("营收增速") or -99) >= 0})
        items.append({"k": "净利增速", "v": fin.get("净利增速"), "unit": "%",
                      "good": "与营收增速匹配才健康", "ok": (fin.get("净利增速") or -99) >= 0})
        steps.append({
            "no": 2, "title": "三张报表", "type": "quantitative",
            "period": f"{fin.get('最新报告期')}（年报口径 {fin.get('年报报告期')}）",
            "items": items,
            "scale": {"营收": fin.get("营业收入_亿"), "归母净利": fin.get("归母净利润_亿"),
                      "经营现金流": fin.get("经营现金流_亿"), "净资产": fin.get("净资产_亿")},
        })
    else:
        steps.append({"no": 2, "title": "三张报表", "type": "quantitative", "items": [],
                      "error": "财务数据获取失败"})

    # ---------- 第 3 步：公司治理与管理层 ----------
    cap = capital_map.get(code, {})
    steps.append({
        "no": 3, "title": "公司治理与管理层", "type": "mixed",
        "quantified": {
            "大股东增持次数": cap.get("增持次数", 0),
            "大股东减持次数": cap.get("减持次数", 0),
            "增减持净向": ("净增持" if cap.get("增持次数", 0) > cap.get("减持次数", 0)
                           else ("净减持" if cap.get("增持次数", 0) < cap.get("减持次数", 0) else "持平/无记录")),
        },
        "checklist": [
            "实控人是谁？国资 / 民企 / 无实控人，治理稳定性差别很大",
            "管理层持股还是纯职业经理人？利益是否绑定",
            "关联交易多不多？大额关联交易是掏空的高发区",
            "过去有无信披违规、财务造假、被交易所问询的记录",
            "审计机构是否为头部事务所？有无频繁更换审计机构",
        ],
    })

    # ---------- 第 4 步：资本行为 ----------
    steps.append({
        "no": 4, "title": "资本行为", "type": "quantitative",
        "items": [
            {"k": "股息率", "v": stock.get("股息率"), "unit": "%",
             "good": "越高越符合「得好报」", "ok": (stock.get("股息率") or 0) >= 3},
            {"k": "连续现金分红", "v": stock.get("连续分红年"), "unit": "年",
             "good": "≥5 年说明回报股东有持续性", "ok": (stock.get("连续分红年") or 0) >= 5},
            {"k": "分红稳定性", "v": stock.get("分红稳定性"), "unit": "/100",
             "good": "越高说明派息越平稳", "ok": (stock.get("分红稳定性") or 0) >= 60},
        ],
        "note": "回购数据暂无可用免费源，需自行在年报「股份回购」章节核对",
    })

    # ---------- 第 5 步：估值 ----------
    if val.get("pe_available") or val.get("pb_available"):
        items = []
        if val.get("pe_available"):
            items.append({"k": "当前 PE", "v": val.get("pe_current"), "unit": "",
                          "good": f"近三年区间 {val.get('pe_min')} ~ {val.get('pe_max')}，中位 {val.get('pe_median')}",
                          "ok": (val.get("pe_percentile") or 100) <= 40})
            items.append({"k": "PE 自身历史分位", "v": val.get("pe_percentile"), "unit": "%",
                          "good": "≤40% 属自身历史偏低区", "ok": (val.get("pe_percentile") or 100) <= 40})
        if val.get("pb_available"):
            items.append({"k": "当前 PB", "v": val.get("pb_current"), "unit": "",
                          "good": f"近三年区间 {val.get('pb_min')} ~ {val.get('pb_max')}，中位 {val.get('pb_median')}",
                          "ok": (val.get("pb_percentile") or 100) <= 40})
            items.append({"k": "PB 自身历史分位", "v": val.get("pb_percentile"), "unit": "%",
                          "good": "≤40% 属自身历史偏低区", "ok": (val.get("pb_percentile") or 100) <= 40})
        steps.append({
            "no": 5, "title": "估值对比", "type": "quantitative",
            "period": f"近三年 {val.get('pe_days') or val.get('pb_days')} 个交易日",
            "items": items,
            "note": "此处为与【自身历史】比较；与同行横向对比见第 7 步",
        })
    else:
        steps.append({"no": 5, "title": "估值对比", "type": "quantitative", "items": [],
                      "error": "个股历史估值获取失败"})

    # ---------- 第 6 步：驱动因素与风险 ----------
    risks = []
    if fin:
        if fin.get("商誉占净资产") and fin["商誉占净资产"] > 0.3 and not is_exempt("商誉占净资产", industry):
            risks.append(f"商誉占净资产 {fin['商誉占净资产']*100:.0f}%，减值风险高")
        if fin.get("资产负债率") and fin["资产负债率"] > 70 and not is_exempt("资产负债率", industry):
            risks.append(f"资产负债率 {fin['资产负债率']:.0f}%，杠杆偏高")
        if fin.get("现金含量") is not None and fin["现金含量"] < 0.8 and not is_exempt("现金流质量", industry):
            risks.append(f"经营现金流仅为净利润的 {fin['现金含量']*100:.0f}%，利润含金量不足")
        rev_g, np_g = fin.get("营收增速"), fin.get("净利增速")
        if rev_g is not None and np_g is not None and rev_g < 0 and np_g < 0:
            risks.append(f"营收 {rev_g:.1f}%、净利 {np_g:.1f}% 双双负增长")
        elif np_g is not None and np_g < -10:
            risks.append(f"净利同比 {np_g:.1f}%，业绩明显下滑")
        if fin.get("扣非占比") is not None and fin["扣非占比"] < 0.7:
            risks.append(f"扣非占比仅 {fin['扣非占比']*100:.0f}%，利润含较多非经常性损益")
    if cap.get("减持次数", 0) > cap.get("增持次数", 0) * 2 and cap.get("减持次数", 0) >= 5:
        risks.append(f"大股东减持 {cap['减持次数']} 次，远多于增持")
    steps.append({
        "no": 6, "title": "驱动因素与风险", "type": "mixed",
        "risks": risks or ["未触发量化风险阈值"],
        "checklist": [
            "未来业绩靠什么拉动？量增（扩产/渗透率）还是价增（提价/结构升级）",
            "主要原材料/成本项是什么？涨价会否吞噬利润",
            "政策是否友好？有没有集采、限价、环保等潜在冲击",
            "是否存在对单一客户/单一产品的重度依赖",
            "技术路线是否存在被替代风险",
        ],
    })

    # ---------- 第 7 步：行业 ----------
    steps.append({
        "no": 7, "title": "行业与竞争格局", "type": "mixed",
        "quantified": {"所属行业": industry or "未知"},
        "checklist": [
            "行业处在导入 / 成长 / 成熟 / 衰退哪个阶段",
            "是风口还是下坡路？政策是扶持还是限制",
            "竞争格局：寡头垄断 / 充分竞争 / 分散无序",
            "这家公司在产业链中的位置，议价能力强弱",
            "行业容量是否还能支撑 5 年增长",
        ],
    })

    # ---------- 第 8 步：情景测算 ----------
    scen = build_scenarios(fin, val, stock)
    steps.append({
        "no": 8, "title": "情景测算", "type": "quantitative",
        "scenarios": scen["scenarios"],
        "basis": scen["basis"],
        "note": "基于历史 ROE 与估值区间的简化推演，仅用于理解下行空间与上行弹性，不构成预测",
    })

    return steps


def build_scenarios(fin, val, stock):
    """乐观/中性/悲观三档：以 ROE 与估值分位为两个变量做简化推演"""
    price = stock.get("价格") or 0
    pb = stock.get("PB") or 0
    roe = (fin or {}).get("ROE") or 0

    pb_med = val.get("pb_median") or pb or 1
    pb_min = val.get("pb_min") or (pb * 0.7 if pb else 0.7)
    pb_max = val.get("pb_max") or (pb * 1.3 if pb else 1.3)

    def px(target_pb):
        if pb and pb > 0:
            return round(price * target_pb / pb, 2)
        return None

    # 悲观：ROE 下滑 30%，估值压到历史低位附近
    # 中性：ROE 维持，估值回归近三年中位
    # 乐观：ROE 提升 20%，估值修复到历史高位附近
    basis = (
        f"以当前 PB {pb}、ROE {roe}% 为基准；"
        f"近三年 PB 区间 {val.get('pb_min', '-')} ~ {val.get('pb_max', '-')}，中位 {val.get('pb_median', '-')}"
        if val.get("pb_available") else
        f"以当前 PB {pb}、ROE {roe}% 为基准；自身历史 PB 数据不足，中高位区间按 ±30% 估算"
    )

    return {
        "basis": basis,
        "scenarios": [
            {
                "name": "悲观", "tone": "cold",
                "roe": round(roe * 0.7, 2), "target_pb": round(pb_min, 2),
                "price": px(pb_min),
                "change": round((px(pb_min) / price - 1) * 100, 1) if price and px(pb_min) else None,
                "desc": "盈利下滑三成，估值杀到近三年最低区域",
            },
            {
                "name": "中性", "tone": "neutral",
                "roe": round(roe, 2), "target_pb": round(pb_med, 2),
                "price": px(pb_med),
                "change": round((px(pb_med) / price - 1) * 100, 1) if price and px(pb_med) else None,
                "desc": "盈利维持，估值回归近三年中枢",
            },
            {
                "name": "乐观", "tone": "warm",
                "roe": round(roe * 1.2, 2), "target_pb": round(pb_max, 2),
                "price": px(pb_max),
                "change": round((px(pb_max) / price - 1) * 100, 1) if price and px(pb_max) else None,
                "desc": "盈利增长两成，估值修复至近三年高位",
            },
        ],
    }
