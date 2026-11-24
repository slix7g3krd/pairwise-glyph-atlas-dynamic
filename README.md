# pairwise-glyph-atlas-dynamic

## 领域

游戏、图形与多媒体

## 主题

动态字形图集

## 说明

字号键、页槽归属校验与码点切分排版。

初始状态下 `graphics/glyph_atlas_dynamic.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
