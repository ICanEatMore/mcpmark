# MCPMark / Toolathlon 基础设施可移植性重构纲要

本文件合并了架构指导与 `DETAILED_DESIGN.md`，作为无 Docker 运行时重构的约束、决策记录和验收基线。它指导实现方式，**不改变 benchmark 语义**。

## 1. 目标与不可变约束

目标是在已有 Model Pod 旁运行一个长期存在的 Evaluation Pod。它通过用户态环境、长期服务和逻辑隔离承载 MCPMark 与 Toolathlon，而不是为 task、worker 或服务动态创建 Kubernetes Pod。

以下内容绝不能因重构改变：

- task、prompt、MCP tool schema、ground truth、verifier、success criterion；
- Kubernetes benchmark 的真实 Kubernetes 语义；
- WebArena/Canvas/Poste/Woo 等完整应用的官方镜像所携带的初始状态与行为；
- 每次评测从可验证 golden state 出发的要求。

允许改变的只有 launcher、state backend、运行时实现（Docker → native/Apptainer）、reset、sandbox、日志和资源调度。

## 2. 目标运行模型

```text
Model Pod ── OpenAI-compatible API ──> 长期 Evaluation Pod
                                      ├─ Python venv / uv
                                      ├─ 固定 Node local environment
                                      ├─ 固定 native binaries
                                      ├─ Chromium + browser contexts
                                      ├─ PostgreSQL daemon
                                      ├─ MCPMark / Toolathlon scheduler + worker pool
                                      ├─ Apptainer/SIF long-running instances
                                      │    └─ WebArena / Canvas / Poste / Woo
                                      └─ 可选：同 Pod 的长期 K3s
```

Pod 是长期计算资源，不是 task isolation primitive。并发度、WebArena 数量和 Toolathlon workers 都是同一 Pod 内的软件层概念。只有在 Evaluation Pod 已被 CPU、内存或 I/O 饱和后，才考虑增加第二个长期 Evaluation Pod。

## 3. 运行时分层决策

| 工作负载 | 目标实现 | 原因 |
|---|---|---|
| MCPMark / Toolathlon harness | 独立 Python venv / uv | 语言依赖隔离，易维护 |
| Python MCP（含 postgres-mcp） | 独立 venv | 避免依赖污染 |
| Node MCP、Playwright MCP | 固定 local `node_modules` | 无须容器 |
| GitHub MCP | 固定版本 native binary | 版本锁定不等于 Docker |
| 普通 Playwright | 共享 Chromium，per-task browser context/profile | 浏览器上下文是正确的逻辑隔离单位 |
| PostgreSQL MCP | Evaluation Pod 内长期 PostgreSQL daemon | 使用 task DB namespace / reset，不是每题容器 |
| WebArena、Canvas、Poste、Woo | immutable SIF + per-slot writable overlay + Apptainer instance | 保持完整官方 OCI 应用和预置状态 |
| Kubernetes benchmark | 首选同 Pod 长期 K3s | 必须保持真实 Kubernetes runtime |

原则：能由 venv、普通进程、Node local environment 或固定二进制可靠表达的依赖，不使用容器；只有 OCI image 自身承载了重要系统状态时，才保留 image-level runtime。完整应用的正式路径是 Apptainer/SIF。只有平台 capability gate 证明 Apptainer 被彻底禁止时，才进入 fat Evaluation image + chroot/bwrap/supervisor fallback；禁止直接解压 tar 后把 host-process 当作默认 backend，也禁止手工重装应用栈。

## 4. 隔离、并发与 reset 契约

### Task isolation

每个任务必须获得独立的：

- workspace、HOME 和 TMP；
- MCP subprocess 与 process group；
- browser context/profile；
- database namespace 和 credential/state lease；
- 必要时的 bubblewrap 文件系统/PID 隔离。

共享长期服务是允许的；共享 task state 不允许。

### 并发

并发由 process pool、semaphore、logical environment slots 与 credential pool 控制，不能映射为 Pod 数。每个 WebArena slot 对应一个独立可写 overlay；普通 Playwright slot 对应一个独立 browser context。

### reset

每种环境都必须实现统一生命周期：

```text
prepare → reset → verify → acquire → evaluate → release → teardown
```

每次 checkpoint 或任务之间的核心流程是 `reset → verify → evaluate`，不是重建 Pod、重新拉镜像或重新 bootstrap。`verify` 至少记录 golden artifact digest、运行时版本、slot ID、reset 结果与服务 fingerprint。reset 或 verify 失败必须阻止 evaluate。

对于完整 OCI 应用，reset 语义为：停止 instance，丢弃 dirty overlay，从同一 immutable SIF 创建 fresh overlay 后重新启动。这样等价于现有“从固定 Docker image 创建 fresh container”的初始文件系统语义。

## 5. Playwright 无 Docker 重构边界

需要清晰区分两个问题：

1. **普通 Playwright**：不需要 Docker。安装固定 Chromium/Playwright，使用 per-task context 和临时 profile；以 context 生命周期完成隔离与清理。
2. **Playwright-WebArena**：浏览器本身同样不需要 Docker，但 Shopping、Shopping Admin、Reddit 是有状态完整应用。不得手工把其应用栈“pip/apt 原生化”；正式路径必须以 Apptainer/SIF + fresh overlay 保留其 OCI 语义。

现有核心替换点是 `src/mcp_services/playwright_webarena/playwright_state_manager.py`：它目前直接依赖 `docker images/load/run/exec/stop/rm`。应拆为 runtime-neutral environment/slot 接口，并提供 Apptainer adapter。若 Apptainer gate 失败，只记录并进入明确批准的 fat-image fallback，不自动切换实现。Docker adapter 可在迁移期作为显式兼容后端保留，但无 Docker 应是主路径。

## 6. 实施顺序与改动范围

1. **Capability Gate**：探测 Chromium、Playwright、PostgreSQL、Apptainer overlay、bubblewrap、K3s 的用户命名空间/cgroup/containerd 能力；输出 fail-fast 诊断。
2. **普通 Playwright**：固定浏览器安装和路径，完善 context/profile、slot semaphore、日志与清理。
3. **WebArena runtime adapter**：从 Docker state manager 提取 runtime-neutral 接口；实现 SIF/overlay/instance 生命周期，并以 Postmill 完成首个 Apptainer 纵向切片；统一端口分配、health check、reset fingerprint 与运行时无关的 post-start hook。
4. **reset 与 fingerprint**：对每类环境增加 golden state 校验、slot lease 和故障阻断。
5. **launcher 与文档**：将 `run-task.sh` 和 `run-benchmark.sh` 的 Docker 网络/image/container 操作移至兼容后端；主 launcher 改为 runtime-neutral。
6. **验证**：执行普通 Playwright smoke、WebArena reset、并发隔离、已有 verifier 等价性和 launcher 选择回归。

预计直接修改的路径：

- `src/mcp_services/playwright_webarena/playwright_state_manager.py`
- `src/mcp_services/playwright/playwright_state_manager.py`
- `src/services.py`
- `run-task.sh`、`run-benchmark.sh`
- `environments/playwright_webarena/` 的 OCI artifact 转换与 slot 工具
- `README.md`、`docs/`、相关测试

当前已有只读探测入口：`python -m src.runtime.capabilities`。它只报告二进制和 kernel/cgroup 前置条件，绝不启动服务、容器或特权 runtime；实际 launcher 仍必须在评测前执行 reset/health gate。

## 7. 权限 Gate 与升级条件

Apptainer 和 K3s 都必须经过实际能力验证。若 Apptainer 在 Pod 内不能以所需 overlay/网络语义运行，不能以 mock 或手工重装替代；应记录限制并选择经确认的兼容后端。若 K3s 因权限、cgroup 或 containerd 限制不能在主 Evaluation Pod 内运行，才申请**一个固定** K3s sandbox Pod，并向负责人说明原因。

任何 fallback 都必须保持 benchmark 语义，且在选择前具有可审计的 capability probe 结果。

## 8. 完成定义

重构完成的判定不是“Docker 调用消失”，而是：无 Docker 的长期 Evaluation Pod 能在保持 task/verifier 语义的前提下运行目标任务；每个环境的 reset 可验证；并发任务之间无状态泄漏；所有权限受限的场景都能给出明确、可操作的诊断与合规 fallback。

## 9. 当前验证快照（2026-08-12）

工作分支为 [`ICanEatMore/mcpmark:refactor/no-docker-playwright`](https://github.com/ICanEatMore/mcpmark/tree/refactor/no-docker-playwright)。当前已完成：

- runtime contract、verified environment session、slot/port lease 和 process-group supervisor；
- evaluator 在 setup、agent 和 verifier 异常路径上的 guaranteed cleanup；
- capability doctor 对 Apptainer、mount namespace、FUSE、overlay、CNI 和 Playwright Chromium 的实际探测；
- 普通 Playwright 通过固定 Chromium executable 启动 `@playwright/mcp@0.0.68`，完成 MCP initialize、tool listing、页面导航和关闭的无 Docker smoke；
- 19 个单元测试、Ruff 与 Python compileall 通过。

当前主机已安装 Apptainer v1.5.3、CNI plugins 和 squashfs-tools，并能完成 OCI → SIF 构建；但平台禁止 mount namespace，且 device cgroup 对 `/dev/fuse` 返回 `EPERM`，因此 `apptainer exec` 在挂载阶段失败。按照本文件和 `DETAILED_DESIGN.md` 的 Gate 约束，WebArena 实现停在此处，不自动降级为 host-process。

目标 Evaluation Pod 至少需要满足：

- 允许创建 Apptainer 所需的 mount namespace；
- 允许进程实际打开 `/dev/fuse`；
- 保留 Apptainer CNI/portmap 所需的网络权限；
- 最简部署方式可采用详细设计建议的 `securityContext.privileged: true`，之后再基于实际 smoke 收紧权限。

Gate 解阻后，下一纵向切片是 Postmill：固定 tar digest → immutable SIF → fresh per-slot writable overlay → instance/readiness → reset/fingerprint → cleanup/leak check。Shopping Admin 和 Shopping 只有在 Postmill 串行与双槽隔离 Gate 通过后才开始。
