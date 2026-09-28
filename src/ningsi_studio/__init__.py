"""凝思 Studio：A09 便携脑电专注力训练与心理状态评估的 Web 应用。

本包只做「产品层」的三件事：HTTP 服务、数据保存、界面呈现；
全部信号处理、量表、行为任务、联合评估、训练与模型能力复用上游 `ningsi` 引擎。
"""

from ningsi_studio import bootstrap  # noqa: F401  (导入即完成引擎路径兜底)

__version__ = "0.1.0"
