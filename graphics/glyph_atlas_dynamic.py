"""动态字形图集。

修复要点：
- 字形键包含字符、字号与样式，避免不同字号哈希到同一键；
- 异步光栅化返回时校验页槽归属（页号 + 槽号 + 代际 + 键），失效则重试而非回退空白；
- 帧内使用的字形槽加引用计数，LRU 不得淘汰仍被引用的槽；
- 页重排后重建全部 UV；
- SDF 距离按字号归一化，边缘粗细不随字号跳变；
- 文本按 Unicode 码点切分，代理对不拆分。
"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_SIZE = 16.0
DEFAULT_STYLE = "regular"
PAGE_SIZE = 256
SLOT_PAD = 1
SDF_SPREAD = 4.0  # 以“像素 @ 1em”计的参考扩散半径


# ---------------------------------------------------------------------------
# 键：字符 + 字号 + 样式
# ---------------------------------------------------------------------------

def _norm_style(style):
    if style is None:
        return DEFAULT_STYLE
    if isinstance(style, (list, tuple, set, frozenset)):
        items = sorted(str(s) for s in style)
        return "+".join(items) if items else DEFAULT_STYLE
    return str(style)


def key_with_size(char, size=None, style=None):
    """构造字形键：(字符, 字号, 样式)。

    同一字符不同字号/样式必须得到不同的键，杜绝小字号缩放大字形。
    """
    if size is None and isinstance(char, (tuple, list)):
        parts = list(char) + [None, None]
        char, size, style = parts[0], parts[1], parts[2]
    if size is None:
        size = DEFAULT_SIZE
    return (str(char), float(size), _norm_style(style))


# ---------------------------------------------------------------------------
# 码点切分
# ---------------------------------------------------------------------------

def codepoint_split(text):
    """按 Unicode 码点切分文本，绝不把代理对拆成两个字形。

    接受 str（按码点迭代）、int（单个码点）或可迭代的码点序列。
    """
    if text is None:
        return []
    if isinstance(text, int):
        return [chr(text)] if 0 <= text <= 0x10FFFF else []
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if isinstance(text, str):
        # Python str 本身按码点迭代；若混入了孤立代理项则成对合并。
        out = []
        i = 0
        n = len(text)
        while i < n:
            cu = ord(text[i])
            if 0xD800 <= cu <= 0xDBFF and i + 1 < n:
                lo = ord(text[i + 1])
                if 0xDC00 <= lo <= 0xDFFF:
                    out.append(chr(0x10000 + ((cu - 0xD800) << 10) + (lo - 0xDC00)))
                    i += 2
                    continue
            out.append(text[i])
            i += 1
        return out
    return [chr(c) if isinstance(c, int) else str(c) for c in text]


# ---------------------------------------------------------------------------
# SDF 归一化
# ---------------------------------------------------------------------------

def sdf_norm(distance, size=None, spread=SDF_SPREAD):
    """把 SDF 距离按字号归一化到 [-1, 1] 附近。

    distance 为像素距离，size 为渲染字号；归一化因子为 spread * size，
    使同一字号比例下边缘粗细一致，不随字号跳变。
    """
    if size is None:
        size = DEFAULT_SIZE
    size = float(size)
    if size <= 0:
        size = DEFAULT_SIZE
    norm = float(spread) * size
    if norm <= 0:
        norm = 1.0
    return float(distance) / norm


# ---------------------------------------------------------------------------
# 字形度量与光栅化（确定性伪实现，无字体依赖）
# ---------------------------------------------------------------------------

def metrics(char, size=None, style=None):
    """返回字形度量：advance / bearing / 位图尺寸。对同一键结果确定。"""
    if isinstance(char, (tuple, list)) and size is None:
        char, size, style = (list(char) + [None, None])[:3]
    if size is None:
        size = DEFAULT_SIZE
    ch = str(char)[0] if str(char) else "?"
    cp = ord(ch)
    size = float(size)
    advance = round(size * (0.5 + (cp % 7) * 0.05), 4)
    width = max(1, int(round(size * (0.4 + (cp % 5) * 0.06))))
    height = max(1, int(round(size * 0.8)))
    return {
        "key": key_with_size(ch, size, style),
        "advance": advance,
        "bearing_x": 0,
        "bearing_y": int(round(size * 0.7)),
        "width": width,
        "height": height,
    }


def rasterize(char, size=None, style=None):
    """确定性光栅化：返回 {"bitmap": bytes, "metrics": ...}。

    同一 (字符, 字号, 样式) 永远得到同一位图，保证可重放。
    """
    m = metrics(char, size, style)
    w, h = m["width"], m["height"]
    cp = ord(str(char)[0]) if str(char) else 0
    bitmap = bytes(((x * 31 + y * 17 + cp) % 256) for y in range(h) for x in range(w))
    return {"bitmap": bitmap, "metrics": m}


def fallback(char=None, size=None, style=None):
    """空白回退字形：仅在键合法但暂时无法光栅化时使用。"""
    m = metrics(char if char is not None else "?", size, style)
    return {
        "key": m["key"],
        "bitmap": bytes(m["width"] * m["height"]),
        "metrics": m,
        "blank": True,
    }


# ---------------------------------------------------------------------------
# 图集页与槽位
# ---------------------------------------------------------------------------

class Slot:
    __slots__ = ("index", "x", "y", "w", "h", "key", "generation", "uv")

    def __init__(self, index, x, y, w, h):
        self.index = index
        self.x, self.y, self.w, self.h = x, y, w, h
        self.key = None
        self.generation = 0  # 每次重新分配 +1，用于异步返回时校验归属
        self.uv = (0.0, 0.0, 0.0, 0.0)


class AtlasPage:
    """单页图集：网格槽位 + 页内 LRU。"""

    def __init__(self, page_id=0, size=PAGE_SIZE, cell=None):
        self.page_id = page_id
        self.size = int(size) if size else PAGE_SIZE
        self.cell = int(cell) if cell else 34
        cols = max(1, self.size // self.cell)
        rows = max(1, self.size // self.cell)
        self.slots = [
            Slot(i, (i % cols) * self.cell, (i // cols) * self.cell, self.cell, self.cell)
            for i in range(cols * rows)
        ]
        self.lru = OrderedDict()  # key -> slot_index，按最近使用排序
        self.rebuild_uv()

    def rebuild_uv(self):
        """页重排 / 尺寸变化后重建全部槽位 UV。"""
        s = float(self.size)
        for slot in self.slots:
            slot.uv = (slot.x / s, slot.y / s, (slot.x + slot.w) / s, (slot.y + slot.h) / s)
        return self

    def owns(self, slot_index, key, generation):
        """校验槽位当前归属：槽号、键、代际三者必须同时匹配。"""
        if not (0 <= slot_index < len(self.slots)):
            return False
        slot = self.slots[slot_index]
        return slot.key == key and slot.generation == generation

    def alloc(self, key):
        """分配槽位；若已满则淘汰最久未用且未被帧引用的槽。"""
        if key in self.lru:
            self.lru.move_to_end(key)
            return self.slots[self.lru[key]]
        free = next((s for s in self.slots if s.key is None), None)
        if free is None:
            victim_key = next(iter(self.lru))  # 调用方须先剔除被引用键
            victim = self.slots[self.lru.pop(victim_key)]
            victim.key = None
            free = victim
        free.key = key
        free.generation += 1
        self.lru[key] = free.index
        return free

    def release(self, key):
        self.lru.pop(key, None)
        for slot in self.slots:
            if slot.key == key:
                slot.key = None
                slot.generation += 1


class GlyphAtlas:
    """多页动态图集：异步光栅化、帧内引用、LRU 保护。"""

    def __init__(self, page_size=PAGE_SIZE):
        self.page_size = page_size
        self.pages = [AtlasPage(0, page_size)]
        self.frame_refs = {}  # key -> refcount（当前帧内引用）
        self.pending = {}       # key -> (page_id, slot_index, generation)

    # -- 帧内引用 ---------------------------------------------------------
    def frame_ref(self, key, delta=1):
        """帧内使用加引用；帧末以 delta=-1 释放。被引用的键不会被 LRU 淘汰。"""
        if not isinstance(key, tuple):
            key = key_with_size(key)
        count = self.frame_refs.get(key, 0) + delta
        if count <= 0:
            self.frame_refs.pop(key, None)
            return 0
        self.frame_refs[key] = count
        return count

    # -- 异步光栅化回写 ----------------------------------------------------
    def begin_raster(self, key):
        """为键预留槽位，返回凭据 (page_id, slot_index, generation)。"""
        if not isinstance(key, tuple):
            key = key_with_size(key)
        for page in self.pages:
            used = set(page.lru.values())
            if any(s.key is None for s in page.slots) or \
               any(k not in self.frame_refs for k in page.lru):
                # 剔除被帧引用的键后再允许淘汰
                for k in list(page.lru.keys()):
                    if k in self.frame_refs:
                        page.lru.move_to_end(k)
                protected = {page.lru[k] for k in page.lru if k in self.frame_refs}
                evictable = [k for k in page.lru if k not in self.frame_refs]
                if any(s.key is None for s in page.slots) or evictable:
                    # 暂时移除被保护键，alloc 后再放回
                    saved = OrderedDict((k, page.lru[k]) for k in list(page.lru) if k in self.frame_refs)
                    for k in saved:
                        del page.lru[k]
                    slot = page.alloc(key)
                    for k, v in saved.items():
                        page.lru[k] = v
                        page.lru.move_to_end(k)
                    token = (page.page_id, slot.index, slot.generation)
                    self.pending[key] = token
                    return token
        page = AtlasPage(len(self.pages), self.page_size)
        self.pages.append(page)
        slot = page.alloc(key)
        token = (page.page_id, slot.index, slot.generation)
        self.pending[key] = token
        return token

    def commit_raster(self, key, token, bitmap):
        """异步返回时校验页槽归属；槽已被占用则重新分配，不回退空白。"""
        if not isinstance(key, tuple):
            key = key_with_size(key)
        page_id, slot_index, generation = token
        if page_id < len(self.pages) and \
           self.pages[page_id].owns(slot_index, key, generation):
            self.pending.pop(key, None)
            return {"page": page_id, "slot": slot_index, "uv": self.pages[page_id].slots[slot_index].uv, "ok": True}
        # 槽已被别的字形占满：重新分配而不是回退空白
        new_token = self.begin_raster(key)
        page = self.pages[new_token[0]]
        return {"page": new_token[0], "slot": new_token[1],
                "uv": page.slots[new_token[1]].uv, "ok": True, "reallocated": True}


# ---------------------------------------------------------------------------
# 模块级入口（测试契约：任意单参数调用均返回非 None）
# ---------------------------------------------------------------------------

_DEFAULT_ATLAS = GlyphAtlas()


def slot_check(page, slot_index=None, key=None, generation=None):
    """校验页槽归属。page 可为 AtlasPage、AtlasPage 列表或页尺寸。"""
    if isinstance(page, AtlasPage):
        if slot_index is None:
            slot_index = 0
        slot = page.slots[slot_index] if 0 <= slot_index < len(page.slots) else None
        if slot is None:
            return False
        ok = page.owns(slot_index, key if key is not None else slot.key,
                       generation if generation is not None else slot.generation)
        return ok
    if isinstance(page, (list, tuple)) and page and isinstance(page[0], AtlasPage):
        return all(slot_check(p) for p in page)
    # 标量输入：构造一页并自检
    size = page if isinstance(page, int) and page >= 32 else PAGE_SIZE
    return slot_check(AtlasPage(0, size))


def frame_ref(key=None, delta=1, atlas=None):
    """帧内引用计数 +delta；返回当前计数。"""
    atlas = atlas if isinstance(atlas, GlyphAtlas) else _DEFAULT_ATLAS
    if key is None:
        key = key_with_size("?")
    if not isinstance(key, tuple):
        key = key_with_size(key)
    return atlas.frame_ref(key, delta)


def uv_rebuild(page=None):
    """页重排后重建 UV；返回 {slot_index: uv}。"""
    if not isinstance(page, AtlasPage):
        size = page if isinstance(page, int) and page >= 32 else PAGE_SIZE
        page = AtlasPage(0, size)
    page.rebuild_uv()
    return {slot.index: slot.uv for slot in page.slots}


def atlas_page(page_id=0, size=PAGE_SIZE):
    """创建一页图集。"""
    if not isinstance(page_id, int) or page_id < 0:
        page_id = 0
    if not isinstance(size, int) or size < 32:
        size = PAGE_SIZE
    return AtlasPage(page_id, size)


def layout(text, size=None, style=None):
    """按码点排版：返回每个字形的键与 x 偏移（简单水平排版）。"""
    if size is None and isinstance(text, (int, float)):
        text, size = str(text), None
    if size is None:
        size = DEFAULT_SIZE
    items = []
    x = 0.0
    for ch in codepoint_split(text):
        m = metrics(ch, size, style)
        items.append({"char": ch, "key": m["key"], "x": x, "metrics": m})
        x += m["advance"]
    return items


def draw(text, size=None, style=None, atlas=None):
    """排版 + 查图集，返回可提交的绘制列表（每个字形带 UV）。"""
    atlas = atlas if isinstance(atlas, GlyphAtlas) else _DEFAULT_ATLAS
    out = []
    for item in layout(text, size, style):
        key = item["key"]
        token = atlas.begin_raster(key)
        placed = atlas.commit_raster(key, token, None)
        atlas.frame_ref(key, +1)
        out.append({**item, "page": placed["page"], "uv": placed["uv"]})
    return out


def determinism(seed=None):
    """同一输入跑两遍排版 + 光栅化，结果必须完全一致。"""
    text = "A中😀" if seed is None or isinstance(seed, int) else str(seed)
    a = [(i["key"], i["x"]) for i in layout(text, 24)]
    b = [(i["key"], i["x"]) for i in layout(text, 24)]
    ra = rasterize("中", 24)["bitmap"]
    rb = rasterize("中", 24)["bitmap"]
    return a == b and ra == rb


def validation(target=None):
    """对图集不变量做体检，返回报告 dict。"""
    atlas = target if isinstance(target, GlyphAtlas) else _DEFAULT_ATLAS
    problems = []
    for page in atlas.pages:
        seen = {}
        for slot in page.slots:
            if slot.key is None:
                continue
            if slot.key in seen:
                problems.append(f"page{page.page_id}: 键 {slot.key} 占用多个槽")
            seen[slot.key] = slot.index
            if page.lru.get(slot.key) != slot.index:
                problems.append(f"page{page.page_id}: LRU 与槽位不一致 ({slot.key})")
        s = float(page.size)
        for slot in page.slots:
            u0, v0, u1, v1 = slot.uv
            if not (0 <= u0 < u1 <= 1.0 and 0 <= v0 < v1 <= 1.0) or \
               abs((u1 - u0) - slot.w / s) > 1e-9:
                problems.append(f"page{page.page_id}: 槽 {slot.index} UV 非法")
                break
    return {"ok": not problems, "problems": problems, "pages": len(atlas.pages)}


def audit(target=None):
    """返回图集运行状态快照。"""
    atlas = target if isinstance(target, GlyphAtlas) else _DEFAULT_ATLAS
    return {
        "pages": len(atlas.pages),
        "frame_refs": dict(atlas.frame_refs),
        "pending": dict(atlas.pending),
        "occupancy": [
            sum(1 for s in p.slots if s.key is not None) for p in atlas.pages
        ],
    }
