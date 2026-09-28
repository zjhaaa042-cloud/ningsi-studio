"""量表：题干下发与计分（口径完全复用引擎 `ningsi.scales`）。"""

from __future__ import annotations

from ningsi import config
from ningsi.scales.instruments import OPTIONS, get_scale
from ningsi.scales.scoring import level_of, score_scale

CODES = ("SAS", "SDS", "SAD")


def define(code: str) -> dict:
    """前端渲染用的量表定义：题干、选项、反向题索引（计分口径不在前端）。"""
    scale = get_scale(code)
    return {
        "code": scale.code,
        "name": scale.name,
        "version": scale.version,
        "factor": scale.factor,
        "size": scale.size,
        "options": [{"value": index + 1, "text": text} for index, text in enumerate(OPTIONS)],
        "items": [
            {"index": index + 1, "text": text, "reverse": bool(reverse)}
            for index, (text, reverse) in enumerate(scale.items)
        ],
        "reverse_items": list(scale.reverse_indices()),
        "standard_factor": config.SAS_STANDARD_FACTOR if scale.code == "SAS" else config.SDS_STANDARD_FACTOR,
        "boundaries": dict(config.SCALE_BOUNDARY),
        "note": "标准分 = 粗分 × 1.25 取整；反向题按 5 − 作答值反转，量表结果只作提示、不作诊断。",
    }


def catalog() -> list[dict]:
    return [
        {"code": code, "name": get_scale(code).name, "size": get_scale(code).size,
         "version": get_scale(code).version, "estimate_minutes": 5}
        for code in ("SAS", "SDS")
    ]


def normalize_responses(responses) -> list[int]:
    if responses is None:
        raise ValueError("缺少作答 responses")
    if isinstance(responses, dict):
        ordered = [responses.get(str(index), responses.get(index)) for index in range(1, 21)]
    else:
        ordered = list(responses)
    values = []
    for item in ordered:
        if item is None or item == "":
            values.append(0)
            continue
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"作答值必须是 1–4 的整数：{item!r}") from exc
        values.append(number)
    return values


def score(code: str, responses) -> dict:
    """计分并返回可直接入库的结构；不合法时抛 ValueError。"""
    values = normalize_responses(responses)
    if len(values) != 20:
        raise ValueError(f"{code} 需要 20 个作答值，收到 {len(values)} 个")
    illegal = [value for value in values if value not in (1, 2, 3, 4)]
    if illegal:
        raise ValueError(f"作答值必须在 1–4 之间：{illegal[:5]}")
    result = score_scale(code, values)
    payload = result.as_dict()
    payload["level"] = payload.get("level") or level_of(payload["standard_score"])
    payload["responses"] = values
    return payload


def score_objects(answers_by_code: dict) -> dict:
    """批量为流水线准备 ScaleResult 对象（供联合评估使用）。"""
    out = {}
    for code, responses in (answers_by_code or {}).items():
        if not responses:
            continue
        out[code.upper()] = score_scale(code.upper(), responses)
    return out
