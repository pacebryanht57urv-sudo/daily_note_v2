# <chip> / <die>

## 设计信息

- 半径 R：<R> um
- gap 排布：<cavity-range: gap>
- 测量约定：1% 输入监控端固定为 1 uW，对应入腔端功率 100 uW；表中插损为对称输入/输出耦合假设下的等效单端插损。
- 数据状态：<completed / skipped / invalid summary>
- 数值来源：先运行 `summarize_die_large_scan.py` 生成 `die_summary.json`、`die_cavity_summary.csv`、`die_family_summary.csv`；如有已审核 family map，再生成 `die_unified_family_alignment.csv` 辅助填写 global μ=0 波长。
- 表中 `Q0 MLE` 为成功拟合模式的 `log10(Q0)` 分布峰值估计；`Q1@1550` 取最靠近 1550 nm 的成功拟合模式。
- 每个腔里的 `mode1/mode2/...` 是本地自动编号，不代表跨腔统一族名；跨腔比较以 `Family A/B/C/...` 为准。

## 统一模式家族划分

判据优先级：先用 FSR / n_g / D2 的连续性确定主族，同时检查同一个 cavity 内 one-FSR panel 的相对分支顺序和 offset，防止在相近 FSR 分支之间发生 family 交换；再用 mode 连续性和 D2 拟合残差排除跳族或误配。Q 和 dip depth 只作为耦合与数据质量参考，不作为族身份的主判据。Family 表中的 `global μ=0 波长` 是在统一 family 后，对各腔的整数 mode index 做跨腔对齐得到的同一纵模编号波长；它不是直接照抄每个单腔的本地 `mode_number_centered=0`。

| 统一族名 | 物理特征 | 对应本地 mode | 质量备注 |
|---|---|---|---|
| Family A | <FSR / n_g / D2 feature> | `<cavity mode>`；... | <quality note> |
| Family B | <FSR / n_g / D2 feature> | `<cavity mode>`；... | <quality note> |
| Family C | <FSR / n_g / D2 feature> | `<cavity mode>`；... | <quality note> |
| Extra / sparse | <why not a main family> | `<cavity mode>`；... | <exclusion note> |

## 现场照片

| 类别 | 链接 |
|---|---|
| die 标识 | [die标识](<../figures/measurement/<chip>/<die>/die标识.jpg>) |
| die 左边 | [die左边](<../figures/measurement/<chip>/<die>/die左边.jpg>) |
| die 右边 | [die右边](<../figures/measurement/<chip>/<die>/die右边.jpg>) |
| 腔照片 | [c1](<../figures/measurement/<chip>/<die>/c1.jpg>) / ... |

## 腔入口、出腔功率与插损

| 腔 | gap | 状态 | Pout | throughput / 单端插损 | 卡片 | 交互 Q | Q 快照 | 灵敏度 |
|---|---:|---|---:|---:|---|---|---|---|
| `c1` | <gap> | <status> | <Pout> | <throughput / IL> | [card](<c1/cavity_card.html>) | [interactive](<c1/Q/interactive_q.html>) | [q_trend](<c1/Q/q_trend.png>) | <pending or link> |
| `cN` | <gap> | <status> | <Pout or not quantified> | <throughput / IL or reason> | [card](<cN/cavity_card.html>) | <link or no formal Q> | <link or no formal Q> | <pending / skipped / link> |

## Family A 横向比较

Family A 是 <short physical description>。

数值从 `die_family_summary.csv` 和审核后的 `die_unified_family_alignment.csv` 填写；不要从单腔图上手抄。

| 腔 | gap | 本地 mode | 对齐 local μ | global μ=0 波长 (nm) | 点数 | FSR (GHz) | n_g | D2 (MHz) | RMS (MHz) | 平均 Q0 (M) | Q0 MLE (M) | 平均 Q1 (M) | Q1@1550 (M) |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `c1` | <gap> | <modeN> | <local mu> | <wavelength> | <count> | <FSR> | <n_g> | <D2> | <RMS> | <Q0 mean> | <Q0 MLE> | <Q1 mean> | <Q1@1550> |

## Family B 横向比较

Family B 是 <short physical description>。

<same table as Family A>

## Family C 横向比较

Family C 是 <short physical description>。

<same table as Family A>

## 额外分支

| 腔 | gap | 本地 mode | 对齐 local μ | global μ=0 波长 (nm) | 点数 | FSR (GHz) | n_g | D2 (MHz) | RMS (MHz) | 平均 Q0 (M) | Q0 MLE (M) | 平均 Q1 (M) | Q1@1550 (M) | 判断 |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `<cavity>` | <gap> | <modeN> | <local mu> | <wavelength> | <count> | <FSR> | <n_g> | <D2> | <RMS> | <Q0 mean> | <Q0 MLE> | <Q1 mean> | <Q1@1550> | <why excluded> |

## gap 分组观察

| gap | 腔 | throughput 范围 | Family A / B | Family C / other | 备注 |
|---:|---|---:|---|---|---|
| <gap> | <cavities> | <range> | <main-family trend> | <low-Q / extra trend> | <caveats> |

## die 级判断

- 插损趋势：<fact + interpretation boundary>
- 统一族对比：<Family A/B/C physical distinction; note local mode labels are not cross-cavity identities>
- global μ=0 波长：<fabrication variation inferred from aligned wavelengths; mention integer shifts and caveats>
- Q0 横向对比：<main-family Q0 ranking/trend>
- Q1 横向对比：<coupling/gap trend>
- 色散横向对比：<D2 / FSR continuity and caveats>
- 有效性：<accepted / limited / skipped cavities>

## 待确认

- <smallest useful follow-up or caveat>
