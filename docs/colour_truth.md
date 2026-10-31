# 颜色真值：`ref_map.png` 能用来验收什么，不能用来验收什么

**触发**：第 N 轮验收时，用 `tools/reference_map.py` 生成的 `ref_map.png` 和我们的渲染逐块比色，
得到加权 mean ΔRGB **39.8**、中位 19.8，看上去像"我们的贴图偏暗"。本文记录核查结果：
**这个偏差是尺子的问题，不是渲染的问题。**

**结论**：`ref_map.png` 是**位置/材质身份**的标尺，**不是**逐像素颜色的标尺。
不许拿它做颜色校准，也不许拿它当"我们偏色"的证据。

---

## 1. `ref_map.png` 的颜色是「地图物品色板」，不是贴图采样

`reference_map.py` 自己的注释（`BLOCK_COLORS` 上方）写得很清楚：

> Where a value is the Minecraft **map colour** … it is exact and official;
> where it is a texture average it is a measured, hand-checked approximation.

而 `DYE_SUFFIXES = ("_stained_glass_pane", "_stained_glass", "_concrete", "_wool", "_carpet")`
意味着任何 `<颜色>_concrete` / `<颜色>_wool` / `<颜色>_carpet` / `<颜色>_stained_glass*`
都直接返回 **16 个染料色之一**。

### 实测：一个 ref 颜色塌掉多个完全不同的贴图

在 16 染料 × 6 种材质共 96 个组合上，把 `block_rgb(nm)` 和该方块顶面贴图的实际均值对比：

```
ref_map 颜色把 >=2 个不同贴图均值塌成一个：15 / 18
```

最典型的 `(249, 255, 254)`（= `MC_DYE_COLORS["white"]`）同时代表 4 张完全不同的贴图：

| 方块 | 真实贴图均值 |
|---|---|
| `white_concrete` | **(207, 213, 214)** |
| `white_wool` | (234, 236, 237) |
| `white_stained_glass` | (255, 255, 255) |
| `white_stained_glass_pane_top` | (31, 31, 31) |

一张颜色不可能同时是这四张。所以 `ref_map` 在这一族上**信息量为 1 位**（"是白色系"），
不是"这个方块看起来什么颜色"。

### 交叉验证：偏的是尺子，不是我们

`white_concrete` 的**原版值就是 `#CFD5D6 = (207, 213, 214)`**，和我们发出去的贴图均值
**逐字节相同**。`ref_map` 说它是 `(249,255,254)`。

`grass_block`：`ref_map` 给 `(127,178,56)`，注释标注 "MC map colour"，
即游戏内地图物品的草地基色 `#7FB238`；我们的值是
`grass_block_top` 灰度 (147,147,147) × biome tint `0x91BD59` = **(86,109,51)**，
这是 Minecraft 在正午全光下顶面的真实算式结果。

两者的比值 1.47 / 1.63 / 1.10 **不是统一的**，所以不存在一个色调映射能把它们对齐 ——
它们本就不是同一个东西。

---

## 2. 所以"颜色对不对"应该怎么验

按能证明的强度从高到低：

| 想证明的事 | 用什么 | 状态 |
|---|---|---|
| **位置正确**：球场、大门、每栋楼在不在该在的位置 | `ref_map.png`（1 px/格，与存档几何一一对应） | ✅ 有效，`ref_map` 是这事的唯一标尺 |
| **材质身份正确**：这一格是混凝土不是羊毛 | `ref_map` 的**色相分类**（16 染料 + 族），不看精确 RGB | ✅ 有效 |
| **贴图真值正确**：这个方块的贴图是不是原版那张 | `assets/textures/block/*.png`（原版资源，外部真值）+ 校园内 0 个方块落到兜底槽 | ✅ 已验证 |
| **细节密度接近原版**：不是被 mip 糊成一片 | 每方块可见 σ、边缘强度 vs 原版贴图 | ✅ 已量化（见 §3） |
| **整体观感接近原版** | 同机位截图并排看 | ✅ 最终验收，且**只认这个** |
| ~~逐像素颜色差~~ | ~~ΔRGB vs `ref_map`~~ | ❌ **范畴错误，已停用** |

因此：**不引入任何 per-texture 增益/校准表**。之前 `tools/build_atlas.py` 里的
`BAKE_GAINS` 校准链正是基于"要贴 `ref_map` 的颜色"这个错误前提建立的，
新路径（原版贴图）不继承它，`calibrate_bake.py` 随之退役。

---

## 3. 细节密度这条（真正导致"比原 MC 粗糙"的那条）

与颜色无关，一并记在这里做对照，数据来自本轮修复前的实测：

| 指标 | 原版 MC 贴图 | 修复前（照片族 + 512px mip5） | 修复后（16×16 + TF_NEAREST） |
|---|---|---|---|
| 每方块可见 σ | 45.6 | 11.6（**3.9× 差**） | 待重拍验证 |
| 边缘强度 | 18.75 | 4.33（**4.3× 差**） | 待重拍验证 |

原因：旧管线用 ambientCG/Poly Haven 的 512px 照片，UV 1 格 = 1 次重复，
屏幕上方块只有 16–20 px，GPU 取到的是 mip5(16×16) = 局部平均 → 所有细节被抹平。
换成 16×16 原版贴图 + `TF_NEAREST` 后，mip 链与 Minecraft 完全一致（16→8→4→2→1），
不存在"越远越糊"的额外损失。

---

## 4. 复现

```bash
# 尺子塌陷的直接证据（96 组染料×材质）
python tools_out/_audit_colour.py

# 校园内是否有方块落到兜底槽（应为 0）
python tools_out/_audit_fallback.py
```

`tools_out/` 是 gitignore 的临时产物目录；这两个脚本只读，不改任何资产。
需要长期复用时把结论搬进 `tools/check_block_textures.py` 的断言里。
