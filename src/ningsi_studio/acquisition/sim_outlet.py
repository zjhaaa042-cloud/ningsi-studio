"""仿真 LSL 数据源（虚线设备）：把仿真脑电发布成一条真实的 LSL EEG 流。

为什么要它：真实设备（如 BioMultiLite）不一定随时在手边，而"接入 LSL"这条链路
能不能用、有没有丢包、断开能不能重连，只有真的经过 LSL 才知道。这个 outlet 让
没有硬件的人也能把整条 LSL 链路跑通——上层代码完全不知道对面是仿真还是设备。

用法（两个终端）：

```powershell
# 终端 1：发布一条仿真 EEG 流（默认 250 Hz、1 通道、type=EEG）
python -m ningsi_studio simulate-outlet --channels 1 --srate 250 --state rest

# 终端 2：启动服务，并在设备列表里选择 lsl:ningsi-sim-eeg
python -m ningsi_studio serve --port 8765
```
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ningsi.acquisition.simulate import SyntheticEEG

LOGGER = logging.getLogger("ningsi.lsl.sim")

DEFAULT_STREAM_NAME = "ningsi-sim-eeg"
DEFAULT_STREAM_TYPE = "EEG"
DEFAULT_SOURCE_ID = "ningsi-sim-outlet-v1"
DEFAULT_CHANNEL_LABELS = ("Fp1", "Fp2", "F3", "F4")

# 轮换状态：让仿真流里既有"专注"也有"困倦"，便于看指标是否真的在动
STATE_CYCLE = ("rest", "focused", "drowsy", "rest", "focused", "loaded")


class SimulatedEegOutlet:
    """按真实采样率推送仿真脑电的 LSL outlet。

    - 用 `deque` 积压样本、单线程按节拍推送，保证推送的时间戳是**真实单调**的；
    - 每个状态持续 `state_seconds` 秒后轮换，这样指标会随时间变化；
    - `chunk_size` 控制每次推送样本数，默认 10 个（250 Hz 下每 40 ms 一次）。
    """

    def __init__(self, *, name: str = DEFAULT_STREAM_NAME, stream_type: str = DEFAULT_STREAM_TYPE,
                 channels: int = 1, srate: float = 250.0, seed: int = 7,
                 chunk_size: int = 10, state_seconds: float = 20.0) -> None:
        if channels < 1:
            raise ValueError("channels 必须 >= 1")
        if srate <= 0:
            raise ValueError("srate 必须 > 0")
        self.name = name
        self.stream_type = stream_type
        self.channels = int(channels)
        self.srate = float(srate)
        self.seed = int(seed)
        self.chunk_size = max(1, int(chunk_size))
        self.state_seconds = float(state_seconds)

        self._sim = SyntheticEEG(srate=self.srate, channels=self.channels, seed=self.seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._outlet = None
        self.pushed_samples = 0
        self.push_errors = 0
        self.states_published: list = []

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> dict:
        try:
            from pylsl import StreamInfo, StreamOutlet, cf_float32
        except ImportError as error:
            raise RuntimeError("未安装 pylsl，无法发布仿真 LSL 流（pip install pylsl）") from error

        info = StreamInfo(self.name, self.stream_type, self.channels, self.srate, cf_float32,
                          DEFAULT_SOURCE_ID)
        channels = info.desc().append_child("channels")
        for index in range(self.channels):
            label = DEFAULT_CHANNEL_LABELS[index] if index < len(DEFAULT_CHANNEL_LABELS) else f"Ch{index + 1}"
            channels.append_child("channel").append_child_value("label", label)
        info.desc().append_child_value("manufacturer", "ningsi-studio-simulator")
        self._outlet = StreamOutlet(info, chunk_size=self.chunk_size, max_buffered=max(1, int(self.srate)))
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, name="sim-lsl-outlet", daemon=True)
        self._thread.start()
        LOGGER.info("仿真 LSL 流已发布：%s（%d ch @ %.0f Hz）", self.name, self.channels, self.srate)
        return {
            "name": self.name,
            "type": self.stream_type,
            "channels": self.channels,
            "srate": self.srate,
            "chunk_size": self.chunk_size,
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._outlet = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 推送
    def _pump(self) -> None:
        interval = self.chunk_size / self.srate
        next_tick = time.monotonic()
        state_index = 0
        state_started = time.monotonic()
        current_state = STATE_CYCLE[0]
        self.states_published.append(current_state)

        while not self._stop.is_set():
            # 到点就换状态，让指标随时间变化
            if (time.monotonic() - state_started) >= self.state_seconds:
                state_index = (state_index + 1) % len(STATE_CYCLE)
                current_state = STATE_CYCLE[state_index]
                state_started = time.monotonic()
                self.states_published.append(current_state)

            block = self._sim.window(current_state, seconds=self.chunk_size / self.srate)
            try:
                self._outlet.push_chunk([list(row) for row in block.T])
                self.pushed_samples += block.shape[1]
                self.push_errors = 0
            except Exception as caught:  # noqa: BLE001 - 短暂发送失败不该让整条流消失
                self.push_errors += 1
                if self.push_errors <= 3 or self.push_errors % 50 == 0:
                    LOGGER.warning("推送失败（第 %d 次）：%s", self.push_errors, caught)
                if self._outlet is None:
                    return
                time.sleep(0.1)

            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()      # 落后了就对齐，避免越堆越多


def run_outlet(*, name: str = DEFAULT_STREAM_NAME, channels: int = 1, srate: float = 250.0,
               seed: int = 7, state_seconds: float = 20.0, duration: float | None = None,
               on_ready=None) -> int:
    """发布仿真流并阻塞（Ctrl+C 停止）；`duration` 不为空时到点自动结束。"""
    outlet = SimulatedEegOutlet(name=name, channels=channels, srate=srate, seed=seed,
                                state_seconds=state_seconds)
    info = outlet.start()
    print(f"仿真 LSL 流已发布：name={info['name']} type={info['type']} "
          f"channels={info['channels']} srate={info['srate']:.0f}Hz", flush=True)
    print("在凝思 Studio 的 /api/devices 里应能看到它，设备名填 lsl:" + info["name"], flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    if on_ready is not None:
        on_ready(info)
    try:
        deadline = None if duration is None else time.time() + float(duration)
        while outlet.running:
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…", flush=True)
    finally:
        pushed = outlet.pushed_samples
        outlet.stop()
    print(f"已停止，共推送 {pushed} 个样本。", flush=True)
    return 0
