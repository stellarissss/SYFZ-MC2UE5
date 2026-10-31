# REBUILD.md -- 在你的 Windows 机器上从零重建世界并出包

> 这份手册的存在原因：10-07 以来关卡由一串手工执行的编辑器脚本组装，
> 顺序只存在于操作者记忆里（REFACTOR_PLAN §8.36/§8.43）。
> `tools/build_world.py` 把这条链变成一条命令，本文件是它的说明书
> 和验收清单。旧入口 `build_release_level.py` 已加闸门（需
> `MC_ALLOW_LEGACY=1`），不要再用来建世界。

## 0. 前置条件（一次性）

### 0.1 本机环境（2026-10-28 迁移后实测，不是文档推测）

环境从旧的 Q 盘整体搬到 `G:\BaiduNetdiskDownload\real\`，目录层级保持不变，
所以旧文档里所有 `Q:\MC2UE5\...` / `Q:\UE\...` 只要把根换掉就仍然成立。

| 项 | 值 | 校验方式 |
|---|---|---|
| 根 `<HOME>` | `G:\BaiduNetdiskDownload\real` | — |
| 仓库 | `<HOME>\MC2UE5\repo` | `git log -1` |
| venv python | `<HOME>\venv\Scripts\python.exe` | py 3.13.9 / numpy 2.5.1 / scipy 1.18.1 / PIL 12.3 |
| UE | `<HOME>\UE\UE_5.8` | `Build.version` = **5.8.3, CL 58210709, ++UE5+Release-5.8** |
| 工具链 | `<HOME>\AutoSDK`、`WindowsKits`、`dotnet`、`UBA`、`VSBuildTools` | `REG` 由 `tools/reg_sdk.py` 写 |
| 体素数据 | `voxel_data\full\overworld.bin`（58,720,789 B） | **唯一数据源，不在 git 里** |
| 仓库外工具树 | `<HOME>\MC2UE5\tools\`（17 个，含 `focus_capture.py`） | 不受版本控制 |

> **`<HOME>\MC2UE5\venv` 已损坏，不要用。** 它是旧环境的残留：`pyvenv.cfg`
> 指向的 base 解释器随旧环境一起没了，运行时报
> `did not find executable at 'Q:\WorkBuddyPortable\...\python.exe'`。
> 这种「路径存在但不能跑」的坑比路径不存在更贵，因为 `os.path.exists()` 会放行。
> 可用的 venv 只有 `<HOME>\venv`。

### 0.2 路径不再硬编码

迁移暴露的问题：`Q:` 只是一个盘符前缀，盘没了以后 `Q:\UE\UE_5.8\...` 依然
是**语法合法的字符串**，于是没有任何一步快速失败，错误最后以
「editor 退出 1」「文件找不到」的形式出现在很远的子进程里。

现在只有两处知道真实路径，且都是**推导**出来的，不再有字面量：

- Python：`tools/mcpaths.py`（`REPO` 由自身位置推，`UE_ROOT`/`VENV_PY`/`LOGS`
  由 `<HOME>` 推）。先跑一次自检：

  ```
  python tools\mcpaths.py        REM 打印全部根目录并逐项 OK/MISS，缺东西直接非 0 退出
  ```

- 批处理：`tools\build_shipping.bat` / `tools\package_shipping.bat` 用
  `%~dp0..\..\..` 推 `<HOME>`，并在启动时打印 `===ENV ...===`。

两者都支持一个覆盖变量 `MC2UE5_HOME`（另有 `MC_UE_ROOT`、`MC_LOG_DIR`、
`MC_VENV_PY`）。换盘只改这一个变量，不改代码。

### 0.3 git-lfs 状态

```
git lfs install
git lfs pull            # 注意：经 ghfast 镜像时 LFS batch 会 4xx，见下
```

本机实测（2026-10-28）：**695/695 全部真图，0 指针、0 缺失**。

迁移当时有 449 个 `assets/textures/**/*.png` 仍是指针。`git lfs pull` 走不通
——LFS 的 batch 端点在 `github.com`，经 `ghfast.top` 镜像返回 4xx。可行做法是
仓库外那个工具（走 `media.githubusercontent.com`，按指针里的 sha256 逐个校验）：

```
python <HOME>\MC2UE5\tools\fetch_lfs_raw.py ^
    --repo stellarissss/SYFZ-MC2UE5 --ref main --root <repo> --include assets/textures
```

> 仓库已改名 `stellarissss/SYFZ-MC2UE5`（旧名 `MC2UE5` 仍重定向，git 不受影响）。
> 该工具直接用 media 主机，所以必须填**新名**。

任何时候先自查，再决定能不能重烘图集：

```
python tools\check_lfs_state.py      REM OK = 全部真图 / INCOMPLETE = 还有指针
```

为什么这项检查有存在必要：**指针文件也是「存在」的**（130 字节 ASCII），
`os.path.exists()` 会放行，烘图集时它把 130 字节当成贴图去平均，产出一张
看起来合理但完全错误的图集。所以「文件在不在」这个判据在这里没有意义。

## 1. 数据保险（强烈建议第一步做）

存档已永久丢失，`overworld.bin` 是项目唯一数据源，且**不在 git 里**：

```
set GH_TOKEN=<你的token>       REM 或把 token 写进 G:\BaiduNetdiskDownload\real\MC2UE5\repo\.gh_token
python tools\archive_data.py
```

上传 `voxel_data.tar.gz` + `content_level.tar.gz`（当前关卡快照）到
GitHub Release `data-backup`。换机/盘损后用 `tools\restore_data.py` 恢复。
同时建议把 bin 本体入 LFS（规则已备好）：

```
git lfs install
git add voxel_data/full/overworld.bin
git commit -m "data: archive the only voxel source via LFS"
git push
```

## 2. 一键重建世界

```
cd G:\BaiduNetdiskDownload\real\MC2UE5\repo
G:\BaiduNetdiskDownload\real\venv\Scripts\python.exe tools\build_world.py
```

它按序执行（每步独立日志，全部 PASS 才继续）：

| # | 阶段 | 脚本 | 产物 |
|---|---|---|---|
| 1 | data | classify.py（仅当 out/classify 缺失） | rolevolume/terrain.npy |
| 2 | data | extract_structures.py | 结构 OBJ（按面选槽：top/side/bottom 三个材质体积） |
| 3 | data | pack_terrain_tiles.py | out/terrain_pack（24 瓦片） |
| 4 | data | build_vegetation_pack.py | out/vegetation_obj（S4.5 树） |
| 5 | data | pick_spawn.py | logs/spawn.json |
| 6 | editor | import_block_textures.py --stage materials | T_/M_MC_*（274 个原版贴图槽） |
| 7 | editor | build_terrain_meshes.py | 24 瓦片 + **碰撞**（本轮新增） |
| 8 | editor | build_vegetation_meshes.py | 植被网格+actor（本轮新增） |
| 9 | editor | import_block_textures.py --stage meshes | 结构重导入+槽位绑定 |
| 10 | editor | ensure_lighting.py | 统一灯光（与 ApplyLook 同值） |
| 11 | editor | apply_spawn.py | 出生点（Y 已镜像到正确半球） |
| 12 | editor | hide_voxel_layer.py | 藏体素层+**碰撞退役**（门控） |
| 13 | editor | level_census.py | 终检 |

日志全部落在 `G:\BaiduNetdiskDownload\real\MC2UE5\logs\`（`MC_LOG_DIR` 可改），
总报告 `logs\build_world_report.json`——**这份 JSON 就是本次构建的
可复现记录**，提交进 git 或随 Release 归档。

常用开关：`--skip-data`（只跑编辑器侧）、`--only=census`（只终检）、
`--engine`/`--project`/`--editor`（换路径）。

## 3. 编译 + 打包

```
cd G:\BaiduNetdiskDownload\real\MC2UE5\repo
tools\build_shipping.bat          REM 编译 Win64 Shipping（含本轮 C++ 改动）
tools\package_shipping.bat        REM cook+stage+pak -> G:\BaiduNetdiskDownload\real\MC2UE5\dist_verify
```

> 首编会检验本轮 C++ 修改（MCTextLogPath / GTourStops / MCFrameCapture
> 路径修复）。编译报错请把完整输出贴回来。

## 4. 验收清单（跑包，不跑编辑器）

按下表逐项打勾；**4.4/4.5 是八轮修复以来第一次真正可能通过的
跨层对应性验收**（此前从未执行，见 §8.43.1 镜像根因）：

| # | 检查 | 判据 |
|---|---|---|
| 4.1 | 输入 | WASD/鼠标生效（配置修复需 cook 后才进包——本包已含） |
| 4.2 | 出生点 | 落地在运动场附近、不在树里/地下；Z 下坠 < 1 格后站稳（地形碰撞生效的判据） |
| 4.3 | 植被 | 校园有树（bark/leaves 材质）；此前为 0 |
| 4.4 | 对应性 | 游走后在运动场环/红面朝向与 `out/ref_map.png`（§8.9 代理真值）**同侧同向**——镜像修复的直接证据 |
| 4.5 | 行走 | 坡地上脚不陷入可见地面、不悬空（碰撞=平滑地形） |
| 4.6 | 诊断 | `%LOCALAPPDATA%`…`Saved\MCLogs\`（或 -MClogdir= 指定处）出现 mc_runtime/applylook/mclayers——C++ 路径修复生效 |
| 4.7 | 灰色系 | §8.23 的灰族塌陷是否仍在（材质侧遗留，独立于本轮） |

已知未收口（不影响可玩性，属后续迭代）：阴影默认关（-MCshadows=1 开）、
灰族色塌陷 §8.23、玻璃族材质配色（网格引用已随重提取恢复，观感需调）。

## 5. 出问题时

- 任何一步 FAIL：先看 `G:\BaiduNetdiskDownload\real\MC2UE5\logs\<步骤名>.txt` 尾部 20 行；
- `hide` 步报「collision: NOT disabled」= 地形瓦片没建上碰撞 → 重跑第 7 步；
- 需要回看体素层对照：`set MC_UNHIDE=1` 后重跑 hide 步。
