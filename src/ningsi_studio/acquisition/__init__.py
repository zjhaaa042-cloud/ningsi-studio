"""采集层：真实设备（LSL）与仿真源。

- `lsl`：常驻缓冲的 LSL 采集（对接任意厂商的 LSL 流）
- `sim_outlet`：把仿真脑电发布成真实 LSL 流，用于无硬件时验证整条链路
"""

__all__ = ["lsl", "sim_outlet"]
