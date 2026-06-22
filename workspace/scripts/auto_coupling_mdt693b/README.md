# MDT693B 自动耦合工作流

## 适用场景

用于 NanoMax300 + MDT693B 控制的光纤-芯片耦合精调。人先用红光和显微镜完成粗对准，程序只负责在已有通光或接近通光状态下自动提高耦合功率。

## 标准流程

1. 人工粗对准。
   - 先用红光确认光纤和波导水平位置大致对齐。
   - 程序不负责盲扫全空间。

2. TOPTICA dashboard 中点击 `Run standard auto coupling`。
   - 按瞬时光功率 `P inst` 判断趋势，不使用长平均功率。
   - 每个轴先朝正方向单调步进；如果没有明显提升，则回到初始点改扫反方向。
   - 当功率低于本轮初始功率连续 3 点时，认为已经越过峰值并停止该方向。
   - 回到扫描过程中功率最大的电压点，并尽量从同一方向接近以减小压电迟滞影响。

3. 第一轮大过程。
   - 主对准轴：`COM7:z COM7:y COM6:z COM6:x`
   - 主对准范围：`20,5,1 V`
   - 主对准步进：`1,0.3,0.1 V`
   - 距离轴：`COM7:x COM6:y`
   - 距离轴范围：`20,6,2 V`
   - 距离轴步进：`2,0.6,0.2 V`

4. 第二轮优化过程。
   - 轴顺序：`COM7:z COM7:y COM6:z COM6:x COM7:x COM6:y`
   - 范围：`5,2,1 V`
   - 步进：`0.5,0.3,0.1 V`

5. 耦合完成后，读取出腔功率并在 dashboard 的 Large-Scan Q 区域计算/保存插损。

## 命令行等价调用

第一轮：

```powershell
python D:\daily_note_v2\workspace\scripts\auto_coupling_mdt693b\monotonic_step_optimize_coupling.py --execute --axis-order COM7:z COM7:y COM6:z COM6:x --round-steps-v 1,0.3,0.1 --round-max-travel-v 20,5,1 --distance-axis-order COM7:x COM6:y --distance-round-steps-v 2,0.6,0.2 --distance-round-max-travel-v 20,6,2 --power-kind inst --channel CH1 --bridge http://127.0.0.1:7870 --min-v 0 --max-v 75
```

第二轮：

```powershell
python D:\daily_note_v2\workspace\scripts\auto_coupling_mdt693b\monotonic_step_optimize_coupling.py --execute --axis-order COM7:z COM7:y COM6:z COM6:x COM7:x COM6:y --round-steps-v 0.5,0.3,0.1 --round-max-travel-v 5,2,1 --power-kind inst --channel CH1 --bridge http://127.0.0.1:7870 --min-v 0 --max-v 75
```
