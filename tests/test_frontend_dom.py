"""DOM 工厂的行为测试：布尔属性必须按 HTML 语义处理。

背景（真实事故）：`el()` 以前对每个 prop 都调用 `setAttribute`，于是
`button(..., { disabled: false })` 变成 `setAttribute('disabled', 'false')`。
HTML 里布尔属性"存在即生效"，与值无关，所以 **页面上所有按钮都被禁用**，
表现为"创建被试 / 开始新会话都点不动"。

`web/js/util.js` 是原生 ES 模块，Node 可以直接 import；只需要一个能装
class / dataset / attribute / textContent / style 的极简元素桩。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from .helpers import StudioTestCase

ROOT = Path(__file__).resolve().parents[1]
UTIL_JS = ROOT / "web" / "js" / "util.js"

# 供 Node 使用的元素桩：只实现 util.js 用到的成员
NODE_STUB = r"""
class ClassList {
  constructor(node) { this.node = node; }
  get value() { return this.node.attributes.class || ''; }
  toggle(name, force) {
    const classes = new Set(this.value.split(/\s+/).filter(Boolean));
    const wanted = force === undefined ? !classes.has(name) : Boolean(force);
    if (wanted) classes.add(name); else classes.delete(name);
    this.node.attributes.class = Array.from(classes).join(' ');
  }
  contains(name) { return this.value.split(/\s+/).includes(name); }
}

class StubNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.dataset = {};
    this._style = {};
    this.textContent = '';
    this.children = [];
    this.listeners = {};
    this.classList = new ClassList(this);
    this.scrollIntoView = () => {};
  }
  /** 真实浏览器里 style 是 CSSStyleDeclaration，这里至少要把 width 这类声明解析出来 */
  get style() { return this._style; }
  set style(text) {
    this._style = {};
    String(text || '').split(';').forEach((pair) => {
      const index = pair.indexOf(':');
      if (index > 0) {
        const key = pair.slice(0, index).trim().replace(/-([a-z])/g, (m, ch) => ch.toUpperCase());
        this._style[key] = pair.slice(index + 1).trim();
      }
    });
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null; }
  hasAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key); }
  removeAttribute(key) { delete this.attributes[key]; }
  appendChild(child) { this.children.push(child); return child; }
  append(child) { this.children.push(child); }
  prepend(child) { this.children.unshift(child); }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  /** 浏览器语义：布尔属性存在即为真 */
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) {
    // 注意：真实浏览器里 node.disabled = false 会移除属性，util.js 用 removeAttribute 已覆盖
    if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled');
  }
  get value() { return this.attributes.value || ''; }
  set value(next) { this.attributes.value = String(next); }
  get className() { return this.attributes.class || ''; }
  set className(next) { this.attributes.class = String(next); }
}

globalThis.document = {
  createElement: (tag) => new StubNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
};
globalThis.Node = StubNode;
"""


def _node_available() -> bool:
    return shutil.which("node") is not None


class UtilDomTest(StudioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not _node_available():
            raise unittest.SkipTest("未安装 node，跳过前端 DOM 测试")

    def run_util(self, script: str) -> dict:
        """在 Node 里加载 util.js 并执行一段脚本，返回其 JSON 结果。"""
        program = (
            NODE_STUB
            + f"\nconst util = await import({json.dumps(UTIL_JS.as_uri())});\n"
            + "const result = await (async () => {\n"
            + script
            + "\n})();\n"
            + "console.log(JSON.stringify(result));\n"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        if completed.returncode != 0:
            raise AssertionError(f"Node 执行失败：{completed.stderr[:600]}")
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_boolean_attribute_false_is_removed(self) -> None:
        """核心回归：disabled:false 不能留下 disabled 属性。"""
        result = self.run_util("""
            const enabled = util.button('开始新会话', () => {}, { primary: true });
            const disabled = util.button('提交', () => {}, { disabled: true });
            return {
              enabledHasAttr: enabled.hasAttribute('disabled'),
              enabledDisabled: enabled.disabled,
              disabledHasAttr: disabled.hasAttribute('disabled'),
              disabledDisabled: disabled.disabled,
              enabledClass: enabled.getAttribute('class'),
            };
        """)
        self.assertFalse(result["enabledHasAttr"],
                         "disabled=false 时不应写出 disabled 属性（否则按钮点不动）")
        self.assertFalse(result["enabledDisabled"], "默认按钮必须是可用的")
        self.assertTrue(result["disabledHasAttr"], "disabled=true 时必须禁用")
        self.assertTrue(result["disabledDisabled"])
        self.assertIn("btn--primary", result["enabledClass"])

    def test_el_boolean_and_property_assignment(self) -> None:
        """el() 的布尔属性与普通属性语义。"""
        result = self.run_util("""
            const hidden = util.el('div', { hidden: false });
            const shown = util.el('div', { hidden: true });
            const input = util.el('input', { id: 'session-device', type: 'text', value: 'sim-bsense' });
            const raw = util.el('div', { attrs: { 'aria-hidden': 'false', disabled: false } });
            const titled = util.el('button', { type: 'button', title: '' });
            return {
              hiddenAttr: hidden.hasAttribute('hidden'),
              shownAttr: shown.hasAttribute('hidden'),
              inputValue: input.value,
              inputId: input.getAttribute('id'),
              inputType: input.getAttribute('type'),
              rawDisabled: raw.hasAttribute('disabled'),
              rawAria: raw.getAttribute('aria-hidden'),
              titleAttr: titled.getAttribute('title'),
            };
        """)
        self.assertFalse(result["hiddenAttr"], "hidden=false 应移除属性")
        self.assertTrue(result["shownAttr"], "hidden=true 应设置属性")
        self.assertEqual(result["inputValue"], "sim-bsense", "input 的 value 应走属性赋值")
        self.assertEqual(result["inputId"], "session-device")
        self.assertEqual(result["inputType"], "text")
        self.assertFalse(result["rawDisabled"], "attrs 里的 disabled=false 也应按布尔语义移除")
        self.assertEqual(result["rawAria"], "false", "非布尔属性的字符串值原样写出")

    def test_table_and_progress_builders(self) -> None:
        """表格与进度条构造器不抛错，且进度条宽度正确。"""
        result = self.run_util("""
            const rows = [{ id: '1', name: '甲' }, { id: '2', name: '乙' }];
            const node = util.table([
              { title: '编号', field: 'id' },
              { title: '名称', field: 'name' },
            ], rows, { caption: '被试列表' });
            const bar = util.progressBar(0.42, { slim: true });
            return {
              tag: node.tagName,
              barClass: bar.node.getAttribute('class'),
              barWidth: bar.node.children[0].style.width,
            };
        """)
        self.assertEqual(result["tag"], "DIV")
        self.assertIn("progress--slim", result["barClass"])
        self.assertEqual(result["barWidth"], "42.0%")
