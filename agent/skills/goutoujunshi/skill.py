"""
狗头军师 — 恋爱军师技能模块（Code-Backed Skill）

从 SKILL.md 加载核心原则作为 prompt_modifier，
提供知识库检索工具让 LLM 按需查阅参考文件。
"""

import os
import re
import glob as glob_mod

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_skill_md() -> str:
    """加载 SKILL.md 核心内容"""
    path = os.path.join(SCRIPT_DIR, "SKILL.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_prompt_modifier() -> str:
    """从 SKILL.md 提取核心原则作为 prompt_modifier（去掉 frontmatter 和参考表）"""
    full = _load_skill_md()
    if not full:
        return ""

    # 去掉 YAML frontmatter
    if full.startswith("---"):
        end = full.find("---", 3)
        if end != -1:
            full = full[end + 3:].strip()

    # 去掉 "## 按需加载" 及之后的内容（那些是文件路径引用，不适用于 agent）
    cutoff = re.search(r'\n## 按需加载\n', full)
    if cutoff:
        full = full[:cutoff.start()]

    # 去掉 "## 长期记忆" 段（agent 有自己的记忆系统）
    full = re.sub(
        r'\n## 长期记忆\n.*?(?=\n## |\Z)',
        '',
        full,
        flags=re.DOTALL,
    )

    # 精简：去掉代码块和引用路径
    full = re.sub(r'```.*?```', '', full, flags=re.DOTALL)
    full = re.sub(r'`references/.*?\.md`', '知识库', full)

    return full.strip()


def _list_knowledge_files() -> list[dict]:
    """列出所有知识库和实用指南文件"""
    files = []
    ref_dir = os.path.join(SCRIPT_DIR, "references")
    if not os.path.exists(ref_dir):
        return files

    for root, dirs, fnames in os.walk(ref_dir):
        for fname in fnames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, SCRIPT_DIR).replace("\\", "/")
            # 读第一行作为标题
            title = fname
            try:
                with open(full, "r", encoding="utf-8") as f:
                    first = f.readline().strip()
                    if first.startswith("# "):
                        title = first[2:].strip()
            except Exception:
                pass
            files.append({
                "path": rel,
                "filename": fname,
                "title": title,
                "category": "knowledge" if "knowledge" in rel else "practical",
            })
    return sorted(files, key=lambda x: x["category"] + x["filename"])


def _search_knowledge(query: str, max_results: int = 3) -> list[dict]:
    """搜索知识库文件，返回相关片段"""
    results = []
    ref_dir = os.path.join(SCRIPT_DIR, "references")
    if not os.path.exists(ref_dir):
        return results

    query_lower = query.lower()
    for root, dirs, fnames in os.walk(ref_dir):
        for fname in fnames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, SCRIPT_DIR).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            # 简易相关度：关键词匹配
            score = content.lower().count(query_lower)
            if score == 0:
                # 尝试分词匹配
                for term in query_lower.split():
                    score += content.lower().count(term)

            if score > 0:
                # 提取相关段落 (标题 + 前后文)
                lines = content.split("\n")
                snippets = []
                for i, line in enumerate(lines):
                    if any(t in line.lower() for t in query_lower.split()):
                        start = max(0, i - 1)
                        end = min(len(lines), i + 3)
                        snippet = "\n".join(lines[start:end])
                        if len(snippet) > 500:
                            snippet = snippet[:500] + "..."
                        snippets.append(snippet)
                        if len(snippets) >= 2:
                            break

                results.append({
                    "path": rel,
                    "filename": fname,
                    "score": score,
                    "snippets": snippets[:2],
                })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]


# ===== Skill 接口（SkillManager 通过反射调用） =====

class GoutoujunshiSkill:
    """狗头军师技能实例"""

    name = "goutoujunshi"
    _modifier_cache: str = None
    _files_cache: list[dict] = None

    def get_prompt_modifier(self, context: dict = None) -> str:
        """返回核心原则 prompt_modifier"""
        if self._modifier_cache is None:
            self._modifier_cache = _extract_prompt_modifier()
        return self._modifier_cache

    def get_tool_schemas(self) -> list[dict]:
        """暴露知识库检索工具 schema"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_love_knowledge",
                    "description": "搜索狗头军师知识库，获取恋爱沟通的深度指导。涵盖：话术编排、约会策划、冲突化解、关系判断、依恋理论、MBTI匹配、社交体系转译、边界与同意等。当需要超出基本沟通风格的专业建议时调用",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词或问题，如 '怎么邀约不显得刻意'、'对方突然冷淡怎么办'、'第一次见面聊什么'、'依恋焦虑怎么沟通'",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_love_knowledge",
                    "description": "列出狗头军师知识库中所有可用的参考文件（恋爱知识库 + 实战指南），用于了解有哪些主题可以深入查询",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
        ]

    def execute_tool(self, tool_name: str, **kwargs) -> dict | None:
        """执行知识库工具"""
        if tool_name == "search_love_knowledge":
            query = kwargs.get("query", "")
            if not query:
                return {"ok": False, "error": "请提供搜索关键词 query"}
            results = _search_knowledge(query)
            if not results:
                return {"ok": True, "data": {"results": [], "hint": "未找到匹配，尝试更具体的查询或使用 list_love_knowledge 浏览知识库"}}
            return {"ok": True, "data": {"results": results, "count": len(results)}}

        if tool_name == "list_love_knowledge":
            if self._files_cache is None:
                self._files_cache = _list_knowledge_files()
            return {"ok": True, "data": {"files": self._files_cache, "count": len(self._files_cache)}}

        return None  # 未处理
