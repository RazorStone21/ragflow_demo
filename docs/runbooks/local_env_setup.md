# 本机环境手册（Windows 11 · WSL2 · Docker Desktop · uv）

> 适用机器：Windows 11 Pro（26200）、16GB 内存、无独立 GPU。
> 本文只覆盖**一次性的本机准备**。RAGFlow 自身的部署见
> [ragflow_setup.md](ragflow_setup.md)；日常开发门禁见 [README.md](../../README.md)。

**当前状态：本机环境已就绪**（2026-10-16 实测）——Docker Desktop 4.95.0 / Engine 29.8.2，
WSL2 后端为 Ubuntu-24.04，`docker compose` 可用。下面的步骤保留为**换机或重装时的复用手册**；
若本机已能跑 `docker compose ps`，直接从 [README](../../README.md) 的「本地启动」进入即可。

修复前实测到的阻塞事实（供判断当前是否又回到这个状态）：

- BIOS 虚拟化**已开**（`VirtualizationFirmwareEnabled=True`）；
- Hyper-V 平台**未启用**（`HypervisorPresent=False`）→ WSL2 起不来；
- 直接 `wsl --update` 报 `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`，**损坏的是 MSI 安装路径**；
- winget 源里有 `Microsoft.WSL` 与 `Docker.DockerDesktop`，因此**不需要**从 GitHub 下载 MSI。

## 步骤 1 · 启用两个 Windows 可选功能（管理员 PowerShell）

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform        /all /norestart
```

## 步骤 2 · 重启

必须重启，否则步骤 3 会撞上同一个 `REGDB_E_CLASSNOTREG`。这一步不能省。

## 步骤 3 · 用 winget 重装 WSL（管理员 PowerShell）

走 winget 而不是 `wsl --update`：后者正是损坏的那条 MSI 路径。
**不得**从 GitHub 直接下 MSI 手动安装（用户既定原则）。

```powershell
winget install --id Microsoft.WSL --exact --accept-source-agreements --accept-package-agreements
```

仍失败时的排查顺序：

1. `wsl --update --web-download`（管理员）；
2. 查 `HKLM\SOFTWARE\Classes\Directory\shell\WSL` 的权限，Administrators 需有完全控制权；
3. 检查安全软件是否拦截了 MSI 写入。

## 步骤 4 · 验证 WSL

```powershell
wsl --status      # 期望输出默认发行版与内核版本，而不是报错
wsl --version
```

## 步骤 5 · 安装 Docker Desktop

```powershell
winget install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
```

启动后：**Settings → General → Use the WSL 2 based engine** 打开；
**Settings → Resources → WSL Integration** 打开。

## 步骤 6 · 给 Docker 配代理（本机 Docker Hub 直连不通，已实测）

**Settings → Resources → Proxies** 填：

```text
http://127.0.0.1:7890
```

注意：shell 里的 `HTTPS_PROXY` 对 **docker daemon 无效**，必须在这里单独配一次。
（`git` / `npx` 等命令行工具则是相反 —— 见「代理速查」。）

## 步骤 7 · 限制 WSL 内存（16GB 机器必做）

写 `%UserProfile%\.wslconfig`（即 `C:\Users\kevin\.wslconfig`）：

```ini
[wsl2]
memory=12GB
```

改完执行 `wsl --shutdown` 生效。理由：不限制时 WSL 会随宿主内存增长，
与 Milvus/Postgres/LiteLLM 争内存，最终整机进入 swap 抖动；
Milvus 被 OOM 会中断正在进行的解析入库。

> 不要顺手加 `kernelCommandLine = vm.max_map_count=...`：那是 Elasticsearch 的要求，
> 本项目 `DOC_ENGINE=infinity` 不起 ES。

## 步骤 8 · 验证关卡（三项都过才算环境就绪）

```bash
docker run --rm hello-world     # Docker + WSL2 + 代理三者同时可用
docker compose version          # compose v2 可用
uv --version                    # uv 已装（本机经 winget 装的 astral-sh.uv）
uv python install 3.12          # 本项目锁定 3.12（CLAUDE.md 1 章：≥3.11,<3.13）
```

## 代理速查

| 场景 | 怎么配 |
|---|---|
| 命令行 git / npx | 前缀 `HTTPS_PROXY=http://127.0.0.1:7890` |
| docker pull | Docker Desktop → Settings → Resources → Proxies |
| uv 下载依赖 | 一般走 PyPI 镜像即可；若不通同样前缀 `HTTPS_PROXY=` |

## 已知坑

- **psycopg 异步在 Windows 默认事件循环上直接罢工**：Windows 默认的
  `ProactorEventLoop` 不被 psycopg 异步模式支持，报
  `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
  Linux/容器（生产目标）默认就是 Selector，所以这是**纯本机问题**，但必须显式绕开：

  ```bash
  uv run uvicorn --factory agent_platform.main:create_app \
      --loop asyncio:SelectorEventLoop --host 0.0.0.0 --port 8000
  ```

  为什么 `--loop` 能写成一个导入串：uvicorn 0.54 的 `Config.get_loop_factory()` 只在
  `--loop` 命中五个已知名字（`none/auto/asyncio/uvloop/zuvloop`）时才查表，否则把它当作
  **任意导入串** `import_from_string()` 并直接当作 loop factory 用。所以
  `asyncio:SelectorEventLoop` 这个类本身就是合法的 factory（无参调用、返回事件循环）。

  为什么不能用 `asyncio.set_event_loop_policy()` 一劳永逸：事件循环如今由 `loop_factory`
  直接构造，**全局 policy 会被完全绕过** —— 设了也不生效，这点很容易误判成"设置没生效"。
  pytest 侧同理，用 `pytest_asyncio_loop_factories` hook（见 `tests/conftest.py`）。

- **`POSTGRES_DSN` 必须写 `127.0.0.1`，不能写 `localhost`**：本机 `localhost` 解析到
  `['::1', '127.0.0.1']`，libpq 先试 IPv6，而 Docker Desktop 只把 Postgres 发布在
  IPv4 上 —— 不带超时时**无限期挂住**（表现为启动无输出、像卡死）。
  实测：`localhost` 10.1s（卡满超时后仍连上）、`127.0.0.1` 0.0s。
  `POSTGRES_CONNECT_TIMEOUT_S` 只兜住最坏情况，消除停顿靠改写主机名。

- **宿主系统代理会劫持发往 localhost 的请求**：httpx 默认 `trust_env=True`，在 Windows 上
  会经 `urllib.request.getproxies()` 读取**注册表**里的 WinINET 代理设置（**不看**
  `HTTP_PROXY` 环境变量）。本机实测开着 Clash 时，连 `127.0.0.1:4000` 的网关请求都会被
  转给代理并返回 502 —— 于是"网关通不通"取决于某台机器上有没有开代理。
  内部客户端已统一 `trust_env=False`（见 `knowledge/gateway_client.py`）；
  写新的内部 HTTP 客户端时**照抄这一条**。

- **工具不在 PATH**：`uv`、`gitleaks`、`pre-commit` 装好后在部分终端里看不见。
  临时办法是把它们的安装目录显式加进 `PATH`；根治办法是重开终端或检查用户级 PATH。
  注意追加时别丢掉 `:$PATH` —— 写成 `export PATH="/c/Users/kevin/.local/bin"` 会把
  整个 PATH 覆盖掉，`tail`/`grep` 等随之消失。
- **换行符**：本仓库必须 `core.autocrlf=false`，否则 `git status` 满屏幽灵改动。
  该设置是**本地配置、不下发**，新克隆的机器上要手动设一次：
  ```bash
  git config core.autocrlf false
  ```
- **控制台中文乱码**：Windows 默认代码页是 GBK，脚本直接 print 中文会花屏。
  本仓脚本入口统一调 `enable_utf8_console()`（见 `core/logging.py`）；
  临时观察 pytest 输出时用 `PYTHONIOENCODING=utf-8`。
  乱码还有一个隐蔽后果：对中文断言做 `grep` 会**静默匹配不到**（不报错，只是没有结果），
  排查时容易被误读成"字符串不存在"。
