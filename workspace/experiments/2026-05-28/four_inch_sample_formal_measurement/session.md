# 2026-05-28 四英寸样品正式测量 session

## 基本信息

- 记录类型：跨天正式测量 session。
- 开始日期：2026-05-28。
- 样品来源：2026-05-20 session 中提到的四英寸样品。
- 样品规模：四英寸样品上共 9 个 chip。
- 本 session 预期持续数天，后续按 chip / die / 测量组逐步补充。

## 样品与人员分配

以下为 2026-05-28 开始测量前由胡志刚口述确认的信息。

| chip | 归属/主要使用者 | 样品用途或类型 | 备注 |
|---|---|---|---|
| chip1 | 吴祖磊 | 薄膜半径较大的超声波传感器 | 主要用于光声气体传感 |
| chip2 | 吴祖磊 | 薄膜半径较大的超声波传感器 | 主要用于光声气体传感 |
| chip3 | 吴祖磊 | 薄膜半径较大的超声波传感器 | 主要用于光声气体传感 |
| chip4 | 刘健飞 | 片上磁力仪样品 |  |
| chip5 | 刘健飞 | 片上磁力仪样品 |  |
| chip6 | 刘健飞 | 片上磁力仪样品 |  |
| chip7 | 胡志刚 | 单腔样品 | 小半径单腔超声传感与光声显微成像前期测试 |
| chip8 | 胡志刚 | 3 x 3 阵列腔样品 | 阵列腔模式间隔、热调谐与后续光梳并行检测 |
| chip9 | 胡志刚 | 4 x 4 阵列腔样品 | 更高通道数阵列腔模式间隔、热调谐与并行检测 |

## 与 2026-05-20 session 的关联

- 2026-05-20 session 记录了该四英寸样品的前序加工、裂片和 chip 层级信息。
- 本 session 不重复复制前序加工记录；仅在测量结果需要解释时引用前序加工事实。
- 后续测量中需要特别追踪：
  - 每个 chip / die 的可测状态。
  - 薄膜、开孔、裂纹、污染、边缘破损等外观状态。
  - 测量异常是否可能来自前序加工、裂片或装载历史。

## chip7 设计摘要

- chip7 为胡志刚样品，包含 16 个 die，按 4 x 4 排布。
- die 编号按设计图和表格：左上角为 `die1-1`，向右列号增加，向下行号增加。
- 每个 die 内为 3 x 3 个独立单腔；同一行 3 个腔设计参数相同。
- 每个 die 内从上到下三行主要改变腔-波导耦合 gap，gap 顺序按下表中列出的三个数依次对应。
- 目标：测试单腔、小半径腔对超声波的传感灵敏度，并服务后续光声显微成像。

设计图：

图 1 展示 chip7 的 4 x 4 die 总排布，用于确认 die 编号与物理位置。

![chip7 总图](<figures/device_design/chip7/CHIP7总图.png>)

图 2 展示前两行 die 的双侧进出结构，边缘耦合波导间距约 30 um，目标是用单根透镜光纤快速找光。

![双侧进出，波导间距 30 um](<figures/device_design/chip7/双侧进出_间距为30um.png>)

图 3 展示第三行 die 的双侧进出结构，边缘耦合波导间距为 127 um，目标是适配 FA 并尝试多腔同时耦合。

![双侧进出，波导间距 127 um](<figures/device_design/chip7/双侧进出_间距为127um.png>)

图 4 展示第四行 die 的同侧进出结构，输入和输出波导位于同一侧且间距为 127 um，目标是方便后续实验布置。

![单侧进出，波导间距 127 um](<figures/device_design/chip7/单侧进出_间距为127um.png>)

通用设计：

| 项目 | 设计值 |
|---|---|
| 腔环宽 | 6 um |
| 耦合波导宽度 | 2 um |
| 边缘耦合形式 | inversed taper |
| taper 最细宽度 | 0.4 um |
| taper 细段长度 | 100 um |
| taper 过渡段长度 | 300 um |
| 深硅区域薄膜直径 | 约为空腔直径的 2 倍 |

单腔 FSR 估算：

| 腔半径 R | 1550 nm 附近预期波长 FSR | 1550 nm 附近预期频率 FSR | 估算说明 |
|---:|---:|---:|---|
| 125 um | 约 1.53 nm | 约 190.9 GHz | 按 `FSR_f = c / (n_g 2πR)`、`n_g = 2.0` 估算 |
| 105 um | 约 1.82 nm | 约 227.3 GHz | 同上 |
| 85 um | 约 2.25 nm | 约 280.8 GHz | 同上 |
| 65 um | 约 2.94 nm | 约 367.0 GHz | 同上 |

chip7 die 设计矩阵：

| die | 腔半径 R | 三行 gap | 边缘耦合波导间距 | 耦合拓扑 / 目的 |
|---|---:|---|---:|---|
| die1-1 | 125 um | 0.75 / 0.80 / 0.85 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die1-2 | 125 um | 0.90 / 0.95 / 1.00 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die1-3 | 105 um | 0.75 / 0.80 / 0.85 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die1-4 | 105 um | 0.90 / 0.95 / 1.00 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die2-1 | 85 um | 0.75 / 0.80 / 0.85 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die2-2 | 85 um | 0.90 / 0.95 / 1.00 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die2-3 | 65 um | 0.75 / 0.80 / 0.85 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die2-4 | 65 um | 0.90 / 0.95 / 1.00 um | 30 um | 双侧进出；单根透镜光纤耦合，便于快速找光 |
| die3-1 | 125 um | 0.75 / 0.80 / 0.85 um | 127 um | 双侧进出；面向 FA 多通道同时耦合 |
| die3-2 | 125 um | 0.90 / 0.95 / 1.00 um | 127 um | 双侧进出；面向 FA 多通道同时耦合 |
| die3-3 | 65 um | 0.75 / 0.80 / 0.85 um | 127 um | 双侧进出；面向 FA 多通道同时耦合 |
| die3-4 | 65 um | 0.90 / 0.95 / 1.00 um | 127 um | 双侧进出；面向 FA 多通道同时耦合 |
| die4-1 | 125 um | 0.75 / 0.80 / 0.85 um | 127 um | 同侧进出 / 单边耦合；方便后续实验布置 |
| die4-2 | 125 um | 0.90 / 0.95 / 1.00 um | 127 um | 同侧进出 / 单边耦合；方便后续实验布置 |
| die4-3 | 65 um | 0.75 / 0.80 / 0.85 um | 127 um | 同侧进出 / 单边耦合；方便后续实验布置 |
| die4-4 | 65 um | 0.90 / 0.95 / 1.00 um | 127 um | 同侧进出 / 单边耦合；方便后续实验布置 |

测量时优先记录：

- 实际找光时 30 um 间距是否明显缩短定位时间。
- 127 um 间距结构是否能与 FA 稳定耦合，能否一次覆盖多个腔。
- 同侧进出结构是否在实验布置上明显更方便，以及是否带来额外串扰或对准问题。
- 小半径腔在光学 Q、耦合深度、超声响应和可重复性上的表现。
- CHIP7 每个 die 内同一行 3 个同参数腔从上到下对应 1-9 号腔；这些结构用于检查加工一致性，不直接作为严格重复样本处理。

## chip8 设计索引

- chip8 主要为 3 x 3 阵列腔；`die1-1` 为吴祖磊设计的可见光波段小腔 Q 测试结构，不纳入胡志刚本次实验记录。
- 阵列腔采用按行蛇形串联方式连接；每个 die 实际只有一根输入波导和一根输出波导。
- 光依次经过阵列腔 `1-2-3-6-5-4-7-8-9`。
- 阵列腔物理编号按左上到右下逐行编号。
- 耦合波导同样采用 inversed taper 设计。
- chip8 阵列腔的基准半径为 125 um，主要设计变量为耦合 gap、阵列内相邻腔半径步进和金属电极宽度。
- 阵列内相邻腔半径步进用于拉开各腔中心频率。
- 每个腔有独立电极；后续计划通过 PWB 实现多路控制，外部施加电压并由回路电流热效应实现腔模式热调谐。
- 版图中薄膜图形与腔图形看起来不同心，这不是设计失误。原因是腔结构在 wafer 正面，薄膜开洞需要从背面深硅刻蚀实现；双面套刻时，深硅层掩模图形需要相对氮化硅微环腔图形水平翻转，才能在正反面对准后得到预期结构。
- 双面套刻完成后，目标结构中薄膜中心与腔中心同心。
- 第一轮测量先看透过率谱，重点检查各腔模式间隔是否符合设计；之后测量热调谐，再考虑封装并与光梳结合做并行检测。

设计图：

图 5 展示 chip8 的 3 x 3 阵列腔结构设计，用于确认阵列布局、串联连接和薄膜 / 腔图形关系。

![chip8 阵列腔结构设计](<figures/device_design/chip8/结构设计.png>)

单腔 FSR 估算：

| 腔半径 R | 1550 nm 附近预期波长 FSR | 1550 nm 附近预期频率 FSR | 估算说明 |
|---:|---:|---:|---|
| 125 um | 约 1.53 nm | 约 190.9 GHz | 按 `FSR_f = c / (n_g 2πR)`、`n_g = 2.0` 估算 |

阵列内模式间隔估算：

| 相邻腔半径步进 | 1550 nm 附近预期波长间隔 | 1550 nm 附近预期频率间隔 | 估算说明 |
|---:|---:|---:|---|
| 16.2 nm | 约 0.201 nm | 约 25.1 GHz | 按 `Δλ/λ ≈ ΔR/R`、`R = 125 um` 估算 |
| 32.4 nm | 约 0.402 nm | 约 50.2 GHz | 同上 |

chip8 die 设计矩阵：

| die | 结构类型 | gap | 相邻腔半径步进 | 金属电极宽度 | 备注 |
|---|---|---:|---:|---:|---|
| die1-1 | 可见光小腔 |  |  |  | 吴祖磊设计；不纳入本人本次实验记录 |
| die1-2 | 3 x 3 阵列腔 | 0.8 um | 16.2 nm | 4 um |  |
| die1-3 | 3 x 3 阵列腔 | 0.9 um | 16.2 nm | 4 um |  |
| die1-4 | 3 x 3 阵列腔 | 1.0 um | 16.2 nm | 4 um |  |
| die2-1 | 3 x 3 阵列腔 | 0.7 um | 16.2 nm | 8 um |  |
| die2-2 | 3 x 3 阵列腔 | 0.8 um | 16.2 nm | 8 um |  |
| die2-3 | 3 x 3 阵列腔 | 0.9 um | 16.2 nm | 8 um |  |
| die2-4 | 3 x 3 阵列腔 | 1.0 um | 16.2 nm | 8 um |  |
| die3-1 | 3 x 3 阵列腔 | 0.7 um | 32.4 nm | 4 um |  |
| die3-2 | 3 x 3 阵列腔 | 0.8 um | 32.4 nm | 4 um |  |
| die3-3 | 3 x 3 阵列腔 | 0.9 um | 32.4 nm | 4 um |  |
| die3-4 | 3 x 3 阵列腔 | 1.0 um | 32.4 nm | 4 um |  |
| die4-1 | 3 x 3 阵列腔 | 0.7 um | 32.4 nm | 8 um |  |
| die4-2 | 3 x 3 阵列腔 | 0.8 um | 32.4 nm | 8 um |  |
| die4-3 | 3 x 3 阵列腔 | 0.9 um | 32.4 nm | 8 um |  |
| die4-4 | 3 x 3 阵列腔 | 1.0 um | 32.4 nm | 8 um |  |

待确认：

- 阵列腔输入 / 输出端口方向与实际装夹方向。
- 本次是否只记录胡志刚样品相关阵列腔，`die1-1` 仅作为排除项保留。

## chip9 设计索引

- chip9 主要为 4 x 4 阵列腔；每个 die 内有 16 个腔。
- 阵列腔串联连接；光依次经过 `1-2-3-4-8-7-6-5-9-10-11-12-16-15-14-13`。
- 阵列腔物理编号按左上到右下逐行编号。
- chip9 阵列腔的基准半径为 65 um，主要设计变量为耦合 gap、阵列内相邻腔半径步进和金属电极宽度。
- 第一轮测量先看透过率谱，重点检查各腔模式间隔是否符合设计，并记录小半径阵列腔的 Q、耦合深度和模式可区分性。

设计图：

图 6 展示 chip9 的 4 x 4 阵列腔结构设计，用于确认阵列布局和串联走线顺序。

![chip9 阵列腔结构设计](<figures/device_design/chip9/chip9阵列结构图.png>)

单腔 FSR 估算：

| 腔半径 R | 1550 nm 附近预期波长 FSR | 1550 nm 附近预期频率 FSR | 估算说明 |
|---:|---:|---:|---|
| 65 um | 约 2.94 nm | 约 367.0 GHz | 按 `FSR_f = c / (n_g 2πR)`、`n_g = 2.0` 估算 |

阵列内模式间隔估算：

| 相邻腔半径步进 | 1550 nm 附近预期波长间隔 | 1550 nm 附近预期频率间隔 | 估算说明 |
|---:|---:|---:|---|
| 8.42 nm | 约 0.201 nm | 约 25.1 GHz | 按 `Δλ/λ ≈ ΔR/R`、`R = 65 um` 估算 |
| 16.84 nm | 约 0.402 nm | 约 50.2 GHz | 同上 |

chip9 die 设计矩阵：

| die | 结构类型 | gap | 相邻腔半径步进 | 金属电极宽度 | 备注 |
|---|---|---:|---:|---:|---|
| die1-1 | 4 x 4 阵列腔 | 0.7 um | 8.42 nm | 4 um |  |
| die1-2 | 4 x 4 阵列腔 | 0.8 um | 8.42 nm | 4 um |  |
| die1-3 | 4 x 4 阵列腔 | 0.9 um | 8.42 nm | 4 um |  |
| die1-4 | 4 x 4 阵列腔 | 1.0 um | 8.42 nm | 4 um |  |
| die2-1 | 4 x 4 阵列腔 | 0.7 um | 8.42 nm | 8 um |  |
| die2-2 | 4 x 4 阵列腔 | 0.8 um | 8.42 nm | 8 um |  |
| die2-3 | 4 x 4 阵列腔 | 0.9 um | 8.42 nm | 8 um |  |
| die2-4 | 4 x 4 阵列腔 | 1.0 um | 8.42 nm | 8 um |  |
| die3-1 | 4 x 4 阵列腔 | 0.7 um | 16.84 nm | 4 um |  |
| die3-2 | 4 x 4 阵列腔 | 0.8 um | 16.84 nm | 4 um |  |
| die3-3 | 4 x 4 阵列腔 | 0.9 um | 16.84 nm | 4 um |  |
| die3-4 | 4 x 4 阵列腔 | 1.0 um | 16.84 nm | 4 um |  |
| die4-1 | 4 x 4 阵列腔 | 0.7 um | 16.84 nm | 8 um |  |
| die4-2 | 4 x 4 阵列腔 | 0.8 um | 16.84 nm | 8 um |  |
| die4-3 | 4 x 4 阵列腔 | 0.9 um | 16.84 nm | 8 um |  |
| die4-4 | 4 x 4 阵列腔 | 1.0 um | 16.84 nm | 8 um |  |

待确认：

- chip9 阵列腔输入 / 输出端口方向与实际装夹方向。
- 金属电极是否与 chip8 相同，主要用于热调谐。

## 平台交代

### 光路与扫描方式

- 激光源：TOPTICA DLC PRO，中心波长约 `1550 nm`。
- 激光器输出后先经过 `99:1` 分束器。
- `1%` 支路进入 MZI，再进入 PD；MZI 的 FSR 为 `40.1228 MHz`，用于提供扫频频率标尺和校正激光三角波扫描的非线性。
- `99%` 主光路先经过可调衰减器，再经过偏振控制器。
- 主光路随后经过第二个 `99:1` 分束器。
- 第二个分束器的 `1%` 支路接光功率计，用于标定进入芯片主路的实际功率；该分束器实测分光比约为 `100:1`。
- 第二个分束器的 `99%` 支路进入 `1 x 8` FA。
- 细扫：激光器由外部函数发生器施加三角波实现波长扫描；函数发生器阻抗为 `High-Z`，频率 `50 Hz`，幅度 `1 Vpp`。
- 细扫方向：当前记忆为扫频电压减小时波长变长、频率减小；电压增大时波长变短、频率增大。后续用 MZI 条纹和已知谱线可再次确认符号。
- 大扫：使用 DLC PRO 内部电极扫描，范围覆盖 `1530-1570 nm`，扫频速度 `2 nm/s`。
- 示波器：Rohde & Schwarz RTE1104。
- 示波器通道：`CH1` 接函数发生器三角波同步信号，用于触发；`CH2` 接芯片透射 PD；`CH3` 接 MZI PD。

### FA 与芯片耦合

- FA：`1 x 8` 光纤阵列，相邻通道间距 `127 um`。
- FA 光纤芯径：`9 um` 单模光纤芯径。
- FA 端面：棱镜光纤端面。
- 当片上波导间距为 `30 um` 时，始终使用 FA 的第 1 路与片上波导对准。
- 在 CHIP7 的 `30 um` 间距结构测量中，FA 其余 7 路先悬空不参与耦合。
- 当片上波导间距为 `127 um` 时，FA 各路与片上波导逐路对准。
- 芯片另一端使用单根棱镜光纤收光。
- 单根棱镜光纤后续在两个去向之间切换：
  - 接光功率计：与由第二个 `99:1` 分束器 `1%` 支路反推得到的入腔功率作比值，得到两个耦合端面的总插损。
  - 接 PD：用于透过率谱和模式测量。
- 单根棱镜光纤切换到功率计或 PD 时，尽量保持相近连接路径和姿态，并分别寻找最大读数状态。

### setup 图像证据

图 7 为本次透过率谱测量的光路卡通图，概括 TOPTICA、MZI 频率标尺、功率标定支路、FA / 芯片耦合和 RTE1104 通道映射。

![透过率谱测量光路卡通图](<figures/setup/optical_setup_cartoon.svg>)

FA 设计文件：

- [8 路光纤阵列设计图](figures/setup/8路光纤阵列设计图.pdf)

图 8 展示 CHIP7 当前测量装置的全局布局，包括显微观察、两侧耦合调节台和样品台。

![CHIP7 实验装置全局图](<figures/setup/chip7/实验装置图全局.jpg>)

图 9 展示 CHIP7 端面耦合区域细节，可见左侧单根棱镜光纤、右侧 FA 和中间芯片位置关系。

![CHIP7 实验装置细节图](<figures/setup/chip7/实验装置图细节.jpg>)

图 10 展示 8 通道 FA 的显微图，用于记录 FA 端面形貌和通道排列。

![8 通道 FA 光显图](<figures/setup/chip7/8通道FA光显图.png>)

图 11 展示 8 通道 FA 的现场手机图，用于记录 FA 与芯片边缘的实际装夹和接近状态。

![8 通道 FA 手机图](<figures/setup/chip7/8通道FA手机图.jpg>)

待确认：

- 细扫方向需要在实际采数时用 MZI 条纹和已知谱线再次确认。
- 第一个 `99:1` 分束器的端口方向是否已按标称比例使用。
- 插损计算是否需要补偿 FA 通道差异或棱镜光纤输出端损耗。
- CHIP7 当前样品装夹方向与 die / 腔编号的对应关系。

## 测量前约定

- CHIP8/CHIP9 每个 die 只有一根输入波导和一根输出波导；CHIP7 端口和实际输入输出方向后续结合实验 setup 图记录。
- CHIP7 为今天优先测量对象；目标是先跑通找光、透过率谱和记录流程，再推进超声波灵敏度测试。
- 透过率谱优先扫大谱，并通过程序判断：
  - 一个 FSR 内有几个模式。
  - 哪些模式属于同一个模式家族。
  - 本征 Q、耦合 Q、透过率和插损。
  - 是否能由实测 FSR 反推出群折射率，并辅助判断偏振。
- 偏振必须记录；TE/TM 群折射率可能差异较大，inversed taper 设计预期使两种偏振插损接近，需用实测验证。
- CHIP8/CHIP9 的热调谐后续记录外加电压、回路电流、估算功率和谐振漂移。

## 记录策略

- 本 session 以自然语言输入为主，`session.md` 作为真实状态。
- 现场密集输入阶段，先在对话中轻量归类、追问和判读；用户明确说“整理一下 / 写进 session / 今天收口 / 落盘”后，再批量写入本文件。
- 每采完一组新数据后，先判断规律、有效性、异常点和下一步意义；确认有效或明确标记为无效后，再写入记录。
- 原始大数据不默认进入 git；本文件只记录路径、样品编号、数据编号、代表图和轻量结果。
- 阵列腔测量按 `die -> cavity` 两级记录：die 级保留定位/端面图、每腔薄膜总览、每腔状态和 `n_g` 总表；每个 cavity 的完整 Q 表、分族图、原始采集图、文件列表和限制说明放入对应 summary，`session.md` 只链接 summary 和保留 compact Q 趋势图。
- 大谱采集约定：CH1 上升沿触发，触发电平 `1 V`；示波器时间窗以触发点为中心，总时长 `20 s`，即触发前 `10 s` 和触发后 `10 s`。

## CHIP7 测量记录

### die1-1 测量总览

- 目标：测量 `chip7 / die1-1` 上的阵列腔，设计半径按 `R = 125 um` 处理。
- 版图：三行 gap 约为 `0.75 / 0.80 / 0.85 um`；边缘耦合波导间距 `30 um`；双侧进出。
- 记录策略：`session.md` 只保留 die 级照片、每腔薄膜图、每腔关键状态和 `n_g`；完整 Q 表、原始图、分族图、文件路径和限制说明保留在每腔 summary 中。
- 统一采集参数：正式大谱为 `1530-1570 nm`、`2 nm/s`、CH1 上升沿 `1 V` 触发、`20 s = 10 s pre + 10 s post`；c4 之后主要使用 `500 kSa/s` / `npz-compressed`。

#### die 定位与端面

| 左侧端面 | die 标识 | 右侧端面 |
|---|---|---|
| ![CHIP7 die1-1 左侧端面](<figures/measurement/chip7/die1-1/die1-1左边.png>) | ![CHIP7 die1-1 标识](<figures/measurement/chip7/die1-1/die标识.png>) | ![CHIP7 die1-1 右侧端面](<figures/measurement/chip7/die1-1/die1-1右边.png>) |

#### 腔薄膜总览

<table>
  <tr><th>c1</th><th>c2</th><th>c3</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-1/c1/c1.png" alt="c1 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c2/c2薄膜区域.jpg" alt="c2 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c3/c3_film_region.png" alt="c3 薄膜"></td>
  </tr>
  <tr><td>0.75 um</td><td>0.75 um；右下疑似未完全刻透</td><td>0.75 um；波导局部缺陷</td></tr>
  <tr><th>c4</th><th>c5</th><th>c6</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-1/c4/c4薄膜结构.jpg" alt="c4 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c5/c5薄膜.jpg" alt="c5 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c6/c6薄膜.jpg" alt="c6 薄膜"></td>
  </tr>
  <tr><td>0.80 um</td><td>0.80 um</td><td>0.80 um</td></tr>
  <tr><th>c7</th><th>c8</th><th>c9</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-1/c7/c7薄膜.jpg" alt="c7 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c8/c8薄膜.jpg" alt="c8 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-1/c9/c9薄膜.jpg" alt="c9 薄膜裂纹"></td>
  </tr>
  <tr><td>0.85 um；首扫饱和后重采</td><td>0.85 um</td><td>0.85 um；裂纹/缺口，未采数据</td></tr>
</table>

#### 每腔状态与 `n_g`

| cavity | gap | status | throughput / single-ended IL | data | `n_g` by family | detail | note |
|---|---:|---|---:|---|---|---|---|
| `c1` | 0.75 um | valid | 5.0% / 6.51 dB | formal | lower 1.8705 (rms 0.998 GHz)<br>upper 1.8626 (rms 1.25 GHz)<br>side 1.8707 (rms 0.429 GHz) | [large_scan_20260528_222935_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c1/large_scan_20260528_222935_1530-1570nm_q_record_summary.md>) | 左侧端面有挂耳状残留；薄膜完整。 |
| `c2` | 0.75 um | valid | 5.5% / 6.30 dB | formal rescan | lower 1.8619 (rms 1.35 GHz)<br>upper 1.8534 (rms 0.364 GHz)<br>side 1.8621 (rms 0.677 GHz) | [large_scan_20260529_123518_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c2/large_scan_20260529_123518_1530-1570nm_q_record_summary.md>) | 右下角疑似未完全刻透；deep_lower rms 较大。 |
| `c3` | 0.75 um | valid-limited | 1.4% / 9.27 dB | formal rescan; 50 kSa/s | lower 1.8612 (rms 0.910 GHz)<br>upper 1.8529 (rms 0.153 GHz)<br>side 1.8615 (rms 0.263 GHz) | [large_scan_20260529_201051_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c3/large_scan_20260529_201051_1530-1570nm_q_record_summary.md>) | 波导局部缺陷；MZI 采样偏低，细节谨慎。 |
| `c4` | 0.80 um | valid | 1.5% / 9.12 dB | formal | lower 1.8603 (rms 0.0373 GHz)<br>upper 1.8524 (rms 0.0118 GHz)<br>side 1.8609 (rms 0.00947 GHz) | [large_scan_20260529_203141_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c4/large_scan_20260529_203141_1530-1570nm_q_record_summary.md>) | 本轮色散拟合最干净，可作为分族和 D2 参考腔。 |
| `c5` | 0.80 um | valid-limited | 3.5% / 7.28 dB | formal | lower 1.8678 (rms 0.407 GHz) | [large_scan_20260529_205241_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c5/large_scan_20260529_205241_1530-1570nm_q_record_summary.md>) | 仅 deep_lower 连续可用；不作三族比较。 |
| `c6` | 0.80 um | valid | 3.5% / 7.28 dB | formal | lower 1.8681 (rms 0.445 GHz)<br>upper 1.8601 (rms 0.457 GHz)<br>side 1.8692 (rms 0.569 GHz) | [large_scan_20260529_211008_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c6/large_scan_20260529_211008_1530-1570nm_q_record_summary.md>) | Q 可比；色散 residual 粗，不精解 D2。 |
| `c7` | 0.85 um | valid | 2.5% / 8.01 dB | formal after rescan | lower 1.8693 (rms 0.490 GHz)<br>upper 1.8612 (rms 0.515 GHz)<br>side 1.8705 (rms 0.615 GHz) | [large_scan_20260529_213414_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c7/large_scan_20260529_213414_1530-1570nm_q_record_summary.md>) | 首扫 CH2 饱和作废；重采通过饱和检查。 |
| `c8` | 0.85 um | valid | 2.5% / 8.01 dB | formal | lower 1.8683 (rms 0.446 GHz)<br>upper 1.8603 (rms 0.450 GHz)<br>side 1.8696 (rms 0.556 GHz) | [large_scan_20260529_214307_1530-1570nm_q_record_summary.md](<results/chip7/die1-1/c8/large_scan_20260529_214307_1530-1570nm_q_record_summary.md>) | Q 水平与 c7 相近；D2 不精解。 |
| `c9` | 0.85 um | broken / skipped | - | not acquired | - | - | 薄膜开裂/缺口延伸到耦合区域附近；不采大谱。 |

说明：input monitor 读数为 `1 uW`；按第二个 `99:1` 分束器实测约 `100:1` 反推，入腔主路功率约 `P_in = 100 uW`。throughput 定义为 `T = P_out / P_in`；单端插损暂按两端对称估算为 `IL_single = -10 log10(T) / 2`，因此该值不区分 FA 输入端和单根棱镜光纤输出端。`lower / upper / side` 是 folded-frequency 分支标签，不表示波长高低；括号中的 rms 是该 family 的拟合残差，只作 `n_g` 可信度提示。Q0/Q1、loaded linewidth、完整分族表不在主 session 展开，统一看每腔 detail summary。

#### Q 趋势图

下列图均为三行竖排：`Q0`、`Q1`、`Tmin / platform`；已删除 loaded linewidth 子图。按实际阵列行排列，每行三个腔对应同一 gap。c9 因结构开裂未采数据。

<table style="width:100%; table-layout:fixed;">
  <tr><th>c1</th><th>c2</th><th>c3</th></tr>
  <tr>
    <td><img src="results/chip7/die1-1/c1/large_scan_20260528_222935_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c1 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-1/c2/large_scan_20260529_123518_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c2 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-1/c3/large_scan_20260529_201051_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c3 Q trends" style="width:100%; display:block;"></td>
  </tr>
  <tr><td>gap 0.75 um</td><td>gap 0.75 um</td><td>gap 0.75 um</td></tr>
  <tr><th>c4</th><th>c5</th><th>c6</th></tr>
  <tr>
    <td><img src="results/chip7/die1-1/c4/large_scan_20260529_203141_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c4 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-1/c5/large_scan_20260529_205241_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c5 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-1/c6/large_scan_20260529_211008_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c6 Q trends" style="width:100%; display:block;"></td>
  </tr>
  <tr><td>gap 0.80 um</td><td>gap 0.80 um</td><td>gap 0.80 um</td></tr>
  <tr><th>c7</th><th>c8</th><th>c9</th></tr>
  <tr>
    <td><img src="results/chip7/die1-1/c7/large_scan_20260529_213414_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c7 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-1/c8/large_scan_20260529_214307_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-1 c8 Q trends" style="width:100%; display:block;"></td>
    <td>未采数据</td>
  </tr>
  <tr><td>gap 0.85 um</td><td>gap 0.85 um</td><td>gap 0.85 um；结构开裂</td></tr>
</table>

#### 原始数据索引

| cavity | formal raw data |
|---|---|
| `c1` | `results/chip7/die1-1/c1/large_scan_20260528_222935_1530-1570nm.csv` |
| `c2` | `results/chip7/die1-1/c2/large_scan_20260529_123518_1530-1570nm.csv` |
| `c3` | `results/chip7/die1-1/c3/large_scan_20260529_201051_1530-1570nm.csv` |
| `c4` | `results/chip7/die1-1/c4/large_scan_20260529_203141_1530-1570nm.csv` |
| `c5` | `results/chip7/die1-1/c5/large_scan_20260529_205241_1530-1570nm.npz` |
| `c6` | `results/chip7/die1-1/c6/large_scan_20260529_211008_1530-1570nm.npz` |
| `c7` | `results/chip7/die1-1/c7/large_scan_20260529_213414_1530-1570nm.npz` |
| `c8` | `results/chip7/die1-1/c8/large_scan_20260529_214307_1530-1570nm.npz` |
| `c9` | - |

阶段性判断：c4 是当前 `die1-1` 中色散拟合最干净的一组，适合作为检查分族和 D2 符号的参考；c6-c8 的 Q 水平较好，但色散 residual 为几百 MHz，优先纳入 Q / `n_g` 横向比较，暂不精细解释 D2；c5 只有一支连续 family；c9 因薄膜开裂停止测量。

### die1-2 测量总览

- 目标：继续测量 `chip7 / die1-2` 阵列腔，设计半径按 `R = 125 um` 处理。
- 版图：三行 gap 约为 `0.90 / 0.95 / 1.00 um`；边缘耦合波导间距 `30 um`；双侧进出。
- 功率标定：input monitor 读数沿用 `1 uW`；按第二个 `99:1` 分束器实测约 `100:1` 反推，入腔主路功率约 `P_in = 100 uW`。
- 统一采集参数：正式大谱为 `1530-1570 nm`、`2 nm/s`、CH1 上升沿 `1 V` 触发、`20 s = 10 s pre + 10 s post`、`500 kSa/s`、`npz-compressed`。

#### die 定位与端面

| 左侧端面 | die 标识 | 右侧端面 |
|---|---|---|
| ![CHIP7 die1-2 左侧端面](<figures/measurement/chip7/die1-2/die左边.jpg>) | ![CHIP7 die1-2 标识](<figures/measurement/chip7/die1-2/die标识.jpg>) | ![CHIP7 die1-2 右侧端面](<figures/measurement/chip7/die1-2/die右边.jpg>) |

#### 腔薄膜总览

<table>
  <tr><th>c1</th><th>c2</th><th>c3</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-2/c1/c1薄膜.jpg" alt="die1-2 c1 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c2/c2薄膜.jpg" alt="die1-2 c2 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c3/c3薄膜掉了.jpg" alt="die1-2 c3 薄膜掉了"></td>
  </tr>
  <tr><td>0.90 um；已测</td><td>0.90 um；已测</td><td>0.90 um；薄膜掉了，跳过</td></tr>
  <tr><th>c4</th><th>c5</th><th>c6</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-2/c4/c4薄膜.jpg" alt="die1-2 c4 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c5/c5薄膜.jpg" alt="die1-2 c5 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c6/c6薄膜.jpg" alt="die1-2 c6 薄膜"></td>
  </tr>
  <tr><td>0.95 um；已测</td><td>0.95 um；已测</td><td>0.95 um；已测</td></tr>
  <tr><th>c7</th><th>c8</th><th>c9</th></tr>
  <tr>
    <td><img src="figures/measurement/chip7/die1-2/c7/c7薄膜.jpg" alt="die1-2 c7 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c8/c8薄膜.jpg" alt="die1-2 c8 薄膜"></td>
    <td><img src="figures/measurement/chip7/die1-2/c9/c9薄膜.jpg" alt="die1-2 c9 薄膜"></td>
  </tr>
  <tr><td>1.00 um；无模式，疑似薄膜破裂，跳过</td><td>1.00 um；已测</td><td>1.00 um；首扫 CH2 饱和作废，重采已测</td></tr>
</table>

#### 每腔状态与 `n_g`

| cavity | gap | status | throughput / single-ended IL | data | `n_g` by family | detail | note |
|---|---:|---|---:|---|---|---|---|
| `c1` | 0.90 um | valid | 6.0% / 6.11 dB | formal | lower 1.8656 (rms 0.887 GHz)<br>upper 1.8565 (rms 1.32 GHz)<br>side 1.8663 (rms 0.753 GHz) | [large_scan_20260529_222227_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c1/large_scan_20260529_222227_1530-1570nm_q_record_summary.md>) | Q 可比；分族可用但 residual 较粗，不精解 D2。 |
| `c2` | 0.90 um | valid | 5.5% / 6.30 dB | formal | lower 1.8685 (rms 0.478 GHz)<br>upper 1.8596 (rms 1.18 GHz)<br>side 1.8692 (rms 0.477 GHz) | [large_scan_20260529_224648_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c2/large_scan_20260529_224648_1530-1570nm_q_record_summary.md>) | Q 可比；upper residual 较粗，不精解 D2；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |
| `c3` | 0.90 um | skipped / damaged film | - | skipped | - | - | 薄膜确认脱落，不测。 |
| `c4` | 0.95 um | valid | 3.7% / 7.16 dB | formal | lower 1.8687 (rms 0.474 GHz)<br>upper 1.8587 (rms 1.37 GHz)<br>side 1.8693 (rms 0.478 GHz) | [large_scan_20260529_225256_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c4/large_scan_20260529_225256_1530-1570nm_q_record_summary.md>) | Q 可比；upper residual 较粗，不精解 D2；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |
| `c5` | 0.95 um | valid | 1.5% / 9.12 dB | formal | lower 1.8692 (rms 1.24 GHz)<br>upper 1.8583 (rms 1.72 GHz)<br>side 1.8692 (rms 0.478 GHz) | [large_scan_20260529_225900_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c5/large_scan_20260529_225900_1530-1570nm_q_record_summary.md>) | Q 可比；deep 分支 residual 较粗，不精解 D2；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |
| `c6` | 0.95 um | valid | 3.0% / 7.61 dB | formal | lower 1.8686 (rms 0.469 GHz)<br>upper 1.8597 (rms 1.19 GHz)<br>side 1.8693 (rms 0.476 GHz) | [large_scan_20260529_230439_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c6/large_scan_20260529_230439_1530-1570nm_q_record_summary.md>) | Q 可比；upper residual 较粗，不精解 D2；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |
| `c7` | 1.00 um | skipped / no modes | 2.0% / 8.49 dB | no formal scan | - | [no_mode_check_20260529_231442.md](<results/chip7/die1-2/c7/no_mode_check_20260529_231442.md>) | 有透过光但未见可用模式；现场判断可能为薄膜破裂，不作 Q / `n_g` 证据。 |
| `c8` | 1.00 um | valid | 3.0% / 7.61 dB | formal | lower 1.8696 (rms 1.23 GHz)<br>upper 1.8583 (rms 1.53 GHz)<br>side 1.8695 (rms 0.477 GHz) | [large_scan_20260529_231737_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c8/large_scan_20260529_231737_1530-1570nm_q_record_summary.md>) | Q 可比；deep 分支 residual 较粗；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |
| `c9` | 1.00 um | valid | 3.8% / 7.10 dB | formal after rescan | lower 1.8692 (rms 0.927 GHz)<br>upper 1.8590 (rms 1.37 GHz)<br>side 1.8694 (rms 0.476 GHz) | [large_scan_20260529_232501_1530-1570nm_q_record_summary.md](<results/chip7/die1-2/c9/large_scan_20260529_232501_1530-1570nm_q_record_summary.md>) | 首扫 CH2 饱和作废；重采通过饱和检查，Q 可比；side 支耦合分支暂记 ambiguous，Q0/Q1 未交换。 |

说明：c1、c2、c4、c5、c6、c8 和 c9 重采 raw-voltage flat-top gate 均通过；未见可见削顶平台。c9 首扫 CH2 饱和作废，仅保留为 invalid 追溯。`lower / upper / side` 是 folded-frequency 分支标签，不表示波长高低。c1、c2、c4、c5、c6、c8 和 c9 的 Q 拟合均为 `74/74` 成功；side 支的 `Tmin/platform` 随波长增大而增大，但这不足以单独判定 overcoupled，且 gap 趋势支持保守处理，因此当前记录中 side 支 Q0/Q1 不交换，耦合分支标记为 ambiguous。

#### Q 趋势图

<table style="width:100%; table-layout:fixed;">
  <tr><th>c1</th><th>c2</th><th>c3</th></tr>
  <tr>
    <td><img src="results/chip7/die1-2/c1/large_scan_20260529_222227_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c1 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-2/c2/large_scan_20260529_224648_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c2 Q trends" style="width:100%; display:block;"></td>
    <td>跳过：薄膜脱落</td>
  </tr>
  <tr><td>gap 0.90 um</td><td>gap 0.90 um</td><td>gap 0.90 um；薄膜掉了</td></tr>
  <tr><th>c4</th><th>c5</th><th>c6</th></tr>
  <tr>
    <td><img src="results/chip7/die1-2/c4/large_scan_20260529_225256_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c4 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-2/c5/large_scan_20260529_225900_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c5 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-2/c6/large_scan_20260529_230439_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c6 Q trends" style="width:100%; display:block;"></td>
  </tr>
  <tr><td>gap 0.95 um</td><td>gap 0.95 um</td><td>gap 0.95 um</td></tr>
  <tr><th>c7</th><th>c8</th><th>c9</th></tr>
  <tr>
    <td>跳过：无可用模式，疑似薄膜破裂</td>
    <td><img src="results/chip7/die1-2/c8/large_scan_20260529_231737_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c8 Q trends" style="width:100%; display:block;"></td>
    <td><img src="results/chip7/die1-2/c9/large_scan_20260529_232501_1530-1570nm_large_scan_q_trends.png" alt="chip7 die1-2 c9 Q trends" style="width:100%; display:block;"></td>
  </tr>
  <tr><td>gap 1.00 um</td><td>gap 1.00 um</td><td>gap 1.00 um</td></tr>
</table>

#### 原始数据索引

| cavity | formal raw data |
|---|---|
| `c1` | `results/chip7/die1-2/c1/large_scan_20260529_222227_1530-1570nm.npz` |
| `c2` | `results/chip7/die1-2/c2/large_scan_20260529_224648_1530-1570nm.npz` |
| `c3` | skipped: film missing |
| `c4` | `results/chip7/die1-2/c4/large_scan_20260529_225256_1530-1570nm.npz` |
| `c5` | `results/chip7/die1-2/c5/large_scan_20260529_225900_1530-1570nm.npz` |
| `c6` | `results/chip7/die1-2/c6/large_scan_20260529_230439_1530-1570nm.npz` |
| `c7` | skipped: no usable modes; suspected broken film; `P_out ~= 2.0 uW` |
| `c8` | `results/chip7/die1-2/c8/large_scan_20260529_231737_1530-1570nm.npz` |
| `c9` | `results/chip7/die1-2/c9/large_scan_20260529_232501_1530-1570nm.npz`; first invalid saturated scan: `results/chip7/die1-2/c9/large_scan_20260529_232142_1530-1570nm.npz` |

阶段性判断：`die1-2 / c1-c2` 的 0.90 um gap、`c4-c6` 的 0.95 um gap 和 `c8-c9` 的 1.00 um gap 均有完整三族 Q 拟合。side 支不再因 `Tmin/platform` 斜率自动判 overcoupled，也不再交换 Q0/Q1；在未交换约定下，side 支低 Q 分支 Q0 随 gap 约为 `0.67M -> 0.74-0.78M -> 0.85-0.86M`，高 Q 分支 Q1 约为 `5.3M -> 6.4-6.9M -> 7.7-8.3M`。这个 gap 趋势更像耦合随 gap 增大而减弱，但具体分支物理含义仍标记为 ambiguous。由于多数组 residual 仍在 `1 GHz` 量级以上，暂不把 die1-2 这些组作为 D2 符号或曲率参考。

## 单组数据记录模板

### YYYY-MM-DD HH:MM 数据组标题

- 样品：chipX / dieX-X / 结构编号。
- 目的或假设：
- 固定变量：
- 扫描变量：
- 仪器状态：
- 偏振状态：
- 读出通道：
- 输入 / 输出端口：
- 原始数据路径：
- 处理脚本：
- 代表图：

事实记录：

- 

关键数值：

| 项目 | 数值 | 备注 |
|---|---:|---|
| 插损 |  |  |
| FSR |  |  |
| 反推群折射率 |  |  |
| 本征 Q |  |  |
| 耦合 Q |  |  |
| 透过率 / 耦合深度 |  |  |

观察：

- 

现场判断：

- 

局限与待确认：

- 

下一步：

- 

## chip / die 状态索引

| 日期 | chip | die / 结构 | 外观状态 | 测量状态 | 数据路径 | 判断 | 待确认 |
|---|---|---|---|---|---|---|---|
| 2026-05-28 | chip1 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip2 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip3 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip4 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip5 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip6 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip7 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip8 | 待补 | 待观察 | 未测 |  |  |  |
| 2026-05-28 | chip9 | 待补 | 待观察 | 未测 |  |  |  |

## 每日进展

### 2026-05-28

事实记录：

- 开始对 2026-05-20 session 中提到的四英寸样品进行正式测量。
- 四英寸样品上共 9 个 chip。
- chip1-3 为吴祖磊样品，主要是薄膜半径较大的超声波传感器，用于光声气体传感。
- chip4-6 为刘健飞样品，主要用于片上磁力仪。
- chip7-9 为本人样品。

现场判断：

- 本 session 将持续数天，涉及样品数量较多，记录重点应放在 chip / die 层级索引、数据有效性判读和可追溯路径上。

待确认：

- 今天使用的具体测量平台、设备、装载方式和通道映射。
- chip 编号与实物摆放位置 / 照片之间的对应关系。
- 今天优先测量的 chip 顺序和每类样品的首要判据。

### 2026-05-29

事实记录：

- 主要完成 `chip7` 的微腔大扫测量、处理、Q 拟合和 session 结构整理。
- `die1-1`：完成 `c1-c8` 的正式大扫/重扫与分析；`c9` 因薄膜开裂且缺口延伸到耦合区域附近，未采大扫数据。`c7` 首扫出现 CH2 饱和，作废后重采并通过饱和检查。
- `die1-2`：确认版图 gap 为 `0.90 / 0.95 / 1.00 um`，修正了不能沿用 `die1-1` gap 的记录问题；完成 `c1`、`c2`、`c4`、`c5`、`c6`、`c8`、`c9` 的正式大扫和三族 Q 拟合，`c3` 薄膜脱落跳过，`c7` 有透过光但未见可用模式，现场判断为薄膜破裂或模式不可用而跳过。
- 功率标定采用 monitor 端 `1 uW`，按第二个 `99:1` 分束器实测约 `100:1` 反推出入腔主路功率约 `100 uW`；session 中记录 throughput 和单端插损，而不是只记录输出功率。
- 正式大扫统一为 `1530-1570 nm`、`2 nm/s`、CH1 上升沿 `1 V` 触发、`20 s = 10 s pre + 10 s post`、`500 kSa/s`、`npz-compressed`。

分析与判断：

- `die1-1 / c4` 的三族色散拟合最干净，适合作为分族和 D2 符号检查的参考；`c6-c8` 的 Q 水平较好，但色散 residual 更粗，当前优先用于 Q / `n_g` 横向比较，不精细解释 D2。
- `die1-2` 的有效腔在 0.90、0.95、1.00 um 三个 gap 行上都有可比 Q 数据，但多数组 residual 在 `1 GHz` 量级以上，因此暂不作为 D2 曲率或符号的强证据。
- 关于 side 支：不再仅因 `Tmin/platform` 随波长增大而自动判定 overcoupled，也不再自动交换 Q0/Q1；在未交换约定下，`die1-2` side 支低 Q 分支 Q0 随 gap 增大约为 `0.67M -> 0.74-0.78M -> 0.85-0.86M`，更像耦合随 gap 增大而减弱，但具体分支物理含义仍标记为 ambiguous。
- 讨论了冷腔/热效应可能对 apparent FSR、`n_g`、Q 的影响：大扫结果可用于比较趋势，但需要避免把热拖拽或分支选择误差直接当作冷腔色散结论。

记录与流程改动：

- 将 `session.md` 从逐腔长段落改为 die 级总览：die 定位/端面照片、薄膜总览表、每腔状态表、Q 趋势图、原始数据索引和阶段性判断。
- 每腔完整证据链下沉到 `*_q_record_summary.md`，主 session 只保留比较所需的 `n_g`、throughput / 单端插损、状态、summary 链接和短判断。
- Q 趋势图改为每个腔三行子图：`Q0`、`Q1`、`Tmin/platform`；删除 loaded linewidth 子图，并按实际阵列行把同 gap 的三个腔横向排在一行。
- `measurement-session` skill 增加固定大扫 fast path：用户说某腔耦合好后，只读当前 session、当前 cavity 的结果和图片目录，不再每轮大范围翻旧文件夹。
- 增加 raw-voltage 饱和 gate：若 CH2/CH3 出现明显平顶/饱和，先停下并标 invalid，不继续处理和拟合；`die1-2 / c9` 首扫因此作废，重扫后保留正式数据。
- 增加显微/薄膜图片读取规则：只读对应 cavity 的指定图片目录，把代表性照片纳入 die/cavity 记录。
- 增加大扫收尾规则和脚本改动：大扫结束回到细扫时，TOPTICA 默认回到 `1550 nm`，再恢复 fine-scan arc factor 和示波器 idle 状态。

待确认 / 下一步：

- 继续后续 die 或 chip 测量时，优先沿用当前 fixed large-scan workflow 和饱和 gate。
- 对 side 支的耦合分支归属仍保留 ambiguous；后续需要结合更多 gap、冷腔扫描或独立耦合模型再判断是否存在过耦合。
- 后续周总结可直接从本日块提取：完成腔数量、跳过原因、主要物理判断、流程改动和待确认问题。
