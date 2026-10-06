"""命令行入口：ningsi-studio serve / doctor / demo / export-ledger。

只做参数解析与流程编排，业务逻辑在 api / core / domain 三层。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ningsi_studio import __version__, bootstrap
from ningsi_studio.app import run as run_app
from ningsi_studio.core import paired_ledger
from ningsi_studio.db import repository as repo
from ningsi_studio.db import sqlite_store as store
from ningsi_studio.settings import Settings, default_web_dir, is_frozen


def _quiet_third_party_noise() -> None:
    """压掉第三方库在控制台刷的 INFO 日志。

    pylsl 每次建流都会把本机所有网卡、liblsl 版本、默认配置逐行打印出来，
    一次会话几十行，把真正有用的输出淹掉。这里只保留 WARNING 及以上。
    """
    logging.getLogger("ningsi.lsl").setLevel(logging.INFO)
    for name in ("pylsl", "liblsl", "LSL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _ensure_importable() -> None:
    """把本仓库的 src/ 加进 sys.path 的最前面。

    为什么需要：用 `Start-Process` / `subprocess` 直接拉起 `python -m ningsi_studio`
    时环境变量不一定被继承，只要 PYTHONPATH 丢了就会报 "No module named ningsi_studio"。
    这里以文件自身位置推断包根目录，保证任何启动方式都能工作。
    """
    src_dir = Path(__file__).resolve().parents[1]      # .../src
    text = str(src_dir)
    if src_dir.exists() and text not in sys.path:
        sys.path.insert(0, text)


def _settings(args) -> Settings:
    settings = Settings()
    for name, target in (("host", "host"), ("port", "port")):
        value = getattr(args, name, None)
        if value:
            setattr(settings, target, int(value) if name == "port" else value)
    data = getattr(args, "data", None)
    if data:
        settings.data_dir = Path(data)
    web = getattr(args, "web", None)
    if web:
        settings.web_dir = Path(web)
    else:
        # 前端目录：源码运行取仓库 web/，打包后取包内 web/（见 settings.default_web_dir）
        settings.web_dir = default_web_dir()
    settings.open_browser = bool(getattr(args, "open", False))
    token = getattr(args, "token", None)
    if token:
        settings.api_token = token
    return settings


def _serve(args) -> int:
    _ensure_importable()
    settings = _settings(args)
    run_app(settings)
    return 0


def _doctor(args) -> int:
    settings = _settings(args)
    settings.ensure_dirs()
    db_path = store.initialize(settings.db_path)
    engine_versions = bootstrap.engine_versions()

    def row(label: str, value: str = "") -> None:
        # 中文标签宽度按 2 计，用自算填充保证列对齐
        width = sum(2 if ord(ch) > 0x2000 else 1 for ch in label)
        print("  " + label + " " * max(1, 22 - width) + value)

    print(f"凝思 Studio {__version__}")
    row("运行方式", (f"打包 exe（{Path(sys.executable).name}，包内资源 "
                     f"{Path(getattr(sys, '_MEIPASS', '')).name or '-'}）" if is_frozen()
                     else "源码运行（python -m ningsi_studio）"))
    row("算法引擎 ningsi", bootstrap.ENGINE_PATH)
    for name, path in (("前端页面 web/index.html", settings.web_dir / "index.html"),
                       ("数据目录", settings.data_dir), ("数据库", db_path)):
        row(name, "存在" if Path(path).exists() else "缺失")
    row("引擎口径版本", json.dumps(engine_versions, ensure_ascii=False))
    try:
        import numpy
        row("numpy", numpy.__version__)
    except ImportError:
        row("numpy", "缺失（信号处理需要，请先安装）")
    try:
        import pylsl
        row("pylsl", f"{getattr(pylsl, '__version__', '已安装')}（可接真实脑电设备）")
    except ImportError:
        row("pylsl", "未安装 -> 只能用仿真源（pip install pylsl 可接真实设备）")
    with store.read_only(db_path) as conn:
        overview = repo.overview(conn)
    row("库内统计", f"被试 {overview['subjects']} 名 / 会话 {overview['sessions']} 次"
                    f"（已完成 {overview['sessions_done']} 次）/ 预警 {overview['alerts']} 条")
    return 0


def _demo(args) -> int:
    """无浏览器跑一次完整会话（快速模式），用于自测与录制脚本。"""
    import time

    from ningsi_studio.api import routes
    from ningsi_studio.core.runtime import SessionManager

    settings = _settings(args)
    settings.ensure_dirs()
    db_path = store.initialize(settings.db_path)
    router = routes.build_router(settings)
    manager: SessionManager = getattr(router, "manager")

    # 编号走与接口层同一套正规化，避免 demo 把非法编号写进库
    from ningsi_studio.api import schemas as api_schemas

    short = api_schemas.normalize_public_id(args.participant)
    with store.connect(db_path) as conn:
        subject = repo.find_subject(conn, short)
        subject_id = subject["id"] if subject else repo.create_subject(conn, short, label="演示被试")
        row = repo.create_session(conn, subject_id, device="sim-bsense", srate=250.0, channels=1,
                                  time_scale=float(getattr(args, "speed", 0.05)),
                                  training_mode="quick",
                                  engine_versions=bootstrap.engine_versions())
    runtime = manager.start(row["uuid"])
    print(f"会话 {row['uuid']} 已启动（time_scale={runtime.scale}），等待完成 ...", flush=True)
    deadline = time.time() + 900
    while runtime.alive and time.time() < deadline:
        time.sleep(0.5)
    runtime.join(timeout=5)
    with store.read_only(db_path) as conn:
        final = repo.get_session(conn, row["uuid"])
        artifacts = repo.list_artifacts(conn, final["id"])
    print(json.dumps({"uuid": row["uuid"], "status": final["status"], "phase": final["phase"],
                      "progress": final["progress"], "error": final["error"],
                      "artifacts": [{"kind": item["kind"], "path": item["path"]} for item in artifacts]},
                     ensure_ascii=False, indent=2))
    manager.shutdown()
    return 0 if final["status"] == "done" else 1


def _check(args) -> int:
    if is_frozen():
        # 静态自检要读源码树（src/tests/web/docs），打包后这些文件不在包内
        print("静态自检需要源码树（src/、tests/、web/、docs/），打包后的 exe 里没有；")
        print("请在源码目录执行：python -m ningsi_studio check")
        return 2
    from ningsi_studio.tools import static_check

    return static_check.main()


def _lsl_check(args) -> int:
    """LSL 链路自检：装了 pylsl 吗、能扫到哪些流、样本速率是否正常。"""
    from ningsi_studio.acquisition import lsl as lsl_module

    print("凝思 Studio · LSL 链路自检")
    try:
        import pylsl
        print(f"  pylsl        : {pylsl.__version__}")
    except ImportError:
        print("  pylsl        : 未安装 -> 真实设备不可用（pip install pylsl）")
        return 1

    try:
        found = lsl_module.probe_streams(timeout=float(args.timeout))
    except lsl_module.LslUnavailable as error:
        print(f"  扫描         : 失败 -> {error}")
        return 1
    print(f"  扫描结果     : {len(found)} 条流")
    for item in found:
        flag = "支持" if item.get("supported") else "忽略"
        print(f"    - [{flag}] {item.get('name')} / type={item.get('stream_type')} / "
              f"{item.get('channel_count')}ch @ {float(item.get('nominal_srate') or 0):.0f}Hz "
              f"/ kind={item.get('kind')}")
    if not found:
        print("  提示         : 先用内置仿真流验证链路："
              "python -m ningsi_studio simulate-outlet --duration 60")
        return 0

    if args.device:
        name = args.device.split(":", 1)[1] if args.device.startswith("lsl:") else args.device
        print(f"  连接测试     : {name}（接收 {args.seconds:.0f} 秒）")
        manager = lsl_module.LiveStreamManager(buffer_seconds=30.0)
        manager.start()
        try:
            ready = manager.wait_for("eeg", timeout=float(args.timeout) + 5.0, min_samples=1)
            print(f"  是否就绪     : {ready}")
            if ready:
                import time as _time
                _time.sleep(float(args.seconds))
                stats = manager.status()["streams"].get("eeg", {})
                descriptor = manager.descriptor("eeg")
                print(f"  流信息       : {descriptor.label if descriptor else '-'}")
                print(f"  实测速率     : {stats.get('observed_srate')} Hz（标称 "
                      f"{descriptor.nominal_srate if descriptor else 0:.0f} Hz）"
                      f"｜缓冲 {stats.get('buffered_samples')} 个样本")
                print(f"  实时性       : {'在收数' if stats.get('live') else '已停/掉线'}"
                      f"（最近样本 {stats.get('seconds_since_last')} 秒前）")
                errors = manager.errors()
                if errors:
                    print(f"  错误         : {errors}")
        finally:
            manager.stop()
    print("  结论         : LSL 链路可用")
    return 0


def _simulate_outlet(args) -> int:
    """发布一条仿真 LSL 流，让没有硬件的人也能验证整条采集链路。"""
    from ningsi_studio.acquisition.sim_outlet import run_outlet

    return run_outlet(name=args.name, channels=args.channels, srate=args.srate,
                      seed=args.seed, state_seconds=args.state_seconds,
                      duration=args.duration)


def _ui_check(args) -> int:
    """真实浏览器端到端：起服务 + 起仿真 LSL 流，点按钮、看实时波形。"""
    import runpy

    script = Path(__file__).resolve().parents[2] / "scripts" / "ui_check.py"
    if not script.exists():
        print(f"找不到界面自检脚本：{script}")
        if is_frozen():
            print("（界面自检需要源码树里的 scripts/ui_check.py，打包后的 exe 里没有）")
        return 2
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    return 0


def _export_ledger(args) -> int:
    settings = _settings(args)
    db_path = store.initialize(settings.db_path)
    with store.read_only(db_path) as conn:
        records = repo.all_sessions_for_ledger(conn)
    target = paired_ledger.rebuild_from_records(settings.data_dir, records)
    print(f"已从库中重放 {len(records)} 条记录到 {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ningsi-studio",
        description="凝思 Studio：A09 便携脑电专注力训练与心理状态评估的 Web 应用",
    )
    parser.add_argument("--version", action="version", version=f"ningsi-studio {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 Web 服务（界面 + 接口）")
    serve.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    serve.add_argument("--data", default=None, help="数据目录（默认 var/studio）")
    serve.add_argument("--web", default=None, help="前端目录（默认仓库内 web/）")
    serve.add_argument("--token", default=None, help="设置后所有 /api 请求需带 X-API-Token")
    serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    serve.set_defaults(func=_serve)

    doctor = sub.add_parser("doctor", help="环境自检：引擎、前端资源、依赖、库状态")
    doctor.add_argument("--data", default=None, help="数据目录（默认 var/studio）")
    doctor.set_defaults(func=_doctor)

    check = sub.add_parser("check", help="静态自检：语法、接口/事件文档、前端资源与接口调用")
    check.set_defaults(func=_check)

    lslcheck = sub.add_parser("lsl-check", help="LSL 链路自检：扫描流并实测样本速率")
    lslcheck.add_argument("--device", default=None, help="要连接的流名，如 lsl:ningsi-sim-eeg")
    lslcheck.add_argument("--timeout", type=float, default=3.0, help="扫描超时（秒）")
    lslcheck.add_argument("--seconds", type=float, default=5.0, help="连接后接收时长（秒）")
    lslcheck.set_defaults(func=_lsl_check)

    outlet = sub.add_parser("simulate-outlet",
                            help="发布一条仿真 EEG 的 LSL 流（无需硬件即可验证采集链路）")
    outlet.add_argument("--name", default="ningsi-sim-eeg", help="流名称（设备名填 lsl:<流名>）")
    outlet.add_argument("--channels", type=int, default=1, help="通道数（默认 1）")
    outlet.add_argument("--srate", type=float, default=250.0, help="采样率（默认 250 Hz）")
    outlet.add_argument("--seed", type=int, default=7, help="随机种子（可复现）")
    outlet.add_argument("--state-seconds", type=float, default=20.0,
                        help="每个状态（静息/专注/困倦/高负荷）持续秒数")
    outlet.add_argument("--duration", type=float, default=None, help="发布多少秒后自动停止")
    outlet.set_defaults(func=_simulate_outlet)

    uicheck = sub.add_parser("ui-check",
                             help="真实浏览器端到端自检：点按钮 + 验证实时波形（需安装 Edge）")
    uicheck.set_defaults(func=_ui_check)

    demo = sub.add_parser("demo", help="不开浏览器跑一次完整会话（快速模式）")
    demo.add_argument("--participant", default="p01", help="被试编号（默认 p01）")
    demo.add_argument("--speed", type=float, default=0.05, help="时间倍率，越小越快")
    demo.add_argument("--data", default=None, help="数据目录（默认 var/studio）")
    demo.set_defaults(func=_demo)

    ledger = sub.add_parser("export-ledger", help="从数据库重放 JSONL 审计台账")
    ledger.add_argument("--data", default=None, help="数据目录（默认 var/studio）")
    ledger.set_defaults(func=_export_ledger)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if is_frozen():
            # 双击 exe 不会带子命令：默认启动服务并打开浏览器（exe 的常见用法）；
            # 想只起服务不弹浏览器就写 `ningsi-studio.exe serve`。
            args = parser.parse_args(["serve", "--open"])
        else:
            parser.print_help()
            return 1
    _quiet_third_party_noise()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
