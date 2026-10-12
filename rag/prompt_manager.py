"""Prompt 版本管理器 — YAML 文件化、版本化、模板变量。"""

from __future__ import annotations

import os

import yaml


class PromptManager:
    def __init__(self, prompts_dir: str = "prompts"):
        self.prompts_dir = prompts_dir

    def _load_all_versions(self, name: str) -> list[dict]:
        """加载指定 prompt 的所有版本文件。"""
        versions = []
        if not os.path.isdir(self.prompts_dir):
            raise FileNotFoundError(f"Prompts directory not found: {self.prompts_dir}")
        for fname in os.listdir(self.prompts_dir):
            if fname.endswith(".yaml") and fname.startswith(name):
                path = os.path.join(self.prompts_dir, fname)
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if data and data.get("name") == name:
                        versions.append(data)
        if not versions:
            raise FileNotFoundError(f"Prompt '{name}' not found in {self.prompts_dir}")
        return sorted(versions, key=lambda x: x.get("version", 0))

    def get(self, name: str, version: int | None = None) -> str:
        """加载 prompt 模板。version=None 时加载最新版本。"""
        all_versions = self._load_all_versions(name)
        if version is not None:
            for v in all_versions:
                if v.get("version") == version:
                    return v["template"]
            raise ValueError(f"Version {version} not found for prompt '{name}'")
        return all_versions[-1]["template"]

    def render(self, prompt_name: str, version: int | None = None, **kwargs) -> str:
        """加载并渲染模板变量。

        模板或入参中出现孤立花括号、非法格式说明符等问题时，``str.format`` 会抛出
        ``ValueError``/``IndexError``；此前这类异常会一路冒泡成未捕获异常并中断整条请求
        （kb_accessories 配件明细失败簇即为此形态：内容存在、失败在取用侧）。
        这里退化为安全替换：已知变量照常填值，未知占位符与非法花括号原样保留，不再抛异常。
        """
        template = self.get(prompt_name, version=version)
        if not isinstance(template, str):
            template = str(template)
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            safe = template
            for key, value in kwargs.items():
                safe = safe.replace("{" + key + "}", str(value))
            return safe

    def list_versions(self, name: str) -> list[dict]:
        """列出 prompt 的所有版本信息。"""
        all_versions = self._load_all_versions(name)
        result = []
        for v in reversed(all_versions):
            changelog = v.get("changelog", [{}])
            latest = changelog[0] if changelog else {}
            result.append(
                {
                    "version": v.get("version"),
                    "date": latest.get("date", ""),
                    "change": latest.get("change", ""),
                }
            )
        return result
