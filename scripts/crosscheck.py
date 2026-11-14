#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crosscheck.py — 权益发放/核销/码库存 逐权益逐自然周交叉验证

健康关系（每权益每周期）:
    发放数 G = 核销数 R + 库存行数 I + 过期数(已含在库存行 state=expired 内)
即 diff = G - R - I，diff != 0 即不一致。

口径约定:
  1. 周期为自然周，周一为首日、周日为末日，period 标签 YYYY-MM-DD~YYYY-MM-DD。
  2. 核销去重: 同码同日的多行重报按一笔核销计；核销数按 (权益, 码) 去重后的笔数计。
  3. 跨周期归属: 核销按其码的发放周期归属；同一码有多笔发放时，取最近一笔发放
     (grant_date 最大, 同日再按行号) 所在周。
  4. 库存(codes.csv)行按本行 grant_date 所在周归属; 码无任何发放时退化为本行日期周。
  5. 维度定位:
     diff < 0: 核销码的发放笔数已被库存占满或无发放可对 -> 核销侧多;
               核销都能对上而库存行数超过发放笔数        -> UNKNOWN。
     diff > 0: 未对上的发放行, 该码在库存仍有行          -> 发放侧多;
               该码在核销与库存均无踪迹                  -> 库存侧少;
               同时命中多类 / 无法归入上述情形           -> UNKNOWN。
  6. 行号均为数据行序号(不含表头, 首条数据=1, 等于物理行号-1)。

输出:
  mismatch.csv  列: benefit,period,dim,diff,rows
  pending.txt   逐组合下钻明细(标 [待核]) + 文末三文件总计数一致性核对行
"""

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timedelta

DIM_GRANT = "发放侧多"
DIM_REDEEM = "核销侧多"
DIM_STOCK = "库存侧少"
DIM_UNKNOWN = "UNKNOWN"


def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def period_label(d):
    monday = d - timedelta(days=d.weekday())  # Monday=0
    sunday = monday + timedelta(days=6)
    return f"{monday.isoformat()}~{sunday.isoformat()}"


def load_rows(path):
    """读 CSV, 返回 dict 列表并注入数据行序号 __line (1-based)。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, 1):
        r["__line"] = i
    return rows


def ref(filename, line):
    return f"{filename}:{line}"


def main():
    ap = argparse.ArgumentParser(description="权益三表逐周期交叉验证")
    ap.add_argument("--datadir", default="/workspace",
                    help="grants/redemptions/codes 所在目录 (默认 /workspace)")
    ap.add_argument("--grants", default=None)
    ap.add_argument("--redemptions", default=None)
    ap.add_argument("--codes", default=None)
    ap.add_argument("--outdir", default=None,
                    help="mismatch.csv/pending.txt 输出目录 (默认同 datadir)")
    args = ap.parse_args()

    g_path = args.grants or f"{args.datadir}/grants.csv"
    r_path = args.redemptions or f"{args.datadir}/redemptions.csv"
    c_path = args.codes or f"{args.datadir}/codes.csv"
    outdir = args.outdir or args.datadir
    import os
    os.makedirs(outdir, exist_ok=True)
    g_name, r_name, c_name = "grants.csv", "redemptions.csv", "codes.csv"

    grants = load_rows(g_path)
    reds_raw = load_rows(r_path)
    codes = load_rows(c_path)

    # ---------- 发放 ----------
    # grants_by_bc[(benefit,code)] = [grant rows sorted by (date,line)]
    grants_by_bc = defaultdict(list)
    for r in grants:
        r["__date"] = parse_date(r["grant_date"])
        grants_by_bc[(r["benefit"], r["code"])].append(r)
    for k in grants_by_bc:
        grants_by_bc[k].sort(key=lambda x: (x["__date"], x["__line"]))

    # 最近一笔发放
    latest_grant = {k: v[-1] for k, v in grants_by_bc.items()}

    # ---------- 核销去重: 同码同日多行折一笔 ----------
    # red_groups[(b,c,date)] = [raw lines]; 再折叠为按码一笔
    red_groups = defaultdict(list)
    for r in reds_raw:
        d = parse_date(r["redeem_date"])
        red_groups[(r["benefit"], r["code"], d)].append(r["__line"])
    dedup_collapsed = sum(1 for v in red_groups.values() if len(v) > 1)

    # red_by_bc[(b,c)] = {"dates": sorted dates, "lines": all raw lines sorted}
    red_by_bc = defaultdict(lambda: {"dates": [], "lines": set()})
    for (b, c, d), lines in red_groups.items():
        red_by_bc[(b, c)]["dates"].append(d)
        red_by_bc[(b, c)]["lines"].update(lines)
    for v in red_by_bc.values():
        v["dates"].sort()
        v["lines"] = sorted(v["lines"])

    # ---------- 库存 ----------
    codes_by_bc = defaultdict(list)
    for r in codes:
        r["__date"] = parse_date(r["grant_date"])
        codes_by_bc[(r["benefit"], r["code"])].append(r)
    for k in codes_by_bc:
        codes_by_bc[k].sort(key=lambda x: (x["__date"], x["__line"]))

    # 库存行 join 发放自检 (benefit,code,grant_date)
    grant_date_keys = {(g["benefit"], g["code"], g["__date"]) for g in grants}
    stock_join_warn = [
        ref(c_name, c["__line"]) for c in codes
        if (c["benefit"], c["code"], c["__date"]) not in grant_date_keys
    ]

    # ---------- 周期分桶 ----------
    # 每桶记录按码归集的三类行
    bucket = defaultdict(lambda: {
        "grant": defaultdict(list),   # code -> [grant rows]
        "redeem": defaultdict(list),  # code -> [raw redemption lines]
        "stock": defaultdict(list),   # code -> [codes rows]
    })

    for r in grants:
        bucket[(r["benefit"], period_label(r["__date"]))]["grant"][r["code"]].append(r)

    orphan_redeem = []   # 无任何发放可对的核销
    for (b, c), info in red_by_bc.items():
        lg = latest_grant.get((b, c))
        if lg is not None:
            period = period_label(lg["__date"])
        else:
            # 无发放可对: 退化为最早核销日行所在周, 交由负差额规则判为核销侧多
            period = period_label(info["dates"][0])
            orphan_redeem.append((b, c, info["lines"]))
        bucket[(b, period)]["redeem"][c] = info["lines"]

    orphan_stock = []
    for (b, c), rows in codes_by_bc.items():
        for cr in rows:
            if (b, c) in latest_grant:
                period = period_label(cr["__date"])
            else:
                period = period_label(cr["__date"])
                orphan_stock.append(ref(c_name, cr["__line"]))
            bucket[(b, period)]["stock"][c].append(cr)

    # ---------- 逐组合判定 ----------
    findings = []
    for (b, period) in sorted(bucket):
        bk = bucket[(b, period)]
        g_by_code, r_by_code, i_by_code = bk["grant"], bk["redeem"], bk["stock"]
        all_codes = set(g_by_code) | set(r_by_code) | set(i_by_code)

        G = sum(len(v) for v in g_by_code.values())
        R = len(r_by_code)              # 按码去重后的核销笔数
        I = sum(len(v) for v in i_by_code.values())
        diff = G - R - I
        if diff == 0:
            continue

        # 行级 FIFO 配对: 核销先占发放槽(同码一笔), 库存再占余下槽; 余下即未对上的发放行
        def leftover_grant_lines(code):
            gl = sorted(g_by_code.get(code, []), key=lambda x: (x["__date"], x["__line"]))
            used, idx = set(), 0
            if code in r_by_code and idx < len(gl):
                used.add(gl[idx]["__line"]); idx += 1
            for _ in range(len(i_by_code.get(code, []))):
                if idx < len(gl):
                    used.add(gl[idx]["__line"]); idx += 1
            return [x for x in gl if x["__line"] not in used], gl

        dim = DIM_UNKNOWN
        offending = []     # (file, line, note) 精确涉事行
        context = []       # (file, line, note) 关联行

        if diff < 0:
            # 题面口径:
            #   核销码的发放笔数已被库存占满(gc==0 或库存行数>=发放数) -> 核销侧多;
            #   核销记录都能对上(无被占满的核销码), 仅库存行数超过发放笔数 -> UNKNOWN。
            blocked, overflow_stock = [], []
            for c in sorted(all_codes):
                gc = len(g_by_code.get(c, []))
                rc = 1 if c in r_by_code else 0
                ic = len(i_by_code.get(c, []))
                if rc == 1 and (gc == 0 or ic >= gc):
                    blocked.append(c)
                if ic + rc > gc:
                    overflow_stock.append((c, ic + rc - gc))
            if blocked:
                dim = DIM_REDEEM
                for c in blocked:
                    for ln in r_by_code[c]:
                        offending.append((r_name, ln, "核销码的发放笔数已被库存占满或无发放可对"))
                    for gr in g_by_code.get(c, []):
                        context.append((g_name, gr["__line"],
                                        f"{gr.get('grant_id', '')} {c} 发放 {gr['grant_date']}"))
                    for sr in i_by_code.get(c, []):
                        context.append((c_name, sr["__line"],
                                        f"{c} 库存行 state={sr.get('state', '')} 已占用发放槽"))
            else:
                # 核销都能对上, 纯粹库存超发 -> UNKNOWN
                dim = DIM_UNKNOWN
                for c, excess in overflow_stock:
                    sr = sorted(i_by_code.get(c, []), key=lambda x: (x["__date"], x["__line"]))
                    for x in sr[-excess:]:
                        offending.append((c_name, x["__line"],
                                          f"{c} 库存行数超过该码发放笔数 {excess} 行"))
                    for gr in g_by_code.get(c, []):
                        context.append((g_name, gr["__line"],
                                        f"{gr.get('grant_id', '')} {c} 发放 {gr['grant_date']}"))
        else:
            # diff > 0: 逐码找未对上的发放行
            grant_side, stock_short, ambiguous = [], [], []
            for c in sorted(all_codes):
                gc = len(g_by_code.get(c, []))
                rc = 1 if c in r_by_code else 0
                ic = len(i_by_code.get(c, []))
                unmatched = gc - rc - ic
                if unmatched <= 0:
                    continue
                left, gl = leftover_grant_lines(c)
                if ic > 0:
                    grant_side.append((c, left, gl))
                elif rc == 0:
                    stock_short.append((c, left, gl))
                else:
                    ambiguous.append((c, left, gl))   # 有核销踪迹却无库存, 规则覆盖不到
            if ambiguous or (grant_side and stock_short):
                dim = DIM_UNKNOWN
                for c, left, gl in grant_side + stock_short + ambiguous:
                    for gr in left:
                        offending.append((g_name, gr["__line"], f"{c} 未对上的发放行(维度无法唯一确定)"))
            elif grant_side:
                dim = DIM_GRANT
                for c, left, gl in grant_side:
                    for gr in left:
                        offending.append((g_name, gr["__line"],
                                          f"{gr.get('grant_id', '')} {c} 未对上的发放行, 该码库存仍有行"))
                    for gr in gl:
                        if gr["__line"] not in {x["__line"] for x in left}:
                            context.append((g_name, gr["__line"], f"{c} 同码发放行(已配对)"))
                    for sr in i_by_code.get(c, []):
                        context.append((c_name, sr["__line"],
                                        f"{c} 库存行 state={sr.get('state', '')}"))
            elif stock_short:
                dim = DIM_STOCK
                for c, left, gl in stock_short:
                    for gr in left:
                        offending.append((g_name, gr["__line"],
                                          f"{gr.get('grant_id', '')} {c} 未对上的发放行, 核销与库存均无踪迹"))
            else:
                dim = DIM_UNKNOWN

        findings.append({
            "benefit": b, "period": period, "dim": dim, "diff": diff,
            "G": G, "R": R, "I": I,
            "offending": offending, "context": context,
        })

    # ---------- 一致性总计数 ----------
    total_grants = len(grants)
    total_red_raw = len(reds_raw)
    total_red_dedup = len(red_by_bc)          # 按码去重后的核销笔数
    total_codes = len(codes)
    global_diff = total_grants - (total_red_dedup + total_codes)
    report_diff_sum = sum(f["diff"] for f in findings)

    # ---------- 写 mismatch.csv ----------
    mismatch_path = f"{outdir}/mismatch.csv"
    with open(mismatch_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["benefit", "period", "dim", "diff", "rows"])
        for fd in findings:
            rows = ";".join(ref(fn, ln) for fn, ln, _ in fd["offending"])
            w.writerow([fd["benefit"], fd["period"], fd["dim"], fd["diff"], rows])

    # ---------- 写 pending.txt ----------
    pending_path = f"{outdir}/pending.txt"
    lines = []
    lines.append("权益三表交叉验证 — 待核明细 (行号=数据行序号, 不含表头, 首条数据=1)")
    lines.append("周期: 自然周(周一至周日) | 核销: 同码同日重报按一笔, 按码去重计数")
    lines.append("核销归属: 码的最近一笔发放所在周 | 库存归属: 库存行 grant_date 所在周")
    lines.append("=" * 76)
    if not findings:
        lines.append("全部权益周期平衡, 无待核项。")
    for idx, fd in enumerate(findings, 1):
        lines.append(
            f"[待核] #{idx} benefit={fd['benefit']} period={fd['period']} "
            f"dim={fd['dim']} diff={fd['diff']:+d} (G={fd['G']} R={fd['R']} I={fd['I']})"
        )
        lines.append("  涉事行号清单:")
        for fn, ln, note in fd["offending"]:
            lines.append(f"    >>> {ref(fn, ln):<24} {note}")
        if fd["context"]:
            lines.append("  关联行(配对/占用依据):")
            for fn, ln, note in fd["context"]:
                lines.append(f"       {ref(fn, ln):<24} {note}")
        lines.append("  状态: 待核")
        lines.append("-" * 76)

    lines.append("")
    lines.append("# ===== 文末一致性核对 =====")
    lines.append(f"# {g_name} 数据行总数: {total_grants}")
    lines.append(
        f"# {r_name} 原始数据行: {total_red_raw} | 同码同日重报折叠 {dedup_collapsed} 组 | "
        f"按码去重后核销笔数: {total_red_dedup}"
    )
    lines.append(f"# {c_name} 数据行总数: {total_codes}")
    lines.append(
        f"# 全局平衡: G - (R去重 + I) = {total_grants} - ({total_red_dedup} + {total_codes}) "
        f"= {global_diff:+d}"
    )
    lines.append(
        f"# mismatch 组合数: {len(findings)} | 报告内 diff 合计: {report_diff_sum:+d} "
        f"=> {'一致' if report_diff_sum == global_diff else '不一致!!'}"
    )
    if stock_join_warn:
        lines.append(f"# 警告: 库存行未能 join 到发放: {';'.join(stock_join_warn)}")
    if orphan_redeem:
        lines.append("# 警告: 无发放可对的核销码: "
                     + ";".join(f"{b}/{c} 行{','.join(map(str, ls))}" for b, c, ls in orphan_redeem))
    if orphan_stock:
        lines.append(f"# 警告: 无发放可对的库存行: {';'.join(orphan_stock)}")

    with open(pending_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ---------- 控制台摘要 ----------
    print(f"周期组合数: {len(bucket)} | 不一致组合: {len(findings)}")
    for fd in findings:
        rows = ";".join(ref(fn, ln) for fn, ln, _ in fd["offending"])
        print(f"  {fd['benefit']} {fd['period']} {fd['dim']:<6} diff={fd['diff']:+d}  {rows}")
    print(f"全局 G-(R去重+I) = {global_diff:+d}; 报告 diff 合计 = {report_diff_sum:+d}; "
          f"一致性: {report_diff_sum == global_diff}")
    print(f"输出: {mismatch_path}\n      {pending_path}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
