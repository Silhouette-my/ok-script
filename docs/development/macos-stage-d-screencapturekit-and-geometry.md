# macOS 前台模式 Stage D：ScreenCaptureKit 持久流与几何

记录日期：2026-09-04

分支：`feature/macos-foreground-mvp`

状态：**本地 contract tests 与 macOS 全量测试通过；官方客户端、1000+ 帧和 packaged `.app` 待验收**

## 1. 本阶段边界

本阶段仅加入：

- `SCContentFilter(desktopIndependentWindow:)`、显式 `SCStreamConfiguration` 和一个持久 `SCStream`；
- `CMSampleBuffer` callback 内的完整帧筛选、BGRA/stride/padding 处理和 owned BGR copy；
- 单槽 latest-frame publication；
- target generation 与独立 capture generation 绑定的 immutable frame packet；
- resize、scale、content rect 或 target rebind 变化时的旧帧失效和流重建；
- consumer 读取时重新验证目标存活；native stop 未确认时进入 fatal，禁止并行启动替代 stream；
- FPS、frame age、overwrite、drop、generation、geometry invalidation 和 rebuild 诊断；
- 帧物理像素到 macOS 全局逻辑点的通用 geometry mapping；
- `DeviceManager` 的 macOS capture provider 路由。

本阶段没有加入 Quartz 输入、`ForegroundGuard`、`HeldInputState`、`release_all()`、relative mouse、任务开放、后台控制或任何 TCC 绕过。

## 2. 生产路径和所有权

```text
SCShareableContent（仅 source resolve/rebind）
  → selected SCWindow
  → SCContentFilter(desktopIndependentWindow:)
  → one SCStreamConfiguration
  → persistent SCStream
  → CMSampleBuffer callback
  → owned BGR ndarray + immutable CaptureGeometry
  → one-slot LatestFrameSlot
```

配置显式使用 BGRA、`showsCursor=False`、`queueDepth=3`、30 FPS、no audio，并在可用系统上设置 `ignoreShadowsSingleWindow=True`。输出尺寸配置只用于请求接近原生 display scale 的 surface；视觉尺寸和 stride 始终以实际 `CVPixelBuffer` 为事实来源。

PyObjC 的 `CVPixelBufferGetBaseAddress()` 作为 `objc.varlist` 处理，在 lock 期间通过 `as_buffer(bytes_per_row * height)` 建立有界 view。BGR 数组在 unlock 前完成连续 owned copy，不依赖 IOSurface callback 生命周期。

## 3. Fail-closed generation 语义

- 每个 callback 捕获不可变 target generation 和 capture generation；发布前后均重新检查当前 target/state/generation；
- target refresh/rebind 开始前先提升 capture generation、清空 latest slot，并阻止旧 target generation 重启；
- actual frame size、content rect、display scale 或 content scale 改变时，当前 generation 立即失效，下一次 consumer poll 重建 stream；
- permission missing/revoked、target lost、fatal stream stop 和 close 均清空 slot；
- fatal start/stop 在同一 target generation 上不会 tight retry；
- native stop 超时或失败后保留未确认 binding 到 close，并保持 fatal；不会启动第二个 stream；
- target 的实时 liveness 检查失败或返回 lost 时停止 stream、清空 slot，不能由旧 snapshot 继续返回帧；
- start/rebuild race 进入 fatal 后不能被并发同步路径重置为 running；fatal start 会清空 callback 可能提前发布的帧和几何；
- conversion failure 不返回此前的旧帧；
- `interaction` 和 device capabilities 继续为空，因此 Stage D capture 就绪不等于任何 Mac task 可执行。

## 4. 几何边界

`CaptureGeometry` 同时记录 outer global logical geometry、global content geometry、raw surface pixels、content rect pixels、normalized frame pixels、display scale、target generation 和 capture generation。

`SCStreamFrameInfoContentRect` 按 Apple API 定义视为 surface points，并使用 `SCStreamFrameInfoScaleFactor` 转成物理像素 crop。帧坐标映射使用 normalized frame 与 global content geometry 的实际比例，不假设 scale 固定为 2.0。

在真实官方客户端验证前，不能声称已证明无标题栏、边框、阴影，也不能声称 1920×1080 或视觉识别已受支持。

## 5. 本地自动化证据

参考解释器：OK-WW sibling `.venv` 的 Python 3.12 arm64。

```bash
../ok-wuthering-waves/.venv/bin/python -m pytest \
  tests/test_screencapturekit_core.py \
  tests/test_screencapturekit_capture.py \
  tests/test_device_manager.py \
  tests/test_platform_imports.py
```

结果：`47 passed, 1 skipped`；skip 为当前 Darwin 上的 Windows-only import isolation case。

```bash
../ok-wuthering-waves/.venv/bin/python -m mypy \
  --follow-imports=skip --ignore-missing-imports \
  ok/device/capture_methods/screencapturekit_core.py \
  ok/device/capture_methods/screencapturekit.py
```

结果：`Success: no issues found in 2 source files`。

```bash
../ok-wuthering-waves/.venv/bin/python -m pytest -q
```

在允许 loopback 的执行边界外结果为 exit code 0。沙箱内仅现有 web-server tests 因禁止绑定 `127.0.0.1` 失败；没有通过修改或跳过测试规避。

另外在当前 Darwin/PyObjC runtime 完成了无捕获 smoke：`SCStreamConfiguration` 的 width/height/BGRA/queueDepth/showsCursor setter、serial dispatch queue，以及 `SCStreamOutput`/`SCStreamDelegate` protocol callback signature 均可构造。

## 6. 仍未验证

- 未请求 Screen Recording permission，未启动真实 `SCStream`；
- 未获得官方《鸣潮》Mac 客户端的实际 frame/content rect/scale；
- 未完成 1920×1080、无 cursor/title/border/shadow、color correctness、1000+ frames、FPS/stall/leak/queue growth 硬件门槛；
- 未验证 resize、display migration、window replacement 和 PID change 的真实恢复路径；
- 未建立 packaged `.app` 的稳定 TCC identity 证据；
- Quartz 输入及全部 fail-closed release 语义仍属于 Stage E。
