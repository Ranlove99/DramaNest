# DramaNest（剧巢）

飞牛 fnOS 原生应用：通用的网络视频下载与管理工具。支持 m3u8 / AES-128 加密视频、HLS 分片与直链媒体，用 ffmpeg 解密合成为 MP4 保存到飞牛共享目录。

> **重要**：本项目**不内置任何视频站点、资源源或网址**。应用首次打开会显示使用声明；所有资源站需由使用者自行添加或导入。请仅下载你有权访问的合法公开内容，遵守所在地区法律法规。

- 项目地址：<https://github.com/Ranlove99/DramaNest>
- 开发者：Ranlove99
- 当前版本：1.0（ARM / x86 双架构）

---

## 功能

- **资源站采集**：苹果 CMS / 海洋 CMS 接口（`?ac=list`、`?ac=detail`），支持分类筛选、分页、详情选集下载。
- **全源搜索**：一键并发搜索所有已添加的资源站，按标题汇总为「一部剧 + 多个来源」。
- **站点测活与排序**：一键测活评分，禁用无效源，禁用源自动沉底，可按分类 + 分数排序。
- **新建下载**：粘贴视频页地址或 m3u8 / mp4 直链，自动解析分集；支持部分短剧系统分集接口。
- **整站扫描**：扫描站点视频列表，批量筛选并提交下载。
- **播放预览**：内置 hls.js，页内预览 m3u8 / mp4，可查看和复制真实播放链接。
- **任务管理**：暂停 / 继续 / 重试 / 取消，逐集进度、分集网格、速度与剩余时间显示。
- **下载可靠性**：ffmpeg 直连 → 重连 → 本地并发分段兜底 → 原链接落盘多级重试；显式封装格式，AES-128 自动解密。
- **目录授权**：可在飞牛「应用设置 → 授权目录」里直接授权文件夹，应用自动读取 `TRIM_DATA_ACCESSIBLE_PATHS` 并优先落盘；设置页也能浏览 / 创建目录并检测应用账号真实读写权限。
- **免责声明**：首次打开显示使用声明，同意后记录到本地，不再重复弹出。

## 目录结构

```
DramaNest/
├── README.md              # 本说明
├── .gitignore             # 忽略私有源、构建产物、缓存
├── LICENSE                # MIT 开源许可
├── build.py               # 打包脚本（arm / x86）
├── sites.example.json     # 资源站格式示例（全部为 example.com 占位，无真实网址）
├── private/               # 【不上传 GitHub】本地私有数据
│   └── cms_sites.json     # 你的真实资源站列表（示例占位，请自行替换）
├── docs/                  # 额外文档
├── src/                   # fpk 源码
│   ├── manifest.tmpl      # manifest 模板（{{PLATFORM}}/{{CHECKSUM}} 占位符）
│   ├── app/
│   │   ├── dramanest.py   # 主程序（全部使用 Python 标准库）
│   │   └── ui/            # 前端（index.html / app.js / style.css / hls.min.js / 图标）
│   │       └── cms_sites.json  # 内置源列表：[]（空，不内置任何源）
│   ├── cmd/               # fnOS 生命周期脚本
│   ├── config/            # privilege / resource
│   ├── wizard/            # 安装向导
│   └── ICON*.PNG          # 应用图标
└── dist/                  # 构建产物（.gitignore 忽略）
    ├── DramaNest_1.0_arm.fpk
    └── DramaNest_1.0_x86.fpk
```

## 安装

在飞牛 NAS「应用中心 → 设置 → 手动安装应用」选择对应架构的 fpk：

- x86_64 机器 → `DramaNest_1.0_x86.fpk`
- aarch64 机器 → `DramaNest_1.0_arm.fpk`

依赖 `python312` 应用（会随包自动声明）；视频合成使用系统 `ffmpeg`（默认 `/usr/bin/ffmpeg`，可在设置中修改）。

### 访问方式

应用注册了 **两个入口**，桌面图标默认走统一网关：

| 入口 ID | 地址 | 说明 |
| --- | --- | --- |
| `dramanest.main`（桌面默认） | `/app/dramanest` | 统一网关，复用飞牛访问域名。**远程访问（`xxx.fnos.net`）请用这个**，走主域路径，不会出现子域名证书不匹配 |
| `dramanest.port` | `http://NAS_IP:18765/` | 端口直连，局域网最稳；也可在浏览器直接打开 |

- 服务同时监听 `target/app.sock`（网关）和 TCP `18765`（端口），两种入口共用同一套接口。
- 若桌面图标打开是空白，可在「应用中心 → DramaNest 剧巢 → 设置 → 入口」切到「端口直连」，或直接用浏览器访问 `http://NAS_IP:18765/`。
- 远程访问时若出现「请求的域名与服务器的证书不匹配」，说明走的是端口入口被飞牛映射出的 `<app>-main.<域名>` 子域名；切到网关入口即可（网关在主域下，证书有效）。
- 如需改端口，同时修改 `manifest.tmpl` 的 `service_port`、`src/cmd/main` 的 `SERVICE_PORT`、`src/app/ui/config` 的 `port` 和 `src/dramanest.sc` 的端口。

### 授权文件夹

`manifest` 里 `disable_authorization_path = false`，所以「应用中心 → DramaNest 剧巢 → 设置」里会出现 **授权目录**：

1. 添加要读写的文件夹（如 `/vol1/1000/Movie`）并保存。
2. 飞牛会通过 `TRIM_DATA_ACCESSIBLE_PATHS` 把路径注入应用进程；保存设置时应用会自动重启并读取。
3. 回到应用「设置 → 浏览 / 授权目录」，点「刷新授权状态」，授权目录会带 **已授权** 标签排在最前，点「选用」即作为下载目录。

未授权时，应用会按「授权目录 → `config/resource` 声明的共享目录（`/var/apps/dramanest/share/data`）→ 应用自有目录」顺序自动找一个真正可写的位置，不会因为默认路径不存在而下载失败。

## 首次使用

1. 打开应用，阅读并同意首次显示的使用声明。
2. 进入「设置」，确认下载根目录：可在飞牛「应用设置 → 授权目录」授权文件夹后点「刷新授权状态」选用，也可直接点「浏览 / 授权目录」确认哪个目录可写。
3. **添加资源站**：进入「设置 → 站点排序与文件夹分类 → 新增站点」，填写名称与苹果 CMS 接口地址（形如 `https://你的站点/api.php/provide/vod/at/json`）；也可在「内置源导入 / 导出」粘贴 JSON 批量导入。
4. 回到「资源站采集」搜索并开始下载。

### 资源站 JSON 格式

格式与 `sites.example.json` 一致：

```json
[
  {
    "name": "示例资源站",
    "api": "https://example.com/api.php/provide/vod/at/json",
    "category": "normal",
    "folder": "常用资源",
    "disabled": false
  }
]
```

- `category`：`normal` 或 `adult`（成人源需在设置中开启开关才会显示）。
- `folder`：自定义分组名，相同分组的源在下拉框中归类。
- `disabled`：设为 `true` 禁用该源，禁用的源会排到最后。

> 你自己收集的真实源列表建议保存在 `private/cms_sites.json`（已被 `.gitignore` 排除），不会随代码上传到 GitHub。

## 从源码构建

需要 Python 3（仅标准库）：

```bash
python3 build.py          # 同时构建 arm 与 x86
python3 build.py arm      # 只构建 arm
python3 build.py x86      # 只构建 x86
```

产物输出到 `dist/`。构建脚本会自动：

- 用固定 mtime 打包，保证可复现；
- 设置 `cmd/*` 为 755 可执行；
- 计算 `app.tgz` 的 MD5 写入 manifest 的 `checksum`；
- 校验 gzip 头、包内结构与执行权限。

## 脱敏说明

为符合开源分发要求，本仓库与 fpk 包均做脱敏处理：

- 应用内**不内置任何视频站点与网址**，内置源列表为空 `[]`；
- 所有示例网址使用 `example.com` 占位；
- 包名、显示名、路径、账号统一为 `dramanest` / `DramaNest`；
- `private/` 目录与 `dist/*.fpk` 已被 `.gitignore` 排除，不会上传。

唯一保留的网址是项目主页 `https://github.com/Ranlove99/DramaNest`，用于「关于」与免责声明展示，非视频源。

## 免责声明

本软件按「现状」提供，仅作为通用下载工具供技术学习与个人合法使用。使用者应对自己的下载行为负责，确保拥有目标内容的合法权利并遵守当地法律。作者对因使用本软件产生的任何直接或间接后果不承担责任。资源站由使用者自行添加，其内容与本项目无关。

## 许可

MIT License，详见 [LICENSE](LICENSE)。
