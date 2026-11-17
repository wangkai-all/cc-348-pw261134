"""漂移时钟校时与可追溯时间轴模拟系统。

模块划分：
- clocks:      参考时钟、分段漂移设备时钟、设备模拟器
- model:       测量记录 / 校时点 / 校正记录 / 边界 / 版本的数据模型
- receiver:    接收服务（原始时间 + 到达顺序，到达顺序不用于真实排序）
- calibration: 分段线性校时映射、不确定区间、时间轴与不可排序边界
- calculator:  校时计算器（暂停/恢复、版本栅栏、局部重算 vs 重建）
- publisher:   入库端（只发布已确认的校时版本）
"""

__all__ = [
    "clocks",
    "model",
    "receiver",
    "calibration",
    "calculator",
    "publisher",
    "app",
    "server",
]
