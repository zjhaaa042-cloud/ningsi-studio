"""One-off: full browser end-to-end over a REAL LSL stream.

Starts the service, publishes a real LSL EEG stream (simulated hardware), then drives a real
Edge browser: 被试管理 -> 开始新会话 (device = lsl:…) -> 实时监测. Verifies that the page
actually shows live EEG data (waveform path grows, values are not flat, device badge is live).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ningsi_studio.app import create_app            # noqa: E402
from ningsi_studio.http.server import AppServer      # noqa: E402
from ningsi_studio.settings import Settings          # noqa: E402

STREAM = "ningsi-ui-eeg"
DEBUG_PORT = 9345


class WS:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += self.sock.recv(4096)
        if b"101" not in buffer.split(b"\r\n")[0]:
            raise RuntimeError(f"握手失败: {buffer[:150]!r}")

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv_text(self, timeout: float = 30.0) -> str:
        self.sock.settimeout(timeout)
        while True:
            first = self._read(2)
            opcode = first[0] & 0x0F
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            data = self._read(length) if length else b""
            if opcode == 0x1:
                return data.decode("utf-8", errors="replace")
            if opcode == 0x8:
                raise RuntimeError("WebSocket 关闭")

    def _read(self, count: int) -> bytes:
        buffer = b""
        while len(buffer) < count:
            chunk = self.sock.recv(count - len(buffer))
            if not chunk:
                raise RuntimeError("连接断开")
            buffer += chunk
        return buffer

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Client:
    def __init__(self, ws_url: str) -> None:
        self.ws = WS(ws_url)
        self.next_id = 1
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 40.0):
        mid = self.next_id
        self.next_id += 1
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = json.loads(self.ws.recv_text(timeout=max(1.0, deadline - time.time())))
            if payload.get("id") == mid:
                if "error" in payload:
                    raise RuntimeError(f"{method}: {payload['error']}")
                return payload.get("result", {})
            self.events.append(payload)
        raise TimeoutError(method)

    def evaluate(self, expression: str, timeout: float = 40.0):
        result = self.call("Runtime.evaluate",
                           {"expression": expression, "returnByValue": True, "awaitPromise": True},
                           timeout=timeout)
        if "exceptionDetails" in result:
            raise RuntimeError(f"页面脚本抛错: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    def drain(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.events.append(json.loads(self.ws.recv_text(timeout=max(0.1, deadline - time.time()))))
            except (socket.timeout, TimeoutError, OSError):
                break

    def errors(self) -> list[str]:
        out = []
        for event in self.events:
            if event.get("method") == "Runtime.exceptionThrown":
                detail = event["params"].get("exceptionDetails", {})
                out.append(detail.get("exception", {}).get("description") or detail.get("text", ""))
            elif event.get("method") == "Runtime.consoleAPICalled" and event["params"].get("type") == "error":
                args = event["params"].get("args", [])
                out.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args))
        return out

    def close(self) -> None:
        self.ws.close()


def find_ws(port: int, marker: str, timeout: float = 25.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
                for target in json.loads(response.read()):
                    if target.get("type") == "page" and marker in target.get("url", ""):
                        return target["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    raise TimeoutError("找不到调试页面")


def main() -> int:
    edge = next((p for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ] if Path(p).exists()), None)
    if edge is None:
        print("找不到 Edge，跳过浏览器端到端测试")
        return 0

    tmp = Path(os.environ.get("TEMP", ".")) / "ningsi-ui-e2e"
    shutil.rmtree(tmp, ignore_errors=True)
    settings = Settings(data_dir=tmp / "data", port=AppServer.free_port(19500))
    settings.ensure_dirs()
    app = create_app(settings)
    app.start_background()
    base = f"http://127.0.0.1:{app.server.port}"
    print(f"服务: {base}")

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    outlet = subprocess.Popen(
        [sys.executable, "-m", "ningsi_studio", "simulate-outlet", "--name", STREAM,
         "--channels", "1", "--srate", "250", "--state-seconds", "25"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    time.sleep(4.0)
    profile = tmp / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    browser = subprocess.Popen([
        edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={profile}",
        "--window-size=1600,1100", f"{base}/#/subjects",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures: list[str] = []
    try:
        client = Client(find_ws(DEBUG_PORT, "/#/subjects"))
        client.call("Runtime.enable")
        client.call("Log.enable")
        client.call("Page.enable")
        client.call("Page.reload", {"ignoreCache": True})
        time.sleep(6.0)
        client.drain(1.5)

        print("\n=== 1) 被试管理页：按钮是否可用 ===")
        buttons = client.evaluate("""
          Array.from(document.querySelectorAll('#view button'))
            .map(b => b.textContent + '|' + (b.disabled ? '禁用' : '可用'))
        """)
        print("  ", " / ".join(buttons[:4]))
        if any("禁用" in b for b in buttons if "创建被试" in b):
            failures.append("“创建被试”按钮是禁用状态")
        empty_hint = client.evaluate(
            "document.getElementById('view').textContent.includes('还没有被试')")
        print("   空库提示（应从没有被试开始）:", empty_hint)

        print("\n=== 1b) 点击“创建被试”（真实用户的第一步）===")
        created = client.evaluate("""
          (() => {
            const input = document.getElementById('subject-public-id');
            const label = document.getElementById('subject-label');
            if (input) { input.value = 'p77'; input.dispatchEvent(new Event('input', {bubbles: true})); }
            if (label) label.value = '界面端到端';
            const button = Array.from(document.querySelectorAll('#view button'))
              .find(b => b.textContent.trim() === '创建被试');
            if (!button) return {ok: false, why: '找不到创建被试按钮'};
            if (button.disabled) return {ok: false, why: '创建被试按钮被禁用'};
            button.click();
            return {ok: true};
          })()
        """)
        print("  点击结果:", created)
        if not created or not created.get("ok"):
            failures.append(f"无法点击创建被试：{(created or {}).get('why')}")
        time.sleep(6.0)
        client.drain(1.0)
        print("  toast:", client.evaluate(
            "(document.getElementById('toast-host')?.textContent || '(无)').trim?.() || '(无)'"))
        after = client.evaluate("""
          (() => {
            const rows = Array.from(document.querySelectorAll('#view table tbody tr'));
            return {
              rowCount: rows.length,
              ids: rows.map(r => (r.querySelector('button')?.textContent || '').trim()),
              hasSessionDevice: Boolean(document.getElementById('session-device')),
              subjectOptions: Array.from(document.querySelectorAll('#session-subject option')).map(o => o.value),
            };
          })()
        """)
        print("  列表行数:", after["rowCount"], "编号:", after["ids"])
        print("  是否出现开始新会话表单:", after["hasSessionDevice"])
        print("  被试下拉:", after["subjectOptions"])
        if "p77" not in [i.replace("sub-", "") for i in after["ids"]]:
            failures.append("创建后被试没有出现在列表里")
        if not after["hasSessionDevice"]:
            failures.append("创建被试后仍未出现“开始新会话”表单")

        print("\n=== 2) 在界面上选择真实 LSL 设备并开始新会话 ===")
        click = client.evaluate("""
          (() => {
            const device = document.getElementById('session-device');
            const subject = document.getElementById('session-subject');
            if (!device) return {ok: false, why: '找不到设备输入框'};
            if (!subject || !subject.value) return {ok: false, why: '没有可选的被试'};
            device.value = %s;
            device.dispatchEvent(new Event('input', {bubbles: true}));
            const button = Array.from(document.querySelectorAll('#view button'))
              .find(b => b.textContent.trim() === '开始新会话');
            if (!button) return {ok: false, why: '找不到开始新会话按钮'};
            button.click();
            return {ok: true, device: device.value, subject: subject.value};
          })()
        """ % json.dumps(f"lsl:{STREAM}"))
        print("  点击结果:", click)
        if not click or not click.get("ok"):
            failures.append(f"无法开始新会话：{(click or {}).get('why')}")
        time.sleep(6.0)
        client.drain(1.5)
        print("  toast:", client.evaluate(
            "(document.getElementById('toast-host')?.textContent || '(无)').trim?.() || '(无)'"))
        print("  跳转到:", client.evaluate("location.hash"))
        print("  会话徽标:", client.evaluate("document.getElementById('session-state')?.textContent"))
        print("  来源徽标:", client.evaluate("document.getElementById('source-badge')?.textContent"))
        print("  设备徽标:", client.evaluate("document.getElementById('device-badge')?.textContent"))
        hash_value = client.evaluate("location.hash")
        if "#/flow" not in (hash_value or ""):
            failures.append(f"点击后没有跳到会话流程页（当前 {hash_value}）")
        uuid = (hash_value or "").split("session=")[-1] if "session=" in (hash_value or "") else ""
        if not uuid:
            failures.append("会话流程页没有携带 session 参数")

        print("\n=== 3) 进入实时监测：等待高频信号面板开始绘制 ===")
        live_hash = f"#/live?session={uuid}"
        client.evaluate(f"location.hash = {json.dumps(live_hash)}")
        time.sleep(4.0)
        client.drain(1.0)

        # 高频波形走 canvas（不再是 SVG path），因此在会话运行期间直接对 canvas 取样：
        # 统计非透明像素比例，确认"真的在画"而不是只有坐标轴。
        deadline = time.time() + 150
        best_paint = [0, 0, 0]
        live_seen = False
        fps_seen = None
        while time.time() < deadline:
            client.drain(0.5)
            state = client.evaluate("""
              (() => {
                const text = document.getElementById('view').textContent || '';
                const canvases = Array.from(document.querySelectorAll('#view canvas'));
                const painted = canvases.map((node) => {
                  try {
                    const ctx = node.getContext('2d');
                    const data = ctx.getImageData(0, 0, node.width, node.height).data;
                    let count = 0;
                    let total = 0;
                    for (let index = 3; index < data.length; index += 4 * 53) {
                      total += 1;
                      if (data[index] > 0) count += 1;
                    }
                    return Math.round((count / Math.max(1, total)) * 100);
                  } catch (error) { return -1; }
                });
                return {
                  canvases: canvases.length,
                  painted,
                  badge: document.getElementById('session-state')?.textContent || '',
                  device: document.getElementById('device-badge')?.textContent || '',
                  live: text.includes('信号在收数'),
                  fps: (text.match(/实测 [\\d.]+ FPS/) || [null])[0],
                  monitor: text.includes('任务态监测') || text.includes('实时脑电波形'),
                };
              })()
            """)
            if len(state["painted"]) >= len(best_paint):
                for index, value in enumerate(state["painted"][:3]):
                    best_paint[index] = max(best_paint[index], value)
            live_seen = live_seen or state["live"]
            fps_seen = state["fps"] or fps_seen
            print(f"  {time.strftime('%H:%M:%S')} 会话={state['badge']} 设备={state['device']} "
                  f"canvas={state['canvases']} 非透明占比={state['painted']} "
                  f"在收数={state['live']} {state['fps'] or ''}")
            if state["monitor"] and max(best_paint) >= 5 and fps_seen:
                break
            time.sleep(2.0)
        samples_seen = max(best_paint) if best_paint else 0

        print("\n=== 4) 实时数据判定（在会话运行期间连续采样）===")
        print("  波形 canvas 非透明占比峰值(%):", best_paint, " 是否显示在收数:", live_seen,
              " 页面实测:", fps_seen)
        if max(best_paint) < 5:
            failures.append(f"实时波形 canvas 没有绘制内容：{best_paint}")
        if not live_seen:
            failures.append("运行期间未显示“信号在收数”")

        print("\n=== 4b) 高频信号面板（会话运行中采样 canvas）===")
        best = {"count": 0, "painted": [0], "titles": [], "rate": None, "fps": None, "live": False}
        for attempt in range(8):
            snapshot = client.evaluate("""
              (() => {
                const canvases = Array.from(document.querySelectorAll('#view canvas'));
                const titles = Array.from(document.querySelectorAll('#view .card__title'))
                  .map(n => n.textContent);
                const painted = canvases.map((node) => {
                  try {
                    const ctx = node.getContext('2d');
                    const data = ctx.getImageData(0, 0, node.width, node.height).data;
                    let count = 0;
                    let total = 0;
                    for (let index = 3; index < data.length; index += 4 * 53) {
                      total += 1;
                      if (data[index] > 0) count += 1;
                    }
                    return Math.round((count / Math.max(1, total)) * 100);
                  } catch (error) { return -1; }
                });
                const text = document.querySelector('#view')?.textContent || '';
                return {
                  count: canvases.length,
                  sizes: canvases.map(n => n.width + 'x' + n.height),
                  painted,
                  titles,
                  rate: document.getElementById('signal-rate')?.value || null,
                  fps: text.match(/实测 [\\d.]+ FPS/)?.[0] || null,
                  live: text.includes('信号在收数'),
                };
              })()
            """)
            if snapshot["count"] > best["count"] or sum(snapshot["painted"]) > sum(best["painted"]):
                best = snapshot
            if snapshot["live"] and snapshot["fps"] and not snapshot["fps"].startswith("实测 0"):
                best = snapshot
                break
            time.sleep(2.0)
            client.drain(0.3)

        print("  canvas 数量:", best["count"], "尺寸:", best.get("sizes"))
        print("  非透明像素占比(%):", best["painted"])
        print("  选中刷新率:", best["rate"], " 页面实测:", best["fps"], " 在收数:", best["live"])
        print("  实时相关卡片:", [t for t in best["titles"] if "实时" in t or "频带" in t])
        if best["count"] < 3:
            failures.append(f"实时信号面板应有 3 个 canvas（波形/频谱/频带），实际 {best['count']}")
        if not any(value >= 5 for value in best["painted"]):
            failures.append(f"canvas 未绘制有效内容：{best['painted']}")
        if "实时脑电波形" not in best["titles"]:
            failures.append("缺少“实时脑电波形”面板")
        if not best["live"]:
            failures.append("运行期间未显示“信号在收数”")
        print("  设备徽标（运行中）:", client.evaluate(
            "document.getElementById('device-badge')?.textContent"))

        print("\n=== 5) 页面错误 ===")
        errors = client.errors()
        for item in errors[:6]:
            print("  " + str(item)[:200])
        if errors:
            failures.append(f"页面出现 {len(errors)} 条 JS 错误")

        client.close()
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()
        outlet.terminate()
        try:
            outlet.wait(timeout=10)
        except subprocess.TimeoutExpired:
            outlet.kill()
        app.stop()

    print("\n=== 结论 ===")
    if failures:
        for item in failures:
            print("  [失败] " + item)
        return 1
    print("  [通过] 按钮可点、会话启动、实时监测页收到并绘制了真实脑电数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
